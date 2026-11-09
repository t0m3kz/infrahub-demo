"""The firewall uses shared interface fragments and context-owned configuration data."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest
from graphql import FieldNode, OperationDefinitionNode, StringValueNode, parse

from tests.unit.firewall_transform_helpers import _make_exchange_data, _make_fw
from transforms.config.firewall import _segment_prefixes, apply_context_routes
from transforms.helpers.firewall import get_firewall_zones
from transforms.helpers.firewall_contexts import firewall_context_zone_data


def test_firewall_query_has_scoped_roots_and_shared_interface_fragment() -> None:
    """One device and one filtered infrastructure zone, not a fleet-wide zone scan."""
    root = Path(__file__).resolve().parents[2]
    query = (root / "queries/config/firewall.gql").read_text()
    operation = next(node for node in parse(query).definitions if isinstance(node, OperationDefinitionNode))
    roots = [field for field in operation.selection_set.selections if isinstance(field, FieldNode)]
    assert len(roots) == len(operation.selection_set.selections) == 2
    assert [root.name.value for root in roots] == ["DcimDevice", "SecurityZone"]
    argument = roots[1].arguments[0]
    assert argument.name.value == "name__value"
    assert isinstance(argument.value, StringValueNode)
    assert argument.value.value == "MANAGEMENT"
    # The contexts are the device's own shared capabilities: the query spreads the one device fragment that
    # selects them, once, and selects no context edge of its own (no HA `contexts`, no `cluster`).
    fragment = (root / "queries/fragments/device_capabilities.gql").read_text()
    assert fragment.count("...FirewallContextConfigFields") == 1
    assert "...FirewallContextConfigFields" not in query
    assert "contexts {" not in query
    assert "...DeviceSharedCapabilitiesOnDeviceFields" in query
    assert "...InterfaceCapabilitiesWithSegmentOnInterfaceFields" in query


@pytest.mark.parametrize("platform", ["paloalto_panos", "juniper_junos", "checkpoint_gaia"])
def test_foreign_global_zone_data_cannot_change_firewall_config(platform: str) -> None:
    """Only the device's shared context supplies configuration, despite unrelated input roots."""
    data = _make_exchange_data(platform, ns_types=("prod", "internet"))
    firewall = _make_fw()
    before = asyncio.run(firewall.transform(data))
    data["SecurityZone"] = {
        "edges": [
            {
                "node": {
                    "name": {"value": "FOREIGN-ZONE"},
                    "network_segments": {
                        "edges": [
                            {
                                "node": {
                                    "id": "foreign",
                                    "gateway": {
                                        "node": {"ip_prefix": {"node": {"prefix": {"value": "198.18.0.0/15"}}}}
                                    },
                                }
                            }
                        ]
                    },
                }
            }
        ]
    }
    assert asyncio.run(firewall.transform(data)) == before
    assert "198.18.0.0/15" not in before


def test_management_routes_keep_filtered_infrastructure_prefixes() -> None:
    """Existing infrastructure routes remain, without accepting unrelated global zones."""
    data = _make_exchange_data("paloalto_panos", ns_types=("prod",))
    firewall = _make_fw()
    device, _, _ = firewall._device_and_platform(data)
    _, contexts, _, _ = firewall.collect_policies(device)
    context = contexts[0]
    context["legs"].append(
        {
            "ns_type": "management",
            "vrf": "MANAGEMENT",
            "zone": "MANAGEMENT",
            "anycast": "100.66.48.1",
            "nameif": "management",
            "interface": "eth1.3600",
        }
    )
    infrastructure = [
        {
            "name": "MANAGEMENT",
            "network_segments": [
                {"id": "service-prefix", "gateway": {"ip_prefix": {"prefix": "10.255.0.0/24"}}},
                {"id": "service-host", "gateway": {"ip_prefix": {"prefix": "10.255.1.5/32"}}},
            ],
        },
        {
            "name": "FOREIGN-ZONE",
            "network_segments": [{"id": "foreign", "gateway": {"ip_prefix": {"prefix": "198.18.0.0/15"}}}],
        },
    ]
    zones = firewall_context_zone_data([context], infrastructure)
    management = next(zone for zone in get_firewall_zones(zones) if zone["name"] == "MANAGEMENT")
    assert management["member_cidrs"] == ["10.255.0.0/24", "10.255.1.5/32"]
    apply_context_routes(context, _segment_prefixes(zones))
    destinations = {route["destination"]: route["nexthop"] for route in context["routes"]}
    assert destinations["10.255.0.0/24"] == "100.66.48.1"
    assert destinations["10.255.1.5/32"] == "100.66.48.1"
    assert "198.18.0.0/15" not in destinations
    other = {**context, "segments": [], "routes": []}
    apply_context_routes(other, _segment_prefixes(firewall_context_zone_data([other])))
    assert "10.255.0.0/24" not in {route["destination"] for route in other["routes"]}


def test_ha_context_segments_supply_address_objects_once() -> None:
    """Repeated served footprints do not duplicate device-level address objects."""
    data = _make_exchange_data("paloalto_panos", ns_types=("prod", "internet"))
    device = data["DcimPhysicalDevice"]["edges"][0]["node"]
    context = device["shared_capabilities"]["edges"][0]["node"]
    context["served_deployments"]["edges"].append(context["served_deployments"]["edges"][0])
    firewall = _make_fw()
    with patch.object(firewall, "_render", return_value="rendered") as render:
        assert asyncio.run(firewall.transform(data)) == "rendered"
    config = render.call_args.args[1]
    prod = next(zone for zone in config["zones"] if zone["name"] == "PROD")
    assert prod["member_cidrs"] == ["10.1.0.0/24"]
    assert config["contexts"][0]["vlan_id"] == 3000
    assert {segment["id"] for segment in config["contexts"][0]["segments"]} == {"seg-prod", "seg-nonprod"}
