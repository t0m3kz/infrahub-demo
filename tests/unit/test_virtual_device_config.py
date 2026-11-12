"""A dedicated virtual firewall gets a real config artifact, not the no-platform stub."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from graphql import FieldNode, OperationDefinitionNode, parse

from checks.firewall import CheckFirewall
from tests.smoke.fixture_builders.common import run_transform
from tests.smoke.fixture_builders.context_members import with_context_members
from tests.smoke.fixture_builders.transit import build_virtual_firewall_data
from tests.smoke.fixture_builders.vrf import interface_vrf_capability, with_interface_vrfs
from transforms.config.firewall import Firewall

EMPTY_ROOTS = {"DcimDevice": {"edges": []}, "SecurityZone": {"edges": []}}


def _root_fields(root_dir: Path, query_file: str) -> list[str]:
    """The root fields of the (single) operation of a query file."""
    document = parse((root_dir / query_file).read_text())
    operation = next(d for d in document.definitions if isinstance(d, OperationDefinitionNode))
    return [s.name.value for s in operation.selection_set.selections if isinstance(s, FieldNode)]


def test_firewall_query_roots_at_the_device_generic(root_dir: Path) -> None:
    """Both device kinds are found by name: the first root is DcimDevice, never one kind."""
    assert _root_fields(root_dir, "queries/config/firewall.gql") == ["DcimDevice", "SecurityZone"]


def test_loadbalancer_query_selects_both_device_kinds(root_dir: Path) -> None:
    """The load balancer config query already reaches a dedicated virtual instance."""
    assert _root_fields(root_dir, "queries/config/loadbalancer.gql") == ["DcimPhysicalDevice", "DcimVirtualDevice"]


def test_virtual_firewall_payload_renders_its_contexts() -> None:
    """A dedicated virtual firewall renders its context legs, routes, NAT, isolation and cluster."""
    output = run_transform(Firewall, build_virtual_firewall_data())
    assert "has no platform defined" not in output
    assert "set hostname fw-dc10101-C007-p-dedicated" in output
    assert "add virtual-system name fw-C007-p-dedicated-context" in output
    assert "set interface eth1.3001 ipv4-address 100.66.0.13 mask-length 29" in output
    assert "set static-route default nexthop gateway address 100.66.32.9 on" in output
    assert "set cluster sync-interface eth7" in output
    assert 'mgmt_cli set simple-cluster name "fw-C007-p-dedicated-ha"' in output


def _checkable_virtual_firewall_data() -> dict:
    """The builder's payload with each interface's zone moved onto an explicit VRF instance and each context given its
    members (the device's HA pair), as the query returns it."""
    data = with_interface_vrfs(with_context_members(build_virtual_firewall_data()))
    device = data["DcimDevice"]["edges"][0]["node"]
    for edge in device["interfaces"]["edges"]:
        interface = edge["node"]
        zone = interface.pop("security_zone", None)
        if zone and zone.get("node") and not interface.get("zone_capabilities"):
            name = zone["node"]["name"]["value"]
            interface.update(interface_vrf_capability({"id": f"vrf-{name.lower()}", "security_zone": zone}))
    return data


def test_virtual_firewall_payload_is_validated_like_a_physical_one() -> None:
    """The firewall check reads the same query shape: a complete dedicated instance raises nothing."""
    errors: list[str] = []
    check = cast(Any, CheckFirewall.__new__(CheckFirewall))
    check.log_error = lambda message: errors.append(message)
    check.validate(_checkable_virtual_firewall_data())
    assert errors == []


@pytest.mark.parametrize(
    ("address", "message"),
    [(None, "no snat_address"), ("10.0.0.5/32", "not a public address")],
)
def test_ha_owned_snat_overrides_stale_interface_references(address: str | None, message: str) -> None:
    """A stale public SNAT on an interface cannot hide an invalid HA-owned context."""
    data = _checkable_virtual_firewall_data()
    device = data["DcimDevice"]["edges"][0]["node"]
    context = device["shared_capabilities"]["edges"][0]["node"]
    context["snat_address"] = {"node": {"address": {"value": address}}} if address else {"node": None}
    for edge in device["interfaces"]["edges"]:
        # A stale reference on an interface: no longer part of the model, and ignored if a payload still carries it.
        edge["node"].setdefault("capabilities", {"edges": []})["edges"].append(
            {
                "node": {
                    "__typename": "ManagedFirewallContext",
                    "snat_address": {"node": {"address": {"value": "192.0.2.99/32"}}},
                }
            }
        )
    errors: list[str] = []
    check = cast(Any, CheckFirewall.__new__(CheckFirewall))
    check.log_error = lambda message: errors.append(message)
    check.validate(data)
    assert len(errors) == 1
    assert message in errors[0]


@pytest.mark.parametrize("data", [EMPTY_ROOTS, {"DcimDevice": {"edges": [{"node": {"name": {"value": "fw-x"}}}]}}])
def test_device_without_data_or_platform_still_yields_the_stub(data: dict) -> None:
    """No device found, or one without a platform: the placeholder, so the artifact never fails."""
    output = run_transform(Firewall, data)
    assert "has no platform defined" in output
    assert "No configuration generated." in output
