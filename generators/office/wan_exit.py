"""The WAN exit of an office as plan builders: the cables from the SD-WAN edge's LAN ports to the distribution switches,
then, per link, a /31 from the PROD transit pool, a dot1q sub-interface per end in the tenant VRF and the eBGP peering
between the switch and the edge. Pure functions of what the caller read (generators/plan.py).

The exit is two plans: the cables first (the edge is built earlier in the same run), then the rest,
which needs the VRF instances of the devices of both ends (``capabilities/vrf.py``) and so their ids.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from generators.capabilities.bgp import (
    plan_address_family,
    plan_password,
    plan_process,
    plan_session_peering,
    plan_shared_as,
)
from generators.capabilities.common import id_ref
from generators.dcim.cabling_plan import CablingOutcome, ChainPorts, PortFacts, plan_cabling
from generators.dcim.device_plan import device_hfid
from generators.plan import EdgesSpec, Hfid, NodeRef, NodeSpec, PrefixAllocationSpec, TopologyPlan
from generators.protocols import DcimVirtualInterface

# The WAN exit: the edge's LAN ports are its template's `downlink` ports, the switch side its `uplink` port.
EDGE_LAN_PORT_ROLE = "downlink"
SWITCH_PORT_ROLE = "uplink"
WAN_EXIT_LINK_PREFIX_LENGTH = 31
WAN_EXIT_PROCESS_ROLE = "regular"
INTERFACE_KIND = DcimVirtualInterface.__name__


def wan_exit_key_name(fabric_name: str) -> str:
    return f"{fabric_name}-wan-exit-key"


def switch_as_description(fabric_name: str) -> str:
    return f"{fabric_name} WAN exit ASN"


def edge_as_description(fabric_name: str) -> str:
    return f"{fabric_name} SD-WAN edge ASN"


@dataclass(frozen=True)
class ExitLink:
    """One cabled edge-to-switch link of the exit: its two ports and their ids, which key the link's prefix."""

    switch: PortFacts
    switch_port_id: str
    edge: PortFacts
    edge_port_id: str


def plan_exit_cabling(
    plan: TopologyPlan, *, edge_name: str, switch_names: Sequence[str], ports: ChainPorts, deployment_id: str
) -> CablingOutcome:
    """Cable every LAN port of the edge to a port of the distribution switches (each edge port to its own switch); the link
    addresses are the tenant-VRF ones of ``plan_exit_links``. A side without a port is a problem in the outcome.
    """
    return plan_cabling(
        plan,
        bottom_ports=ports.with_role([edge_name], EDGE_LAN_PORT_ROLE),
        top_ports=ports.with_role(switch_names, SWITCH_PORT_ROLE),
        strategy="rack",
        offset=0,
        bottom_sorting="bottom_up",
        top_sorting="bottom_up",
        deployment_id=deployment_id,
        pool_id=None,
        p2p_prefix_length=WAN_EXIT_LINK_PREFIX_LENGTH,
        positions={},
    )


def _sub_interface(
    plan: TopologyPlan,
    *,
    port: PortFacts,
    role: str,
    vlan: int,
    address: NodeRef,
    vrf_id: str,
    tracked_key: str,
) -> NodeRef:
    """A dot1q sub-interface ``<trunk>.<vlan>`` on ``port`` in the VRF instance ``vrf_id`` with its address, riding on the
    trunk (its ``parent`` and ``vlan_id``).
    """
    name = f"{port.port_name}.{vlan}"
    key = f"sub:{tracked_key}"
    plan.add(
        NodeSpec(
            key=key,
            kind=INTERFACE_KIND,
            data={
                "name": name,
                "device": device_hfid(port.device_name),
                "status": "active",
                "role": role,
                "ip_address": address,
                "parent": port.hfid,
                "vlan_id": vlan,
            },
            ownership="owned",
            hfid=Hfid(INTERFACE_KIND, (port.device_name, name)),
        )
    )
    plan.add(EdgesSpec(NodeRef(key), "capabilities", (vrf_id,), retry_not_found=True, kind=DcimVirtualInterface))
    return NodeRef(key)


