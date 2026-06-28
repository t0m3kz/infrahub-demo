"""Generators for network segment deployment activations.

Two generators handle different segment types:
  - VlanSegmentGenerator:  traditional 802.1Q VLAN (allocates VLAN ID only)
  - VxlanSegmentGenerator: VXLAN overlay (allocates VLAN ID + VNI)

Both share the same base class that handles:
  1. Determine target deployments from segment.deployments
  2. For each target deployment, create (or upsert) a ManagedSegmentDeployment
     with locally-allocated VLAN ID (and VNI for VXLAN) from DC pool ranges.

VLAN IDs and VNIs are allocated from the DC's CoreNumberPool via from_pool.
The idempotency check (existing SegmentDeployment lookup) ensures from_pool
is only called for genuinely new deployments, avoiding double allocation.
"""

from __future__ import annotations

from typing import Any

from utils.data_cleaning import clean_data

from ..common import CommonGenerator
from ..protocols import DcimPhysicalDevice, DcimPhysicalInterface, DcimVirtualInterface

# Typename constants for both segment types
MANAGED_SEGMENT_TYPES = ("ManagedVlanSegment", "ManagedVxlanSegment")


class BaseSegmentGenerator(CommonGenerator):
    """Shared segment activation logic for VLAN and VXLAN segments."""

    # Subclasses set this to their concrete GraphQL root key
    graphql_root_key: str = ""
    # Whether this generator allocates VNI from vni_pool
    allocate_vni: bool = False

    async def generate(self, data: dict[str, Any]) -> None:
        """Create or upsert ManagedSegmentDeployment records for the segment."""
        cleaned = clean_data(data)
        segment_list = cleaned.get(self.graphql_root_key, [])
        if not segment_list:
            self.logger.error(f"No {self.graphql_root_key} data in GraphQL response")
            return

        segment = segment_list[0]
        segment_id: str = segment.get("id", "")
        segment_name: str = segment.get("name", "")

        if not segment_id or not segment_name:
            self.logger.error("Segment missing id or name — cannot proceed")
            return

        self.logger.info(f"Processing segment: {segment_name}")

        # ----------------------------------------------------------------
        # Resolve target deployments directly from segment
        # VlanSegment: single deployment (cardinality one, key "deployment")
        # VxlanSegment: multiple deployments (cardinality many, key "deployments")
        # ----------------------------------------------------------------
        raw = segment.get("deployments") or segment.get("deployment")
        if isinstance(raw, dict):
            target_deployments: list[dict] = [raw]
        else:
            target_deployments: list[dict] = raw or []

        if not target_deployments:
            self.logger.warning(f"Segment {segment_name} has no target deployments (assign deployment to the segment)")
            return

        self.logger.info(
            f"Segment {segment_name} will be activated in "
            f"{len(target_deployments)} deployment(s): "
            f"{[d.get('name', d.get('id')) for d in target_deployments]}"
        )

        # ----------------------------------------------------------------
        # Create/upsert SegmentDeployment per deployment
        # ----------------------------------------------------------------
        for dep in target_deployments:
            dep_id: str = dep.get("id", "")
            dep_name: str = dep.get("name", dep_id)
            if not dep_id:
                self.logger.warning("Deployment entry missing id — skipping")
                continue

            await self._activate_segment_in_deployment(
                segment_id=segment_id,
                segment_name=segment_name,
                deployment_id=dep_id,
                deployment_name=dep_name,
            )

    async def _activate_segment_in_deployment(
        self,
        segment_id: str,
        segment_name: str,
        deployment_id: str,
        deployment_name: str,
    ) -> None:
        """Create or upsert one SegmentDeployment record.

        VLAN ID is always allocated from the DC's vlan_pool via from_pool.
        VNI is allocated from vni_pool only for VXLAN segments.
        Idempotency: checks for existing SegmentDeployment first — from_pool
        is only called for genuinely new deployments.
        """
        # --- Check idempotency first ---
        try:
            existing = await self.client.filters(
                kind="ManagedSegmentDeployment",
                segment__ids=[segment_id],
                deployment__ids=[deployment_id],
            )
            if existing:
                self.logger.info(
                    f"  [{deployment_name}] SegmentDeployment already exists for {segment_name} — skipping"
                )
                await existing[0].save()  # register with tracker
                return
        except Exception as exc:
            self.logger.warning(f"  [{deployment_name}] Error checking existing activations: {exc}")

        # --- VLAN ID (always from pool via from_pool) ---
        vlan_pool = await self._get_dc_pool(deployment_id, deployment_name, "vlan_pool")
        if vlan_pool is None:
            self.logger.error(
                f"  [{deployment_name}] No vlan_pool for {segment_name}. Attach a CoreNumberPool to the DC's vlan_pool."
            )
            return

        # Unique identifier per segment+deployment ensures stable allocation
        vlan_identifier = f"{segment_id}-{deployment_id}-vlan"
        self.logger.info(f"  [{deployment_name}] Allocating VLAN ID from pool {vlan_pool.name.value}")

        # --- VNI (only for VXLAN segments) ---
        # VNI must be globally consistent — the same segment must carry the same VNI
        # in every DC so that EVPN type-2/3 routes stitch correctly across DCI.
        # Strategy: reuse the VNI already allocated for another DC's SegmentDeployment
        # of the same segment. Only fall back to pool allocation when this is the
        # first DC to activate the segment (no prior SegmentDeployment exists yet).
        vni_from_pool: dict[str, Any] | None = None
        vni_literal: int | None = None
        if self.allocate_vni:
            # Check if another DC already allocated a VNI for this segment
            try:
                existing_deployments = await self.client.filters(
                    kind="ManagedSegmentDeployment",
                    segment__ids=[segment_id],
                )
                for ed in existing_deployments:
                    await ed.resolve()
                    existing_vni = getattr(ed, "vni", None)
                    if existing_vni and getattr(existing_vni, "value", None):
                        vni_literal = existing_vni.value
                        self.logger.info(
                            f"  [{deployment_name}] Reusing VNI {vni_literal} "
                            f"from existing SegmentDeployment for {segment_name}"
                        )
                        break
            except Exception as exc:
                self.logger.warning(f"  [{deployment_name}] Could not check existing VNIs: {exc}")

            if vni_literal is None:
                # First DC to activate this segment — allocate from pool
                vni_pool = await self._get_dc_pool(deployment_id, deployment_name, "vni_pool")
                if vni_pool is not None:
                    vni_identifier = f"{segment_id}-vni"
                    vni_from_pool = {"from_pool": {"id": vni_pool.id}, "identifier": vni_identifier}
                    self.logger.info(f"  [{deployment_name}] Allocating VNI from pool {vni_pool.name.value}")
                else:
                    self.logger.warning(
                        f"  [{deployment_name}] No vni_pool — VXLAN segment {segment_name} "
                        "will not have L2 VNI allocated"
                    )

        # --- Create SegmentDeployment with pool-allocated values ---
        activation_data: dict[str, Any] = {
            "vlan_id": {"from_pool": {"id": vlan_pool.id}, "identifier": vlan_identifier},
            "segment": {"id": segment_id},
            "deployment": {"id": deployment_id},
            "status": "provisioning",
        }
        if vni_literal is not None:
            activation_data["vni"] = vni_literal
        elif vni_from_pool is not None:
            activation_data["vni"] = vni_from_pool

        try:
            activation = await self.client.create(
                kind="ManagedSegmentDeployment",
                data=activation_data,
            )
            await activation.save(allow_upsert=True)
            self.logger.info(
                f"  [{deployment_name}] SegmentDeployment saved "
                f"(segment={segment_name}, vlan=from_pool, vni={'from_pool' if vni_from_pool else 'none'})"
            )
        except Exception as exc:
            self.logger.error(f"  [{deployment_name}] Failed to create SegmentDeployment for {segment_name}: {exc}")

    async def _get_dc_pool(self, deployment_id: str, deployment_name: str, pool_attr: str) -> Any:
        """Fetch a pool (vlan_pool, vni_pool, l3_vni_pool) from the TopologyDataCenter.

        Returns the pool SDK object, or None if not found.
        Uses a per-run cache to avoid re-fetching the DC for each pool attribute.
        """
        cache = getattr(self, "_dc_cache", {})
        if deployment_id not in cache:
            try:
                cache[deployment_id] = await self.client.get(
                    kind="TopologyDataCenter",
                    id=deployment_id,
                    include=["vlan_pool", "vni_pool", "l3_vni_pool"],
                    prefetch_relationships=True,
                )
                self._dc_cache = cache
            except Exception:
                self.logger.debug(f"  [{deployment_name}] Could not fetch DC {deployment_id}")
                return None

        dc = cache.get(deployment_id)
        pool_rel = getattr(dc, pool_attr, None) if dc else None
        pool_peer = getattr(pool_rel, "peer", None) if pool_rel else None
        if pool_peer and getattr(pool_peer, "id", None):
            return pool_peer
        self.logger.debug(f"  [{deployment_name}] No {pool_attr} on deployment {deployment_id}")
        return None


