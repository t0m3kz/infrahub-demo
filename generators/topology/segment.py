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
  4. Assign the segment to leaf/tor customer-facing interfaces, allocating a
     LOCAL VLAN ID per VLAN domain (MLAG pair or standalone device) —
     independent domains may reuse the same numeric VLAN ID, since IEEE
     802.1Q VLAN ID has only local significance (unlike VNI, which is the
     real DC-wide/fabric-wide segment identifier).
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
    DcimPhysicalInterface,
    ManagedSegmentDeployment,
    ManagedStandaloneVlanDomain,
    ManagedVlanDomainSegment,
    ManagedVxlanSegment,
    SecurityZone,
)

# Devices whose customer-facing ports a segment is offered on. `edge` is the
# colocation metro's on-ramp router (generators/topology/colocation.py) — the
# only VTEP a metro has, so its customer ports are where a stretched segment
# lands in the colocation.
_ACCESS_VTEP_ROLES = frozenset({"leaf", "tor", "l2-leaf", "access-leaf", "edge"})
# Devices that act as a deployment's EVPN Multi-Site border gateway for a
# stretched segment: the DC's border leaves and the metro's edges.
_BORDER_GATEWAY_ROLES = frozenset({"border-leaf", "edge"})
# Bootstrap pool (data/bootstrap/18_vni_pools.yml) every stretched segment's
# single VNI comes from — disjoint from the per-site {fabric}-vni-pool band.
STRETCHED_VNI_POOL_NAME = "GLOBAL-L2VNI"


