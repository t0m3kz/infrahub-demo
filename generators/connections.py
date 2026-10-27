"""Cabling mixin for CommonGenerator."""

from __future__ import annotations

import asyncio
import ipaddress
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from infrahub_sdk.protocols import CoreIPAddressPool, CoreIPPrefixPool

if TYPE_CHECKING:
    import logging
    from .common import CommonGenerator

from utils.limits import WAIT_FOR_PEER_GENERATOR

from .helpers import CableTypeDetector, CablingPlanner
from .helpers.cable_attributes import RackPosition, cable_attributes, endpoint_label
from .helpers.common import save_with_node_not_found_retry
from .protocols import (
    DcimCable,
    DcimPhysicalDevice,
    DcimPhysicalInterface,
    DcimVirtualInterface,
    IpamIPAddress,
    LocationRack,
    LocationSuite,
)
from .types import CablingOptions, ChainHop

# Default border_role_for for _cable_border_services: every bootstrap border
# tier (N9K-C9336C-FX2_BORDER_LEAF's Ethernet1/[25-28]/[29-32], border-spine's
# equivalent blocks, N9K-C9316D-GX_EDGE's Ethernet1/[15-16]) names its
# firewall-facing and load-balancer-facing port blocks after the service role
# itself, so the border-facing role and the service role are the same string.
# Shared by dc.py (DC-wide border-leaf), pod.py (per-pod border-spine), and
# colocation_fabric.py (zone edge) instead of each inlining its own copy — a caller
# whose border tier uses a different port-naming convention should build and
# pass its own dict instead of reusing this one.
BORDER_ROLE_FOR_SERVICES: dict[str, str] = {"firewall": "firewall", "load-balancer": "load-balancer"}


def tracked_save_kwargs(track: bool) -> dict[str, Any]:
    """save() kwargs for a write the run does (track) or does not (not track) own.

    track=False adds update_group_context=False so the node is written but not
    added to the run's tracking group, so delete_unused_nodes can never reclaim
    it. track=True keeps the plain upsert (the run claims the node).
    """
    return {"allow_upsert": True} if track else {"allow_upsert": True, "update_group_context": False}


def _peer_id(rel: Any) -> str | None:
    """Id of a loaded relationship's peer, None when it is missing or unset."""
    return rel.id if rel is not None and rel.initialized else None


def _address_key(address: str) -> str:
    """Canonical text of an interface address, for matching what the server returns to what was asked."""
    return str(ipaddress.ip_interface(address))


