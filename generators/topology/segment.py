"""Generator for VXLAN segment deployment activations.

VlanSegment has no generator — its vlan_id is a plain manual attribute
(single-site, no pool allocation, no SegmentDeployment realization record).
Overlap validation across VlanSegments on the same fabric is a device check,
not generator logic.

VxlanSegmentGenerator handles:
  0. Address a segment without a gateway: a prefix from the tenant pool of
     its environment's VRF namespace (Tenant-<namespace>-IPv4, keyed by the
     segment name) and the anycast gateway on its .1, once
     (ensure_segment_addressing). An authored gateway is kept.
  1. Determine target customer deployments from segment.customer_deployments
  2. Resolve each customer deployment's hosting node — the VNI pool and
     SegmentDeployment record live on the TopologySegmentHosting node: the
     parent (TopologyDataCenter or TopologyColocationMetro) of a DC or
     colocation footprint, the footprint itself for an office (TopologyCustomerOffice).
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

from infrahub_sdk.protocols import CoreIPPrefixPool, CoreNumberPool, IpamNamespace

from utils.data_cleaning import clean_data
from utils.exchange_transit import OFFSET_ANYCAST, namespace_type_for_environment

from ..common import CommonGenerator
from ..connections import CablingMixin
from ..far_end import far_end_interfaces
from ..helpers.routing_tag import ensure_segment_routing_tag
from ..helpers.security_tag import ensure_segment_tag
from ..helpers.security_zone import ensure_security_zone
from ..pools import PoolMixin
from ..protocols import (
    DcimPhysicalDevice,
    DcimPhysicalInterface,
    DcimVirtualInterface,
    IpamPrefix,
    ManagedSegmentDeployment,
    ManagedVlanDomainSegment,
    ManagedVxlanSegment,
)
from ..vlan_domain import SERVICE_PORT_ROLES, VlanDomainMixin, segment_lock_key

# Bootstrap pool (data/bootstrap/18_vni_pools.yml) every stretched segment's
# single VNI comes from — disjoint from the per-site {fabric}-vni-pool band.
STRETCHED_VNI_POOL_NAME = "GLOBAL-L2VNI"


def tenant_pool_name(namespace_name: str) -> str:
    """The bootstrap prefix pool tenant segments of VRF namespace ``namespace_name`` are addressed from
    (data/bootstrap/23b_tenant_pools.yml); its default_prefix_length is the segment's prefix length."""
    return f"Tenant-{namespace_name}-IPv4"


