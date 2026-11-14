"""The raw builders of vrf_instances_helpers.py emit only fields queries/validation/vrf_instances.gql selects."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from graphql import FragmentDefinitionNode, OperationDefinitionNode, parse

from tests.unit.query_shape_helpers import Tree as _Tree
from tests.unit.query_shape_helpers import _selection_tree
from tests.unit.query_shape_helpers import unselected as _unselected
from tests.unit.vrf_instances_helpers import (
    address,
    raw_device,
    raw_instance,
    raw_interface,
    raw_mlag,
    raw_response,
    raw_root_instance,
    raw_row,
    raw_service_port,
    raw_svi,
)
from utils.graphql_queries import load_query

QUERY = Path(__file__).resolve().parents[3] / "queries" / "validation" / "vrf_instances.gql"


def _query_tree() -> _Tree:
    """Compile the native operation with the same transitive fragment loader as production."""
    document = parse(load_query(QUERY))
    fragments = {d.name.value: d for d in document.definitions if isinstance(d, FragmentDefinitionNode)}
    operation = next(d for d in document.definitions if isinstance(d, OperationDefinitionNode))
    return _selection_tree(operation.selection_set, fragments)


def _everything() -> dict[str, Any]:
    """A response built with every builder, each with its optional parts."""
    row = raw_row("r1", 100, "PROD")
    port = raw_service_port(
        "Ethernet2",
        "firewall",
        "ManagedFirewallContext",
        3000,
        ("internal-firewall", "PROD", "prod"),
        served=(("fp", "p"),),
    )
    interfaces = [
        raw_interface("i1", addr=address("10.0.0.1/31", "PROD")),
        raw_svi("s1", 100, "seg", "v1", "PROD"),
    ]
    device = raw_device(
        "d1",
        "leaf-1",
        rows=[row],
        capabilities=[raw_mlag("m1")],
        vrfs=[raw_instance("v1", "leaf-1", "PROD")],
        shared_others=["ManagedSNMP"],
        interfaces=interfaces,
        service_ports=[port],
        groups=("provider_stubs",),
    )
    border = raw_device("d2", "bl-1", role="border-leaf", deployment_kind="TopologyDataCenter")
    zone = raw_device("d3", "sw-1", deployment_kind="TopologyColocationZone")
    old = raw_device("d4", "old-1", status="decommissioned")
    empty = raw_root_instance(raw_instance("v2", "x", "NONPROD", 60001, "non_prod"))
    return raw_response(device, border, zone, old, instances=(empty,))


def test_the_query_tree_resolves_the_in_file_fragments() -> None:
    """The fragments contribute their fields under the paths that spread them."""
    device = _query_tree()["DcimDevice"]["edges"]["node"]
    assert "segment_deployment" in device["vlans"]["edges"]["node"]
    far_end = device["service_ports"]["edges"]["node"]["cable"]["node"]["endpoints"]["edges"]["node"]
    assert "ip_address" in far_end["device"]["node"]["interfaces"]["edges"]["node"]


def test_the_query_selects_memberships_hosting_and_the_vrf_root() -> None:
    """The device's ``shared_capabilities``, the deployment with its parent, the ``ManagedVRF`` root and the SVI's VRF."""
    tree = _query_tree()
    device = tree["DcimDevice"]["edges"]["node"]
    assert "vrfs" not in device
    assert {"security_zone", "deployment"} <= set(device["shared_capabilities"]["edges"]["node"])
    assert "parent" in device["deployment"]["node"]
    assert {"deployment", "security_zone", "shared_devices"} <= set(tree["ManagedVRF"]["edges"]["node"])
    assert "devices" not in tree["ManagedVRF"]["edges"]["node"]
    # An interface's VRF is the ManagedVRF among its capabilities (alias zone_capabilities), with its deployment.
    assert "vrf" not in device["interfaces"]["edges"]["node"]
    assert "deployment" in device["interfaces"]["edges"]["node"]["zone_capabilities"]["edges"]["node"]
    for instance in (device["shared_capabilities"]["edges"]["node"], tree["ManagedVRF"]["edges"]["node"]):
        assert "namespace" not in instance
        assert set(instance["security_zone"]["node"]) == {"id", "name", "vrf_type", "l3_vni"}
    assert set(tree["SecurityZone"]["edges"]["node"]) == {"id", "name", "vrf_type", "l3_vni"}
    assert "IpamNamespace" not in tree
    interface = device["interfaces"]["edges"]["node"]
    assert "security_zone" not in interface
    # The VRF and circuits are read under the alias `zone_capabilities`; the interface's own `capabilities` stays the
    # only one.
    zone_capabilities = interface["zone_capabilities"]["edges"]["node"]
    assert set(zone_capabilities["security_zone"]["node"]) == {"id", "name", "vrf_type", "l3_vni"}
    assert set(zone_capabilities["vrf"]["node"]["security_zone"]["node"]) == {"id", "name", "vrf_type", "l3_vni"}
    # The tree merges the interface kinds: an SVI's address is read by id too (S5 compares it with the gateway's).
    registry_address = interface["ip_address"]["node"]
    assert set(registry_address) == {"id", "address", "ip_namespace"}
    assert "segment_vlan" not in interface
    assert "id" in device["vlans"]["edges"]["node"]["segment_deployment"]["node"]["segment"]["node"]["gateway"]["node"]
    assert set(registry_address["ip_namespace"]["node"]) == {"name"}


def test_the_raw_builders_emit_only_fields_the_query_selects() -> None:
    """A builder key the query does not select would make the unit fixtures drift from the real payload."""
    assert _unselected(_everything(), _query_tree()) == []


def test_the_pin_catches_a_field_the_query_does_not_select() -> None:
    """Guard of the guard: an unselected key is reported with its path."""
    response = _everything()
    response["DcimDevice"]["edges"][0]["node"]["bogus"] = {"value": 1}
    assert _unselected(response, _query_tree()) == [".DcimDevice.edges.node.bogus"]
