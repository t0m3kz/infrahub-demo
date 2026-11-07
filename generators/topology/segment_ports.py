"""Reconciles the switch ports a VXLAN segment is tagged on, then its VLAN rows.

The ports are derived from two places: the instances of every AppComponent on the segment
(queries/topology/add/segment_components.gql): the interfaces each instance's
physical host is cabled to (the port-channel for a bonded one); and, for a segment a firewall context terminates
(``terminated_on``), the border service ports tagged with that context, the ones facing its HA pair. The segment owns
them, so the same reconcile runs from both directions that change the answer:

  - add_app_component_segment, when a component's instances or segment change;
  - add_vxlan_segment, when the segment itself is written — creation and its footprints.

The edge between a port and a segment is ``segments`` on the PORT (one-sided: the segment has no list of its ports,
and a change of it re-runs nothing). Provenance is the edge's ``source`` property (generators/helpers/edge_source.py):
this reconcile writes the ports its components derive with its own account as the source, and removes an edge ONLY
when its source is that account and the port is no longer derived (an instance moved, a component left, the segment
has no component any more). An edge with no source was set by a person from the port: no generator removes or changes
it, whatever the components say. A person's edge on a port that is also derived stays as it is (source unset: it is
not re-stamped, so it survives a later component leaving); a port a person adds gets its VLAN row at the next run
of the segment's generators.

Writes RelationshipAdd/RelationshipRemove on the port, untracked: a tagged port belongs to the device's generator, not
to a run.

The rows (one per device, the MLAG peers of a tagged device included even with no port of the segment, so a dual-homed
port-channel is tagged on both; generators/segment_vlan.py) decide which switches route the segment, so the same reconcile
ends with the VRF instances and SVIs of every device the segment's rows were on before or are on after (a switch that
lost its row included): generators/helpers/vrf.py, inside the segment lock (lock order: segment, then the device locks).

A deleted segment is not reconciled (nothing to read its rows from, and no trigger runs on a delete). Its rows and
SVIs go by the schema's cascade, not by this code: ``SegmentDeployment.vlans`` is ``on_delete: cascade``
and so is the row's ``svi``; whether the server also removes the deployment with the segment is not proven (its
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
from utils.exchange_transit import FIREWALL_CONTEXT_KIND, SERVICE_CONTEXT_ROLES

from ..far_end import far_end_interfaces
from ..helpers.edge_source import add_edges_with_source, current_account_id, remove_edges
from ..helpers.vrf import VrfMixin
from ..protocols import DcimPhysicalInterface
from ..segment_vlan import SegmentVlanMixin, segment_lock_key
from .endpoint import ENDPOINT_NIC_ROLES

_SWITCHED_PORT_KINDS = frozenset({"DcimPhysicalInterface", "DcimLAGInterface"})
"""The kinds of DcimSwitchedInterface: the ports that can carry a segment."""

_SEGMENT_COMPONENTS_QUERY_PATH = Path(__file__).resolve().parents[2] / "queries/topology/add/segment_components.gql"


class SegmentPortTagMixin(VrfMixin, SegmentVlanMixin):
    """Mixin providing the segment's port tags and, for a VXLAN segment, its VLAN rows and what routes on them.

    Expects the host class to provide what SegmentVlanMixin does.
    """

    _account_id_cache: str | None = None

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
                ports=state["segment"].get("ports") or [],
                terminated_port_ids=self._terminated_service_port_ids(state["segment"]),
            )
            if added or removed:
                state = await self._fetch_segment_vlan_state(segment_id)
            before = self._row_device_ids(state)
            if await self._reconcile_segment_vlans_locked(segment_id, segment_name, state):
                state = await self._fetch_segment_vlan_state(segment_id)
            await self.reconcile_vrfs(before | self._row_device_ids(state))
        return added, removed

    @staticmethod
    def _terminated_service_port_ids(segment: dict[str, Any]) -> set[str]:
        """The border service ports of the firewall context the segment is terminated on (empty when it is not).

        The ports of the context's ``capabilities`` that are a switch port or a LAG with the firewall service role:
        the border leaf ports facing the HA pair (the members' own sub-interfaces are virtual interfaces).
        ``segment`` is the cleaned state segment (queries/topology/add/segment_vlans.gql).
        """
        context = segment.get("terminated_on") or {}
        return {
            cap["id"]
            for cap in context.get("capabilities") or []
            if cap.get("id")
            and cap.get("typename") in _SWITCHED_PORT_KINDS
            and cap.get("role") == SERVICE_CONTEXT_ROLES[FIREWALL_CONTEXT_KIND]
        }

    @staticmethod
    def _row_device_ids(state: dict[str, Any] | None) -> set[str]:
        """The devices the segment has a row on, whatever deployment the row sits under (the keys of the state rows)."""
        return {device_id for item in (state or {}).get("deployments", []) for device_id in item["rows"]}

    async def _segment_components(self, segment_id: str) -> list[dict[str, Any]]:
        """Every AppComponent on the segment, with its instances (segment_components.gql)."""
        result = await self.client.execute_graphql(
            query=_SEGMENT_COMPONENTS_QUERY_PATH.read_text(), variables={"segment_id": segment_id}
        )
        return clean_data(result).get("AppComponent") or []

    async def _generator_account_id(self) -> str:
        """The account of this run's client token, asked once per run (the id every edge this reconcile writes
        carries as its source)."""
        if self._account_id_cache is None:
            self._account_id_cache = await current_account_id(self.client)
        return self._account_id_cache

    async def _reconcile_segment_interface_tags(
        self,
        *,
        segment_id: str,
        segment_name: str,
        ports: list[dict[str, Any]],
        terminated_port_ids: set[str],
    ) -> tuple[int, int]:
        """Make the ports this reconcile owns exactly the customer-facing
        targets of the instances of EVERY component on the segment plus
        ``terminated_port_ids`` (the border service ports of the firewall
        context terminating it): add the
        missing ones with this run's account as the edge source, remove an edge
        whose source is that account and that no instance lands on any more (an
        instance that moved, a component that left, a segment no longer terminated). ``ports`` are the ports
        on the segment today, ``{"id", "source_id", ...}``
        (SegmentVlanMixin._segment_ports). An edge with another source or none is
        never removed (a person's port stays, even with no component on the
        segment), and a port already on the segment is not written again, so a
        person's edge on a derived port keeps its (empty) source. Written with
        RelationshipAdd/RelationshipRemove on the port's ``segments`` — touches
        only this segment's edge on each port and never enters this run's
        tracking group.

        The account is resolved only when there is something to compare or write.

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
        desired_ids = await self._customer_facing_target_ids(device_ids) | terminated_port_ids

        current_ids = {port["id"] for port in ports}
        to_add = sorted(desired_ids - current_ids)
        owned = [port["id"] for port in ports if port.get("source_id") and port["id"] not in desired_ids]
        to_remove: list[str] = []
        if to_add or owned:
            account_id = await self._generator_account_id()
            to_remove = sorted(
                port["id"] for port in ports if port.get("source_id") == account_id and port["id"] not in desired_ids
            )
            await add_edges_with_source(
                self.client, node_ids=to_add, relation="segments", peer_id=segment_id, source_id=account_id
            )
            await remove_edges(self.client, node_ids=to_remove, relation="segments", peer_id=segment_id)
        self.logger.info(
            f"Segment {segment_name}: {len(desired_ids)} port(s) from {len(components)} "
            f"component(s) and {len(terminated_port_ids)} firewall service port(s) — tagged {len(to_add)}, untagged {len(to_remove)}"
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