class VxlanSegmentGenerator(PoolMixin, CablingMixin, VlanDomainMixin, CommonGenerator):
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

    async def _read_segment_vni(self, segment_id: str) -> int | None:
        """The VNI already allocated to any deployment of the segment, or None."""
        deployments = await self.client.filters(kind=ManagedSegmentDeployment, segment__ids=[segment_id])
        return self._extract_existing_vni(deployments)

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

        # Before the zone and the tag: the routed-tenant test of the tag reads
        # the gateway's namespace, so a gateway written after it would leave
        # the first run untagged.
        await self.ensure_segment_addressing(segment)

        # The segment's macro trust zone (PROD-ZONE/NONPROD-ZONE) from its own
        # environment; unlocks the cross-zone branch in RulesPlanner.zone_context.
        await ensure_security_zone(
            self.client,
            kind=segment.get("typename"),
            segment_id=segment_id,
            environment=segment.get("environment"),
            current_zone=(segment.get("security_zone") or {}).get("name"),
        )
        # The segment's group tag (a routed tenant segment only), before any
        # segment lock is taken: the helper takes its own.
        await ensure_segment_tag(self.client, self.resource_lock, segment=segment)
        # And its routing tag (the number of its segment community), same segments, one write when absent.
        await ensure_segment_routing_tag(self.client, segment=segment)

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
                refreshed = await self.wait_for_parent_generator_and_refetch(
                    ("add_dc", "dc_pod_cascade", "add_colocation_metro", "add_office_fabric"), dep_id
                )
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

        # The one read of the segment's existing deployments: it answers both
        # "which deployments already exist" (idempotency) and "which VNI does a
        # stretched segment already carry" (reuse). A failure is fatal: creating
        # on a blind read would allocate a second VNI / duplicate the records.
        try:
            existing_deployments = await self.client.filters(
                kind=ManagedSegmentDeployment,
                segment__ids=[segment_id],
            )
        except Exception as exc:
            self.fail(f"Segment {segment_name}: cannot read its existing SegmentDeployments: {exc}")
        existing_by_deployment_id: dict[str, Any] = {}
        for existing in existing_deployments:
            deployment_id = getattr(getattr(existing, "deployment", None), "id", None)
            if deployment_id and deployment_id not in existing_by_deployment_id:
                existing_by_deployment_id[deployment_id] = existing
        # Only a stretched segment shares a VNI across deployments; a local one
        # always draws from its own deployment's pool.
        reusable_vni = self._extract_existing_vni(existing_deployments) if stretched else None

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
            )
            if not success:
                failed_deployments.append(dep_name)
            elif (
                stretched
                and reusable_vni is None
                and dep_id not in existing_by_deployment_id
                and dep is not target_deployments[-1]
            ):
                # This activation just drew the stretched VNI from the global
                # pool: read it back so the remaining deployments reuse that
                # value instead of drawing a second one.
                reusable_vni = await self._read_segment_vni(segment_id)

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

    async def ensure_segment_addressing(self, segment: dict[str, Any]) -> None:
        """Give a gateway-less segment its prefix and anycast gateway, once; never touch an authored gateway.

        The prefix comes from the tenant pool of the segment's environment
        namespace (``p`` -> PROD, every other value -> NON-PROD, the rule that
        places every tenant VRF), at the pool's default length, allocated with
        the segment name as identifier: the name is the segment's HFID and is
        computed from owner/customer_name/environment, so it survives a delete
        and recreate, and an allocation retried after a failed write returns
        the same prefix. A segment with a gateway is skipped, so a rename never
        renumbers a live segment and a second pass allocates nothing. The
        gateway is the prefix's .1 (OFFSET_ANYCAST).

        Every write is untracked: the prefix is a pool mutation, the address is
        saved with track=False and the segment (the run's own target) with
        update_group_context=False, so no later run's cleanup deletes them. The
        lock serializes two concurrent runs of the segment, which would
        otherwise both create the gateway address. ``segment`` (the cleaned
        query dict) gets the new gateway, so the zone and tag helpers of this
        run see it.
        """
        if segment.get("gateway"):
            return
        segment_id: str = segment["id"]
        segment_name: str = segment["name"]
        environment = segment.get("environment")
        if not environment:
            self.logger.error(f"Segment {segment_name} has no environment: its address pool cannot be derived")
            return
        namespace_type = namespace_type_for_environment(environment)
        namespaces = await self.client.filters(kind=IpamNamespace, namespace_type__value=namespace_type)
        if len(namespaces) != 1:
            self.logger.error(
                f"Segment {segment_name}: expected one IpamNamespace of type {namespace_type}, found "
                f"{len(namespaces)} - load data/bootstrap/22_namespaces.yml"
            )
            return
        namespace = namespaces[0]
        pool_name = tenant_pool_name(namespace.name.value)
        pools = await self.client.filters(kind=CoreIPPrefixPool, name__value=pool_name)
        if not pools:
            self.logger.error(
                f"Segment {segment_name}: tenant pool {pool_name} not found - load data/bootstrap/23b_tenant_pools.yml"
            )
            return
        async with self.resource_lock(f"segment-addressing-{segment_id}"):
            prefix = await self.client.allocate_next_ip_prefix(
                resource_pool=pools[0],
                kind=IpamPrefix,
                identifier=segment_name,
                member_type="address",
                data={"role": "customer", "status": "active", "description": segment_name},
            )
            if prefix is None:
                self.logger.error(f"Segment {segment_name}: tenant pool {pool_name} returned no prefix")
                return
            gateway = (
                await self.upsert_prefix_addresses(
                    prefix,
                    offsets=(OFFSET_ANYCAST,),
                    description=f"{segment_name} anycast gateway",
                    track=False,
                )
            )[OFFSET_ANYCAST]
            segment_obj = await self.client.create(
                kind=segment["typename"], data={"id": segment_id, "gateway": {"id": gateway.id}}
            )
            await segment_obj.save(allow_upsert=True, update_group_context=False)
        segment["gateway"] = {
            "id": gateway.id,
            "address": gateway.address.value,
            "ip_prefix": {
                "id": prefix.id,
                "prefix": prefix.prefix.value,
                "ip_namespace": {"id": namespace.id, "name": namespace.name.value, "namespace_type": namespace_type},
            },
        }
        self.logger.info(f"Segment {segment_name}: gateway {gateway.address.value} from {pool_name}")

    def _resolve_target_deployments(self, segment: dict[str, Any], segment_name: str) -> list[dict[str, Any]]:
        """Resolve segment.customer_deployments to their hosting nodes (see _resolve_hosting_parent)."""
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
        """Resolve a customer footprint to the TopologySegmentHosting node that hosts its segments.

        VLAN/VNI pools and SegmentDeployment records live on that node: the
        footprint's parent (TopologyDataCenter / TopologyColocationMetro) for a
        CustomerDC/CustomerColocation, the footprint itself for a
        CustomerOffice, which is SegmentHosting in its own right (its parent
        is only the "Campus and Office" label, which hosts nothing). The query selects
        ``vni_pool`` on the footprint only where the footprint hosts itself —
        its presence is the signal, whether the pool is set yet or not — the
        same rule as transforms/helpers/segments.py's segment_hosting_candidates.
        Either way the node must be in the GraphQL response (the ``... on
        TopologyCustomer*`` fragments of the vxlan_segment query) — no fallback fetch.
        """
        cust_id: str = customer_deployment.get("id", "")
        cust_name: str = customer_deployment.get("name", cust_id)
        if not cust_id:
            self.logger.warning("Customer deployment entry missing id — skipping")
            return None

        if "vni_pool" in customer_deployment:
            return customer_deployment

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
        Idempotency: the caller passes the deployment's existing
        SegmentDeployment, if any (generate() reads them all once) — from_pool
        is only called for genuinely new deployments. reusable_vni is the VNI a
        stretched segment already carries (None for a local one, and for the
        first stretched deployment, which draws it from vni_pool).
        """
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
                vni_from_pool = {"from_pool": {"id": vni_pool_id}}
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
        # A cable has exactly two endpoints (min_count = max_count = 2): the far end of a member port is one port.
        far_end_by_port = await far_end_interfaces(self.client, cabled, include=[])
        border_ports = {
            far_end.id: far_end for far_end in far_end_by_port.values() if far_end.role.value in SERVICE_PORT_ROLES
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
            border_port = far_end_by_port.get(port.id)
            if border_port is None or border_port.id not in border_ports:
                continue
            if border_port.id in routed_parents:
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
                    "border_port_id": border_port.id,
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
                    await segment_obj.add_relationships(relation_to_update="capabilities", related_nodes=to_add)
                if to_remove:
                    await segment_obj.remove_relationships(relation_to_update="capabilities", related_nodes=to_remove)
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

        domain_by_device = await self._resolve_vlan_domains([leg["border_device"] for leg in legs])
        for leg in legs:
            port = leg["member_port"]
            device_id: str = leg["member_device_id"]
            domain_id = domain_by_device[leg["border_device"].id]
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
                capability_ids=[segment_id],
                ip_address_id=ip_id,
            )
