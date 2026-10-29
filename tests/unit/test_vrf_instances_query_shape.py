"""The raw builders of vrf_instances_helpers.py emit only fields queries/validation/vrf_instances.gql selects."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tests.unit.query_shape_helpers import Tree as _Tree
from tests.unit.query_shape_helpers import query_tree
from tests.unit.query_shape_helpers import unselected as _unselected
from tests.unit.vrf_instances_helpers import (
    address,
    raw_context,
    raw_device,
    raw_instance,
    raw_interface,
    raw_mlag,
    raw_response,
    raw_root_instance,
    raw_row,
    raw_svi,
)

QUERY = Path(__file__).resolve().parents[2] / "queries" / "validation" / "vrf_instances.gql"


def _query_tree() -> _Tree:
    return query_tree(QUERY)


def _everything() -> dict[str, Any]:
    """A response built with every builder, each with its optional parts."""
    row = raw_row("r1", 100, "PROD")
    context = raw_context("ManagedFirewallContext", 100, ("firewall", "PROD", "prod"))
    interfaces = [
        raw_interface("i1", addr=address("10.0.0.1/31", "PROD")),
        raw_interface("i2", name="Ethernet2", role="firewall", capabilities=[context]),
        raw_svi("s1", 100, "r1", "v1", "PROD"),
    ]
    device = raw_device(
        "d1",
        "leaf-1",
        rows=[row],
        capabilities=[raw_mlag("m1")],
        vrfs=[raw_instance("v1", "leaf-1", "PROD")],
        shared_others=["ManagedSNMP"],
        interfaces=interfaces,
        groups=("provider_stubs",),
    )
    border = raw_device("d2", "bl-1", role="border-leaf", deployment_kind="TopologyDataCenter")
    zone = raw_device("d3", "sw-1", deployment_kind="TopologyColocationZone")
    old = raw_device("d4", "old-1", status="decommissioned")
    empty = raw_root_instance(raw_instance("v2", "x", "NON-PROD", 200, "non_prod"))
    return raw_response(device, border, zone, old, instances=(empty,))


def test_the_query_tree_resolves_the_in_file_fragments() -> None:
    """The fragments contribute their fields under the paths that spread them."""
    device = _query_tree()["DcimDevice"]["edges"]["node"]
    assert "segment_deployment" in device["vlan_domain_segments"]["edges"]["node"]
    assert (
        "ip_address"
        in device["interfaces"]["edges"]["node"]["capabilities"]["edges"]["node"]["capabilities"]["edges"]["node"]
    )


def test_the_query_selects_memberships_hosting_and_the_vrf_root() -> None:
    """The device's ``shared_capabilities``, the deployment with its parent, the ``ManagedVRF`` root and the SVI's VRF."""
    tree = _query_tree()
    device = tree["DcimDevice"]["edges"]["node"]
    assert "vrfs" not in device
    assert {"namespace", "deployment"} <= set(device["shared_capabilities"]["edges"]["node"])
    assert "parent" in device["deployment"]["node"]
    assert {"deployment", "namespace", "shared_devices"} <= set(tree["ManagedVRF"]["edges"]["node"])
    assert "devices" not in tree["ManagedVRF"]["edges"]["node"]
    assert "deployment" in device["interfaces"]["edges"]["node"]["vrf"]["node"]


def test_the_raw_builders_emit_only_fields_the_query_selects() -> None:
    """A builder key the query does not select would make the unit fixtures drift from the real payload."""
    assert _unselected(_everything(), _query_tree()) == []


def test_the_pin_catches_a_field_the_query_does_not_select() -> None:
    """Guard of the guard: an unselected key is reported with its path."""
    response = _everything()
    response["DcimDevice"]["edges"][0]["node"]["bogus"] = {"value": 1}
    assert _unselected(response, _query_tree()) == [".DcimDevice.edges.node.bogus"]
