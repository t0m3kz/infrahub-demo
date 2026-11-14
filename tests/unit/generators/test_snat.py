"""Unit tests for 4d: SNAT rendering per firewall vendor and the edge's public-space export."""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from transforms.common import UngovernedTransitError
from transforms.config.edge import Edge
from transforms.config.firewall import Firewall, apply_context_routes
from utils.internet_prefixes import get_internet_prefixes
from utils.routing_domain import RoutingDomainError

_ROOT = Path(__file__).parent.parent.parent.parent
_CONFIGS = _ROOT / "tests" / "smoke" / "configs"

_VENDORS = ("checkpoint_gaia", "juniper_junos", "paloalto_panos")
_NAT_MARKERS = ("nat rules", "security nat", "add nat-rule")


def _run(transform_cls: type, data: dict[str, Any]) -> str:
    client = MagicMock()
    client.clone.return_value = client
    client.schema = MagicMock()
    client.schema.get = AsyncMock(return_value=MagicMock())
    instance = transform_cls(client=client, infrahub_node=MagicMock(), root_directory=str(_ROOT))
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(instance.transform(data))
    finally:
        loop.close()


def _firewall_input(vendor: str, *, snat: bool) -> dict[str, Any]:
    data = json.loads((_CONFIGS / f"firewall_{vendor}_exchange_transit" / "input.json").read_text())
    if not snat:
        device = data["DcimPhysicalDevice"]["edges"][0]["node"]
        for edge in device["shared_capabilities"]["edges"]:
            if edge["node"]["__typename"] == "ManagedFirewallContext":
                edge["node"].pop("snat_address", None)
    return data


@pytest.mark.parametrize("vendor", _VENDORS)
def test_context_without_snat_address_renders_no_internet_nat(vendor: str) -> None:
    output = _run(Firewall, _firewall_input(vendor, snat=False))
    internet_nat = [
        line
        for line in output.splitlines()
        if any(m in line for m in _NAT_MARKERS) and ("INTERNET" in line or "internet" in line)
    ]
    assert not internet_nat
    assert "192.0.2" not in output


def _ctx(snat: str | None, *, internet: bool = True, management: bool = False) -> dict[str, Any]:
    legs = [
        {
            "ns_type": "prod",
            "zone": "PROD",
            "nameif": "prod",
            "anycast": "100.66.0.1",
            "interface": "e.3000",
            "vrf": "P",
        }
    ]
    if internet:
        legs.append(
            {
                "ns_type": "internet",
                "zone": "INTERNET",
                "nameif": "internet",
                "anycast": "100.66.32.1",
                "interface": "e.3400",
                "vrf": "I",
            }
        )
    if management:
        legs.append(
            {
                "ns_type": "management",
                "zone": "MANAGEMENT",
                "nameif": "management",
                "anycast": "100.66.48.1",
                "vip": "100.66.48.4",
                "interface": "e.3600",
                "vrf": "M",
            }
        )
    return {"legs": legs, "segments": [], "snat": snat}


def test_internet_nat_entry_keeps_the_legacy_shape() -> None:
    context = _ctx("192.0.2.5")
    apply_context_routes(context, {})
    [entry] = context["nat"]
    assert entry["address"] == "192.0.2.5"
    assert entry["egress"]["zone"] == "INTERNET"
    assert entry["suffix"] == ""
    assert entry["own_address"] is True
    assert [leg["zone"] for leg in entry["tenant_legs"]] == ["PROD"]


def test_management_nat_entry_translates_to_the_leg_vip_and_owns_nothing() -> None:
    context = _ctx("192.0.2.5", management=True)
    apply_context_routes(context, {})
    internet, management = context["nat"]
    assert internet["address"] == "192.0.2.5"
    assert management["address"] == "100.66.48.4"
    assert management["egress"]["zone"] == "MANAGEMENT"
    assert management["suffix"] == "-MANAGEMENT"
    assert management["own_address"] is False
    assert [leg["zone"] for leg in management["tenant_legs"]] == ["PROD"]


def test_management_nat_needs_no_snat_address() -> None:
    context = _ctx(None, internet=False, management=True)
    apply_context_routes(context, {})
    assert [entry["egress"]["zone"] for entry in context["nat"]] == ["MANAGEMENT"]


@pytest.mark.parametrize("snat, internet", [(None, True), ("192.0.2.5", False)])
def test_no_nat_entry_without_snat_or_internet_leg(snat: str | None, internet: bool) -> None:
    context = _ctx(snat, internet=internet)
    apply_context_routes(context, {})
    assert context["nat"] == []


def test_no_nat_entry_without_a_tenant_leg() -> None:
    context = _ctx("192.0.2.5", management=True)
    context["legs"] = [leg for leg in context["legs"] if leg["ns_type"] != "prod"]
    apply_context_routes(context, {})
    assert context["nat"] == []