@dataclass
class _LayoutCache:
    """What one generator run has already read for cable lengths.

    Racks and suites are data, never generated, and no generator moves a device between racks, so a
    device's position, a rack and a suite are read once per run, however many cabling plans, HA pairs
    and MLAG pairs ask for them.
    """

    positions: dict[str, RackPosition | None] = field(default_factory=dict)
    racks: dict[str, Any] = field(default_factory=dict)
    suites: dict[str, Any] = field(default_factory=dict)


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
    if TYPE_CHECKING:
        _retry_delay = staticmethod(CommonGenerator._retry_delay)
    # CommonGenerator._safe_rel_add — annotation only, needed by ensure_vlan_subinterface.
    _safe_rel_add: Any
    # Set on first use by _cable_layouts, one per generator instance (= per run).
    _layout_cache: _LayoutCache | None

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
            f"[strategy={strategy}, offset={cabling_offset}, speed-matched]"
        )

        # Retry querying interfaces until template instantiation completes.
        # Templates are applied asynchronously; a fixed sleep is fragile under load.
        src_interfaces: list = []
        dst_interfaces: list = []
        for _attempt in range(WAIT_FOR_PEER_GENERATOR.attempts):
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
            delay = self._retry_delay(WAIT_FOR_PEER_GENERATOR.delay, _attempt)
            self.logger.info(
                f"Interfaces not ready yet (src={len(src_interfaces)}, dst={len(dst_interfaces)}) — "
                f"retrying in {delay:.2f}s (attempt {_attempt + 1}/{WAIT_FOR_PEER_GENERATOR.attempts})"
            )
            if _attempt < WAIT_FOR_PEER_GENERATOR.attempts - 1:
                await asyncio.sleep(delay)

        if not src_interfaces or not dst_interfaces:
            self.logger.error(
                f"Interfaces still not found after {WAIT_FOR_PEER_GENERATOR.attempts} attempts "
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
        cabling_plan = planner.build_cabling_plan(scenario=strategy, cabling_offset=cabling_offset)

        if not cabling_plan:
            # Both sides have interfaces (checked above), so an empty plan means
            # devices that were asked to be cabled won't be — that's a failure.
            self.logger.error(
                f"No cabling connections planned for {sorted(bottom_devices)} -> {sorted(top_devices)} "
                f"(strategy={strategy}, offset={cabling_offset})"
            )
            return []

        # Resolve technical pool for P2P address allocation
        technical_pool = await self._resolve_pool(
            provided=options.get("pool"),
            kind=CoreIPPrefixPool,
            fallback_name=None,
        )

        return await self._execute_cabling_plan(cabling_plan, iface_map, options, technical_pool)

    async def _cable_layouts(self, device_ids: Iterable[str]) -> dict[str, RackPosition | None]:
        """Rack position of each device, for the length of the cables between them.

        THE lookup behind every cable's ``length_cm`` (this mixin's cabling, the HA sync cable, the MLAG
        peer-link cables). A device missing from the result, or mapped to None, has no derivable position:
        a virtual device, a device with no rack, a rack with no suite.

        Memoised per run (_LayoutCache): only the devices, racks and suites not read yet are read, in at most
        three batched reads (devices -> racks -> suites), none when everything asked for is known or no
        device has a rack. A device the read does not return, or whose rack or suite is not visible yet, is
        not memoised, so the next call looks again.
        """
        ids = sorted(set(device_ids))
        cache: _LayoutCache | None = getattr(self, "_layout_cache", None)
        if cache is None:
            cache = self._layout_cache = _LayoutCache()
        missing = [device_id for device_id in ids if device_id not in cache.positions]
        if missing:
            await self._read_layouts(missing, cache)
        return {device_id: cache.positions.get(device_id) for device_id in ids}

    async def _read_layouts(self, device_ids: list[str], cache: _LayoutCache) -> None:
        """Read the devices, then the racks and suites they need that the cache lacks, and memoise the positions."""
        devices = await self.client.filters(kind=DcimPhysicalDevice, ids=device_ids, include=["rack"])
        rack_id_by_device = {device.id: _peer_id(getattr(device, "rack", None)) for device in devices}
        rack_ids = sorted({rack_id for rack_id in rack_id_by_device.values() if rack_id and rack_id not in cache.racks})
        if rack_ids:
            cache.racks.update(
                {
                    rack.id: rack
                    for rack in await self.client.filters(kind=LocationRack, ids=rack_ids, include=["parent"])
                }
            )
        suite_ids = sorted(
            {
                suite_id
                for rack_id in rack_id_by_device.values()
                if rack_id in cache.racks
                and (suite_id := _peer_id(getattr(cache.racks[rack_id], "parent", None)))
                and suite_id not in cache.suites
            }
        )
        if suite_ids:
            cache.suites.update(
                {suite.id: suite for suite in await self.client.filters(kind=LocationSuite, ids=suite_ids)}
            )

        for device_id, rack_id in rack_id_by_device.items():
            if rack_id is None:
                cache.positions[device_id] = None
                continue
            rack = cache.racks.get(rack_id)
            suite = cache.suites.get(_peer_id(getattr(rack, "parent", None)) or "") if rack is not None else None
            if rack is None or suite is None:
                continue
            cache.positions[device_id] = RackPosition(
                suite_id=suite.id,
                row_index=rack.row_index.value,
                index=rack.index.value,
                rows=suite.rows.value,
                racks_per_row=suite.racks_per_row.value,
                rack_pitch_cm=suite.rack_pitch_cm.value,
                row_pitch_cm=suite.row_pitch_cm.value,
            )

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
        layouts = await self._cable_layouts(interface.device.id for pair in cabling_plan for interface in pair)
        for src_interface, dst_interface in cabling_plan:
            endpoint_names = sorted(
                [
                    endpoint_label(src_interface.device.display_label, src_interface.name.value),
                    endpoint_label(dst_interface.device.display_label, dst_interface.name.value),
                ]
            )
            cable_name = "__".join(endpoint_names)
            link_identifier = "__".join(sorted([src_interface.id, dst_interface.id]))

            cable_type = CableTypeDetector.detect_cable_type(
                src_interface.interface_type.value, dst_interface.interface_type.value
            )

            # Use the already-fetched interface objects. Their `cable` relationship
            # was loaded with include=["cable"], so re-saving them below preserves
            # whatever cable they already point at instead of sending null.
            updated_src = iface_map[src_interface.id]
            updated_dst = iface_map[dst_interface.id]

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

                src_ip, dst_ip = await self.upsert_p2p_addresses(p2p_prefix)
                updated_src.ip_address = src_ip.id
                updated_dst.ip_address = dst_ip.id

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

            # Create the cable LAST, and let it establish the link from its own
            # side via `endpoints` — the reverse of the interface's `cable`.
            #
            # Never the other way round: writing the freshly created cable's id
            # back onto an interface reads a node the server may not have made
            # visible yet, which fails as
            #   "Unable to find the node <uuid> / DcimCable in the database"
            #   (NODE_NOT_FOUND, 404)
            # intermittently under concurrent generator runs. The cable's
            # endpoints reference interfaces that came from the device's
            # object_template, so they are always already visible.
            cable = await self.client.create(
                kind=DcimCable,
                data={
                    "name": cable_name,
                    "type": cable_type,
                    "endpoints": [updated_src.id, updated_dst.id],
                    "deployment": {"id": self.deployment_id} if self.deployment_id else None,
                    **cable_attributes(
                        cable_type,
                        endpoint_names,
                        layouts.get(src_interface.device.id),
                        layouts.get(dst_interface.device.id),
                    ),
                },
            )
            await cable.save(allow_upsert=True)

            # In-memory only, after the last save of either interface: create_routing()
            # builds the underlay peerings from the cables of the interface objects
            # returned here (cable_map in generators/helpers/routing.py), so they have
            # to carry it. Nothing saves these interfaces again, so the cable id this
            # assignment holds is never sent back to a server that cannot yet see it.
            updated_src.cable = cable
            updated_dst.cable = cable

            cabled_pairs.append((updated_src, updated_dst))
            self.logger.info(f"  - Created connection {cable_name}")

        return cabled_pairs

    async def upsert_p2p_addresses(
        self,
        prefix: Any,
        *,
        address_length: int | None = None,
        description: str | None = None,
        track: bool = True,
    ) -> list[Any]:
        """Upsert both addresses of a /31 (RFC 3021) or /127 (RFC 6164) P2P prefix.

        The two-address case of upsert_prefix_addresses(): offsets 0 and 1,
        returned in that order.
        """
        by_offset = await self.upsert_prefix_addresses(
            prefix, offsets=(0, 1), address_length=address_length, description=description, track=track
        )
        return list(by_offset.values())

    async def upsert_prefix_addresses(
        self,
        prefix: Any,
        *,
        offsets: tuple[int, ...],
        address_length: int | None = None,
        description: str | None = None,
        track: bool = True,
    ) -> dict[int, Any]:
        """Upsert the addresses at the given host offsets of an allocated prefix; returns offset -> address node.

        Queried before created: allocate_next_ip_prefix() is idempotent per
        identifier, but IpamIPAddress's (address, ip_namespace) uniqueness is
        only enforced by an async validator, so two overlapping runs for the
        same link would otherwise both blind-create the same address (seen on
        DC4's hyper-spine mesh as a "Process schema integrity" merge failure).
        Saved even when found: the run tracks only what it saves, so a reused
        but unsaved address is one delete_unused_nodes removes.

        address_length defaults to the prefix's own length. description, when
        given, is (re)written on every run.

        track=False saves the addresses with update_group_context=False, for
        a caller addressing a link it does not own (e.g. a shared
        FirewallContext reached by every customer on the cluster): the
        addresses are written but never claimed by this run's tracking group,
        so no run's cleanup can delete them.
        """
        network = ipaddress.ip_network(prefix.prefix.value, strict=False)
        length = network.prefixlen if address_length is None else address_length
        # network[offset], not .hosts(): .hosts() yields one address for /31 and /127.
        addresses = {offset: f"{network[offset]}/{length}" for offset in offsets}
        found = await self._find_addresses(list(addresses.values()), prefix.ip_namespace.id)
        return {
            offset: await self._save_address(
                address=address,
                ip_namespace=prefix.ip_namespace,
                existing=found.get(_address_key(address)),
                description=description,
                track=track,
            )
            for offset, address in addresses.items()
        }

    async def upsert_ip_address(
        self, *, address: str, ip_namespace: Any, description: str | None = None, track: bool = True
    ) -> Any:
        """Upsert one IpamIPAddress in ``ip_namespace`` (a peer node, or ``{"id": ...}``); returns the node.

        Queried before created, saved even when found, description and track
        as in upsert_prefix_addresses().
        """
        namespace_id = ip_namespace["id"] if isinstance(ip_namespace, dict) else ip_namespace.id
        found = await self._find_addresses([address], namespace_id)
        return await self._save_address(
            address=address,
            ip_namespace=ip_namespace,
            existing=found.get(_address_key(address)),
            description=description,
            track=track,
        )

    async def _find_addresses(self, addresses: Sequence[str], namespace_id: str) -> dict[str, Any]:
        """The IpamIPAddress nodes that already exist among ``addresses`` in the namespace, by canonical address.

        THE find-before-create read (the race guard of upsert_prefix_addresses()): one filtered read for all the
        addresses, however many.
        """
        nodes = await self.client.filters(
            kind=IpamIPAddress, address__values=list(addresses), ip_namespace__ids=[namespace_id]
        )
        return {_address_key(node.address.value): node for node in nodes}

    async def _save_address(
        self, *, address: str, ip_namespace: Any, existing: Any | None, description: str | None, track: bool
    ) -> Any:
        """Save one address, creating it when ``existing`` (what _find_addresses found) is None.

        Saved even when found: the run tracks only what it saves. An existing address is re-created from
        its id only to (re)write a description.
        """
        ip = existing
        if not ip or description is not None:
            ip = await self.client.create(
                kind=IpamIPAddress,
                data={
                    **({"id": ip.id} if ip else {}),
                    "address": address,
                    "ip_namespace": ip_namespace,
                    **({"description": description} if description is not None else {}),
                },
            )
        await ip.save(**tracked_save_kwargs(track))
        return ip

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
            top_interfaces, bottom_interfaces = await asyncio.gather(
                self.client.filters(
                    kind=DcimPhysicalInterface,
                    device__name__values=top_devices,
                    role__value=top_role,
                    include=["cable"],
                ),
                self.client.filters(
                    kind=DcimPhysicalInterface,
                    device__name__values=bottom_devices,
                    role__value=bottom_role,
                    include=["cable"],
                ),
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
            leg_plan = planner.build_cabling_plan(scenario="chain", cabling_offset=cabling_offset)
            if not leg_plan:
                # A speed mismatch or a port shortfall already raised CablingPlanError
                # inside build_cabling_plan; this guards any other empty plan.
                self.logger.error(
                    f"create_chain_cabling: {sorted(top_devices)}<->{sorted(bottom_devices)} cabling produced "
                    f"no connections between {top_role}-role and {bottom_role}-role ports."
                )
                all_leg_pairs.append([])
                continue

            all_leg_pairs.append(await self._execute_cabling_plan(leg_plan, iface_map, options, technical_pool))

        return all_leg_pairs

    async def ensure_vlan_subinterface(
        self,
        *,
        device_id: str,
        device_name: str,
        trunk_iface: Any,
        vlan_id_value: int,
        capability_obj: Any | None = None,
        extra_capability_objs: Sequence[Any] = (),
        capability_ids: Sequence[str] = (),
        ip_address_id: str | None = None,
        track: bool = True,
        role: str = "service",
    ) -> Any | None:
        """Upsert a VLAN-tagged DcimVirtualInterface (<trunk>.<vlan_id>) on
        trunk_iface, linked to capability_obj (and any extra_capability_objs,
        e.g. the exchanges a firewall-context leg belongs to, and any
        capability_ids a caller holds without the node) via capabilities.
        Capabilities are only ever added, never removed — shared by
        customer_dc.py/customer_colocation.py's FirewallContext sub-interfaces
        and segment.py's inline VxlanSegment termination. Trunk-interface
        resolution stays with the caller since the fallback behavior differs.

        No capability: a sub-interface the caller links itself (an office's WAN
        exit, which the BGP peering names as its interface), with its own ``role``.

        track=False makes every save here (the sub-interface and its
        capability-link re-save) use update_group_context=False, for a
        sub-interface reached by more than one target (e.g. a shared
        FirewallContext's per-firewall leg): written, never claimed by this
        run's group. This helper saves no
        IP itself — ip_address_id must already exist; untracked addressing is
        upsert_p2p_addresses(track=False).
        """
        sub_iface_name = f"{trunk_iface.name.value}.{vlan_id_value}"
        sub_iface_data: dict[str, Any] = {
            "name": sub_iface_name,
            "device": {"id": device_id},
            "parent_interface": {"id": trunk_iface.id},
            "status": "active",
            "role": role,
            **({"ip_address": {"id": ip_address_id}} if ip_address_id else {}),
        }

        try:
            sub_iface = await self.client.create(kind=DcimVirtualInterface, data=sub_iface_data)
            await sub_iface.save(**tracked_save_kwargs(track))
            wanted = [cap.id for cap in (capability_obj, *extra_capability_objs) if cap is not None]
            wanted.extend(capability_ids)
            if wanted:
                # A node built from create(data) does not know the capabilities the server already holds, and
                # a RelationshipManager must be loaded before it is added to: one get with the relationship
                # included (only the peer ids are read), not fetch(), which costs a get plus a read per peer kind.
                stored = await self.client.get(kind=DcimVirtualInterface, id=sub_iface.id, include=["capabilities"])
                capabilities = getattr(stored, "capabilities")
                linked = {peer.id for peer in capabilities.peers}
                missing = [cap_id for cap_id in dict.fromkeys(wanted) if cap_id not in linked]
                if missing:
                    for cap_id in missing:
                        await self._safe_rel_add(capabilities, {"id": cap_id})
                    # The capability may be an exchange another run created a moment ago.
                    await save_with_node_not_found_retry(stored, self.logger, **tracked_save_kwargs(track))
            self.logger.info(f"Upserted sub-interface {sub_iface_name} on {device_name}")
            return sub_iface
        except Exception as exc:
            self.logger.error(f"Failed to create sub-interface {sub_iface_name} on {device_name}: {exc}")
            return None

    async def ensure_prefix_address_pool(
        self,
        *,
        pool_name: str,
        prefix_id: str,
        prefix_length: int,
        namespace_id: str | None = None,
        track: bool = True,
    ) -> Any | None:
        """Wrap an EXISTING prefix in a CoreIPAddressPool named (and identified
        by) pool_name, so allocate_next_ip_address can hand out its addresses —
        IpamPrefix itself is not a CoreResourcePool, and the SDK only allocates
        from a real CoreIPAddressPool. Upserted by name on every run, so a moved
        prefix re-points the pool's resources. track=True: the caller's run owns
        it; track=False for a pool several targets allocate from (a load
        balancer context's VIP range), which no single run may delete.
        Addresses that already exist in the prefix (a gateway, another pool's
        reservation) are never handed out again. None on error."""
        try:
            pool = await self.client.create(
                kind=CoreIPAddressPool,
                data={
                    "name": pool_name,
                    "default_address_type": "IpamIPAddress",
                    "default_prefix_length": prefix_length,
                    "ip_namespace": {"id": namespace_id} if namespace_id else {"hfid": ["default"]},
                    "identifier": pool_name,
                    "resources": [prefix_id],
                },
            )
            await pool.save(**tracked_save_kwargs(track))
            self.logger.info(f"Upserted IP address pool '{pool_name}'")
            return pool
        except Exception as exc:
            self.logger.error(f"Failed to upsert IP address pool '{pool_name}': {exc}")
            return None

    async def allocate_prefix_address(
        self, *, pool: Any, identifier: str, prefix_length: int, description: str
    ) -> str | None:
        """The id of the address ``identifier`` reserves in ``pool`` — the same
        address on every run with the same identifier. None on error."""
        try:
            ip_obj = await self.client.allocate_next_ip_address(
                resource_pool=pool,
                kind=IpamIPAddress,
                identifier=identifier,
                prefix_length=prefix_length,
                data={"description": description},
            )
        except Exception as exc:
            self.logger.error(f"Failed to allocate address '{identifier}': {exc}")
            return None
        return ip_obj.id if ip_obj is not None else None

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
