"""Generator assigning a segment to the exact switch interface an
AppComponent's instance is cabled to.

A DC-wide sweep used to tag every leaf/tor customer port in a segment's
deployment, whether or not that customer had anything cabled there (see
generators/topology/segment.py, which no longer does this). That
over-provisions every access switch in the DC for a tenant that may only
have a handful of servers. This generator instead reacts to AppComponent —
the thing that actually names where a tenant's compute runs (``instances``)
and which segment it belongs to (``network_segment``) — and tags only the
interface(s) that instance is cabled to.

Resolution per instance:
  - A bare physical device (DcimPhysicalDevice, ManagedControllerPhysical)
    is used directly.
  - A virtual instance (DcimVirtualDevice, ManagedControllerVirtual) walks
    its hosting_device to the physical host actually cabled to the fabric.
  - Anything else (e.g. CloudInstance) has no on-prem port to tag and is
    skipped — cloud segmentation is handled in the Cloud namespace, not here.

For a bonded instance (role=lag NICs bundled into a DcimLAGInterface, see
generators/topology/endpoint.py), the switch-side far end of a member's
cable is itself a role=lag physical port that is a member of the switch's
own port-channel — the port-channel is what carries the segment's VLAN
(ConfigDB/NX-OS trunk config lives on the aggregate, not the raw member),
so that is what gets tagged, not the raw member port.

A segment with no AppComponent at all referencing it gets no customer-port
assignment — there is intentionally no DC-wide fallback (see
generators/topology/segment.py's module docstring).

Both on-prem segment kinds are reconciled the same way; they differ only
after the tags. A ManagedVxlanSegment then has its VLAN domain rows
reconciled (its local VLAN is per switch). A ManagedVlanSegment has none: its
vlan_id is the segment's own, so tagging its ports is the whole job — which is
how a colocation cage's VLAN segment gets its switch ports from the cage's
cabling instead of hand-typed pins. Any other segment (a cloud segment) has no
on-prem port to tag.

Reconciled per SEGMENT, not per component: the triggering component only
names the segment. The desired tags are the union over every AppComponent
on it (queries/topology/add/segment_components.gql); a port no instance
lands on any more (an instance moved, a component left, a port assigned to the
segment by hand) is untagged, and the segment's VLAN domain rows follow
(SegmentPortTagMixin, shared with add_vxlan_segment, which a change of the
segment's ``capabilities`` triggers, data/events/99_actions.yml). A component
that moves to ANOTHER segment leaves the old segment's tags until that
segment is reconciled again (the next run of either generator on it).

An instance's physical device may not be cabled yet (add_endpoint has not
run, or hasn't reached it) — nothing is tagged until it is. Once add_endpoint
cables it, that run dispatches this generator for every component instanced
on the endpoint or a VM it hosts (EndpointConnectivityGenerator.
_fan_out_component_segments), so the tags follow the cabling.
"""

from __future__ import annotations

from typing import Any

from utils.data_cleaning import clean_data

from ..common import CommonGenerator
from ..pools import PoolMixin
from ..protocols import ManagedVlanSegment, ManagedVxlanSegment
from ..vlan_domain import TAGGABLE_INTERFACE_KINDS, segment_lock_key
from .segment_ports import SegmentPortTagMixin


class AppInstanceSegmentGenerator(PoolMixin, SegmentPortTagMixin, CommonGenerator):
    """Reconciles a segment's customer-facing switch-port tags from the
    instances of every AppComponent on it, then, for a VXLAN segment, the
    segment's LOCAL VLAN ID rows on the VLAN domains (MLAG pair or device
    without MLAG) of the switches those ports belong to.

    Ownership: a tagged switch port belongs to the device's generator and a
    row is shared by every component on the segment and by add_vxlan_segment,
    so this run claims neither. Both are shared desired state of the SEGMENT,
    written untracked and reconciled explicitly — tags via
    RelationshipAdd/RelationshipRemove on the segment, rows via
    VlanDomainMixin — under the per-segment lock. This generator saves
    nothing into its own tracking group.
    """

    graphql_root_key = "AppComponent"

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
        if segment_kind not in (ManagedVxlanSegment.__name__, ManagedVlanSegment.__name__):
            self.logger.info(
                f"Component {fqdn}: segment '{segment_name}' is a {segment_kind}, not on-prem — nothing to assign"
            )
            return
        if not component.get("instances"):
            # Still reconciled: the instances this component used to have
            # leave their switch ports tagged until the segment is.
            self.logger.info(f"Component {fqdn}: no instances set — reconciling segment '{segment_name}' anyway")

        if segment_kind == ManagedVlanSegment.__name__:
            added, removed = await self._reconcile_vlan_segment(segment_id, segment_name)
            self.logger.info(
                f"Component {fqdn}: VLAN segment '{segment_name}' reconciled — tagged {added}, untagged {removed} "
                "interface(s)"
            )
            return

        tags = await self.reconcile_vxlan_segment(segment_id, segment_name)
        if tags is None:
            # The segment is gone or is no VXLAN segment: nothing to assign.
            self.logger.info(f"Component {fqdn}: segment {segment_id} is not a VXLAN segment — nothing to assign")
            return
        added, removed = tags
        self.logger.info(
            f"Component {fqdn}: segment '{segment_name}' reconciled — tagged {added}, untagged {removed} interface(s)"
        )

    async def _reconcile_vlan_segment(self, segment_id: str, segment_name: str) -> tuple[int, int]:
        """Reconcile a VLAN segment's switch-port tags, under the same
        per-segment lock as a VXLAN segment's. Its current tags are read
        inside the lock (one get with the relationship included), so two
        components on the segment never reconcile from a stale view. No VLAN
        domain step: a VLAN segment's vlan_id is its own, not per switch.

        Returns (interfaces tagged, interfaces untagged); (0, 0) when the
        segment is gone.
        """
        async with self.resource_lock(segment_lock_key(segment_id)):
            segment_obj = await self.client.get(
                kind=ManagedVlanSegment,
                id=segment_id,
                include=["capabilities"],
                raise_when_missing=False,
            )
            if not segment_obj:
                self.logger.info(f"VLAN segment {segment_id} not found — nothing to assign")
                return 0, 0
            current_ids = {
                peer.id
                for peer in getattr(segment_obj, "capabilities").peers
                if peer.typename in TAGGABLE_INTERFACE_KINDS
            }
            return await self._reconcile_segment_interface_tags(
                segment_id=segment_id,
                segment_name=segment_name,
                segment_kind=ManagedVlanSegment,
                current_ids=current_ids,
                segment_obj=segment_obj,
            )
