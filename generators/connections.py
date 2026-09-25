"""Cabling mixin for CommonGenerator."""

from __future__ import annotations

import asyncio
import ipaddress
from typing import TYPE_CHECKING, Any, Callable, Literal

from infrahub_sdk.protocols import CoreIPPrefixPool

if TYPE_CHECKING:
    import logging

from .helpers import CableTypeDetector, CablingPlanner
from .protocols import DcimCable, DcimPhysicalInterface, DcimVirtualInterface, IpamIPAddress
from .types import CablingOptions, ChainHop

_INTERFACE_READY_MAX_RETRIES = 10
_INTERFACE_READY_RETRY_DELAY = 3.0
_INTERFACE_READY_RETRY_CAP = 20.0
_INTERFACE_READY_RETRY_JITTER = 0.25

# Default border_role_for for _cable_border_services: every bootstrap border
# tier (N9K-C9336C-FX2_BORDER_LEAF's Ethernet1/[25-28]/[29-32], border-spine's
# equivalent blocks, N9K-C9316D-GX_EDGE's Ethernet1/[15-16]) names its
# firewall-facing and load-balancer-facing port blocks after the service role
# itself, so the border-facing role and the service role are the same string.
# Shared by dc.py (DC-wide border-leaf), pod.py (per-pod border-spine), and
# colocation.py (metro edge) instead of each inlining its own copy — a caller
# whose border tier uses a different port-naming convention should build and
# pass its own dict instead of reusing this one.
BORDER_ROLE_FOR_SERVICES: dict[str, str] = {"firewall": "firewall", "load-balancer": "load-balancer"}


