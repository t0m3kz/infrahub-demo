"""Reconciles the switch ports a VXLAN segment is tagged on, then its VLAN rows.

The ports are derived from the instances of every AppComponent on the segment
(queries/topology/add/segment_components.gql): the interfaces each instance's
physical host is cabled to (the port-channel for a bonded one). The segment owns
them, so the same reconcile runs from both directions that change the answer:

  - add_app_component_segment, when a component's instances or segment change;
  - add_vxlan_segment, when the segment itself is written — creation, its
    footprints, and an edit of its ``capabilities`` (a port assigned to the
    segment by hand is removed by this reconcile, unless the segment has no
    component at all: then there is nothing to compare with and the ports stay).

Writes RelationshipAdd/RelationshipRemove on the segment's ``capabilities``
only, untracked: a tagged port belongs to the device's generator, not to a run.

The rows decide which switches route the segment, so the same reconcile ends with the VRF instances and SVIs of every
switch of every VLAN domain the segment's rows were on before or are on after (a switch that lost its row included):
generators/helpers/vrf.py, inside the segment lock (lock order: segment, then the device locks).

A deleted segment is not reconciled (nothing to read its rows from, and no trigger runs on a delete). Its rows and
SVIs go by the schema's cascade, not by this code: ``SegmentDeployment.vlan_domain_segments`` is ``on_delete: cascade``
and so is the row's ``svis``; whether the server also removes the deployment with the segment is not proven (its
``segment`` is a mandatory attribute-kind relationship with no cascade of its own). What can linger converges later:
the next ``reconcile_vrfs`` of each switch (any segment, circuit or context run that touches it) deletes an SVI whose
row is no longer routed (role ``svi`` and not desired) and then the instance with no demand left, in the same pass;
until then checks/vrf_instances.py reports the instance as a WARNING.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from utils.app_instances import physical_host
from utils.data_cleaning import clean_data

from ..far_end import far_end_interfaces
from ..helpers.vrf import VrfMixin
from ..protocols import DcimPhysicalInterface, ManagedVxlanSegment
from ..vlan_domain import VlanDomainMixin, segment_lock_key
from .endpoint import ENDPOINT_NIC_ROLES

_SEGMENT_COMPONENTS_QUERY_PATH = Path(__file__).resolve().parents[2] / "queries/topology/add/segment_components.gql"


class SegmentPortTagMixin(VrfMixin, VlanDomainMixin):
    """Mixin providing the segment's port tags and, for a VXLAN segment, its VLAN rows and what routes on them.

    Expects the host class to provide what VlanDomainMixin does.
    """

    async def reconcile_vxlan_segment(self, segment_id: str, segment_name: str) -> tuple[int, int] | None:
        """Tag the segment's ports from its components, reconcile its VLAN rows, then the VRFs and SVIs of the
        switches they touch, under the segment lock.

        Returns (ports tagged, ports untagged); None when the segment is gone or is no VXLAN segment.
        """
        async with self.resource_lock(segment_lock_key(segment_id)):
            state = await self._fetch_segment_vlan_state(segment_id)
            if state is None:
                return None
            added, removed = await self._reconcile_segment_interface_tags(
                segment_id=segment_id,
                segment_name=segment_name,
                current_ids=self.tagged_interface_ids(state["segment"]),
            )
            if added or removed:
                state = await self._fetch_segment_vlan_state(segment_id)
            before = self._row_device_ids(state)
            if await self._reconcile_segment_vlan_domains_locked(segment_id, segment_name, state):
                state = await self._fetch_segment_vlan_state(segment_id)
            await self.reconcile_vrfs(before | self._row_device_ids(state))
        return added, removed

    @staticmethod
    def _row_device_ids(state: dict[str, Any] | None) -> set[str]:
        """The devices of the VLAN domains the segment has a row on, whatever deployment the row sits under.

        A domain is a device or a ``ManagedMLAG`` of two (utils/vlan_domain.py); the query returns its devices with
        the row (segment_vlan_domains.gql), so no read follows.
        """
        state = state or {}
        return {
            device_id
            for item in state.get("deployments", [])
            for domain_id in item["rows"]
            for device_id in state.get("domain_devices", {}).get(domain_id, [])
        }

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
        current_ids: set[str],
    ) -> tuple[int, int]:
        """Make the segment's switch-port tags exactly the customer-facing
        targets of the instances of EVERY component on the segment: tag the
        missing ones, untag ports no instance lands on any more (an instance
        that moved, or a component that left). ``current_ids`` are the
        physical/port-channel interfaces tagged today. Written with
        RelationshipAdd/RelationshipRemove on the segment's
        capabilities — touches only this segment's edge on each
        port and never enters this run's tracking group.

        The segment node is fetched only when there is a tag to add or remove.

        Returns (interfaces tagged, interfaces untagged).
        """
        components = await self._segment_components(segment_id)
        if not components:
            # Nothing names where the segment is wanted, so there is no desired set to compare the tags with:
            # they are authored (or a component is yet to be added) and stay as they are.
            self.logger.info(f"Segment {segment_name}: no AppComponent on it — its ports are left as they are")
            return 0, 0
        # Several components/virtual instances can resolve to the same host.
        device_ids = list(
            dict.fromkeys(
                device_id
                for component in components
                for instance in component.get("instances") or []
                if (device_id := self._resolve_physical_device_id(instance))
            )
        )
        desired_ids = await self._customer_facing_target_ids(device_ids)

        to_add = sorted(desired_ids - current_ids)
        to_remove = sorted(current_ids - desired_ids)
        if to_add or to_remove:
            segment_obj = await self.client.get(kind=ManagedVxlanSegment, id=segment_id)
            if to_add:
                await segment_obj.add_relationships(relation_to_update="capabilities", related_nodes=to_add)
            if to_remove:
                await segment_obj.remove_relationships(relation_to_update="capabilities", related_nodes=to_remove)
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
        instance) has no on-prem device to resolve. The resolution itself
        (utils.app_instances.physical_host) is shared with the NX-OS GPO
        transform that classifies the same ports."""
        host = physical_host(instance)
        return host.get("id") if host else None

    async def _customer_facing_target_ids(self, device_ids: list[str]) -> set[str]:
        """The ids of every switch interface the devices are cabled to: the far
        end of a role=uplink NIC directly, a role=lag NIC's switch-side
        port-channel. Several bond members resolving to the SAME port-channel
        dedup to one id. Read-only: one query for the NICs of all the devices,
        two for the far ends of all their cables."""
        if not device_ids:
            return set()
        nics = await self.client.filters(
            kind=DcimPhysicalInterface,
            device__ids=device_ids,
            role__values=list(ENDPOINT_NIC_ROLES),
            include=["cable"],
        )
        cabled = [nic for nic in nics if nic.cable and nic.cable.id]
        if not cabled:
            self.logger.info(
                f"  Device(s) {', '.join(device_ids)} have no cabled uplink/lag interface yet — nothing to tag"
            )
            return set()

        far_ends = await far_end_interfaces(self.client, cabled, include=["lag"])
        target_ids = {
            target_id for far_end in far_ends.values() if (target_id := self._customer_facing_target_id(far_end))
        }
        if not target_ids:
            self.logger.info(f"  Device(s) {', '.join(device_ids)}: cabled but no customer-facing switch port resolved")
        return target_ids

    def _customer_facing_target_id(self, far_end: Any) -> str | None:
        """The id of the interface to tag for a NIC whose cable ends on `far_end`:
        the far-end port itself for a plain uplink, or its own switch-side
        port-channel when the far end is a role=lag member — the aggregate is
        what carries the VLAN, not the raw member port. None if a lag far end
        has no port-channel yet."""
        if far_end.role.value != "lag":
            return far_end.id

        port_channel_peer = getattr(far_end.lag, "peer", None)
        if not port_channel_peer:
            self.logger.warning(f"  Far-end port {far_end.id} is role=lag but has no port-channel yet — skipping")
            return None
        return port_channel_peer.id
