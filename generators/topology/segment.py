"""Generator for VXLAN segment deployment activations.

VlanSegment has no generator — its vlan_id is a plain manual attribute
(single-site, no pool allocation, no SegmentDeployment realization record).
Overlap validation across VlanSegments on the same fabric is a device check,
not generator logic.

VxlanSegmentGenerator handles:
  1. Determine target customer deployments from segment.customer_deployments
  2. Resolve each customer deployment's parent (TopologyDataCenter or
     TopologyColocationMetro) — the VNI pool and SegmentDeployment record
     live on the parent (TopologySegmentHosting), not on the customer footprint.
  3. For each resolved parent, create (or upsert) a ManagedSegmentDeployment
     with a locally-allocated VNI from its pool range.
  4. For a STRETCHED segment, realize a LOCAL VLAN ID on every border gateway
     of each deployment (EVPN Multi-Site BGW plumbing — see
     _realize_border_gateways). Customer-facing interface assignment is NOT
     done here: it is driven by AppComponent.instances, one physical device
     at a time, in generators/topology/app_instance_segment.py — a segment
     with no AppComponent referencing it gets no customer-port assignment at
     all. Independent VLAN domains may reuse the same numeric VLAN ID, since
     IEEE 802.1Q VLAN ID has only local significance (unlike VNI, which is
     the real DC-wide/fabric-wide segment identifier).
  5. Create inline sub-interfaces when terminate_inline is set.

VNIs are allocated via from_pool. A local segment draws from its parent's
own vni_pool, independently in every parent. A stretched segment draws ONE VNI
from the global GLOBAL-L2VNI pool and every other parent reuses it. The per-site
pools all hand out the same band, so a stretched VNI taken from one site's pool
and written into another site would collide with whatever that site's own pool
had already given a different segment. The two bands are disjoint, so a
stretched VNI can never meet a local one. The idempotency check (existing
SegmentDeployment lookup) ensures from_pool is only called for genuinely new
deployments, avoiding double allocation.
"""

from __future__ import annotations

from typing import Any

from infrahub_sdk.protocols import CoreNumberPool

from utils.data_cleaning import clean_data

from ..common import CommonGenerator
from ..connections import CablingMixin
from ..helpers.pools import CUSTOMER_VLAN_ID_MAX, CUSTOMER_VLAN_ID_MIN
from ..helpers.rules import RulesPlanner
from ..named_objects import GetOrCreateByNameMixin
from ..pools import PoolMixin
from ..protocols import (
    DcimPhysicalDevice,
    ManagedSegmentDeployment,
    ManagedVxlanSegment,
    SecurityZone,
)
from ..vlan_domain import VlanDomainMixin

# Devices that act as a deployment's EVPN Multi-Site border gateway for a
# stretched segment: the DC's border leaves and the metro's edges. `edge` is
# also the colocation metro's on-ramp router — its only VTEP, so it is both
# the metro's border gateway and (via AppInstance resolution, not here) where
# a stretched segment's customer ports land.
_BORDER_GATEWAY_ROLES = frozenset({"border-leaf", "edge"})
# Bootstrap pool (data/bootstrap/18_vni_pools.yml) every stretched segment's
# single VNI comes from — disjoint from the per-site {fabric}-vni-pool band.
STRETCHED_VNI_POOL_NAME = "GLOBAL-L2VNI"