# --- MANAGEMENT NAT per vendor (golden input: shared PROD+NONPROD, acme PROD)

_VENDOR_MANAGEMENT_NAT = {
    "paloalto_panos": [
        "rulebase nat rules snat-PROD-MANAGEMENT to MANAGEMENT",
        "rulebase nat rules snat-NONPROD-MANAGEMENT source-translation dynamic-ip-and-port translated-address 100.66.48.4",
    ],
    "juniper_junos": [
        "security nat source pool SNAT-POOL-MANAGEMENT address 100.66.48.4/32",
        "rule-set SNAT-NONPROD-MANAGEMENT to zone MANAGEMENT",
    ],
    "checkpoint_gaia": [
        'mgmt_cli add host name "SNAT-dc1-fw-shared-MANAGEMENT" ip-address "100.66.48.4"',
        'original-source "NONPROD" original-destination "MANAGEMENT" translated-source "SNAT-dc1-fw-shared-MANAGEMENT"',
    ],
}


@pytest.mark.parametrize("vendor", sorted(_VENDOR_MANAGEMENT_NAT))
def test_management_nat_survives_without_a_snat_address(vendor: str) -> None:
    output = _run(Firewall, _firewall_input(vendor, snat=False))
    for expected in _VENDOR_MANAGEMENT_NAT[vendor]:
        assert expected in output
    assert "192.0.2" not in output


# --- edge


def _origin(*prefixes: str, zone_type: str = "internet", name: str = "INET") -> dict[str, Any]:
    """A cleaned TopologySessionCircuit of ``public_origins``: its VRF's zone and the prefixes it originates."""
    return {
        "name": name,
        "vrf": {
            "id": f"vrf-{name}",
            "security_zone": {"id": f"zone-{zone_type}", "name": zone_type.upper(), "vrf_type": zone_type},
        },
        "originated_prefixes": [{"prefix": prefix} for prefix in prefixes],
    }


def test_internet_prefixes_keep_only_public_ipv4_origins_of_internet_circuits() -> None:
    """IPv6 is dropped, duplicates collapse, and only an INTERNET circuit may originate."""
    roots = [
        _origin("192.0.2.0/24", "2001:db8::/32", name="a"),
        _origin("192.0.2.0/24", name="b"),
        _origin(name="no-origin", zone_type="prod"),
    ]
    assert get_internet_prefixes(roots) == ["192.0.2.0/24"]
    assert get_internet_prefixes(None) == []


def test_originated_prefixes_of_a_non_internet_circuit_are_refused() -> None:
    """A PROD circuit originating a prefix is a routing-domain error, never silently ignored."""
    with pytest.raises(RoutingDomainError, match="require an INTERNET circuit"):
        get_internet_prefixes([_origin("198.51.100.0/24", zone_type="prod")])


def _edge_output(*, prefixes: bool) -> str:
    data = json.loads((_CONFIGS / "edge_cisco_nxos_internet_bgw" / "input.json").read_text())
    data = copy.deepcopy(data)
    data.pop("IpamPrefix", None)
    data.pop("public_origins", None)
    if prefixes:
        data["public_origins"] = {
            "edges": [
                {
                    "node": {
                        "name": {"value": "INET"},
                        "vrf": {
                            "node": {
                                "id": "vrf-inet",
                                "security_zone": {
                                    "node": {
                                        "id": "zone-INTERNET",
                                        "name": {"value": "INTERNET"},
                                        "vrf_type": {"value": "internet"},
                                        "l3_vni": {"value": 60002},
                                    }
                                },
                            }
                        },
                        "originated_prefixes": {"edges": [{"node": {"prefix": {"value": "192.0.2.0/24"}}}]},
                    }
                }
            ]
        }
    return _run(Edge, data)


def test_edge_aggregates_and_permits_only_the_public_prefix() -> None:
    output = _edge_output(prefixes=True)
    assert "      aggregate-address 192.0.2.0/24 summary-only" in output
    assert "ip prefix-list TRANSIT-OUT-PUBLIC seq 10 permit 192.0.2.0/24" in output


def test_edge_without_a_known_public_prefix_has_no_aggregate() -> None:
    output = _edge_output(prefixes=False)
    assert "aggregate-address" not in output
    assert "RM-TRANSIT" not in output


def test_a_transit_session_without_policies_is_refused() -> None:
    """The fixed transit maps are gone: an upstream session with no import or export policy renders nothing."""
    data = json.loads((_CONFIGS / "edge_cisco_nxos_internet_bgw" / "input.json").read_text())
    for edge in data["DcimDevice"]["edges"][0]["node"]["capabilities"]["edges"]:
        for peering in ((edge["node"].get("peerings") or {}).get("edges")) or []:
            peering["node"].pop("import_policies", None)
            peering["node"].pop("export_policies", None)
    with pytest.raises(UngovernedTransitError, match="transit session"):
        _run(Edge, data)
