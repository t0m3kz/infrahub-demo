"""The circuit check queries select every field the checks read, and the checks read every field they select."""

from __future__ import annotations

import pytest

from tests.unit.circuit_placement_check_helpers import DCI, DF10, HANDOFF, INET_OFFICE, TUNNEL, VPN_CIRCUIT, _edges
from tests.unit.generators.test_circuit_query_shapes import _QUERIES, ENCAPSULATION, _payload_paths, _selected_paths

PLACEMENT = _QUERIES / "validation" / "circuit_placement.gql"

# checks/connectivity/circuit_placement.py: what it reads from a carrier circuit.
_PHYSICAL_READS = [
    "id",
    "name",
    "status",
    "circuit_type",
    "provider.id",
    "locations.id",
    "locations.__typename",
    "locations.name",
    "locations.owner.id",
    "locations.owner.org_id",
    "capabilities.id",
    "capabilities.__typename",
    "capabilities.name",
    "capabilities.device.id",
    "capabilities.device.__typename",
    "capabilities.device.name",
    "capabilities.device.deployment.id",
    "capabilities.device.deployment.__typename",
    "capabilities.device.deployment.name",
    # a cloud transport has no deployment: its region or SaaS region places it
    "capabilities.device.region.id",
    "capabilities.device.region.__typename",
    "capabilities.device.region.name",
    "capabilities.device.saas_region.id",
    "capabilities.device.saas_region.__typename",
    "capabilities.device.saas_region.name",
    # the provider's port has no deployment: its own location (a zone, a region, a SaaS region) places it
    "capabilities.device.location.id",
    "capabilities.device.location.__typename",
    "capabilities.device.location.name",
    # a provider's own router has no place: its owner is the circuit's provider
    "capabilities.device.owner.id",
    "capabilities.device.owner.__typename",
]

# What it reads from the end ports of a virtual circuit (the shared VirtualPort fragment): the port, the physical
# circuits on it and on its parent interface (utils.circuit_underlay.port_underlay), the cable on either (port_cabled).
_PORT_READS = [
    "capabilities.id",
    "capabilities.name",
    "capabilities.device.id",
    "capabilities.device.__typename",
    "capabilities.device.name",
    # a handoff's locations are exactly where its end devices are deployed
    "capabilities.device.deployment.id",
    "capabilities.device.deployment.__typename",
    "capabilities.device.deployment.name",
    # a cloud transport has no deployment: its region or SaaS region places it
    "capabilities.device.region.id",
    "capabilities.device.region.__typename",
    "capabilities.device.region.name",
    "capabilities.device.saas_region.id",
    "capabilities.device.saas_region.__typename",
    "capabilities.device.saas_region.name",
    "capabilities.device.capabilities.__typename",
    "capabilities.device.capabilities.process_role",
    # the shape of a handoff end: a cloud peering interface (its vlan_id) or a routed physical port (its address)
    "capabilities.__typename",
    f"{ENCAPSULATION}.__typename",
    f"{ENCAPSULATION}.vlan_id",
    "capabilities.ip_address.id",
    # a hop inside one facility is a cable: on the physical port, or on the parent of a sub-interface
    "capabilities.cable.id",
    f"{ENCAPSULATION}.parent.cable.id",
    *(
        f"capabilities.{where}capabilities.{field}"
        for where in ("", "encapsulation_capabilities.parent.")
        for field in (
            "__typename",
            "id",
            "name",
            "status",
            "circuit_type",
            "locations.id",
            "locations.__typename",
            "locations.name",
            "capabilities.id",
            "capabilities.name",
            "capabilities.device.name",
        )
    ),
]

# What it reads from a session circuit, for every circuit_type: the office locations (with their owner), a dci's and a
# handoff's status, the namespace of a handoff or vpn, and the fields it refuses on the other types.
_SESSION_READS = [
    "id",
    "name",
    "status",
    "circuit_type",
    "locations.id",
    "locations.__typename",
    "locations.name",
    "locations.owner.id",
    "locations.owner.org_id",
    "vrf.id",
    "vrf.deployment.id",
    "vrf.deployment.name",
    *(f"vrf.security_zone.{field}" for field in ("id", "name", "vrf_type", "l3_vni")),
    "password.id",
    "registered_prefixes.id",
]
_ROOTS = ["TopologyCarrierCircuit", "TopologySessionCircuit"]


@pytest.fixture(scope="module")
def placement_paths() -> dict[str, set[str]]:
    """Selected paths per root of circuit_placement.gql."""
    return {root: _selected_paths(PLACEMENT, root, by_alias=True) for root in _ROOTS}


def _reads(root: str) -> list[str]:
    return _PHYSICAL_READS if root == "TopologyCarrierCircuit" else [*_SESSION_READS, *_PORT_READS]


@pytest.mark.parametrize("root", _ROOTS)
def test_the_placement_query_selects_what_the_check_reads(root: str, placement_paths: dict[str, set[str]]) -> None:
    """Every path the check reads from a circuit of this kind is selected by circuit_placement.gql."""
    missing = [path for path in _reads(root) if path not in placement_paths[root]]
    assert not missing, f"read but not selected under {root}: {missing}"


def test_the_placement_query_selects_nothing_the_check_never_reads(placement_paths: dict[str, set[str]]) -> None:
    """A selected leaf that no kind's rules read is dead weight on every run."""
    read = {path for root in placement_paths for path in _reads(root)}
    prefixes = {".".join(path.split(".")[:i]) for path in read for i in range(1, len(path.split(".")))}
    unread = {path for paths in placement_paths.values() for path in paths} - read - prefixes
    assert not unread, f"selected but never read: {sorted(unread)}"


def test_the_placement_query_has_no_direct_zone_or_ipam_routing_edges(
    placement_paths: dict[str, set[str]],
) -> None:
    """A session's routing context is its local VRF, not a direct zone or prefix annotation."""
    paths = placement_paths["TopologySessionCircuit"]
    assert {"vrf.id", "vrf.deployment.id", "vrf.deployment.name", "vrf.security_zone.id"} <= paths
    assert not {"security_zone", "ip_namespace"} & paths
    assert not any(".ip_address.security_zone" in path or ".ip_prefix.security_zone" in path for path in paths)


@pytest.mark.parametrize(
    ("root", "fixture"),
    [
        ("TopologyCarrierCircuit", DF10),
        ("TopologyCarrierCircuit", INET_OFFICE),
        ("TopologySessionCircuit", TUNNEL),
        ("TopologySessionCircuit", DCI),
        ("TopologySessionCircuit", HANDOFF),
        ("TopologySessionCircuit", VPN_CIRCUIT),
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
