"""Generator re-running a segment's reconcile when a port is assigned to, or withdrawn from, one of its deployments.
Started by the `updated` rule on the deployment's ``capabilities``; it writes only port -> row edges and rows, so it
raises no event of that rule.
"""

from __future__ import annotations

from typing import Any

from utils.data_cleaning import clean_data

from ..common import CommonGenerator
from ..connections import CablingMixin
from ..pools import PoolMixin
from ..protocols import ManagedVxlanSegment
from .segment_ports import SegmentPortTagMixin


class SegmentDeploymentPortsGenerator(PoolMixin, CablingMixin, SegmentPortTagMixin, CommonGenerator):
    """Reconciles the segment of a deployment whose assigned ports changed; writes untracked."""

    graphql_root_key = "ManagedSegmentDeployment"

    async def generate(self, data: dict[str, Any]) -> None:
        deployments = clean_data(data).get(self.graphql_root_key) or []
        if not deployments:
            self.logger.error(f"No {self.graphql_root_key} data in GraphQL response")
            return
        deployment = deployments[0]
        where = (deployment.get("deployment") or {}).get("name") or deployment.get("id")
        segment = deployment.get("segment") or {}
        segment_id, segment_name = segment.get("id", ""), segment.get("name", "")
        if segment.get("typename") != ManagedVxlanSegment.__name__ or not segment_id:
            self.logger.info(f"Deployment {where}: segment '{segment_name}' is no VXLAN segment — nothing to reconcile")
            return
        tags = await self.reconcile_vxlan_segment(segment_id, segment_name)
        if tags is None:
            self.logger.info(f"Deployment {where}: segment {segment_id} is gone — nothing to reconcile")
            return
        added, removed = tags
        self.logger.info(
            f"Deployment {where}: segment '{segment_name}' reconciled — tagged {added}, untagged {removed} port(s)"
        )
