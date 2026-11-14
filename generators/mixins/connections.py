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
    from generators.mixins.common import CommonGenerator

from generators.helpers import CableTypeDetector, CablingPlanner
from generators.helpers.cable_attributes import RackPosition, cable_attributes, endpoint_label
from generators.helpers.interface_vrf import join_capabilities, leave_vrfs, vrf_ids
from generators.mixins.far_end import ports_free_for
from generators.mixins.types import CablingOptions, ChainHop
from generators.protocols import (
    DcimCable,
    DcimPhysicalDevice,
    DcimPhysicalInterface,
    DcimVirtualInterface,
    IpamIPAddress,
    LocationRack,
    LocationSuite,
    ManagedInterfaceEncapsulation,
)
from utils.interface_encapsulation import encapsulation_name
from utils.limits import DEFAULT_NAMESPACE, WAIT_FOR_PEER_GENERATOR


def tracked_save_kwargs(track: bool) -> dict[str, Any]:
    """save() kwargs for a write the run owns (track) or does not (not track). track=False adds
    update_group_context=False so delete_unused_nodes never reclaims the node; track=True is the plain upsert.
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
    """What one run has already read for cable lengths: device positions, racks and suites (data, never moved by a
    generator), read once per run.
    """

    positions: dict[str, RackPosition | None] = field(default_factory=dict)
    racks: dict[str, Any] = field(default_factory=dict)
    suites: dict[str, Any] = field(default_factory=dict)


class CablingMixin:
    """Device-to-device cabling methods for CommonGenerator.
    The host provides ``client``, ``logger``, ``deployment_id``, ``_resolve_pool`` and ``_retry_delay``."""

    # Attribute declarations for the type checker, provided by CommonGenerator / InfrahubGenerator
    client: Any
    logger: logging.Logger
    deployment_id: str
    # CommonGenerator._resolve_pool / _retry_delay, annotation only
    _resolve_pool: Any
    if TYPE_CHECKING:
        _retry_delay = staticmethod(CommonGenerator._retry_delay)
    # Set on first use by _cable_layouts, one per run
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
        """Create cabling connections between device layers: query interfaces, build the plan, then per connection
        create the cable, allocate IPs and save the interfaces. All saves use allow_upsert=True for idempotency and
        tracking.
        """
        if options is None:
            options = CablingOptions()
        cabling_offset: int = int(options.get("cabling_offset", 0))
        self.logger.info(
            f"Creating cabling: {len(bottom_devices)} bottom → {len(top_devices)} top "
            f"[strategy={strategy}, offset={cabling_offset}, speed-matched]"
        )

        # Retry until template instantiation completes (templates apply asynchronously)
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

        iface_map: dict[str, Any] = {iface.id: iface for iface in src_interfaces + dst_interfaces}

        planner = CablingPlanner(
            bottom_interfaces=src_interfaces,
            top_interfaces=dst_interfaces,
            bottom_sorting=bottom_sorting,
            top_sorting=top_sorting,
        )
        cabling_plan = planner.build_cabling_plan(scenario=strategy, cabling_offset=cabling_offset)

        if not cabling_plan:
            # An empty plan with interfaces on both sides means devices asked to be cabled won't be: a failure
            self.logger.error(
                f"No cabling connections planned for {sorted(bottom_devices)} -> {sorted(top_devices)} "
                f"(strategy={strategy}, offset={cabling_offset})"
            )
            return []

        technical_pool = await self._resolve_pool(
            provided=options.get("pool"),
            kind=CoreIPPrefixPool,
            fallback_name=None,
        )

        return await self._execute_cabling_plan(cabling_plan, iface_map, options, technical_pool)

    async def _cable_layouts(self, device_ids: Iterable[str]) -> dict[str, RackPosition | None]:
        """Rack position of each device, for cable lengths (cabling, HA sync cable, MLAG peer-link cables). A missing or
        None entry has no derivable position (virtual device, no rack, rack with no suite). Memoised per run in at most
        three batched reads (devices, racks, suites); anything not visible yet is not memoised.
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
        """Read the devices, then the racks and suites the cache lacks, and memoise the positions."""
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
        """Execute a built cabling plan: create each cable, allocate P2P IPs if a pool is given, save both interfaces.
        Shared tail of create_cabling() and create_chain_cabling()."""
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

            # These interfaces were loaded with include=["cable"], so re-saving them preserves their cable instead of
            # sending null
            updated_src = iface_map[src_interface.id]
            updated_dst = iface_map[dst_interface.id]

            # Allocate P2P addresses if a pool is given (prefix length 127 for IPv6, 31 for IPv4)
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

            # update_group_context=False: physical interfaces come from the object_template and must never be
            # delete_unused_nodes candidates
            updated_src.description.value = cable_name
            updated_src.status.value = "active"
            await updated_src.save(allow_upsert=True, update_group_context=False)

            updated_dst.description.value = cable_name
            updated_dst.status.value = "active"
            await updated_dst.save(allow_upsert=True, update_group_context=False)

            # Create the cable LAST and let it set the link from its own side via `endpoints`: writing a fresh cable's
            # id onto an interface can hit NODE_NOT_FOUND under concurrent runs
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

            # In-memory only, after the last save: create_routing() reads the cable from these returned interface
            # objects (cable_map in helpers/routing.py) and nothing saves them again
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
        """Upsert both addresses of a /31 or /127 P2P prefix (offsets 0 and 1, returned in that order)."""
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
        """Upsert addresses at the given host offsets of an allocated prefix; returns offset -> address node. Queried
        before created (uniqueness is an async validator) and saved even when found so the run tracks it;
        ``track=False`` skips tracking.
        """
        network = ipaddress.ip_network(prefix.prefix.value, strict=False)
        length = network.prefixlen if address_length is None else address_length
        # network[offset], not .hosts(): .hosts() yields one address for /31 and /127
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

    async def _find_addresses(self, addresses: Sequence[str], namespace_id: str) -> dict[str, Any]:
        """The IpamIPAddress nodes that already exist among ``addresses`` in the namespace, by canonical address, in one
        filtered read (the find-before-create guard).
        """
        nodes = await self.client.filters(
            kind=IpamIPAddress, address__values=list(addresses), ip_namespace__ids=[namespace_id]
        )
        return {_address_key(node.address.value): node for node in nodes}

    async def _save_address(
        self, *, address: str, ip_namespace: Any, existing: Any | None, description: str | None, track: bool
    ) -> Any:
        """Save one address, creating it when ``existing`` is None. Saved even when found, since the run tracks only
        what it saves; an existing address is re-created from its id to rewrite the description.
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
        """Cable an ordered chain of device groups end to end, index-paired; fewer devices on a side are reused
        round-robin. Each hop's ``down_role`` ports cable to the next hop's ``up_role`` ports; returns one list of (src,
        dst) pairs per leg.
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
            top_interfaces, bottom_interfaces = (
                await ports_free_for(self.client, list(top_interfaces), {i.device.id for i in bottom_interfaces}),
                await ports_free_for(self.client, list(bottom_interfaces), {i.device.id for i in top_interfaces}),
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
                # build_cabling_plan raises CablingPlanError on a speed mismatch or port shortfall; this guards any
                # other empty plan
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
        ip_address_id: str | None = None,
        vrf_id: str | None = None,
        track: bool = True,
        role: str = "service",
    ) -> Any | None:
        """Upsert a VLAN-tagged DcimVirtualInterface on trunk_iface, named <trunk>.<vlan_id>, riding on the trunk
        through its ``ManagedInterfaceEncapsulation`` (saved with the interface) and put in the VRF instance ``vrf_id``
        (generators/helpers/interface_vrf.py). The caller resolves the trunk interface. ``track=False`` saves with
        update_group_context=False for a sub-interface reached by more than one target. ``ip_address_id`` must already
        exist.
        """
        sub_iface_name = f"{trunk_iface.name.value}.{vlan_id_value}"
        sub_iface_data: dict[str, Any] = {
            "name": sub_iface_name,
            "device": {"id": device_id},
            "status": "active",
            "role": role,
            **({"ip_address": {"id": ip_address_id}} if ip_address_id else {}),
        }

        try:
            sub_iface = await self.client.create(kind=DcimVirtualInterface, data=sub_iface_data)
            await sub_iface.save(**tracked_save_kwargs(track))
            encapsulation = await self.client.create(
                kind=ManagedInterfaceEncapsulation,
                data={
                    "name": encapsulation_name(device_name, sub_iface_name),
                    "parent": {"id": trunk_iface.id},
                    "vlan_id": vlan_id_value,
                    "capabilities": [{"id": sub_iface.id}],
                },
            )
            await encapsulation.save(**tracked_save_kwargs(track))
            if vrf_id:
                # A node built from create(data) does not know the capabilities the server holds: one get with the
                # relationship included; edges are written one by one with NODE_NOT_FOUND retry (the VRF may be new)
                stored = await self.client.get(kind=DcimVirtualInterface, id=sub_iface.id, include=["capabilities"])
                peers = getattr(stored, "capabilities").peers
                stale_vrfs = [old for old in vrf_ids(peers) if old != vrf_id]
                missing = [vrf_id] if vrf_id not in {peer.id for peer in peers} else []
                await join_capabilities(self.client, self.logger, sub_iface.id, missing)
                await leave_vrfs(self.client, sub_iface.id, stale_vrfs)
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
        """Wrap an EXISTING prefix in a CoreIPAddressPool named pool_name so allocate_next_ip_address can hand out its
        addresses. Upserted by name each run, so a moved prefix re-points the pool. track=False for a pool several
        targets share; existing addresses are never handed out again. None on error.
        """
        try:
            pool = await self.client.create(
                kind=CoreIPAddressPool,
                data={
                    "name": pool_name,
                    "default_address_type": "IpamIPAddress",
                    "default_prefix_length": prefix_length,
                    "ip_namespace": {"id": namespace_id} if namespace_id else {"hfid": [DEFAULT_NAMESPACE]},
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
        """The id of the address ``identifier`` reserves in ``pool``: the same address on every run. None on error."""
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
        """Cable border-leaf/border-spine<->firewall<->load-balancer per connectivity_mode, index-paired
        (border[0]<->fw[0], ...). pbr: two independent legs on the service device's "uplink" ports. inline: one chain
        border<->firewall<->load-balancer<->border using "uplink" and "downlink" ports. No-ops for any leg with nothing
        to cable.
        """
        border_to_firewall = ChainHop(devices=border_names, down_role=border_role_for["internal-firewall"])
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
