"""Generator assigning a VXLAN segment to the exact switch interface an
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

Reconciled per SEGMENT, not per component: the triggering component only
names the segment. The desired tags are the union over every AppComponent
on it (queries/topology/add/segment_components.gql); a port no instance
lands on any more (an instance moved, a component left) is untagged, and
the segment's VLAN domain activations follow (VlanDomainMixin). A component
that moves to ANOTHER segment leaves the old segment's tags until that
segment is reconciled again (its own add_vxlan_segment run does not touch
tags; the next add_app_component_segment run of any component still on it
does).

An instance's physical device may not be cabled yet (add_endpoint has not
run, or hasn't reached it) — nothing is tagged until it is; re-triggering
once cabling lands later is a known gap, not solved here.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from utils.data_cleaning import clean_data

from ..common import CommonGenerator
from ..far_end import far_end_interface
from ..pools import PoolMixin
from ..protocols import (
    DcimLAGInterface,
    DcimPhysicalInterface,
    ManagedVxlanSegment,
)
from ..vlan_domain import VlanDomainMixin, segment_lock_key

# AppInstance kinds that ARE a physical device/controller — used directly.
_PHYSICAL_INSTANCE_TYPES = frozenset({"DcimPhysicalDevice", "ManagedControllerPhysical"})
# AppInstance kinds hosted BY a physical device — walk hosting_device to it.
_VIRTUAL_INSTANCE_TYPES = frozenset({"DcimVirtualDevice", "ManagedControllerVirtual"})
# Interface roles a tenant's traffic can actually ride on at the device side.
_CUSTOMER_FACING_NIC_ROLES = ("uplink", "lag")
_SEGMENT_COMPONENTS_QUERY_PATH = Path(__file__).resolve().parents[2] / "queries/topology/add/segment_components.gql"


class AppInstanceSegmentGenerator(PoolMixin, VlanDomainMixin, CommonGenerator):
    """Reconciles a segment's customer-facing switch-port tags from the
    instances of every AppComponent on it, then the segment's LOCAL VLAN ID
    activations on the VLAN domains (MLAG pair or standalone) of the switches
    those ports belong to.

    Ownership: a tagged switch port belongs to the device's generator and an
    activation is shared by every component on the segment and by
    add_vxlan_segment, so this run claims neither. Both are shared desired
    state of the SEGMENT, written untracked and reconciled explicitly — tags
    via RelationshipAdd/RelationshipRemove on the segment, activations via
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

        if not segment_id:
            self.logger.info(f"Component {fqdn}: no network_segment set — nothing to assign")
            return
        if not component.get("instances"):
            # Still reconciled: the instances this component used to have
            # leave their switch ports tagged until the segment is.
            self.logger.info(f"Component {fqdn}: no instances set — reconciling segment '{segment_name}' anyway")

        segment_obj = await self.client.get(kind=ManagedVxlanSegment, id=segment_id, raise_when_missing=False)
        if not segment_obj:
            # A ManagedVlanSegment's ports are hand-assigned in its data
            # file and it has no VLAN domain activations: nothing to do.
            self.logger.info(f"Component {fqdn}: segment {segment_id} is not a VXLAN segment — nothing to assign")
            return

        async with self.resource_lock(segment_lock_key(segment_id)):
            state = await self._fetch_segment_vlan_state(segment_id)
            if state is None:
                self.logger.info(f"Component {fqdn}: segment {segment_id} is not a VXLAN segment — nothing to assign")
                return
            added, removed = await self._reconcile_segment_interface_tags(
                segment_id=segment_id,
                segment_name=segment_name,
                segment_obj=segment_obj,
                current_ids=self.tagged_interface_ids(state["segment"]),
            )
            if added or removed:
                state = await self._fetch_segment_vlan_state(segment_id)
            await self._reconcile_segment_vlan_domains_locked(segment_id, segment_name, state)

        self.logger.info(
            f"Component {fqdn}: segment '{segment_name}' reconciled — tagged {added}, untagged {removed} interface(s)"
        )

    async def _segment_components(self, segment_id: str) -> list[dict[str, Any]]:
        """Every AppComponent on the segment, with its instances (segment_components.gql)."""
        result = await self.client.execute_graphql(
            query=_SEGMENT_COMPONENTS_QUERY_PATH.read_text(), variables={"segment_id": segment_id}
        )
        return clean_data(result).get("AppComponent") or []

    async def _reconcile_segment_interface_tags(
        self,
        *,
        segment_id: str,
        segment_name: str,
        segment_obj: Any,
        current_ids: set[str],
    ) -> tuple[int, int]:
        """Make the segment's switch-port tags exactly the customer-facing
        targets of the instances of EVERY component on the segment: tag the
        missing ones, untag ports no instance lands on any more (an instance
        that moved, or a component that left). ``current_ids`` are the
        physical/port-channel interfaces tagged today. Written with
        RelationshipAdd/RelationshipRemove on the segment's
        interface_capabilities — touches only this segment's edge on each
        port and never enters this run's tracking group.

        Returns (interfaces tagged, interfaces untagged).
        """
        components = await self._segment_components(segment_id)
        # Several components/virtual instances can resolve to the same host.
        device_ids = list(
            dict.fromkeys(
                device_id
                for component in components
                for instance in component.get("instances") or []
                if (device_id := self._resolve_physical_device_id(instance))
            )
        )
        resolved = await asyncio.gather(*(self._customer_facing_targets(device_id) for device_id in device_ids))
        desired_ids: set[str] = set()
        for targets in resolved:
            desired_ids.update(targets)

        to_add = sorted(desired_ids - current_ids)
        to_remove = sorted(current_ids - desired_ids)
        if to_add:
            await segment_obj.add_relationships(relation_to_update="interface_capabilities", related_nodes=to_add)
        if to_remove:
            await segment_obj.remove_relationships(relation_to_update="interface_capabilities", related_nodes=to_remove)
        self.logger.info(
            f"Segment {segment_name}: {len(desired_ids)} customer-facing port(s) from {len(components)} "
            f"component(s) — tagged {len(to_add)}, untagged {len(to_remove)}"
        )
        return len(to_add), len(to_remove)

    @staticmethod
    def _resolve_physical_device_id(instance: dict[str, Any]) -> str | None:
        """The physical device id an AppInstance is actually cabled through.

        A bare physical device/controller is used directly. A virtual
        instance walks its hosting_device. Anything else (e.g. a cloud
        instance) has no on-prem device to resolve."""
        typename = instance.get("typename")
        if typename in _PHYSICAL_INSTANCE_TYPES:
            return instance.get("id")
        if typename in _VIRTUAL_INSTANCE_TYPES:
            hosting = instance.get("hosting_device") or {}
            return hosting.get("id")
        return None

    async def _customer_facing_targets(self, device_id: str) -> dict[str, Any]:
        """Every switch interface `device_id` is cabled to, as {id: target}:
        the far end of a role=uplink NIC directly, a role=lag NIC's
        switch-side port-channel. Several bond members resolving to the SAME
        port-channel dedup to one entry. Read-only."""
        nics = await self.client.filters(
            kind=DcimPhysicalInterface,
            device__ids=[device_id],
            role__values=list(_CUSTOMER_FACING_NIC_ROLES),
            include=["cable"],
        )
        cabled = [nic for nic in nics if nic.cable and nic.cable.id]
        if not cabled:
            self.logger.info(f"  Device {device_id} has no cabled uplink/lag interface yet — nothing to tag")
            return {}

        # Each nic's resolution is independent read-only lookups, so they run
        # concurrently; the dict merge itself stays single-threaded.
        resolved_targets = await asyncio.gather(*(self._resolve_customer_facing_target(nic) for nic in cabled))
        targets = {target.id: target for target in resolved_targets if target is not None}
        if not targets:
            self.logger.info(f"  Device {device_id}: cabled but no customer-facing switch port resolved")
        return targets

    async def _resolve_customer_facing_target(self, nic: Any) -> Any | None:
        """The far end of `nic`'s cable, as the object to tag: the far-end
        port itself for a plain uplink, or its own switch-side port-channel
        when the far end is a role=lag member — the aggregate is what
        carries the VLAN, not the raw member port. None if the cable has no
        resolvable far end, or a lag far end has no port-channel yet."""
        far_end = await far_end_interface(self.client, nic, include=["lag"])
        if far_end is None:
            return None
        if far_end.role.value != "lag":
            return far_end

        port_channel_peer = getattr(far_end.lag, "peer", None)
        if not port_channel_peer:
            self.logger.warning(f"  Far-end port {far_end.id} is role=lag but has no port-channel yet — skipping")
            return None
        return await self.client.get(kind=DcimLAGInterface, id=port_channel_peer.id)
