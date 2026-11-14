"""Generator for VXLAN segment deployment activations (add_vxlan_segment). Addresses a gateway-less segment, creates a
ManagedSegmentDeployment with a VNI per hosting node, then reconciles port tags and VLAN rows. A local segment draws its
VNI from its parent's vni_pool; a stretched one draws ONE from GLOBAL-L2VNI.
"""

from __future__ import annotations

from typing import Any

from infrahub_sdk.protocols import CoreNumberPool, CoreStandardGroup

from generators.helpers.routing_tag import ensure_segment_routing_tag
from generators.helpers.security_tag import ensure_segment_tag
from generators.mixins.common import CommonGenerator
from generators.mixins.connections import CablingMixin
from generators.mixins.pools import PoolMixin
from generators.mixins.segment_firewall import SegmentPolicyMixin
from generators.protocols import (
    ManagedSegmentDeployment,
)
from generators.segments.segment_addressing import SegmentAddressingMixin
from generators.segments.segment_ports import SegmentPortTagMixin
from utils.data_cleaning import clean_data
from utils.limits import GROUP_SEGMENT_DEPLOYMENTS, STRETCHED_VNI_POOL


class VxlanSegmentGenerator(
    PoolMixin, CablingMixin, SegmentAddressingMixin, SegmentPortTagMixin, SegmentPolicyMixin, CommonGenerator
):
    """VXLAN segment generator: allocates a VNI per deployment and reconciles the segment's VLAN rows."""

    graphql_root_key = "ManagedVxlanSegment"
    _deployments_group: Any = None

    async def _get_stretched_vni_pool(self) -> dict[str, Any] | None:
        """Return the global stretched-segment VNI pool as {"id", "name"}, or None."""
        try:
            pool = await self.client.get(kind=CoreNumberPool, name__value=STRETCHED_VNI_POOL, raise_when_missing=False)
        except Exception as exc:
            self.logger.warning(f"Error looking up VNI pool {STRETCHED_VNI_POOL}: {exc}")
            return None
        if pool is None:
            return None
        return {"id": pool.id, "name": STRETCHED_VNI_POOL}

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

        await self.ensure_segment_addressing(segment)
        # Its default-deny policy, created with the segment: a segment is closed before any rule opens it.
        await self.ensure_segment_policy(segment)
        # The segment's group tag (routed tenant segment only), before any segment lock: the helper takes its own.
        await ensure_segment_tag(self.client, self.resource_lock, segment=segment)
        # And its routing tag (the number of its segment community), same segments, one write when absent.
        await ensure_segment_routing_tag(self.client, segment=segment)

        target_deployments = self._resolve_target_deployments(segment, segment_name)
        if not target_deployments:
            self.logger.error(f"Segment {segment_name}: could not resolve any hosting parent — cannot proceed")
            return

        # The DC may still be bootstrapping (no vni_pool yet): wait for it and re-resolve rather than fail.
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
        # A shared segment (one VLAN id everywhere, derived from its VNI) draws the ONE global VNI too, even in one DC.
        stretched = stretch_scope != "local" or (segment.get("vlan_scope") or "local") == "shared"
        self.logger.info(f"Segment {segment_name}: stretch_scope={stretch_scope}")

        stretched_vni_pool: dict[str, Any] | None = None
        if stretched:
            stretched_vni_pool = await self._get_stretched_vni_pool()
            if stretched_vni_pool is None:
                # A site pool would reintroduce the collision: fail instead.
                self.logger.error(
                    f"Segment {segment_name}: stretched VNI pool '{STRETCHED_VNI_POOL}' not found — "
                    "load data/bootstrap/18_vni_pools.yml"
                )
                return

        self.logger.info(
            f"Segment {segment_name} will be activated in "
            f"{len(target_deployments)} deployment(s): "
            f"{[d.get('name', d.get('id')) for d in target_deployments]}"
        )

        # The one read of existing deployments (idempotency and stretched-VNI reuse); a failure is fatal.
        try:
            existing_deployments = await self.client.filters(
                kind=ManagedSegmentDeployment,
                segment__ids=[segment_id],
                include=["member_of_groups"],
            )
        except Exception as exc:
            self.fail(f"Segment {segment_name}: cannot read its existing SegmentDeployments: {exc}")
        existing_by_deployment_id: dict[str, Any] = {}
        for existing in existing_deployments:
            deployment_id = getattr(getattr(existing, "deployment", None), "id", None)
            if deployment_id and deployment_id not in existing_by_deployment_id:
                existing_by_deployment_id[deployment_id] = existing
        # Only a stretched segment shares a VNI across deployments.
        reusable_vni = self._extract_existing_vni(existing_deployments) if stretched else None

        # Create/upsert SegmentDeployment per deployment
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
                # Read back the stretched VNI just drawn so the remaining deployments reuse it.
                reusable_vni = await self._read_segment_vni(segment_id)

        if failed_deployments:
            self.logger.error(
                f"Segment {segment_name}: activation failed for deployments {failed_deployments}. "
                "State may be partially applied."
            )

        # LOCAL VLAN ID per device: reconciled from scratch, written untracked (SegmentPortTagMixin).
        await self.reconcile_vxlan_segment(segment_id, segment_name)

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
        """Resolve a customer footprint to the TopologySegmentHosting node that hosts its segments: its parent (DC /
        metro), or the office itself.
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

    async def _segment_deployments_group(self) -> Any:
        """The group every segment deployment joins (GROUP_SEGMENT_DEPLOYMENTS), read once per run."""
        if self._deployments_group is None:
            self._deployments_group = await self.client.get(
                kind=CoreStandardGroup, name__value=GROUP_SEGMENT_DEPLOYMENTS
            )
        return self._deployments_group

    async def _join_segment_deployments(self, deployment: Any) -> None:
        """Add an existing deployment to GROUP_SEGMENT_DEPLOYMENTS if not a member (one RelationshipAdd keeps its other
        groups).
        """
        if any(peer.hfid == [GROUP_SEGMENT_DEPLOYMENTS] for peer in deployment.member_of_groups.peers):
            return
        group = await self._segment_deployments_group()
        await deployment.add_relationships(relation_to_update="member_of_groups", related_nodes=[group.id])

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
        """Create or upsert one SegmentDeployment record (VNI and status only); from_pool is called only for a new
        deployment. ``vni_pool`` is a {"id", "name"} dict; ``reusable_vni`` is the VNI a stretched segment already
        carries.
        """
        # --- Check idempotency first ---
        if existing_deployment:
            self.logger.info(f"  [{deployment_name}] SegmentDeployment already exists for {segment_name} — skipping")
            try:
                await existing_deployment.save(allow_upsert=True)  # register with tracker
                await self._join_segment_deployments(existing_deployment)
            except Exception as exc:
                # Not registered, the record would be deleted by this run's cleanup and take its VLAN rows with it.
                self.fail(f"  [{deployment_name}] Failed to re-save existing activation: {exc}")
            return True

        # --- VNI ---
        # A stretched segment must carry the same VNI in every site (EVPN type-2/3 stitching); a local one draws from its own pool.
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
            # The target group of add_segment_deployment_ports: a port assigned to the deployment re-runs the reconcile.
            "member_of_groups": [{"id": (await self._segment_deployments_group()).id}],
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