class VxlanSegmentGenerator(GetOrCreateByNameMixin, PoolMixin, CablingMixin, CommonGenerator):
    """VXLAN segment generator — allocates a VNI from the DC's pool, assigns
    the segment to leaf/tor customer-facing interfaces and physical host uplinks
    (allocating a LOCAL VLAN ID per VLAN domain — MLAG pair or standalone
    device — as it goes), and creates inline sub-interfaces when
    terminate_inline is set.
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
                deployment_rel = getattr(existing, "deployment", None)
                if deployment_rel is not None:
                    await deployment_rel.fetch()
                deployment_peer = getattr(deployment_rel, "peer", None)
                deployment_obj = deployment_peer or deployment_rel
                deployment_id = getattr(deployment_obj, "id", None)
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
        """Assign this segment to all leaf/tor customer-facing interfaces in each deployment.

        ``stretched``: the segment spans more than one deployment, so its EVPN
        routes cross sites through each site's border gateways — see
        _BORDER_GATEWAY_ROLES."""
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
                stretched=stretched,
            )

    async def _resolve_vlan_domain(self, device: Any) -> tuple[str, str]:
        """Return (domain_kind, domain_id) for a leaf/tor device: its
        ManagedMLAG if paired, else the device itself is its own standalone
        VLAN domain. Requires device.capabilities to already be fetched
        (batch-included by the caller to avoid an N+1 query pattern)."""
        caps = getattr(device, "capabilities", None)
        if caps is not None:
            for peer in caps.peers:
                if peer.typename == "ManagedMLAG":
                    return "ManagedMLAG", peer.id
        return "DcimPhysicalDevice", device.id

    async def _ensure_standalone_vlan_domain(self, device: Any) -> tuple[str, str]:
        """Create/upsert the ManagedStandaloneVlanDomain (and its vlan_pool)
        for a non-MLAG device, lazily — only when it's actually assigned a
        segment, avoiding speculative pool creation for idle leafs/tors.
        Returns (domain id, pool id).

        Both are upserted on every run, existing or not: the generator deletes
        what a run does not save, so a rerun that only read them back would
        delete them. Segments are generated in parallel; both upserts are keyed
        by name, so concurrent runs converge on the same domain and pool.
        """
        domain_name = f"{device.name.value}-vlan-domain"
        domain_obj = await self.client.create(
            kind=ManagedStandaloneVlanDomain,
            data={"name": domain_name, "status": "active", "capabilities": [{"id": device.id}]},
        )
        await domain_obj.save(allow_upsert=True)
        domain_id = domain_obj.id
        pool = await self.upsert_number_pool(
            pool_name=f"{domain_name}-vlan-pool",
            description=f"Local VLAN ID pool for standalone VLAN domain {domain_name}",
            start_range=CUSTOMER_VLAN_ID_MIN,
            end_range=CUSTOMER_VLAN_ID_MAX,
            node="ManagedVlanDomainSegment",
            node_attribute="vlan_id",
            parent_kind="ManagedStandaloneVlanDomain",
            parent_id=domain_id,
            parent_attr="vlan_pool",
        )
        return domain_id, pool.id

    async def _ensure_vlan_domain_segment(
        self, segment_id: str, segment_name: str, domain_id: str, pool_id: str | None = None
    ) -> None:
        """Upsert one ManagedVlanDomainSegment (segment, VLAN domain) pair,
        allocating vlan_id from that domain's own pool via from_pool.
        Idempotent: an existing record for this pair keeps its vlan_id and is
        only saved again, so this run's tracking group keeps it (an unsaved
        one is deleted). A known pool_id (a standalone domain this run just
        ensured) skips reading it back from the domain.
        """
        existing = await self.client.filters(
            kind=ManagedVlanDomainSegment,
            segment__ids=[segment_id],
            vlan_domain__ids=[domain_id],
        )
        if existing:
            await existing[0].save(allow_upsert=True)  # register with tracker
            return

        if not pool_id:
            domain = await self.client.get(kind="ManagedGenericVlanDomain", id=domain_id, include=["vlan_pool"])
            vlan_pool_rel = getattr(domain, "vlan_pool", None)
            pool_id = getattr(vlan_pool_rel, "id", None) if vlan_pool_rel else None
        if not pool_id:
            self.logger.error(
                f"VLAN domain {domain_id} has no vlan_pool — cannot allocate VLAN ID for segment {segment_name}"
            )
            return

        vlan_identifier = f"{segment_id}-{domain_id}-vlan"
        activation = await self.client.create(
            kind=ManagedVlanDomainSegment,
            data={
                "segment": {"id": segment_id},
                "vlan_domain": {"id": domain_id},
                "vlan_id": {"from_pool": {"id": pool_id}, "identifier": vlan_identifier},
            },
        )
        await activation.save(allow_upsert=True)
        self.logger.info(f"  Allocated VLAN ID from domain {domain_id}'s pool for segment {segment_name}")

    async def _assign_segment_to_dc_interfaces(
        self,
        segment_id: str,
        segment_obj: Any,
        segment_name: str,
        deployment_id: str,
        deployment_name: str,
        stretched: bool = False,
    ) -> None:
        """Find all leaf/tor/edge customer-facing interfaces in a deployment, add
        the segment to their interface_capabilities relationship (queried by the
        leaf/edge transforms), and — per distinct VLAN domain (MLAG pair or
        standalone device) touched — upsert a ManagedVlanDomainSegment realizing
        this segment's LOCAL VLAN ID in that domain.

        A stretched segment additionally gets a VLAN on every border gateway of
        the deployment even though those have no customer port for it: an EVPN
        Multi-Site BGW only re-originates a VNI it has configured (NX-OS needs
        the `vlan`/`vn-segment` pair plus the NVE member), and the VLAN domain
        segment is what the transform renders that from."""
        devices = await self.client.filters(
            kind=DcimPhysicalDevice,
            deployment__ids=[deployment_id],
            role__values=sorted(_ACCESS_VTEP_ROLES | (_BORDER_GATEWAY_ROLES if stretched else frozenset())),
            include=["capabilities"],
        )
        if not devices:
            self.logger.debug(
                f"  [{deployment_name}] No access or border-gateway devices — skipping interface assignment"
            )
            return
        border_gateways = [d for d in devices if stretched and d.role.value in _BORDER_GATEWAY_ROLES]

        device_ids = [d.id for d in devices]
        interfaces = await self.client.filters(
            kind=DcimPhysicalInterface,
            device__ids=device_ids,
            role__value="customer",
            include=["device"],
        )
        if not interfaces and not border_gateways:
            self.logger.debug(f"  [{deployment_name}] No customer/downlink interfaces — skipping")
            return

        assigned = 0
        updated = 0
        touched_device_ids: set[str] = set()
        for iface in interfaces:
            iface_services = getattr(iface, "interface_capabilities")
            await iface_services.fetch()
            existing_ids = {peer.id for peer in iface_services.peers}
            changed = False

            if segment_id not in existing_ids:
                iface_services.add(segment_obj)
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

            # device is a mandatory Parent relationship (schemas/base/dcim.yml),
            # always resolvable given include=["device"] above.
            touched_device_ids.add(iface.device.peer.id)

        # Resolve each touched device's VLAN domain (MLAG-or-standalone) and
        # upsert one ManagedVlanDomainSegment per distinct domain.
        touched_device_ids.update(d.id for d in border_gateways)
        touched_devices = [d for d in devices if d.id in touched_device_ids]
        domain_pools: dict[str, str | None] = {}
        for device in touched_devices:
            domain_kind, domain_id = await self._resolve_vlan_domain(device)
            pool_id = None
            if domain_kind == "DcimPhysicalDevice":
                domain_id, pool_id = await self._ensure_standalone_vlan_domain(device)
            domain_pools[domain_id] = domain_pools.get(domain_id) or pool_id

        for domain_id, pool_id in domain_pools.items():
            await self._ensure_vlan_domain_segment(segment_id, segment_name, domain_id, pool_id)

        self.logger.info(
            f"  [{deployment_name}] Assigned segment '{segment_name}' to {assigned} interface(s) "
            f"({len(interfaces) - assigned} already assigned, {updated} interface(s) updated) "
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
