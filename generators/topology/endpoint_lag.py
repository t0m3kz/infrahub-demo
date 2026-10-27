"""LAG flow of the endpoint generator: a server's bonds wired into a switch pair's ManagedMLAG domain.

Split out of generators/topology/endpoint.py for size/coherence. Shares the target resolution
(EndpointTargetsMixin) and the device grouping and switch-pair selection (EndpointUplinkMixin) with
the plain uplink flow.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from netutils.interface import sort_interface_list

from ..far_end import far_end_interfaces
from ..helpers.cabling import pick_matched_switch_port_name
from ..helpers.interface_naming import get_lag_name
from ..protocols import DcimLAGInterface, DcimPhysicalDevice, DcimPhysicalInterface
from ..types import CablingOptions, ConnectionFingerprint
from .endpoint_targets import interface_type_of

if TYPE_CHECKING:
    from ..connections import CablingMixin
    from ..endpoint import EndpointUplinkMixin
    from .endpoint_targets import EndpointTargetsMixin


class EndpointLagMixin:
    """Mixin wiring a LAG-based endpoint's bond(s) to a switch pair's ManagedMLAG domain.

    Expects the host class to provide: ``client``, ``logger``, ``data``, ``planned_connections``,
    ``create_cabling``, ``_resolve_target_interfaces`` (EndpointTargetsMixin), ``_group_by_device``
    and ``_select_switch_pair`` (EndpointUplinkMixin). The cross-mixin methods are aliases of the real
    ones, for the type checker only: nothing is defined at runtime, so nothing shadows them in the MRO.
    """

    client: Any
    logger: logging.Logger
    data: Any
    planned_connections: set[ConnectionFingerprint]
    if TYPE_CHECKING:
        _group_by_device = EndpointUplinkMixin._group_by_device
        _resolve_target_interfaces = EndpointTargetsMixin._resolve_target_interfaces
        _select_switch_pair = EndpointUplinkMixin._select_switch_pair
        create_cabling = CablingMixin.create_cabling

    async def _process_lag_endpoint_connections(
        self, members_by_bond: dict[str, list[DcimPhysicalInterface]], deployment_type: str
    ) -> None:
        """Wire a LAG-based endpoint's bond(s) to a switch pair's ManagedMLAG domain.

        Each server bond fans out to one port-channel on EACH target switch —
        every switch owns its own port-channel (member = the physical port this
        bond's cable lands on), and both port-channels reference the same
        mlag_domain so they act as one logical vPC/MLAG-attached-host link.
        Mirrors mlag.py's peer-link pattern one level down (server<->switch
        instead of switch<->switch).

        Requires the target pair to already share exactly one ManagedMLAG
        domain — per user direction, a pair with no shared domain is skipped
        with a warning rather than auto-provisioned here (unlike
        DeviceMixin._ensure_mlag_pairs, which creates the domain for switch
        pairs it itself creates).
        """
        all_target_interfaces = await self._resolve_target_interfaces(deployment_type)
        if not all_target_interfaces:
            return

        device_groups = self._group_by_device(all_target_interfaces)
        if len(device_groups) < 2:
            self.logger.error(
                f"Endpoint {self.data['name']}: Need 2 switches for MLAG-attached bonds, found {len(device_groups)}."
            )
            return

        # A later bond lands on the same pair as the earlier ones.
        switch_a_name, switch_b_name = self._select_switch_pair(list(device_groups))

        switches = await self.client.filters(
            kind=DcimPhysicalDevice, name__values=[switch_a_name, switch_b_name], include=["capabilities", "platform"]
        )
        switch_by_name = {s.name.value: s for s in switches}
        mlag_ids_by_switch: dict[str, set[str]] = {}
        for name, switch in switch_by_name.items():
            caps = switch.capabilities
            mlag_ids_by_switch[name] = {peer.id for peer in caps.peers if peer.typename == "ManagedMLAG"}

        shared_domains = mlag_ids_by_switch.get(switch_a_name, set()) & mlag_ids_by_switch.get(switch_b_name, set())
        if len(shared_domains) != 1:
            self.logger.error(
                f"Endpoint {self.data['name']}: {switch_a_name}/{switch_b_name} share "
                f"{len(shared_domains)} MLAG domain(s) (need exactly 1) — cannot wire LAG bond(s). "
                "Pair the switches into a ManagedMLAG domain first."
            )
            return
        mlag_domain_id = next(iter(shared_domains))

        lags = await self.client.filters(
            kind=DcimLAGInterface, device__ids=[switch_by_name[name].id for name in (switch_a_name, switch_b_name)]
        )
        existing_lag_ids: set[int] = {lag.lag_id.value for lag in lags}
        # The switch port at the far end of every already-cabled member, in one batch for all the bonds.
        far_ends = await far_end_interfaces(
            self.client,
            [m for members in members_by_bond.values() if len(members) >= 2 for m in members if m.cable and m.cable.id],
            include=["lag"],
        )

        for bond_name, members in members_by_bond.items():
            if len(members) < 2:
                self.logger.error(f"Bond {bond_name} on {self.data['name']} has < 2 member interfaces — cannot wire it")
                continue

            member_names = sort_interface_list([m.name.value for m in members])
            member_by_name = {m.name.value: m for m in members}

            # Resolve each switch's port for this bond: reuse the existing one if
            # a prior run already cabled this member (read off the far end of its
            # cable), else claim a free port. Reusing — rather than
            # skipping outright — keeps the cable/port-channel/switch port
            # re-touched (create_cabling + the LAG save below are both
            # allow_upsert=True) so this run's tracking group re-includes them;
            # otherwise delete_unused_nodes would delete still-valid prior-run
            # objects that nothing in THIS run touched again.
            free_ports_by_switch: dict[str, list[DcimPhysicalInterface]] = {}
            for name, server_interface_name in zip((switch_a_name, switch_b_name), member_names):
                # A member only takes a switch port of its own type, since
                # create_cabling refuses a speed mismatch.
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
            for switch_name, server_interface_name in zip((switch_a_name, switch_b_name), member_names):
                member = member_by_name[server_interface_name]
                if (far_end := far_ends.get(member.id)) is not None:
                    switch_port_by_name[switch_name] = far_end
                    continue
                members_needing_fresh_port[switch_name] = server_interface_name

            # When BOTH members need a fresh port, prefer the SAME port name on
            # both switches (e.g. Ethernet1/1/8 on both) over independently
            # picking each switch's own first-free port — falls back to
            # independent picks when no common free name exists.
            matched_name = None
            if switch_a_name in members_needing_fresh_port and switch_b_name in members_needing_fresh_port:
                free_names_by_switch = {
                    name: [p.name.value for p in ports] for name, ports in free_ports_by_switch.items()
                }
                matched_name = pick_matched_switch_port_name(free_names_by_switch, (switch_a_name, switch_b_name))

            for switch_name, server_interface_name in members_needing_fresh_port.items():
                if not free_ports_by_switch[switch_name]:
                    member_type = interface_type_of(member_by_name[server_interface_name]) or "matching"
                    self.logger.error(
                        f"Bond {bond_name} on {self.data['name']}: no free {member_type} port on {switch_name} "
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

            if switch_a_name not in switch_port_by_name or switch_b_name not in switch_port_by_name:
                continue

            existing_lag_objs = [getattr(switch_port_by_name[n], "lag", None) for n in (switch_a_name, switch_b_name)]
            lag_id = next(
                (lag.peer.lag_id.value for lag in existing_lag_objs if lag and lag.id and lag.peer),
                None,
            )
            if lag_id is None:
                lag_id = self._next_free_lag_id(existing_lag_ids)
                existing_lag_ids.add(lag_id)

            # Cable both members before either port-channel exists: a
            # port-channel whose member port has no cable is config for a link
            # that is not there. create_cabling only warns when it plans
            # nothing (e.g. a speed mismatch), so an empty result is checked here.
            all_cabled = True
            for switch_name, server_interface_name in zip((switch_a_name, switch_b_name), member_names):
                switch_port = switch_port_by_name[switch_name]
                fingerprint = ConnectionFingerprint(
                    server_name=self.data["name"],
                    server_interface=server_interface_name,
                    switch_name=switch_name,
                    switch_interface=switch_port.name.value,
                )
                if fingerprint in self.planned_connections:
                    continue
                self.planned_connections.add(fingerprint)
                cabled = await self.create_cabling(
                    bottom_devices=[self.data["name"]],
                    bottom_interfaces=[server_interface_name],
                    top_devices=[switch_name],
                    top_interfaces=[switch_port.name.value],
                    strategy="intra_rack",
                    options=CablingOptions(cabling_offset=0, pool=None),
                )
                if not cabled:
                    all_cabled = False
                    self.logger.error(
                        f"Bond {bond_name} on {self.data['name']}: no cable from {server_interface_name} to "
                        f"{switch_name}:{switch_port.name.value} — its port-channels are not created"
                    )
            if not all_cabled:
                continue

            for switch_name in (switch_a_name, switch_b_name):
                switch = switch_by_name[switch_name]
                switch_port = switch_port_by_name[switch_name]
                platform = switch.platform
                platform_name = platform.peer.name.value if platform.peer else ""
                lag_obj = await self.client.create(
                    kind=DcimLAGInterface,
                    data={
                        "name": get_lag_name(platform_name, lag_id),
                        "description": f"{self.data['name']}:{bond_name}",
                        "device": {"id": switch.id},
                        "status": "active",
                        "role": "lag",
                        "lag_id": lag_id,
                        "lacp_mode": "active",
                        "mlag_domain": {"id": mlag_domain_id},
                        "member_interfaces": [{"id": switch_port.id}],
                    },
                )
                await lag_obj.save(allow_upsert=True)
                self.logger.info(
                    f"{self.data['name']}: bond {bond_name} → {switch_name}:{get_lag_name(platform_name, lag_id)} "
                    f"(member {switch_port.name.value}, mlag_domain={mlag_domain_id})"
                )

    @staticmethod
    def _next_free_lag_id(existing_ids: set[int]) -> int:
        """Smallest positive lag_id not already in use (100 reserved for MLAG peer-link, see mlag.py)."""
        candidate = 1
        taken = existing_ids | {100}
        while candidate in taken:
            candidate += 1
        return candidate