class VxlanSegmentGenerator(GetOrCreateByNameMixin, PoolMixin, CablingMixin, VlanDomainMixin, CommonGenerator):
    """VXLAN segment generator — allocates a VNI from the DC's pool, realizes
    a LOCAL VLAN ID on every border gateway of a stretched segment's
    deployments, and creates inline sub-interfaces when terminate_inline is
    set. Customer-facing interface assignment is not done here — see
    generators/topology/app_instance_segment.py.
    """

    graphql_root_key = "ManagedVxlanSegment"

    async def _get_stretched_vni_pool(self) -> dict[str, Any] | None:
        """Return the global stretched-segment VNI pool as {"id", "name"}, or None."""
        try:
            pool = await self.client.get(
                kind=CoreNumberPool, name__value=STRETCHED_VNI_POOL_NAME, raise_when_missing=False
            )
        except Exception as exc:
            self.logger.warning(f"Error looking up VNI pool {STRETCHED_VNI_POOL_NAME}: {exc}")
            return None
        if pool is None:
            return None
        return {"id": pool.id, "name": STRETCHED_VNI_POOL_NAME}

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

        await self._ensure_security_zone(
            segment_id=segment_id,
            segment_name=segment_name,
            environment=segment.get("environment") or "p",
        )

        target_deployments = self._resolve_target_deployments(segment, segment_name)
        if not target_deployments:
            self.logger.error(f"Segment {segment_name}: could not resolve any hosting parent — cannot proceed")
            return

        # A segment can reference a customer that just boarded onto a DC
        # still being bootstrapped (its own add_dc/dc_pod_cascade hasn't
        # created vni_pool yet) — wait for that in-flight parent
        # and re-resolve rather than fail immediately (same race as
        # customer_dc.py's firewall/LB device read).
        missing_pool_ids = [
            dep["id"] for dep in target_deployments if dep.get("id") and not (dep.get("vni_pool") or {}).get("id")
        ]
        if missing_pool_ids:
            for dep_id in missing_pool_ids:
                for parent_generator in ("add_dc", "dc_pod_cascade", "add_colocation_metro"):
                    refreshed = await self.wait_for_parent_generator_and_refetch(parent_generator, dep_id)
                    if refreshed is not None:
                        cleaned = clean_data(refreshed)
                        refreshed_list = cleaned.get(self.graphql_root_key, [])
                        if refreshed_list:
                            segment = refreshed_list[0]
            target_deployments = self._resolve_target_deployments(segment, segment_name)

        stretch_scope = segment.get("stretch_scope") or "local"
        stretched = stretch_scope != "local"
        self.logger.info(f"Segment {segment_name}: stretch_scope={stretch_scope}")

        stretched_vni_pool: dict[str, Any] | None = None
        if stretched:
            stretched_vni_pool = await self._get_stretched_vni_pool()
            if stretched_vni_pool is None:
                # Falling back to a site pool would reintroduce the collision
                # this pool exists to prevent — fail instead.
                self.logger.error(
                    f"Segment {segment_name}: stretched VNI pool '{STRETCHED_VNI_POOL_NAME}' not found — "
                    "load data/bootstrap/18_vni_pools.yml"
                )
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
                deployment_id = getattr(getattr(existing, "deployment", None), "id", None)
                if deployment_id and deployment_id not in existing_by_deployment_id:
                    existing_by_deployment_id[deployment_id] = existing
            if stretched:
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
                vni_pool=stretched_vni_pool if stretched else dep.get("vni_pool"),
                existing_deployment=existing_by_deployment_id.get(dep_id),
                reusable_vni=reusable_vni,
                stretch_scope=stretch_scope,
            )
            if not success:
                failed_deployments.append(dep_name)

        if failed_deployments:
            self.logger.error(
                f"Segment {segment_name}: activation failed for deployments {failed_deployments}. "
                "State may be partially applied."
            )

        await self._assign_to_deployment_interfaces(segment, target_deployments, stretched=stretch_scope != "local")
        await self._create_inline_sub_interfaces(segment, target_deployments)

    async def _ensure_security_zone(self, segment_id: str, segment_name: str, environment: str) -> None:
        """Assign this segment's macro trust classification, derived from its
        own `environment`. Unlocks the cross-zone branch in
        RulesPlanner.zone_context/pick_profile_name (generators/helpers/rules.py),
        which application_security.py already calls unconditionally but which
        is otherwise permanently inert — no segment ever carried security_zone.
        """
        zone_name = RulesPlanner.pick_zone_name(environment)
        zone_obj = await self._get_or_create_by_name(
            kind=SecurityZone,
            name=zone_name,
            create_data={"name": zone_name, **RulesPlanner.zone_seed(zone_name)},
            created_log="Created security zone: %s",
        )
        if zone_obj is None:
            self.logger.warning(f"Segment {segment_name}: could not get-or-create security zone {zone_name}")
            return
        zone_id = zone_obj.id

        try:
            segment_obj = await self.client.create(
                kind=ManagedVxlanSegment,
                data={"id": segment_id, "security_zone": {"id": zone_id}},
            )
            await segment_obj.save(allow_upsert=True)
        except Exception as exc:
            self.logger.warning(f"Segment {segment_name}: failed to assign security_zone {zone_name}: {exc}")

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
        vni_pool: dict[str, Any] | None = None,
        existing_deployment: Any | None = None,
        reusable_vni: int | None = None,
        stretch_scope: str = "local",
    ) -> bool:
        """Create or upsert one SegmentDeployment record (VNI-and-status only —
        local VLAN ID realization is per VLAN domain, see
        _assign_segment_to_dc_interfaces/ManagedVlanDomainSegment).

        VNI is allocated from vni_pool, a {"id": ..., "name": ...} dict: for a
        local segment the deployment's own pool, read straight from the
        vxlan_segment query's TopologySegmentHosting parent fragment (see
        queries/topology/add/vxlan_segment.gql); for a stretched one the
        global GLOBAL-L2VNI pool, used only by the first deployment — the
        rest reuse its VNI. A local segment never reuses another site's VNI.
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

        stretched = stretch_scope != "local"
        if not stretched:
            reusable_vni = None
        elif reusable_vni is None:
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

        # --- VNI ---
        # A stretched segment must carry the same VNI in every site so that
        # EVPN type-2/3 routes stitch correctly across DCI: reuse the VNI the
        # first site drew from the global pool. A local segment has no such
        # constraint and always draws from its own site's pool.
        vni_from_pool: dict[str, Any] | None = None
        vni_literal: int | None = reusable_vni
        if vni_literal is not None:
            self.logger.info(f"  [{deployment_name}] Reusing VNI {vni_literal} for {segment_name}")
        else:
            # Local segment, or the first site to activate a stretched one
            vni_pool_id = (vni_pool or {}).get("id")
            if vni_pool_id:
                # Local segments use per-deployment identifiers; stretched fallback
                # keeps one identifier to converge to a shared VNI.
                vni_identifier = (
                    f"{segment_id}-{deployment_id}-vni" if stretch_scope == "local" else f"{segment_id}-vni"
                )
                vni_from_pool = {"from_pool": {"id": vni_pool_id}, "identifier": vni_identifier}
                self.logger.info(f"  [{deployment_name}] Allocating VNI from pool {(vni_pool or {}).get('name')}")
            else:
                self.logger.warning(
                    f"  [{deployment_name}] No vni_pool — VXLAN segment {segment_name} will not have L2 VNI allocated"
                )

        # --- Create SegmentDeployment with pool-allocated values ---
        activation_data: dict[str, Any] = {
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
                f"(segment={segment_name}, vni={'from_pool' if vni_from_pool else 'none'})"
            )
            return True
        except Exception as exc:
            self.logger.error(f"  [{deployment_name}] Failed to create SegmentDeployment for {segment_name}: {exc}")
            return False

    async def _assign_to_deployment_interfaces(
        self, segment: dict[str, Any], target_deployments: list[dict[str, Any]], stretched: bool = False
    ) -> None:
        """For a stretched segment, realize its LOCAL VLAN ID on every border
        gateway of each deployment — see _realize_border_gateways. A local
        segment has no border-gateway concern and does nothing here;
        customer-port assignment for either kind comes from
        AppComponent.instances, not from here (see
        generators/topology/app_instance_segment.py)."""
        if not stretched:
            return
        segment_id: str = segment.get("id", "")
        segment_name: str = segment.get("name", "")
        if not segment_id or not target_deployments:
            return

        for dep in target_deployments:
            dep_id: str = dep.get("id", "")
            dep_name: str = dep.get("name", dep_id)
            if not dep_id:
                continue
            await self._realize_border_gateways(
                segment_id=segment_id,
                segment_name=segment_name,
                deployment_name=dep_name,
                device_deployment_ids=self._device_deployment_ids(dep),
            )

    @staticmethod
    def _device_deployment_ids(hosting_parent: dict[str, Any]) -> list[str]:
        """Deployment ids whose devices a segment can land on: the hosting
        parent itself (border leaves, a metro's edges) plus, for a DC, each of
        its pods — leafs and ToRs are deployed into the pod, not the DC. The
        query selects ids only on TopologyPod children (vxlan_segment.gql)."""
        pod_ids = [child["id"] for child in hosting_parent.get("children") or [] if child.get("id")]
        return [hosting_parent["id"], *pod_ids]

    async def _realize_border_gateways(
        self,
        segment_id: str,
        segment_name: str,
        deployment_name: str,
        device_deployment_ids: list[str],
    ) -> None:
        """A stretched segment's EVPN Multi-Site border gateways must carry its
        LOCAL VLAN ID even though they have no customer port for it: an EVPN
        Multi-Site BGW only re-originates a VNI it has configured (NX-OS needs
        the `vlan`/`vn-segment` pair plus the NVE member), and the VLAN domain
        segment is what the transform renders that from. Realizes one
        ManagedVlanDomainSegment per distinct VLAN domain touched; never
        touches interface_capabilities — border gateways have no
        customer-facing role to tag.
        """
        devices = await self.client.filters(
            kind=DcimPhysicalDevice,
            deployment__ids=device_deployment_ids,
            role__values=sorted(_BORDER_GATEWAY_ROLES),
            include=["capabilities"],
        )
        if not devices:
            self.logger.debug(f"  [{deployment_name}] No border-gateway devices — skipping")
            return

        domain_pools = await self._realize_segment_on_devices(segment_id, segment_name, devices)
        self.logger.info(
            f"  [{deployment_name}] Realized segment '{segment_name}' on {len(devices)} border gateway(s) "
            f"across {len(domain_pools)} VLAN domain(s)"
        )

    async def _ensure_inline_vlan_id(self, ha_node: dict[str, Any], segment_name: str) -> int | None:
        """Allocate (or reuse) this segment's local VLAN ID on the HA pair's
        own inline_vlan_id/inline_vlan_pool — lazily created here on first
        use. Both HA peers' sub-interfaces are created with this same
        literal value, independent of the leaf/MLAG VLAN domain mechanism
        (a firewall/LB HA pair is its own L2 domain). One HA pair supports
        exactly one inline-terminated segment at a time (single vlan_id slot).

        Uses client.create(data={"id": ..., "from_pool": ...}) + save(allow_upsert=True),
        not client.get()+mutate-attribute+save() — from_pool reassignment on an
        already-fetched node's attribute wrapper is broken in the SDK for Number
        attrs (nests under "value", server rejects BigInt); passing the same shape
        through create()'s data dict with an explicit id serializes correctly.
        """
        ha_id: str = ha_node.get("id", "")
        if not ha_id:
            self.logger.warning(f"Segment '{segment_name}' inline_service missing id — cannot allocate VLAN ID")
            return None

        # The pool is this segment's and is upserted on every run (by name) so
        # the run keeps it; an unsaved pool is deleted with the run's leftovers.
        pool_obj = await self.upsert_number_pool(
            pool_name=f"{ha_id}-inline-vlan-pool",
            description=f"Local VLAN ID pool for inline-terminated segments on HA {ha_id}",
            start_range=CUSTOMER_VLAN_ID_MIN,
            end_range=CUSTOMER_VLAN_ID_MAX,
            node="ManagedHA",
            node_attribute="inline_vlan_id",
        )
        existing_vlan_id = (ha_node.get("inline_vlan_id") or {}).get("value")
        if existing_vlan_id:
            return existing_vlan_id

        try:
            node = await self.client.create(
                kind="ManagedHA",
                data={
                    "id": ha_id,
                    "inline_vlan_pool": {"id": pool_obj.id},
                    "inline_vlan_id": {"from_pool": {"id": pool_obj.id}, "identifier": f"{ha_id}-inline-vlan"},
                },
            )
            # update_group_context=False: the HA pair belongs to the deployment
            # generator; tracking it here would delete it on the next run that
            # reuses the allocated VLAN ID.
            await node.save(allow_upsert=True, update_group_context=False)
        except Exception as exc:
            self.logger.error(f"Failed to allocate inline VLAN ID for HA '{ha_id}': {exc}")
            return None

        return getattr(node.inline_vlan_id, "value", None)

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

        # Inline termination's VLAN ID is independent of the leaf/MLAG VLAN
        # domain mechanism — the HA pair terminating this segment inline is
        # its own local L2 domain (both peers must agree on one tag), backed
        # by a small pool on the HA node itself (ManagedHA.inline_vlan_pool).
        vlan_id = await self._ensure_inline_vlan_id(ha_node, segment_name)
        if vlan_id is None:
            return

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

            # Find the trunk/uplink physical interface on this device — role=uplink
            # first, falling back to the first physical interface alphabetically.
            try:
                trunk_iface = await self.find_role_interface(
                    device_id=device_id, role="uplink", fallback_any_physical=True
                )
            except Exception as exc:
                self.logger.warning(f"  [{device_name}] Error fetching interfaces: {exc}")
                continue
            if trunk_iface is None:
                self.logger.warning(f"  [{device_name}] No trunk/uplink interface found — skipping")
                continue

            ip_address_data: Any = {"id": gateway["id"]} if gateway.get("id") else None

            await self.ensure_vlan_subinterface(
                device_id=device_id,
                device_name=device_name,
                trunk_iface=trunk_iface,
                vlan_id_value=vlan_id,
                capability_obj=segment_obj,
                ip_address_id=ip_address_data["id"] if ip_address_data else None,
            )
        return None
