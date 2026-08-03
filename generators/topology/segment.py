"""Generator for VXLAN segment deployment activations.

VlanSegment has no generator — its vlan_id is a plain manual attribute
(single-site, no pool allocation, no SegmentDeployment realization record).
Overlap validation across VlanSegments on the same fabric is a device check,
not generator logic.

VxlanSegmentGenerator handles:
  1. Determine target customer deployments from segment.customer_deployments
  2. Resolve each customer deployment's parent (TopologyDataCenter or
     TopologyColocationMetro) — VLAN/VNI pools and SegmentDeployment records
     live on the parent (TopologySegmentHosting), not on the customer footprint.
  3. For each resolved parent, create (or upsert) a ManagedSegmentDeployment
     with locally-allocated VLAN ID + VNI from its pool ranges.
  4. Assign the segment to leaf/tor customer-facing interfaces.
  5. Create inline sub-interfaces when terminate_inline is set.

VLAN IDs and VNIs are allocated from the parent's CoreNumberPool via from_pool.
The idempotency check (existing SegmentDeployment lookup) ensures from_pool
is only called for genuinely new deployments, avoiding double allocation.
"""

from __future__ import annotations

from typing import Any

from utils.data_cleaning import clean_data

from ..common import CommonGenerator
from ..protocols import (
    DcimPhysicalDevice,
    DcimPhysicalInterface,
    DcimVirtualInterface,
    ManagedSegmentDeployment,
    ManagedVxlanSegment,
    TopologySegmentHosting,
)