class CablingMixin:
    """Mixin providing device-to-device cabling methods for CommonGenerator.

    Expects the host class to provide: ``client``, ``logger``, ``deployment_id``,
    ``_resolve_pool``, and ``_retry_delay`` (all present on ``CommonGenerator``).
    """

    # Attribute declarations for the type checker — provided by CommonGenerator / InfrahubGenerator
    client: Any
    logger: logging.Logger
    deployment_id: str
    # CommonGenerator._resolve_pool / _retry_delay — annotation only.
    _resolve_pool: Any
    _retry_delay: Callable[..., float]
    # CommonGenerator._safe_rel_add — annotation only, needed by ensure_vlan_subinterface.
    _safe_rel_add: Any

    async def create_cabling(
        self,
        bottom_devices: list[str],
        bottom_interfaces: list[str],
        top_devices: list[str],
        top_interfaces: list[str],
        strategy: Literal[
            "pod",
            "rack",
            "intra_rack",
            "intra_rack_middle",
            "intra_rack_mixed",
        ] = "rack",
        options: CablingOptions | None = None,
        bottom_sorting: Literal["top_down", "bottom_up"] = "bottom_up",
        top_sorting: Literal["top_down", "bottom_up"] = "bottom_up",
    ) -> list[tuple[Any, Any]]:
        """Create cabling connections between device layers.

        Simple approach: query interfaces → build plan → for each connection:
        create cable, fetch interfaces, allocate IPs, save interfaces.
        All saves use allow_upsert=True for idempotency and generator tracking.
        """
        if options is None:
            options = CablingOptions()
        cabling_offset: int = int(options.get("cabling_offset", 0))
        self.logger.info(
            f"Creating cabling: {len(bottom_devices)} bottom → {len(top_devices)} top "
            f"[strategy={strategy}, offset={cabling_offset}, strict_speed_validation=True]"
        )

        # Retry querying interfaces until template instantiation completes.
        # Templates are applied asynchronously; a fixed sleep is fragile under load.
        src_interfaces: list = []
        dst_interfaces: list = []
        for _attempt in range(_INTERFACE_READY_MAX_RETRIES):
            src_interfaces = await self.client.filters(
                kind=DcimPhysicalInterface,
                device__name__values=bottom_devices,
                name__values=bottom_interfaces,
                include=["cable"],
            )
            dst_interfaces = await self.client.filters(
                kind=DcimPhysicalInterface,
                device__name__values=top_devices,
                name__values=top_interfaces,
                include=["cable"],
            )
            if src_interfaces and dst_interfaces:
                break
            delay = self._retry_delay(
                _INTERFACE_READY_RETRY_DELAY,
                _attempt,
                cap=_INTERFACE_READY_RETRY_CAP,
                jitter=_INTERFACE_READY_RETRY_JITTER,
            )
            self.logger.info(
                f"Interfaces not ready yet (src={len(src_interfaces)}, dst={len(dst_interfaces)}) — "
                f"retrying in {delay:.2f}s (attempt {_attempt + 1}/{_INTERFACE_READY_MAX_RETRIES})"
            )
            if _attempt < _INTERFACE_READY_MAX_RETRIES - 1:
                await asyncio.sleep(delay)

        if not src_interfaces or not dst_interfaces:
            self.logger.error(
                f"Interfaces still not found after {_INTERFACE_READY_MAX_RETRIES} attempts "
                f"(src={len(src_interfaces)}, dst={len(dst_interfaces)}) — skipping cabling"
            )
            return []

        # Build lookup map for O(1) access after cabling plan is built
        iface_map: dict[str, Any] = {iface.id: iface for iface in src_interfaces + dst_interfaces}

        # Build cabling plan
        planner = CablingPlanner(
            bottom_interfaces=src_interfaces,
            top_interfaces=dst_interfaces,
            bottom_sorting=bottom_sorting,
            top_sorting=top_sorting,
        )
        strict_plan_profile = {
            # Always enforce speed-aware strict matching for physical cabling plans.
            # Creating mismatched links is not useful operationally.
            "speed_aware": True,
            "validate_speeds": True,
            "strict_speed_validation": True,
        }
        cabling_plan = planner.build_cabling_plan(
            scenario=strategy,
            cabling_offset=cabling_offset,
            **strict_plan_profile,
        )

        if not cabling_plan:
            self.logger.warning("No cabling connections planned")
            return []

        # Resolve technical pool for P2P address allocation
        technical_pool = await self._resolve_pool(
            provided=options.get("pool"),
            kind=CoreIPPrefixPool,
            fallback_name=None,
        )

        return await self._execute_cabling_plan(cabling_plan, iface_map, options, technical_pool)

    async def _execute_cabling_plan(
        self,
        cabling_plan: list[tuple[Any, Any]],
        iface_map: dict[str, Any],
        options: CablingOptions,
        technical_pool: Any,
    ) -> list[tuple[Any, Any]]:
        """Execute an already-built cabling plan: create each cable, allocate
        P2P IPs if a pool is given, save both interfaces. Shared tail for
        create_cabling() and create_chain_cabling()."""
        cabled_pairs: list[tuple[Any, Any]] = []
        for src_interface, dst_interface in cabling_plan:
            endpoint_names = sorted(
                [
                    f"{src_interface.device.display_label}-{src_interface.name.value}",
                    f"{dst_interface.device.display_label}-{dst_interface.name.value}",
                ]
            )
            cable_name = "__".join(endpoint_names)
            link_identifier = "__".join(sorted([src_interface.id, dst_interface.id]))

            cable_type = CableTypeDetector.detect_cable_type(
                src_interface.interface_type.value, dst_interface.interface_type.value
            )

            cable = await self.client.create(
                kind=DcimCable,
                data={
                    "name": cable_name,
                    "type": cable_type,
                    "endpoints": [src_interface.id, dst_interface.id],
                    "deployment": {"id": self.deployment_id} if self.deployment_id else None,
                },
            )
            await cable.save(allow_upsert=True)

            # Use already-fetched interface objects; set cable to prevent upsert sending null
            updated_src = iface_map[src_interface.id]
            updated_dst = iface_map[dst_interface.id]
            updated_src.cable = cable
            updated_dst.cable = cable

            # Allocate P2P addresses if pool provided
            # prefix_length: 127 for IPv6 (RFC 6164, default), 31 for IPv4 (RFC 3021, exception)
            p2p_prefix_length: int = options.get("p2p_prefix_length", 31)
            if technical_pool:
                p2p_prefix = await self.client.allocate_next_ip_prefix(
                    resource_pool=technical_pool,
                    identifier=link_identifier,
                    prefix_length=p2p_prefix_length,
                    member_type="address",
                    data={"role": "technical", "is_pool": True},
                )
                self.logger.info(f"- Allocated prefix {p2p_prefix.display_label} for {cable_name}")

                # Iterate the network directly — works for both /31 (RFC 3021) and
                # /127 (RFC 6164) where .hosts() returns only one address in Python.
                network = ipaddress.ip_network(p2p_prefix.prefix.value, strict=False)
                addrs = list(network)
                ip_namespace = p2p_prefix.ip_namespace

                for iface, addr in [(updated_src, addrs[0]), (updated_dst, addrs[1])]:
                    ip = await self.client.create(
                        kind=IpamIPAddress,
                        data={"address": f"{addr}/{p2p_prefix_length}", "ip_namespace": ip_namespace},
                    )
                    await ip.save(allow_upsert=True)
                    iface.ip_address = ip.id

            # update_group_context=False: physical interfaces come from the device's
            # object_template, not from this generator run — they must never be
            # candidates for the tracking group's delete_unused_nodes cleanup (e.g.
            # a port dropped from this run's cabling plan because amount_of_spines
            # shrank would otherwise be deleted as "unused" even though it's a real,
            # still-existing hardware interface).
            updated_src.description.value = cable_name
            updated_src.status.value = "active"
            await updated_src.save(allow_upsert=True, update_group_context=False)

            updated_dst.description.value = cable_name
            updated_dst.status.value = "active"
            await updated_dst.save(allow_upsert=True, update_group_context=False)

            cabled_pairs.append((updated_src, updated_dst))
            self.logger.info(f"  - Created connection {cable_name}")

        return cabled_pairs

    async def create_chain_cabling(
        self, hops: list[ChainHop], options: CablingOptions | None = None
    ) -> list[list[tuple[Any, Any]]]:
        """Cable an ordered chain of device groups end to end — e.g.
        border-leaf<->firewall<->load-balancer<->border-leaf — one leg per
        consecutive pair of hops, index-paired ("chain" strategy): device i
        on one side cables ONLY to device i on the other, forming N
        independent redundant paths rather than an any-to-any mesh. Fewer
        devices on one side are reused round-robin.

        Each hop's ``down_role`` interfaces cable to the next hop's
        ``up_role`` interfaces. An empty ``devices`` list, or zero matching
        ports on either side, skips that leg (logged as an error if ports
        are missing on a non-empty device list).

        Returns one list of cabled (src, dst) pairs per leg, in chain order.
        A skipped leg contributes an empty list.
        """
        if options is None:
            options = CablingOptions()
        cabling_offset: int = int(options.get("cabling_offset", 0))
        technical_pool = await self._resolve_pool(
            provided=options.get("pool"),
            kind=CoreIPPrefixPool,
            fallback_name=None,
        )

        all_leg_pairs: list[list[tuple[Any, Any]]] = []
        for top_hop, bottom_hop in zip(hops, hops[1:]):
            top_devices = top_hop.get("devices") or []
            bottom_devices = bottom_hop.get("devices") or []
            if not top_devices or not bottom_devices:
                all_leg_pairs.append([])
                continue

            top_role = top_hop.get("down_role", "")
            bottom_role = bottom_hop.get("up_role", "")
            top_interfaces = await self.client.filters(
                kind=DcimPhysicalInterface, device__name__values=top_devices, role__value=top_role, include=["cable"]
            )
            bottom_interfaces = await self.client.filters(
                kind=DcimPhysicalInterface,
                device__name__values=bottom_devices,
                role__value=bottom_role,
                include=["cable"],
            )
            if not top_interfaces or not bottom_interfaces:
                self.logger.error(
                    f"create_chain_cabling: cannot cable {sorted(top_devices)}<->{sorted(bottom_devices)} — "
                    f"{top_role}_ports={len(top_interfaces)}, {bottom_role}_ports={len(bottom_interfaces)}."
                )
                all_leg_pairs.append([])
                continue

            iface_map: dict[str, Any] = {iface.id: iface for iface in list(bottom_interfaces) + list(top_interfaces)}
            planner = CablingPlanner(bottom_interfaces=bottom_interfaces, top_interfaces=top_interfaces)
            leg_plan = planner.build_cabling_plan(
                scenario="chain",
                cabling_offset=cabling_offset,
                speed_aware=True,
                validate_speeds=True,
                strict_speed_validation=True,
            )
            if not leg_plan:
                self.logger.error(
                    f"create_chain_cabling: {sorted(top_devices)}<->{sorted(bottom_devices)} cabling produced "
                    f"no connections — likely an interface speed mismatch between {top_role}-role and "
                    f"{bottom_role}-role ports. Check the speed-mismatch log output above for the exact "
                    "speed groups involved."
                )
                all_leg_pairs.append([])
                continue

            all_leg_pairs.append(await self._execute_cabling_plan(leg_plan, iface_map, options, technical_pool))

        return all_leg_pairs

    async def find_role_interface(
        self, *, device_id: str, role: str, fallback_any_physical: bool = False
    ) -> Any | None:
        """Return the first physical interface matching role on device_id, or
        (if fallback_any_physical) the alphabetically-first physical
        interface when no role match exists. Returns None on error or no
        match — shared trunk/uplink resolution for
        ensure_vlan_subinterface() callers.
        """
        ifaces = await self.client.filters(kind=DcimPhysicalInterface, device__ids=[device_id], role__value=role)
        if ifaces:
            return ifaces[0]
        if not fallback_any_physical:
            return None
        all_ifaces = await self.client.filters(kind=DcimPhysicalInterface, device__ids=[device_id])
        if not all_ifaces:
            return None
        return sorted(all_ifaces, key=lambda i: i.name.value)[0]

    async def ensure_vlan_subinterface(
        self,
        *,
        device_id: str,
        device_name: str,
        trunk_iface: Any,
        vlan_id_value: int,
        capability_obj: Any,
        ip_address_id: str | None = None,
    ) -> Any | None:
        """Upsert a VLAN-tagged DcimVirtualInterface (<trunk>.<vlan_id>) on
        trunk_iface, linked to capability_obj via interface_capabilities —
        shared by customer_dc.py/customer_colocation.py's FirewallContext
        sub-interfaces and segment.py's inline VxlanSegment termination.
        Trunk-interface resolution stays with the caller since fallback
        behavior differs (see find_role_interface).
        """
        sub_iface_name = f"{trunk_iface.name.value}.{vlan_id_value}"
        sub_iface_data: dict[str, Any] = {
            "name": sub_iface_name,
            "device": {"id": device_id},
            "parent_interface": {"id": trunk_iface.id},
            "status": "active",
            "role": "service",
            **({"ip_address": {"id": ip_address_id}} if ip_address_id else {}),
        }

        try:
            sub_iface = await self.client.create(kind=DcimVirtualInterface, data=sub_iface_data)
            await sub_iface.save(allow_upsert=True)
            iface_capabilities = getattr(sub_iface, "interface_capabilities")
            await iface_capabilities.fetch()
            if not any(peer.id == capability_obj.id for peer in iface_capabilities.peers):
                await self._safe_rel_add(iface_capabilities, capability_obj)
                await sub_iface.save(allow_upsert=True)
            self.logger.info(f"Upserted sub-interface {sub_iface_name} on {device_name}")
            return sub_iface
        except Exception as exc:
            self.logger.error(f"Failed to create sub-interface {sub_iface_name} on {device_name}: {exc}")
            return None

    async def _cable_border_services(
        self,
        *,
        border_role_for: dict[str, str],
        connectivity_mode: Literal["pbr", "inline"],
        border_names: list[str],
        firewall_names: list[str],
        load_balancer_names: list[str],
    ) -> None:
        """Cable border-leaf/border-spine<->firewall<->load-balancer per
        connectivity_mode. Index-paired (border[0]<->fw[0], border[1]<->fw[1],
        ...), never any-to-any — each border/firewall/load-balancer triple is
        one independent redundant path. Fewer devices on one side are reused
        round-robin.
        - pbr: two independent legs, each on the service device's "uplink" ports.
        - inline: one chain — border<->firewall<->load-balancer<->border.
          Every device has an "uplink" (toward the previous hop) and "downlink"
          (toward the next), distinct from load-balancer's own "customer"-role
          VIP ports (untouched here).
        No-ops for any leg with nothing to cable (create_chain_cabling's own
        empty-devices handling).
        """
        border_to_firewall = ChainHop(devices=border_names, down_role=border_role_for["firewall"])
        firewall_hop = ChainHop(devices=firewall_names, up_role="uplink")
        await self.create_chain_cabling([border_to_firewall, firewall_hop])

        if connectivity_mode == "inline":
            middle_firewall_hop = ChainHop(devices=firewall_names, down_role="downlink")
            middle_lb_hop = ChainHop(devices=load_balancer_names, up_role="uplink")
            await self.create_chain_cabling([middle_firewall_hop, middle_lb_hop])

            border_to_lb = ChainHop(devices=border_names, down_role=border_role_for["load-balancer"])
            return_lb_hop = ChainHop(devices=load_balancer_names, up_role="downlink")
            await self.create_chain_cabling([border_to_lb, return_lb_hop])
        else:
            border_to_lb = ChainHop(devices=border_names, down_role=border_role_for["load-balancer"])
            lb_hop = ChainHop(devices=load_balancer_names, up_role="uplink")
            await self.create_chain_cabling([border_to_lb, lb_hop])
