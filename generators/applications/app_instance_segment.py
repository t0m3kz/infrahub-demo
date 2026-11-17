"""Generator assigning a segment to the switch interface an AppComponent's instance is cabled to (no DC-wide fallback,
see segment.py). A physical instance is used directly, a virtual one via its hosting_device, a bonded one on the switch
port-channel; CloudInstance is skipped. Reconciled per segment (desired ports are the union over its components plus
assigned ports), via reconcile_vxlan_segment (segment_ports.py).
"""

from __future__ import annotations

from typing import Any

from generators.base import BaseGenerator
from generators.protocols import ManagedVxlanSegment
from generators.segments.segment_addressing import ensure_segment_addressing
from generators.segments.segment_ports import reconcile_vxlan_segment
from utils.data_cleaning import clean_data


class AppInstanceSegmentGenerator(BaseGenerator):
    """Reconcile a segment's customer-port tags from its components' instances, then its VLAN rows (and MLAG peers);
    untracked.
    """

    graphql_root_key = "AppComponent"
    ensure_segment_addressing = ensure_segment_addressing

    async def generate(self, data: dict[str, Any]) -> None:
        cleaned = clean_data(data)
        component_list = cleaned.get(self.graphql_root_key, [])
        if not component_list:
            self.logger.error(f"No {self.graphql_root_key} data in GraphQL response")
            return

        component = component_list[0]
        fqdn: str = component.get("fqdn", "")
        segment = component.get("network_segment") or {}
        segment_id: str = segment.get("id", "")
        segment_name: str = segment.get("name", "")
        segment_kind: str = segment.get("typename", "")

        if not segment_id:
            self.logger.info(f"Component {fqdn}: no network_segment set — nothing to assign")
            return
        if segment_kind != ManagedVxlanSegment.__name__:
            self.logger.info(
                f"Component {fqdn}: segment '{segment_name}' is a {segment_kind}, not on-prem — nothing to assign"
            )
            return
        if not component.get("instances"):
            # Still reconciled: the old instances' ports stay tagged until the segment is
            self.logger.info(f"Component {fqdn}: no instances set — reconciling segment '{segment_name}' anyway")

        await self.ensure_segment_addressing(segment)
        tags = await reconcile_vxlan_segment(self, segment_id, segment_name)
        if tags is None:
            # The segment is gone or not VXLAN: nothing to assign
            self.logger.info(f"Component {fqdn}: segment {segment_id} is not a VXLAN segment — nothing to assign")
            return
        added, removed = tags
        self.logger.info(
            f"Component {fqdn}: segment '{segment_name}' reconciled — tagged {added}, untagged {removed} interface(s)"
        )
