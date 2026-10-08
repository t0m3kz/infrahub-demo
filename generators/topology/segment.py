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
  4. Reconcile the segment's LOCAL VLAN ID activations (one per VLAN domain
     that needs it: every switch carrying it on a port, plus — for a
     STRETCHED segment — every border gateway of each deployment, the EVPN
     Multi-Site BGW plumbing: a BGW only re-originates a VNI it has
     configured, and the activation is what the transform renders that
     from). See VlanDomainMixin.reconcile_segment_vlan_domains (shared
     with app_instance_segment.py, written untracked). Customer-facing
     interface assignment is NOT
     done here: it is driven by AppComponent.instances, one physical device
     at a time, in generators/topology/app_instance_segment.py — a segment
     with no AppComponent referencing it gets no customer-port assignment at
     all. Independent VLAN domains may reuse the same numeric VLAN ID, since
     IEEE 802.1Q VLAN ID has only local significance (unlike VNI, which is
     the real DC-wide/fabric-wide segment identifier).
  5. Terminate the segment inline when terminate_inline is set: tag the
     border-leaf ports facing the inline_service HA pair, then give each
     member a <port>.<vlan> sub-interface with its own address (the gateway
     is the pair's virtual IP).

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

from ipaddress import ip_network
from typing import Any

from infrahub_sdk.protocols import CoreNumberPool

from utils.data_cleaning import clean_data

from ..common import CommonGenerator
from ..connections import CablingMixin
from ..helpers.rules import RulesPlanner
from ..named_objects import GetOrCreateByNameMixin
from ..pools import PoolMixin
from ..protocols import (
    DcimCable,
    DcimPhysicalDevice,
    DcimPhysicalInterface,
    DcimVirtualInterface,
    ManagedSegmentDeployment,
    ManagedVlanDomainSegment,
    ManagedVxlanSegment,
    SecurityZone,
)
from ..vlan_domain import SERVICE_PORT_ROLES, VlanDomainMixin, segment_lock_key

# Bootstrap pool (data/bootstrap/18_vni_pools.yml) every stretched segment's
# single VNI comes from — disjoint from the per-site {fabric}-vni-pool band.
STRETCHED_VNI_POOL_NAME = "GLOBAL-L2VNI"


class VxlanSegmentGenerator(GetOrCreateByNameMixin, PoolMixin, CablingMixin, VlanDomainMixin, CommonGenerator):
    """VXLAN segment generator — allocates a VNI from the DC's pool,
    reconciles the segment's LOCAL VLAN ID activations (border gateways of a
    stretched segment included), and creates inline sub-interfaces when
    terminate_inline is set. Customer-facing interface assignment is not done here — see
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

        # LOCAL VLAN ID per VLAN domain: shared desired state of the segment
        # (both this generator and add_app_component_segment reach it),
        # reconciled from scratch and written untracked — see
        # VlanDomainMixin.reconcile_segment_vlan_domains.
        # Inline termination tags the border-leaf ports facing the HA pair
        # first, so the reconciliation gives the segment a VLAN in their
        # domains; the HA members' sub-interfaces then take that VLAN.
        inline_legs = await self._reconcile_inline_service_ports(segment)
        await self.reconcile_segment_vlan_domains(segment_id, segment_name)
        await self._create_inline_sub_interfaces(segment, inline_legs)

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
            # Shared by every segment of the environment: no segment owns it.
            track=False,
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
            # update_group_context=False: this is the run's own TARGET. A
            # tracked save puts it in the run's group, and a later run that
            # skips this save (zone lookup failed) would delete the segment.
            await segment_obj.save(allow_upsert=True, update_group_context=False)
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
        VlanDomainMixin.reconcile_segment_vlan_domains/ManagedVlanDomainSegment).

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

    async def _inline_legs(self, segment: dict[str, Any]) -> list[dict[str, Any]]:
        """Every (HA member port, border-leaf service port) cable of the
        segment's inline_service pair — where the segment terminates.

        A member port counts when its cable lands on a border-leaf port whose
        role is a service role (firewall/load-balancer, BORDER_ROLE_FOR_SERVICES):
        in pbr mode that is the firewall's "uplink", in inline mode the chain
        end toward the border leaf. A border port that already parents routed
        sub-interfaces (pbr mode's FirewallContext P2P legs) cannot also be the
        segment's L2 trunk, so that leg is an error and is skipped.
        """
        segment_name: str = segment.get("name", "")
        inline_service = segment.get("inline_service") or {}
        if not inline_service.get("id"):
            self.logger.warning(f"Segment '{segment_name}' has terminate_inline=true but no inline_service")
            return []
        member_names = {
            cap["id"]: cap.get("name") or cap["id"] for cap in inline_service.get("capabilities") or [] if cap.get("id")
        }
        member_ids = list(member_names)
        if not member_ids:
            self.logger.warning(f"Segment '{segment_name}' inline_service has no member devices")
            return []

        member_ports = await self.client.filters(kind=DcimPhysicalInterface, device__ids=member_ids, include=["cable"])
        cabled = [port for port in member_ports if getattr(getattr(port, "cable", None), "id", None)]
        cable_ids = sorted({port.cable.id for port in cabled})
        cables = (
            {
                cable.id: cable
                for cable in await self.client.filters(kind=DcimCable, ids=cable_ids, include=["endpoints"])
            }
            if cable_ids
            else {}
        )
        far_ends: dict[str, list[str]] = {}
        for port in cabled:
            cable = cables.get(port.cable.id)
            peers = getattr(cable, "endpoints").peers if cable is not None else []
            far_ends[port.id] = [peer.id for peer in peers if peer.id != port.id]
        far_ids = sorted({far_id for ids in far_ends.values() for far_id in ids})
        border_ports = {
            iface.id: iface
            for iface in (await self.client.filters(kind=DcimPhysicalInterface, ids=far_ids) if far_ids else [])
            if iface.role.value in SERVICE_PORT_ROLES
        }
        if not border_ports:
            self.logger.error(
                f"Segment '{segment_name}': no inline_service member port is cabled to a border-leaf service port"
            )
            return []

        routed = await self.client.filters(kind=DcimVirtualInterface, parent_interface__ids=sorted(border_ports))
        routed_parents = {getattr(sub, "parent_interface").id for sub in routed}
        border_device_ids = sorted({port.device.id for port in border_ports.values()})
        border_devices = {
            device.id: device
            for device in await self.client.filters(
                kind=DcimPhysicalDevice, ids=border_device_ids, include=["capabilities"]
            )
        }

        legs: list[dict[str, Any]] = []
        for port in sorted(cabled, key=lambda p: (p.device.id, p.name.value)):
            for far_id in far_ends[port.id]:
                border_port = border_ports.get(far_id)
                if border_port is None:
                    continue
                if far_id in routed_parents:
                    self.logger.error(
                        f"Segment '{segment_name}': border port {border_port.name.value} is a routed parent "
                        f"(pbr-mode FirewallContext legs) — it cannot also carry the segment; use connectivity_mode inline"
                    )
                    continue
                legs.append(
                    {
                        "member_port": port,
                        "member_device_id": port.device.id,
                        "member_device_name": member_names.get(port.device.id, port.device.id),
                        "border_port_id": far_id,
                        "border_device": border_devices[border_port.device.id],
                    }
                )
        return legs

    async def _reconcile_inline_service_ports(self, segment: dict[str, Any]) -> list[dict[str, Any]]:
        """Make the segment's border-leaf service-port tags exactly the ports
        facing its inline_service members — none unless terminate_inline —
        and return those legs. The tags put the border leaves' VLAN domains
        in scope (reconcile_segment_vlan_domains then allocates the VLAN the
        HA members' sub-interfaces take) and make the border leaf render the
        port as a trunk carrying it. Written with RelationshipAdd/Remove,
        untracked, under the segment lock; customer-facing tags are
        app_instance_segment.py's and are left alone.
        """
        segment_id: str = segment.get("id", "")
        segment_name: str = segment.get("name", "")
        legs = await self._inline_legs(segment) if segment.get("terminate_inline") else []
        desired = {leg["border_port_id"] for leg in legs}

        async with self.resource_lock(segment_lock_key(segment_id)):
            state = await self._fetch_segment_vlan_state(segment_id)
            current = self.tagged_interface_ids(state["segment"], service_ports=True) if state else set()
            to_add = sorted(desired - current)
            to_remove = sorted(current - desired)
            if to_add or to_remove:
                segment_obj = await self.client.get(kind=ManagedVxlanSegment, id=segment_id)
                if to_add:
                    await segment_obj.add_relationships(
                        relation_to_update="interface_capabilities", related_nodes=to_add
                    )
                if to_remove:
                    await segment_obj.remove_relationships(
                        relation_to_update="interface_capabilities", related_nodes=to_remove
                    )
        if to_add or to_remove:
            self.logger.info(
                f"Segment {segment_name}: border service ports — tagged {len(to_add)}, untagged {len(to_remove)}"
            )
        return legs

    async def _create_inline_sub_interfaces(self, segment: dict[str, Any], legs: list[dict[str, Any]]) -> None:
        """One <member port>.<vlan> DcimVirtualInterface per inline leg,
        carrying the segment, owned by this run (dropped legs are cleaned up
        with it).

        - VLAN: the segment's VLAN ID in the facing border leaf's VLAN domain,
          so both ends of the cable tag it the same.
        - Address: the member's OWN address, reserved from the segment's
          prefix (per-port identifier, so a re-run keeps it). The segment
          gateway is the HA pair's virtual IP and is never put on a member —
          the device transforms render it per vendor (floating/standby/VRRP).
        """
        if not legs:
            return
        segment_id: str = segment.get("id", "")
        segment_name: str = segment.get("name", "")

        prefix = ((segment.get("gateway") or {}).get("ip_prefix")) or {}
        pool = None
        prefix_length = 0
        if prefix.get("id") and prefix.get("prefix"):
            prefix_length = ip_network(prefix["prefix"], strict=False).prefixlen
            pool = await self.ensure_prefix_address_pool(
                pool_name=f"inline-{segment_id}-pool",
                prefix_id=prefix["id"],
                prefix_length=prefix_length,
                namespace_id=(prefix.get("ip_namespace") or {}).get("id"),
            )
        else:
            self.logger.warning(f"Segment '{segment_name}' has no gateway prefix — sub-interfaces get no address")

        vlan_by_domain = {
            activation.vlan_domain.id: activation.vlan_id.value
            for activation in await self.client.filters(
                kind=ManagedVlanDomainSegment, segment__ids=[segment_id], include=["vlan_domain"]
            )
        }
        segment_obj = await self.client.get(kind=ManagedVxlanSegment, id=segment_id)

        for leg in legs:
            port = leg["member_port"]
            device_id: str = leg["member_device_id"]
            domain_id, _pool_id = await self._resolve_device_vlan_domain(leg["border_device"])
            vlan_id = vlan_by_domain.get(domain_id)
            if vlan_id is None:
                self.logger.error(
                    f"Segment '{segment_name}': no VLAN in border leaf {leg['border_device'].name.value}'s "
                    f"VLAN domain — cannot terminate it on {port.name.value}"
                )
                continue
            ip_id = None
            if pool is not None:
                ip_id = await self.allocate_prefix_address(
                    pool=pool,
                    identifier=f"{segment_id}-{port.id}-inline",
                    prefix_length=prefix_length,
                    description=f"Inline {segment_name} — {port.name.value}",
                )
            await self.ensure_vlan_subinterface(
                device_id=device_id,
                device_name=leg["member_device_name"],
                trunk_iface=port,
                vlan_id_value=vlan_id,
                capability_obj=segment_obj,
                ip_address_id=ip_id,
            )