class VxlanSegmentGenerator(CommonGenerator):
    """VXLAN segment generator — allocates VLAN ID + VNI from pools, assigns
    the segment to leaf/tor customer-facing interfaces and physical host uplinks,
    and creates inline sub-interfaces when terminate_inline is set.
    """

    graphql_root_key = "ManagedVxlanSegment"

    @staticmethod
    def _extract_existing_vni(existing_deployments: list[Any]) -> int | None:
        """Return the first already allocated VNI from existing deployments, if any."""
        for deployment in existing_deployments:
            existing_vni = getattr(deployment, "vni", None)
            if existing_vni and getattr(existing_vni, "value", None):
                return existing_vni.value
        return None

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

        target_deployments = self._resolve_target_deployments(segment, segment_name)
        if not target_deployments:
            self.logger.error(f"Segment {segment_name}: could not resolve any hosting parent — cannot proceed")
            return

        self.logger.info(
            f"Segment {segment_name} will be activated in "
            f"{len(target_deployments)} deployment(s): "
            f"{[d.get('name', d.get('id')) for d in target_deployments]}"
        )

        existing_by_deployment_id: dict[str, Any] = {}
        reusable_vni: int | None = None
        try:
            existing_deployments = await self.client.filters(
                kind=ManagedSegmentDeployment,
                segment__ids=[segment_id],
            )
            for existing in existing_deployments:
                await existing.resolve()
                deployment_rel = getattr(existing, "deployment", None)
                deployment_peer = getattr(deployment_rel, "peer", None)
                deployment_obj = deployment_peer or deployment_rel
                deployment_id = getattr(deployment_obj, "id", None)
                if deployment_id and deployment_id not in existing_by_deployment_id:
                    existing_by_deployment_id[deployment_id] = existing
            reusable_vni = self._extract_existing_vni(existing_deployments)
        except Exception as exc:
            self.logger.warning(f"Segment {segment_name}: failed to prefetch existing deployments: {exc}")

        # ----------------------------------------------------------------
        # Create/upsert SegmentDeployment per deployment
        # ----------------------------------------------------------------
        failed_deployments: list[str] = []
        for dep in target_deployments:
            dep_id: str = dep.get("id", "")
            dep_name: str = dep.get("name", dep_id)
            if not dep_id:
                self.logger.warning("Deployment entry missing id — skipping")
                continue

            success = await self._activate_segment_in_deployment(
                segment_id=segment_id,
                segment_name=segment_name,
                deployment_id=dep_id,
                deployment_name=dep_name,
                existing_deployment=existing_by_deployment_id.get(dep_id),
                reusable_vni=reusable_vni,
            )
            if not success:
                failed_deployments.append(dep_name)

        if failed_deployments:
            self.logger.error(
                f"Segment {segment_name}: activation failed for deployments {failed_deployments}. "
                "State may be partially applied."
            )

        await self._assign_to_deployment_interfaces(segment, target_deployments)
        await self._create_inline_sub_interfaces(segment, target_deployments)

    def _resolve_target_deployments(self, segment: dict[str, Any], segment_name: str) -> list[dict[str, Any]]:
        """Resolve segment.customer_deployments to their hosting parents.

        VLAN/VNI pools and SegmentDeployment records live on the parent
        (TopologyDataCenter / TopologyColocationMetro via TopologySegmentHosting),
        not on the customer footprint itself.
        """
        customer_deployments: list[dict] = segment.get("customer_deployments") or []
        if not customer_deployments:
            self.logger.warning(
                f"Segment {segment_name} has no customer deployments (assign customer_deployments to the segment)"
            )
            return []

        return [
            hosting_parent
            for cust_dep in customer_deployments
            if (hosting_parent := self._resolve_hosting_parent(cust_dep, segment_name))
        ]

    def _resolve_hosting_parent(self, customer_deployment: dict[str, Any], segment_name: str) -> dict[str, Any] | None:
        """Resolve a customer footprint (CustomerDC/CustomerColocation) to its hosting parent.

        VLAN/VNI pools and SegmentDeployment records live on the parent
        (TopologyDataCenter / TopologyColocationMetro via TopologySegmentHosting),
        not on the customer footprint itself. The parent must be present in the
        GraphQL response (see the ... on TopologyCustomerDC/Colocation fragments
        in the vxlan_segment query) — no fallback fetch.
        """
        cust_id: str = customer_deployment.get("id", "")
        cust_name: str = customer_deployment.get("name", cust_id)
        if not cust_id:
            self.logger.warning("Customer deployment entry missing id — skipping")
            return None

        parent = customer_deployment.get("parent")
        if isinstance(parent, dict) and parent.get("id"):
            return parent

        self.logger.error(
            f"Segment {segment_name}: customer deployment {cust_name} has no parent in the query response "
            "— check the query includes the parent fragment for its concrete type"
        )
        return None

    async def _activate_segment_in_deployment(
        self,
        segment_id: str,
        segment_name: str,
        deployment_id: str,
        deployment_name: str,
        existing_deployment: Any | None = None,
        reusable_vni: int | None = None,
    ) -> bool:
        """Create or upsert one SegmentDeployment record.

        VLAN ID and VNI are always allocated from the deployment's pools.
        Idempotency: checks for existing SegmentDeployment first — from_pool
        is only called for genuinely new deployments.
        """
        # Backward-compatible fallback for direct invocations that do not pass
        # prefetched context from generate().
        if existing_deployment is None:
            try:
                existing = await self.client.filters(
                    kind=ManagedSegmentDeployment,
                    segment__ids=[segment_id],
                    deployment__ids=[deployment_id],
                )
                if existing:
                    existing_deployment = existing[0]
            except Exception as exc:
                self.logger.warning(
                    f"Error checking existing activations for {segment_name} in {deployment_name}: {exc}"
                )

        if reusable_vni is None:
            try:
                existing_for_segment = await self.client.filters(
                    kind=ManagedSegmentDeployment,
                    segment__ids=[segment_id],
                )
                reusable_vni = self._extract_existing_vni(existing_for_segment)
            except Exception as exc:
                self.logger.warning(f"Error checking reusable VNI for {segment_name}: {exc}")

        # --- Check idempotency first ---
        if existing_deployment:
            self.logger.info(f"  [{deployment_name}] SegmentDeployment already exists for {segment_name} — skipping")
            try:
                await existing_deployment.save(allow_upsert=True)  # register with tracker
            except Exception as exc:
                self.logger.warning(f"  [{deployment_name}] Failed to re-save existing activation: {exc}")
            return True

        # --- VLAN ID (always from pool via from_pool) ---
        vlan_pool = await self._get_dc_pool(deployment_id, deployment_name, "vlan_pool")
        if vlan_pool is None:
            self.logger.error(
                f"  [{deployment_name}] No vlan_pool for {segment_name}. Attach a CoreNumberPool to the DC's vlan_pool."
            )
            return False

        # Unique identifier per segment+deployment ensures stable allocation
        vlan_identifier = f"{segment_id}-{deployment_id}-vlan"
        self.logger.info(f"  [{deployment_name}] Allocating VLAN ID from pool {vlan_pool.name.value}")

        # --- VNI ---
        # VNI must be globally consistent — the same segment must carry the same VNI
        # in every DC so that EVPN type-2/3 routes stitch correctly across DCI.
        # Strategy: reuse the VNI already allocated for another DC's SegmentDeployment
        # of the same segment. Only fall back to pool allocation when this is the
        # first DC to activate the segment (no prior SegmentDeployment exists yet).
        vni_from_pool: dict[str, Any] | None = None
        vni_literal: int | None = reusable_vni
        if vni_literal is not None:
            self.logger.info(
                f"  [{deployment_name}] Reusing VNI {vni_literal} from existing SegmentDeployment for {segment_name}"
            )
        else:
            # First DC to activate this segment — allocate from pool
            vni_pool = await self._get_dc_pool(deployment_id, deployment_name, "vni_pool")
            if vni_pool is not None:
                vni_identifier = f"{segment_id}-vni"
                vni_from_pool = {"from_pool": {"id": vni_pool.id}, "identifier": vni_identifier}
                self.logger.info(f"  [{deployment_name}] Allocating VNI from pool {vni_pool.name.value}")
            else:
                self.logger.warning(
                    f"  [{deployment_name}] No vni_pool — VXLAN segment {segment_name} will not have L2 VNI allocated"
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
                kind=ManagedSegmentDeployment,
                data=activation_data,
            )
            await activation.save(allow_upsert=True)
            self.logger.info(
                f"  [{deployment_name}] SegmentDeployment saved "
                f"(segment={segment_name}, vlan=from_pool, vni={'from_pool' if vni_from_pool else 'none'})"
            )
            return True
        except Exception as exc:
            self.logger.error(f"  [{deployment_name}] Failed to create SegmentDeployment for {segment_name}: {exc}")
            return False

    async def _get_dc_pool(self, deployment_id: str, deployment_name: str, pool_attr: str) -> Any:
        """Fetch a pool (vlan_pool, vni_pool, l3_vni_pool) from a TopologySegmentHosting parent.

        deployment_id is a TopologyDataCenter or TopologyColocationMetro id — both
        inherit the pool relationships from TopologySegmentHosting, so the generic
        kind resolves either concrete type.
        Returns the pool SDK object, or None if not found.
        Uses a per-run cache to avoid re-fetching the parent for each pool attribute.
        """
        cache = getattr(self, "_dc_cache", {})
        if deployment_id not in cache:
            try:
                cache[deployment_id] = await self.client.get(
                    kind=TopologySegmentHosting,
                    id=deployment_id,
                    include=["vlan_pool", "vni_pool", "l3_vni_pool"],
                    prefetch_relationships=True,
                )
                self._dc_cache = cache
            except Exception:
                self.logger.debug(f"  [{deployment_name}] Could not fetch deployment {deployment_id}")
                return None

        dc = cache.get(deployment_id)
        pool_rel = getattr(dc, pool_attr, None) if dc else None
        pool_peer = getattr(pool_rel, "peer", None) if pool_rel else None
        if pool_peer and getattr(pool_peer, "id", None):
            return pool_peer
        self.logger.debug(f"  [{deployment_name}] No {pool_attr} on deployment {deployment_id}")
        return None

    async def _assign_to_deployment_interfaces(
        self, segment: dict[str, Any], target_deployments: list[dict[str, Any]]
    ) -> None:
        """Assign this segment to all leaf/tor customer-facing interfaces in each deployment."""
        segment_id: str = segment.get("id", "")
        segment_name: str = segment.get("name", "")
        if not segment_id or not target_deployments:
            return

        segment_obj = await self.client.get(kind=ManagedVxlanSegment, id=segment_id)
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
            role__values=["leaf", "tor", "l2-leaf"],
        )
        if not devices:
            self.logger.debug(f"  [{deployment_name}] No leaf/tor/l2-leaf devices — skipping interface assignment")
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
        updated = 0
        for iface in interfaces:
            iface_services = getattr(iface, "interface_capabilities")
            await iface_services.fetch()
            existing_ids = {peer.id for peer in iface_services.peers}
            changed = False

            if segment_id not in existing_ids:
                await iface_services.add(segment_obj)
                assigned += 1
                changed = True

            if iface.status.value != "active":
                iface.status.value = "active"
                changed = True

            if changed:
                # update_group_context=False: a physical interface belongs to the
                # device's object_template, not to this generator run — never a
                # delete_unused_nodes candidate.
                await iface.save(allow_upsert=True, update_group_context=False)
                updated += 1

        self.logger.info(
            f"  [{deployment_name}] Assigned segment '{segment_name}' to {assigned} interface(s) "
            f"({len(interfaces) - assigned} already assigned, {updated} interface(s) updated)"
        )

    async def _create_inline_sub_interfaces(
        self, segment: dict[str, Any], target_deployments: list[dict[str, Any]]
    ) -> None:
        """When terminate_inline is true, create DcimVirtualInterface sub-interfaces
        on all inline_service (ManagedHA) member devices.

        For each member device:
        - Finds the trunk/uplink physical interface (role=uplink or first physical interface)
        - Creates a DcimVirtualInterface named <parent>.<vlan_id> per deployment VLAN
        - Attaches the segment to interface_capabilities
        - Assigns the gateway IP from segment.gateway
        """
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

        # Gateway IP lives directly on the segment (one anycast address, v4 or v6)
        gateway = segment.get("gateway") or {}
        if not gateway.get("id"):
            self.logger.warning(
                f"Segment '{segment_name}' has no gateway — sub-interfaces created without IP addresses"
            )

        dep_ids: list[str] = [d["id"] for d in target_deployments if d.get("id")]

        # Fetch SegmentDeployments to get the allocated VLAN IDs
        vlan_by_dep: dict[str, int] = {}
        if dep_ids:
            try:
                existing = await self.client.filters(
                    kind=ManagedSegmentDeployment,
                    segment__ids=[segment_id],
                    deployment__ids=dep_ids,
                )
                for sd in existing:
                    await sd.resolve()
                    deployment_rel = getattr(sd, "deployment", None)
                    deployment_peer = getattr(deployment_rel, "peer", None)
                    deployment_obj = deployment_peer or deployment_rel
                    dep_id = getattr(deployment_obj, "id", None)
                    vlan_val = getattr(getattr(sd, "vlan_id", None), "value", None)
                    if dep_id and vlan_val and dep_id not in vlan_by_dep:
                        vlan_by_dep[dep_id] = vlan_val
            except Exception as exc:
                self.logger.warning(f"Could not fetch SegmentDeployments for inline termination: {exc}")

        if not vlan_by_dep:
            self.logger.warning(
                f"Segment '{segment_name}' has no allocated VLAN IDs yet — run again after pool allocation completes"
            )
            return

        # Use the first VLAN (segments typically have one VLAN ID per DC, pick any for naming)
        vlan_id = next(iter(vlan_by_dep.values()))

        # Fetch the segment SDK object for interface_capabilities linkage
        segment_obj = await self.client.get(kind=ManagedVxlanSegment, id=segment_id)
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

            gateway_ip_str: str | None = gateway.get("address")
            ip_address_data: Any = {"id": gateway["id"]} if gateway.get("id") else None

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