def plan_exit_links(
    plan: TopologyPlan,
    *,
    office_name: str,
    fabric_name: str,
    edge_name: str,
    links: Sequence[ExitLink],
    transit_pool: str,
    vlan: int,
    vrf_ids: Mapping[tuple[str, str], str],
    device_ids: Mapping[str, str],
    zone: str,
    asn_pool_id: str,
    as_ids: Mapping[str, str | None],
    families: Mapping[tuple[str, str], str],
    router_ids: Mapping[str, str],
    unicast: tuple[str, str],
    logger: logging.Logger,
) -> None:
    """Address, VRF-place and peer every link of the exit, in the order of ``links`` (sorted by switch): per link a /31
    from the ``transit_pool`` (keyed by the ids of its two ports, the switch taking the lower address) and a sub-interface
    per end in the instance of the ``zone`` (``vrf_ids``: (device id, zone) -> instance); one process per switch (its
    loopback address as router id: ``router_ids`` by switch name; a switch without one is skipped, warned) and one on the
    edge (the edge's first handoff address as router id), each in the AS of its side (found by description in ``as_ids``,
    else drawn from ``asn_pool_id``); one eBGP peering per link with the exit's key.
    """
    key = plan_password(
        plan,
        name=wan_exit_key_name(fabric_name),
        description=f"eBGP auth key of the WAN exit of office {office_name}",
    )
    switch_as = plan_shared_as(
        plan,
        description=switch_as_description(fabric_name),
        asn_pool=asn_pool_id,
        existing_id=as_ids.get(switch_as_description(fabric_name)),
        key="shared-as:wan-exit",
    )
    edge_as = plan_shared_as(
        plan,
        description=edge_as_description(fabric_name),
        asn_pool=asn_pool_id,
        existing_id=as_ids.get(edge_as_description(fabric_name)),
        key="shared-as:sdwan-edge",
    )
    if switch_as is None or edge_as is None:
        raise ValueError(f"Office {office_name}: no AS number for the WAN exit")
    unicast_ref = plan_address_family(plan, unicast, families)
    prefixes: list[tuple[ExitLink, PrefixAllocationSpec, NodeRef, NodeRef]] = []
    for link in links:
        label = f"WAN exit {link.switch.device_name} <-> {edge_name}"
        prefix = PrefixAllocationSpec(
            key=f"wan-exit:{link.switch.device_name}",
            identifier=f"wan-exit__{'__'.join(sorted([link.edge_port_id, link.switch_port_id]))}",
            prefix_length=WAN_EXIT_LINK_PREFIX_LENGTH,
            role="technical",
            pool_name=transit_pool,
            is_pool=True,
            member_type="address",
            description=label,
            address_offsets=(0, 1),
            address_description=label,
            address_ownership="owned",
            track_prefix=True,
        )
        plan.add(prefix)
        switch_sub = _sub_interface(
            plan,
            port=link.switch,
            role=SWITCH_PORT_ROLE,
            vlan=vlan,
            address=NodeRef(prefix.address_key(0)),
            vrf_id=vrf_ids[(device_ids[link.switch.device_name], zone)],
            tracked_key=f"{link.switch.device_name}/{link.switch.port_name}",
        )
        edge_sub = _sub_interface(
            plan,
            port=link.edge,
            role=EDGE_LAN_PORT_ROLE,
            vlan=vlan,
            address=NodeRef(prefix.address_key(1)),
            vrf_id=vrf_ids[(device_ids[edge_name], zone)],
            tracked_key=f"{edge_name}/{link.edge.port_name}",
        )
        prefixes.append((link, prefix, switch_sub, edge_sub))
    if not prefixes:
        return
    # The edge has no loopback: its router-id is its first handoff address, as the gateway's is.
    edge_process = plan_process(
        plan,
        device=device_hfid(edge_name),
        name=edge_name,
        suffix="wan-exit",
        kind="bgp",
        description=f"WAN exit eBGP process of {edge_name}",
        data={
            "process_role": WAN_EXIT_PROCESS_ROLE,
            "local_as": id_ref(edge_as),
            "router_id": id_ref(NodeRef(prefixes[0][1].address_key(1))),
        },
    )
    for link, _prefix, switch_sub, edge_sub in prefixes:
        switch = link.switch.device_name
        router_id = router_ids.get(switch)
        if router_id is None:
            logger.warning(f"Office {office_name}: {switch} has no addressed loopback — no WAN exit BGP")
            continue
        switch_process = plan_process(
            plan,
            device=device_hfid(switch),
            name=switch,
            suffix="wan-exit",
            kind="bgp",
            description=f"WAN exit eBGP process of {switch}",
            data={
                "process_role": WAN_EXIT_PROCESS_ROLE,
                "local_as": id_ref(switch_as),
                "router_id": id_ref(router_id),
            },
        )
        plan_session_peering(
            plan,
            # Peerings share a name space with circuits: neither side's bare name.
            name=f"WAN-EXIT-{switch}-{edge_name}",
            description=f"WAN exit {switch} <-> {edge_name} (eBGP in the tenant VRF)",
            status="active",
            session_type="EBGP",
            peering_role="regular",
            process_ids=[NodeRef(f"bgp:{switch_process.parts[0]}"), NodeRef(f"bgp:{edge_process.parts[0]}")],
            address_families=[unicast_ref],
            interface_ids=[switch_sub, edge_sub],
            password=key,
        )
