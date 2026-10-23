"""The circuit check queries select every field the checks read, and the checks read every field they select.

Unit tests feed canned payloads, so a field a check reads but the query does not select passes here and fails only on
a real stack (the cleaned dict simply lacks it). These tests parse the ``.gql`` files with graphql-core, flatten them
to the field paths the cleaned data carries (``edges``/``node``/``value`` wrappers dropped, fragments merged) and
compare them with the paths the checks read, listed by hand below, and with the paths of the fixtures the check
tests feed.
"""

from __future__ import annotations

import pytest

from tests.unit.test_circuit_placement_check import DCI, DF10, HANDOFF, INET_OFFICE, TUNNEL, _edges
from tests.unit.test_circuit_query_shapes import _QUERIES, _payload_paths, _selected_paths
from tests.unit.test_routing_invariants_check import _vif

PLACEMENT = _QUERIES / "validation" / "circuit_placement.gql"
INVARIANTS = _QUERIES / "validation" / "routing_invariants.gql"

# checks/circuit_placement.py: what it reads from a physical circuit.
_PHYSICAL_READS = [
    "id",
    "name",
    "owner.id",
    "owner.org_id",
    "locations.id",
    "locations.__typename",
    "locations.name",
    "locations.owner.id",
    "locations.owner.org_id",
    "capabilities.id",
    "capabilities.name",
    "capabilities.device.id",
    "capabilities.device.name",
    "capabilities.device.deployment.id",
    "capabilities.device.deployment.__typename",
    "capabilities.device.deployment.name",
]

# What it reads from the end ports of a virtual circuit (the shared VirtualPort fragment): the port, the physical
# circuits on it and on its parent interface (utils.circuit_underlay.port_underlay), the cable on either
# (port_cabled), the device and its processes.
_PORT_READS = [
    "capabilities.id",
    "capabilities.name",
    "capabilities.device.id",
    "capabilities.device.__typename",
    "capabilities.device.name",
    "capabilities.device.capabilities.__typename",
    "capabilities.device.capabilities.process_role",
    # a hop inside one facility is a cable: on the physical port, or on the parent of a sub-interface
    "capabilities.cable.id",
    "capabilities.parent_interface.cable.id",
    *(
        f"capabilities.{where}capabilities.{field}"
        for where in ("", "parent_interface.")
        for field in (
            "__typename",
            "id",
            "name",
            "status",
            "locations.id",
            "locations.__typename",
            "locations.name",
            "capabilities.id",
            "capabilities.name",
            "capabilities.device.name",
        )
    ),
]

_VIRTUAL_READS = {
    "TopologyTunnelCircuit": [
        "id",
        "name",
        "owner.id",
        "owner.org_id",
        "locations.id",
        "locations.__typename",
        "locations.name",
        "locations.owner.id",
        "locations.owner.org_id",
    ],
    "TopologyInterconnectCircuit": [
        "id",
        "name",
        "status",
        "owner.id",
        "owner.org_id",
        "locations.id",
        "locations.__typename",
        "locations.name",
    ],
    "TopologyHandoffCircuit": [
        "id",
        "name",
        "status",
        "locations.id",
        "locations.__typename",
        "locations.name",
        "ip_namespace.name",
        "ip_namespace.namespace_type",
        "cloud_endpoint.name",
        "cloud_endpoint.vlan_id",
    ],
}

# checks/routing_invariants.py: what it reads from a cloud virtual interface and its handoff circuit.
_VIF_READS = [
    "id",
    "name",
    "virtual_network.account.environment",
    "virtual_circuit.name",
    "virtual_circuit.ip_namespace.name",
    "virtual_circuit.ip_namespace.namespace_type",
]


@pytest.fixture(scope="module")
def placement_paths() -> dict[str, set[str]]:
    """Selected paths per root of circuit_placement.gql."""
    roots = ["TopologyPhysicalCircuit", *_VIRTUAL_READS]
    return {root: _selected_paths(PLACEMENT, root) for root in roots}


def _reads(root: str) -> list[str]:
    return _PHYSICAL_READS if root == "TopologyPhysicalCircuit" else [*_VIRTUAL_READS[root], *_PORT_READS]


@pytest.mark.parametrize("root", ["TopologyPhysicalCircuit", *_VIRTUAL_READS])
def test_the_placement_query_selects_what_the_check_reads(root: str, placement_paths: dict[str, set[str]]) -> None:
    """Every path the check reads from a circuit of this kind is selected by circuit_placement.gql."""
    missing = [path for path in _reads(root) if path not in placement_paths[root]]
    assert not missing, f"read but not selected under {root}: {missing}"


def test_the_placement_query_selects_nothing_the_check_never_reads(placement_paths: dict[str, set[str]]) -> None:
    """A selected leaf that no kind's rules read is dead weight on every run (the shared port fragment is read by
    the union of the kinds: a Tunnel never reads the BGP processes, the Interconnect does)."""
    read = {path for root in placement_paths for path in _reads(root)}
    prefixes = {".".join(path.split(".")[:i]) for path in read for i in range(1, len(path.split(".")))}
    unread = {path for paths in placement_paths.values() for path in paths} - read - prefixes
    assert not unread, f"selected but never read: {sorted(unread)}"


@pytest.mark.parametrize(
    ("root", "fixture"),
    [
        ("TopologyPhysicalCircuit", DF10),
        ("TopologyPhysicalCircuit", INET_OFFICE),
        ("TopologyTunnelCircuit", TUNNEL),
        ("TopologyInterconnectCircuit", DCI),
        ("TopologyHandoffCircuit", HANDOFF),
    ],
)
def test_the_check_fixtures_carry_only_selected_fields(
    root: str, fixture: dict, placement_paths: dict[str, set[str]]
) -> None:
    """A fixture path the query does not select is a field no real response carries."""
    fake_paths = _payload_paths(fixture)
    assert _edges([fixture])  # the fixtures are raw query nodes
    unselected = fake_paths - placement_paths[root]
    assert not unselected, f"fixture fields the query does not select under {root}: {sorted(unselected)}"


def test_the_invariants_query_selects_what_the_check_reads() -> None:
    selected = _selected_paths(INVARIANTS, "CloudVirtualInterface")
    missing = [path for path in _VIF_READS if path not in selected]
    assert not missing, f"read but not selected: {missing}"


def test_the_invariants_query_selects_nothing_more_of_the_handoff() -> None:
    selected = {
        path for path in _selected_paths(INVARIANTS, "CloudVirtualInterface") if path.startswith("virtual_circuit")
    }
    prefixes = {".".join(path.split(".")[:i]) for path in _VIF_READS for i in range(1, len(path.split(".")))}
    assert selected - set(_VIF_READS) - prefixes == set()


def test_the_invariants_fixture_carries_only_selected_fields() -> None:
    selected = _selected_paths(INVARIANTS, "CloudVirtualInterface")
    assert _payload_paths(_vif("vif-1", "prod", env="p")) <= selected