class VlanSegmentGenerator(BaseSegmentGenerator):
    """VLAN segment generator — allocates only VLAN ID from vlan_pool."""

    graphql_root_key = "ManagedVlanSegment"
    allocate_vni = False


class VxlanSegmentGenerator(BaseSegmentGenerator):
    """VXLAN segment generator — allocates VLAN ID + VNI from pools, assigns
    the segment to leaf/tor customer-facing interfaces and physical host uplinks,
    and creates inline sub-interfaces when terminate_inline is set.

    VIP/pool-member wiring is handled by AppComponentGenerator (triggered when
    AppComponent nodes are created or updated).
    """

    graphql_root_key = "ManagedVxlanSegment"
    allocate_vni = True

    async def generate(self, data: dict[str, Any]) -> None:
        await super().generate(data)
        await self._assign_to_deployment_interfaces(data)
        await self._create_inline_sub_interfaces(data)

    async def _assign_to_deployment_interfaces(self, data: dict[str, Any]) -> None:
        """Assign this segment to all leaf/tor customer-facing interfaces in each deployment."""
        cleaned = clean_data(data)
        segment_list = cleaned.get(self.graphql_root_key, [])
        if not segment_list:
            return

        segment = segment_list[0]
        segment_id: str = segment.get("id", "")
        segment_name: str = segment.get("name", "")
        raw = segment.get("deployments") or segment.get("deployment")
        target_deployments: list[dict] = [raw] if isinstance(raw, dict) else (raw or [])

        if not segment_id or not target_deployments:
            return

        segment_obj = await self.client.get(kind="ManagedVxlanSegment", id=segment_id)
        if not segment_obj:
            self.logger.warning(f"Could not fetch segment SDK object for {segment_name}")
            return

        for dep in target_deployments:
            dep_id: str = dep.get("id", "")
            dep_name: str = dep.get("name", dep_id)
            if not dep_id:
                continue
            await self._assign_segment_to_dc_interfaces(
                segment_id=segment_id,
                segment_obj=segment_obj,
                segment_name=segment_name,
                deployment_id=dep_id,
                deployment_name=dep_name,
            )

    async def _assign_segment_to_dc_interfaces(
        self,
        segment_id: str,
        segment_obj: Any,
        segment_name: str,
        deployment_id: str,
        deployment_name: str,
    ) -> None:
        """Find all leaf/tor customer-facing interfaces in a DC and add the segment
        to their interface_capabilities relationship (queried by the leaf transform)."""
        devices = await self.client.filters(
            kind=DcimPhysicalDevice,
            deployment__ids=[deployment_id],
            role__values=["leaf", "tor"],
        )
        if not devices:
            self.logger.debug(f"  [{deployment_name}] No leaf/tor devices — skipping interface assignment")
            return

        device_ids = [d.id for d in devices]
        interfaces = await self.client.filters(
            kind=DcimPhysicalInterface,
            device__ids=device_ids,
            role__value="customer",
        )
        if not interfaces:
            self.logger.debug(f"  [{deployment_name}] No customer/downlink interfaces — skipping")
            return

        assigned = 0
        for iface in interfaces:
            iface_services = getattr(iface, "interface_capabilities")
            await iface_services.fetch()
            existing_ids = {peer.id for peer in iface_services.peers}
            if segment_id not in existing_ids:
                await iface_services.add(segment_obj)
            iface.status.value = "active"
            await iface.save(allow_upsert=True)
            if segment_id not in existing_ids:
                assigned += 1

        self.logger.info(
            f"  [{deployment_name}] Assigned segment '{segment_name}' to {assigned} interface(s) "
            f"({len(interfaces) - assigned} already assigned)"
        )

    async def _create_inline_sub_interfaces(self, data: dict[str, Any]) -> None:
        """When terminate_inline is true, create DcimVirtualInterface sub-interfaces
        on all inline_service (ManagedHA) member devices.

        For each member device:
        - Finds the trunk/uplink physical interface (role=uplink or first physical interface)
        - Creates a DcimVirtualInterface named <parent>.<vlan_id> per deployment VLAN
        - Attaches the segment to interface_capabilities
        - Assigns the gateway IP from segment.prefix
        """
        cleaned = clean_data(data)
        segment_list = cleaned.get(self.graphql_root_key, [])
        if not segment_list:
            return

        segment = segment_list[0]
        terminate_inline = segment.get("terminate_inline") or False
        if not terminate_inline:
            return

        segment_id: str = segment.get("id", "")
        segment_name: str = segment.get("name", "")
        inline_service = segment.get("inline_service") or {}
        ha_node = inline_service if inline_service.get("id") else {}
        if not ha_node:
            self.logger.warning(
                f"Segment '{segment_name}' has terminate_inline=true but no inline_service — skipping sub-interface creation"
            )
            return

        # Resolve all member devices from ManagedHA.capabilities
        member_devices = [cap for cap in (ha_node.get("capabilities") or []) if cap.get("id")]
        if not member_devices:
            self.logger.warning(
                f"Segment '{segment_name}' inline_service has no member devices — skipping sub-interface creation"
            )
            return

        # Collect gateway IPs and their namespaces from segment.prefix
        # Each prefix entry may have a different gateway_ip (one per namespace/VRF)
        prefix_entries = segment.get("prefix") or []
        if not prefix_entries:
            self.logger.warning(f"Segment '{segment_name}' has no prefix — sub-interfaces created without IP addresses")

        # Collect per-deployment VLAN IDs by querying existing SegmentDeployments
        raw_deps = segment.get("deployments") or segment.get("deployment")
        target_deployments: list[dict] = [raw_deps] if isinstance(raw_deps, dict) else (raw_deps or [])
        dep_ids: list[str] = [d["id"] for d in target_deployments if d.get("id")]

        # Fetch SegmentDeployments to get the allocated VLAN IDs
        vlan_by_dep: dict[str, int] = {}
        for dep_id in dep_ids:
            try:
                existing = await self.client.filters(
                    kind="ManagedSegmentDeployment",
                    segment__ids=[segment_id],
                    deployment__ids=[dep_id],
                )
                for sd in existing:
                    await sd.resolve()
                    vlan_val = getattr(getattr(sd, "vlan_id", None), "value", None)
                    if vlan_val:
                        vlan_by_dep[dep_id] = vlan_val
                        break
            except Exception as exc:
                self.logger.warning(f"Could not fetch SegmentDeployment for dep {dep_id}: {exc}")

        if not vlan_by_dep:
            self.logger.warning(
                f"Segment '{segment_name}' has no allocated VLAN IDs yet — run again after pool allocation completes"
            )
            return

        # Use the first VLAN (segments typically have one VLAN ID per DC, pick any for naming)
        vlan_id = next(iter(vlan_by_dep.values()))

        # Fetch the segment SDK object for interface_capabilities linkage
        segment_obj = await self.client.get(kind="ManagedVxlanSegment", id=segment_id)
        if not segment_obj:
            self.logger.warning(f"Could not fetch segment SDK object for '{segment_name}'")
            return

        self.logger.info(
            f"Segment '{segment_name}' terminate_inline=true — creating sub-interfaces on "
            f"{len(member_devices)} device(s), VLAN {vlan_id}"
        )

        for member in member_devices:
            device_id: str = member.get("id", "")
            device_name: str = member.get("name", device_id)

            # Find the trunk/uplink physical interface on this device
            trunk_iface = await self._find_trunk_interface(device_id, device_name)
            if trunk_iface is None:
                self.logger.warning(f"  [{device_name}] No trunk/uplink interface found — skipping")
                continue

            # Sub-interface name: <parent>.<vlan_id> (e.g. Ethernet1/1.100)
            sub_iface_name = f"{trunk_iface.name.value}.{vlan_id}"

            # Pick gateway IP and namespace from the first prefix entry (no per-DC differentiation for FW)
            gateway_ip_str: str | None = None
            ip_namespace_id: str | None = None
            if prefix_entries:
                first_prefix = prefix_entries[0]
                gateway_ip_str = first_prefix.get("gateway_ip")
                ns = first_prefix.get("ip_namespace") or {}
                ip_namespace_id = ns.get("id")

            ip_address_data: Any = None
            if gateway_ip_str and ip_namespace_id:
                # Allocate/upsert the gateway IP address
                try:
                    ip_obj = await self.client.create(
                        kind="IpamIPAddress",
                        data={
                            "address": gateway_ip_str,
                            "ip_namespace": {"id": ip_namespace_id},
                        },
                    )
                    await ip_obj.save(allow_upsert=True)
                    ip_address_data = {"id": ip_obj.id}
                except Exception as exc:
                    self.logger.warning(f"  [{device_name}] Could not upsert gateway IP {gateway_ip_str}: {exc}")

            try:
                sub_iface = await self.client.create(
                    kind=DcimVirtualInterface,
                    data={
                        "name": sub_iface_name,
                        "device": {"id": device_id},
                        "parent_interface": {"id": trunk_iface.id},
                        "status": "active",
                        "role": "service",
                        **({"ip_address": ip_address_data} if ip_address_data else {}),
                    },
                )
                await sub_iface.save(allow_upsert=True)

                # Link segment to interface_capabilities
                iface_services = getattr(sub_iface, "interface_capabilities")
                await iface_services.fetch()
                existing_ids = {peer.id for peer in iface_services.peers}
                if segment_id not in existing_ids:
                    await iface_services.add(segment_obj)
                    await sub_iface.save(allow_upsert=True)

                self.logger.info(
                    f"  [{device_name}] Upserted sub-interface {sub_iface_name}"
                    + (f" with IP {gateway_ip_str}" if gateway_ip_str else "")
                )
            except Exception as exc:
                self.logger.error(f"  [{device_name}] Failed to create sub-interface {sub_iface_name}: {exc}")

    async def _find_trunk_interface(self, device_id: str, device_name: str) -> Any:
        """Return the trunk/uplink physical interface for a device.

        Looks for role=uplink first, then falls back to the first physical interface.
        """
        try:
            uplinks = await self.client.filters(
                kind=DcimPhysicalInterface,
                device__ids=[device_id],
                role__value="uplink",
            )
            if uplinks:
                return uplinks[0]
            # Fallback: first physical interface alphabetically
            all_ifaces = await self.client.filters(
                kind=DcimPhysicalInterface,
                device__ids=[device_id],
            )
            if all_ifaces:
                return sorted(all_ifaces, key=lambda i: i.name.value)[0]
        except Exception as exc:
            self.logger.warning(f"  [{device_name}] Error fetching interfaces: {exc}")
        return None
