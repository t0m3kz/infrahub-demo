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

A segment with no AppComponent at all referencing it gets no generated customer-port
assignment — there is intentionally no DC-wide fallback (see
generators/topology/segment.py's module docstring).

A ManagedVxlanSegment has its VLAN rows reconciled (its local VLAN is per
switch), then its ports joined to them. Any other segment (a cloud segment)
has no on-prem port to tag.

Reconciled per SEGMENT, not per component: the triggering component only
names the segment. The desired ports are the union over every AppComponent
on it (queries/topology/add/segment_components.gql) plus the ports assigned to
the segment's deployments (a person's request). The edge is the port's
membership of its switch's row (the row's ``capabilities``), written only by the
reconcile; a port no longer desired (an instance moved, a component left, an
assignment withdrawn) loses it (SegmentPortTagMixin, shared with
add_vxlan_segment and add_segment_deployment_ports). A component that moves to
ANOTHER segment leaves the old segment's ports until that segment is reconciled
again (the next run of any of those generators on it).

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
from ..connections import CablingMixin
from ..pools import PoolMixin
from ..protocols import ManagedVxlanSegment
from .segment_addressing import SegmentAddressingMixin
from .segment_ports import SegmentPortTagMixin


class AppInstanceSegmentGenerator(
    PoolMixin, CablingMixin, SegmentAddressingMixin, SegmentPortTagMixin, CommonGenerator
):
    """Reconciles a segment's customer-facing switch-port tags from the
    instances of every AppComponent on it, then, for a VXLAN segment, the
    segment's LOCAL VLAN ID rows on the switches those ports belong to
    (and their MLAG peers).

    Ownership: a tagged switch port belongs to the device's generator and a
    row is shared by every component on the segment and by add_vxlan_segment,
    so this run claims neither. Both are shared desired state of the SEGMENT,
    written untracked and reconciled explicitly — tags via
    RelationshipAdd/RelationshipRemove on the port, rows via
    SegmentVlanMixin — under the per-segment lock. This generator saves
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
        if segment_kind != ManagedVxlanSegment.__name__:
            self.logger.info(
                f"Component {fqdn}: segment '{segment_name}' is a {segment_kind}, not on-prem — nothing to assign"
            )
            return
        if not component.get("instances"):
            # Still reconciled: the instances this component used to have
            # leave their switch ports tagged until the segment is.
            self.logger.info(f"Component {fqdn}: no instances set — reconciling segment '{segment_name}' anyway")

        await self.ensure_segment_addressing(segment)
        tags = await self.reconcile_vxlan_segment(segment_id, segment_name)
        if tags is None:
            # The segment is gone or is no VXLAN segment: nothing to assign.
            self.logger.info(f"Component {fqdn}: segment {segment_id} is not a VXLAN segment — nothing to assign")
            return
        added, removed = tags
        self.logger.info(
            f"Component {fqdn}: segment '{segment_name}' reconciled — tagged {added}, untagged {removed} interface(s)"
        )
