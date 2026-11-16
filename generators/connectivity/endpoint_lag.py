"""LAG flow of the endpoint generator: a server's bonds wired into a switch pair's ManagedMLAG domain. ``read_lag_wiring``
reads what the choice of ports and LAG ids depends on, ``plan_lag_wiring`` is the pure builder (one cable per member, one
port-channel per switch), so the caller holds its ``plan_locks`` around both and the apply.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from netutils.interface import sort_interface_list

from generators.connectivity.connection_fingerprint import ConnectionFingerprint
from generators.connectivity.endpoint_targets import interface_type_of
from generators.connectivity.endpoint_uplink import group_by_device, plan_cables, select_switch_pair
from generators.dcim.far_end import far_end_interfaces
from generators.helpers.cable_attributes import RackPosition
from generators.helpers.cabling import pick_matched_switch_port_name
from generators.helpers.interface_naming import get_lag_name
from generators.plan import Hfid, NodeSpec, TopologyPlan
from generators.protocols import DcimLAGInterface, DcimPhysicalDevice, DcimPhysicalInterface

MLAG_PEER_LINK_LAG_ID = 100
"""The lag_id reserved for the MLAG peer-link (see mlag.py)."""


@dataclass(frozen=True)
class LagReads:
    """What the LAG ports and ids are chosen from: the switch pair (by name), the MLAG domain they share, the lag ids in
    use on them and the switch port at the far end of every already cabled member.
    """

    switch_a: str
    switch_b: str
    switches: dict[str, Any]
    mlag_domain_id: str
    existing_lag_ids: frozenset[int]
    far_ends: dict[str, Any]


@dataclass(frozen=True)
class LagWiring:
    """The two plans of the wiring, applied in this order: the cables, then the port-channels (a port-channel is created
    after both of its members are cabled). ``connections`` are the cables as fingerprints.
    """

    cabling: TopologyPlan
    port_channels: TopologyPlan
    connections: tuple[ConnectionFingerprint, ...] = ()


def next_free_lag_id(existing_ids: set[int]) -> int:
    """Smallest positive lag_id not already in use (100 reserved for MLAG peer-link, see mlag.py)."""
    candidate = 1
    taken = existing_ids | {MLAG_PEER_LINK_LAG_ID}
    while candidate in taken:
        candidate += 1
    return candidate


async def read_lag_wiring(
    client: Any,
    logger: logging.Logger,
    *,
    endpoint_name: str,
    members_by_bond: Mapping[str, Sequence[Any]],
    target_interfaces: Sequence[Any],
    existing_switch_names: set[str],
) -> LagReads | None:
    """Pick the switch pair and read its MLAG domain, LAG ids and the far ends of the cabled members. None when there are
    no targets; an error (``logger.error``) when the pair is not two switches sharing exactly one domain.
    """
    if not target_interfaces:
        return None
    device_groups = group_by_device(target_interfaces, logger)
    if len(device_groups) < 2:
        logger.error(f"Endpoint {endpoint_name}: Need 2 switches for MLAG-attached bonds, found {len(device_groups)}.")
        return None

    # A later bond lands on the same pair as the earlier ones.
    switch_a, switch_b = select_switch_pair(list(device_groups), existing_switch_names)
    found = await client.filters(
        kind=DcimPhysicalDevice, name__values=[switch_a, switch_b], include=["capabilities", "platform"]
    )
    switches = {switch.name.value: switch for switch in found}
    mlag_ids = {
        name: {peer.id for peer in switch.capabilities.peers if peer.typename == "ManagedMLAG"}
        for name, switch in switches.items()
    }
    shared = mlag_ids.get(switch_a, set()) & mlag_ids.get(switch_b, set())
    if len(shared) != 1:
        logger.error(
            f"Endpoint {endpoint_name}: {switch_a}/{switch_b} share {len(shared)} MLAG domain(s) (need exactly 1) — "
            "cannot wire LAG bond(s). Pair the switches into a ManagedMLAG domain first."
        )
        return None

    lags = await client.filters(kind=DcimLAGInterface, device__ids=[switches[switch_a].id, switches[switch_b].id])
    # The switch port at the far end of every already-cabled member, in one batch for all the bonds.
    far_ends = await far_end_interfaces(
        client,
        [m for members in members_by_bond.values() if len(members) >= 2 for m in members if m.cable and m.cable.id],
        include=["lag"],
    )
    return LagReads(
        switch_a=switch_a,
        switch_b=switch_b,
        switches=switches,
        mlag_domain_id=next(iter(shared)),
        existing_lag_ids=frozenset(lag.lag_id.value for lag in lags),
        far_ends=far_ends,
    )


def plan_lag_wiring(
    logger: logging.Logger,
    reads: LagReads,
    *,
    endpoint_name: str,
    members_by_bond: Mapping[str, Sequence[Any]],
    target_interfaces: Sequence[Any],
    deployment_id: str,
    positions: Mapping[str, RackPosition | None],
) -> LagWiring:
    """Wire each bond to the pair's ManagedMLAG domain: one port-channel per target switch on the same domain. A bond
    reuses the switch ports an earlier run cabled (read off the far end of its members, and the port-channel's lag id),
    else it claims the lowest free port of its member's type, the same name on both switches when they have one. A
    bond that cannot be wired is an error (``logger.error``).
    """
    switch_a, switch_b = reads.switch_a, reads.switch_b
    device_groups = group_by_device(target_interfaces, logger)
    existing_lag_ids = set(reads.existing_lag_ids)
    port_channels = TopologyPlan(root_id=None)
    planned: set[ConnectionFingerprint] = set()
    connections: list[tuple[ConnectionFingerprint, str | None, str | None]] = []

    for bond_name, members in members_by_bond.items():
        if len(members) < 2:
            logger.error(f"Bond {bond_name} on {endpoint_name} has < 2 member interfaces — cannot wire it")
            continue

        member_names = sort_interface_list([m.name.value for m in members])
        member_by_name = {m.name.value: m for m in members}

        # Reuse the switch port a prior run cabled (read off the far end); else claim a free one. Reusing re-touches
        # the cable and port-channel so tracking keeps them.
        free_ports_by_switch: dict[str, list[DcimPhysicalInterface]] = {}
        for name, server_interface_name in zip((switch_a, switch_b), member_names):
            # A member only takes a switch port of its own type (a speed mismatch is refused).
            member_type = interface_type_of(member_by_name[server_interface_name])
            free_ports = [
                p
                for p in device_groups[name]
                if not (p.cable and p.cable.id) and (member_type is None or interface_type_of(p) == member_type)
            ]
            free_port_by_name = {p.name.value: p for p in free_ports}
            sorted_names = sort_interface_list(list(free_port_by_name.keys()))
            free_ports_by_switch[name] = [free_port_by_name[n] for n in sorted_names]

        switch_port_by_name: dict[str, DcimPhysicalInterface] = {}
        members_needing_fresh_port: dict[str, str] = {}
        for switch_name, server_interface_name in zip((switch_a, switch_b), member_names):
            member = member_by_name[server_interface_name]
            if (far_end := reads.far_ends.get(member.id)) is not None:
                switch_port_by_name[switch_name] = far_end
                continue
            members_needing_fresh_port[switch_name] = server_interface_name

        # When both members need a fresh port, prefer the same port name on both switches.
        matched_name = None
        if switch_a in members_needing_fresh_port and switch_b in members_needing_fresh_port:
            free_names_by_switch = {name: [p.name.value for p in ports] for name, ports in free_ports_by_switch.items()}
            matched_name = pick_matched_switch_port_name(free_names_by_switch, (switch_a, switch_b))

        for switch_name, server_interface_name in members_needing_fresh_port.items():
            if not free_ports_by_switch[switch_name]:
                member_type = interface_type_of(member_by_name[server_interface_name]) or "matching"
                logger.error(
                    f"Bond {bond_name} on {endpoint_name}: no free {member_type} port on {switch_name} "
                    f"for {server_interface_name}"
                )
                continue
            if matched_name is not None:
                switch_port = next(p for p in free_ports_by_switch[switch_name] if p.name.value == matched_name)
            else:
                switch_port = free_ports_by_switch[switch_name][0]
            free_ports_by_switch[switch_name].remove(switch_port)
            device_groups[switch_name].remove(switch_port)
            switch_port_by_name[switch_name] = switch_port

        if switch_a not in switch_port_by_name or switch_b not in switch_port_by_name:
            continue

        existing_lag_objs = [getattr(switch_port_by_name[n], "lag", None) for n in (switch_a, switch_b)]
        lag_id = next((lag.peer.lag_id.value for lag in existing_lag_objs if lag and lag.id and lag.peer), None)
        if lag_id is None:
            lag_id = next_free_lag_id(existing_lag_ids)
            existing_lag_ids.add(lag_id)

        for switch_name, server_interface_name in zip((switch_a, switch_b), member_names):
            switch_port = switch_port_by_name[switch_name]
            fingerprint = ConnectionFingerprint(
                server_name=endpoint_name,
                server_interface=server_interface_name,
                switch_name=switch_name,
                switch_interface=switch_port.name.value,
            )
            if fingerprint in planned:
                continue
            planned.add(fingerprint)
            connections.append(
                (fingerprint, interface_type_of(member_by_name[server_interface_name]), interface_type_of(switch_port))
            )

        for switch_name in (switch_a, switch_b):
            switch = reads.switches[switch_name]
            switch_port = switch_port_by_name[switch_name]
            platform = switch.platform
            lag_name = get_lag_name(platform.peer.name.value if platform.peer else "", lag_id)
            port_channels.add(
                NodeSpec(
                    f"lag-{switch_name}-{lag_id}",
                    DcimLAGInterface,
                    {
                        "name": lag_name,
                        "description": f"{endpoint_name}:{bond_name}",
                        "device": {"id": switch.id},
                        "status": "active",
                        "role": "lag",
                        "lag_id": lag_id,
                        "lacp_mode": "active",
                        "mlag_domain": {"id": reads.mlag_domain_id},
                        "member_interfaces": [{"id": switch_port.id}],
                    },
                    ownership="owned",
                    hfid=Hfid("DcimLAGInterface", (switch_name, lag_name)),
                )
            )
            logger.info(
                f"{endpoint_name}: bond {bond_name} → {switch_name}:{lag_name} "
                f"(member {switch_port.name.value}, mlag_domain={reads.mlag_domain_id})"
            )

    cabling = TopologyPlan(root_id=None)
    plan_cables(cabling, connections, deployment_id=deployment_id, positions=positions)
    return LagWiring(cabling, port_channels, tuple(fingerprint for fingerprint, _, _ in connections))
