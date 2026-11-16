"""EVPN Multi-Site for a colocation zone or virtual colocation as plan builders: the border-gateway processes of the
edges and, for a pair of physical edges, their interlink and the iBGP FABRIC session. Pure functions of what the caller
read (generators/plan.py): no write, no read of anything the run creates; ``ColocationFabricGenerator`` applies them in
the plan of its zone.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from generators.capabilities.bgp import (
    EVPN_AF,
    IPV6_UNICAST_AF,
    plan_address_family,
    plan_password,
    plan_process,
    plan_session_peering,
)
from generators.capabilities.common import Ref, id_ref
from generators.dcim.cabling_plan import ChainHop, ChainPorts, Link, plan_chain_legs
from generators.dcim.device_plan import LOOPBACK_KIND, device_hfid
from generators.helpers.cable_attributes import RackPosition
from generators.helpers.interface_naming import get_loopback_name
from generators.helpers.routing import p2p_prefix_length
from generators.plan import AddressAllocationSpec, Hfid, NodeRef, NodeSpec, TopologyPlan

# EVPN Multi-Site border gateway: a tunnel forms only between VTEPs of one address family and the fabrics run an IPv6
# underlay, so the VTEP is a dedicated IPv6 loopback (the IPv4 Loopback0 stays the 32-bit router-id).
COLO_VTEP_LOOPBACK_INDEX = 1
# The two edges of a zone share no fabric: one direct link (role `interlink`) carries a /127 and the iBGP FABRIC
# session.
COLO_INTERLINK_ROLE = "interlink"
COLO_P2P_IPV6_PREFIX_LENGTH = p2p_prefix_length("ipv6")
COLO_FABRIC_AFS = (IPV6_UNICAST_AF, EVPN_AF)


def site_as_description(metro_name: str) -> str:
    """The description that keys the AS every zone of the metro shares as its EVPN site AS."""
    return f"{metro_name.lower()} EVPN site ASN"


def fabric_key_name(fabric_name: str) -> str:
    return f"{fabric_name}-fabric-key"


def overlay_process_ref(edge_name: str) -> NodeRef:
    """The overlay process ``plan_edge_borders`` plans for ``edge_name``."""
    return NodeRef(f"bgp:{edge_name}-bgp-overlay")


@dataclass(frozen=True)
class EvpnEdge:
    """An edge of the zone: its name, its platform (it names the VTEP loopback) and the kind of device it is."""

    name: str
    platform: str
    device_kind: str


@dataclass(frozen=True)
class EdgePairReads:
    """What the edge pair needs of the edges that exist: the ports of the two edges (the live ones, else their
    template's) and their rack positions.
    """

    ports: ChainPorts
    positions: Mapping[str, RackPosition | None] = field(default_factory=dict)


def plan_edge_borders(
    plan: TopologyPlan,
    *,
    edges: Sequence[EvpnEdge],
    site_as: Ref,
    routers: Mapping[str, Ref | None],
    vtep_pool: Ref,
) -> None:
    """Make the ``edges`` EVPN Multi-Site border gateways: an IPv6 VTEP loopback each (a /128 from ``vtep_pool``, keyed
    by the edge: the same on a re-run) and an overlay ``ManagedBGP`` process in the metro's ``site_as`` with the edge's
    loopback address (``routers``: edge name -> the address, an id or a ``NodeRef`` of the plan) as router id. DCI
    sessions and the Multi-Site VIP are not built here.
    """
    for edge in edges:
        router_id = routers.get(edge.name)
        if router_id is None:
            raise ValueError(f"{edge.name} has no addressed loopback: it cannot be an EVPN border gateway")
        address = AddressAllocationSpec(
            key=f"vtep-address:{edge.name}",
            identifier=f"{edge.name}-vtep",
            prefix_length=128,
            description=f"EVPN VTEP loopback for {edge.name}",
            ownership="shared",
            pool_id=vtep_pool,
        )
        plan.add(address)
        vtep_name = get_loopback_name(edge.platform, COLO_VTEP_LOOPBACK_INDEX)
        plan.add(
            NodeSpec(
                key=f"vtep:{edge.name}",
                kind=LOOPBACK_KIND,
                data={
                    "name": vtep_name,
                    "description": "EVPN VTEP source (IPv6)",
                    "device": device_hfid(edge.name, edge.device_kind),
                    "status": "active",
                    "role": "loopback-vtep",
                    "ip_address": NodeRef(address.key),
                },
                ownership="owned",
                hfid=Hfid(LOOPBACK_KIND, (edge.name, vtep_name)),
            )
        )
        plan_process(
            plan,
            device=device_hfid(edge.name, edge.device_kind),
            name=edge.name,
            suffix="overlay",
            kind="bgp",
            description=f"EVPN Multi-Site border-gateway process for {edge.name}",
            data={"local_as": id_ref(site_as), "router_id": id_ref(router_id), "process_role": "overlay"},
        )


def plan_edge_pair(
    plan: TopologyPlan,
    *,
    first: str,
    second: str,
    label: str,
    fabric_name: str,
    reads: EdgePairReads,
    interlink_pool: Ref,
    families: Mapping[tuple[str, str], str],
    deployment_id: str,
    problems: list[str],
) -> list[Link]:
    """Link two physical edges and peer them over that link (iBGP FABRIC: IPv6 unicast and l2vpn evpn): the interlink
    cable (a /127 from ``interlink_pool``), the zone's FABRIC key and the session between the two overlay processes of
    ``plan_edge_borders``. A pair without a port of role ``interlink`` is a problem the caller raises before anything is
    written. Returns the links (none: no session).
    """
    links = plan_chain_legs(
        plan,
        hops=(
            ChainHop(devices=[first], down_role=COLO_INTERLINK_ROLE),
            ChainHop(devices=[second], up_role=COLO_INTERLINK_ROLE),
        ),
        ports=reads.ports,
        deployment_id=deployment_id,
        positions=reads.positions,
        problems=problems,
        pool_id=interlink_pool,
        p2p_prefix_length=COLO_P2P_IPV6_PREFIX_LENGTH,
    )
    if not links:
        return links
    key = plan_password(
        plan,
        name=fabric_key_name(fabric_name),
        description=f"iBGP FABRIC session auth key for {label} {fabric_name}",
    )
    plan_session_peering(
        plan,
        name=f"FABRIC-{first}-{second}",
        description=(
            f"Colocation {label} {fabric_name} edge pair {first} <-> {second}: IPv6 unicast + EVPN over the interlink"
        ),
        status="active",
        session_type="IBGP",
        peering_role="fabric",
        process_ids=[overlay_process_ref(first), overlay_process_ref(second)],
        address_families=[plan_address_family(plan, family, families) for family in COLO_FABRIC_AFS],
        interface_ids=[
            NodeRef(f"port:{port.device_name}/{port.port_name}") for link in links for port in (link.bottom, link.top)
        ],
        password=key,
    )
    return links
