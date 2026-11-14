"""Reconciles a VXLAN segment's VLAN rows, then the switch ports tagged with them (see AGENTS.md VLAN rows). Desired
ports: instance-cabled ports, border service ports of a terminating firewall context, and ports assigned to the
deployment. Edges are untracked; stale ones removed. Ends with the VRF instances and SVIs of every touched device,
inside the segment lock. A deleted segment is left to the next ``reconcile_vrfs``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from generators.connectivity.endpoint import ENDPOINT_NIC_ROLES
from generators.helpers.edges import add_edges, remove_edge
from generators.helpers.vrf import VrfMixin
from generators.mixins.far_end import far_end_interfaces
from generators.mixins.segment_vlan import SWITCHED_PORT_KINDS, SegmentVlanMixin, segment_lock_key
from generators.protocols import DcimPhysicalInterface
from utils.app_instances import physical_host
from utils.data_cleaning import clean_data
from utils.exchange_transit import FIREWALL_CONTEXT_KIND, SERVICE_CONTEXT_ROLES
from utils.firewall_split import footprint_contexts, steering_contexts
from utils.query_flatten import load_flat_query
from utils.service_contexts import member_border_ports

# The device of each desired port, one read: the port joins that device's row of the segment.
_PORT_DEVICES_QUERY = """
query port_devices($ids: [ID]) {
  DcimInterface(ids: $ids) {
    edges { node { id device { node { id } } } }
  }
}
"""

_SEGMENT_COMPONENTS_QUERY_PATH = Path(__file__).resolve().parents[2] / "queries/topology/add/segment_components.gql"


class SegmentPortTagMixin(VrfMixin, SegmentVlanMixin):
    """Mixin providing the segment's port tags and, for a VXLAN segment, its VLAN rows and what routes on them."""

    async def reconcile_vxlan_segment(self, segment_id: str, segment_name: str) -> tuple[int, int] | None:
        """Reconcile the segment's VLAN rows, join the ports to them, then the VRFs and SVIs, under the segment lock.
        Returns (ports tagged, ports untagged); None when the segment is gone or is no VXLAN segment."""
        async with self.resource_lock(segment_lock_key(segment_id)):
            state = await self._fetch_segment_vlan_state(segment_id)
            if state is None:
                return None
            derived = await self._derived_segment_port_ids(segment_id, segment_name, state["segment"])
            requested = {
                port_id: item["id"] for item in state["deployments"] for port_id in item.get("requested") or []
            }
            port_devices = await self._port_devices(derived | requested.keys())
            before = self._row_device_ids(state)
            if await self._reconcile_segment_vlans_locked(
                segment_id, segment_name, state, {device_id for device_id in port_devices.values() if device_id}
            ):
                state = await self._fetch_segment_vlan_state(segment_id)
                if state is None:
                    return None
            added, removed = await self._reconcile_segment_port_rows(
                segment_name=segment_name,
                ports=state["segment"].get("ports") or [],
                port_devices=port_devices,
                requested={port_id: deployment for port_id, deployment in requested.items() if port_id not in derived},
                rows_by_device=self._rows_by_device(state),
            )
            await self.reconcile_vrfs(before | self._row_device_ids(state))
        return added, removed

    @staticmethod
    def _terminated_service_port_ids(segment: dict[str, Any]) -> set[str]:
        """The border service ports of the footprint's internal firewall context for a ``firewall_gateway`` segment
        (empty if none).
        """
        if not segment.get("firewall_gateway"):
            return set()
        contexts = [
            context
            for footprint in segment.get("customer_deployments") or []
            for context in steering_contexts(footprint_contexts(footprint))
        ]
        return {
            port["id"]
            for context in contexts
            for member in context.get("shared_devices") or []
            for port in member_border_ports(member)
            if port.get("id")
            and port.get("typename") in SWITCHED_PORT_KINDS
            and port.get("role") == SERVICE_CONTEXT_ROLES[FIREWALL_CONTEXT_KIND]
        }

    @staticmethod
    def _row_device_ids(state: dict[str, Any] | None) -> set[str]:
        """The devices the segment has a row on, whatever deployment the row sits under (the keys of the state rows)."""
        return {device_id for item in (state or {}).get("deployments", []) for device_id in item["rows"]}

    @staticmethod
    def _rows_by_device(state: dict[str, Any]) -> dict[str, tuple[str, str]]:
        """{device id: (id of the segment's row on it, id of the deployment the row is under)}."""
        return {
            device_id: (row["id"], item["id"])
            for item in state.get("deployments", [])
            for device_id, row in item["rows"].items()
        }

    async def _segment_components(self, segment_id: str) -> list[dict[str, Any]]:
        """Every AppComponent on the segment, with its instances (segment_components.gql)."""
        result = await self.client.execute_graphql(
            query=load_flat_query(_SEGMENT_COMPONENTS_QUERY_PATH), variables={"segment_id": segment_id}
        )
        return clean_data(result).get("AppComponent") or []

    async def _derived_segment_port_ids(self, segment_id: str, segment_name: str, segment: dict[str, Any]) -> set[str]:
        """The ids of every port the segment's components derive, plus the border service ports of a terminating
        context. Read-only.
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
        terminated = self._terminated_service_port_ids(segment)
        port_ids = await self._customer_facing_target_ids(device_ids) | terminated
        self.logger.info(
            f"Segment {segment_name}: {len(port_ids)} port(s) from {len(components)} "
            f"component(s) and {len(terminated)} firewall service port(s)"
        )
        return port_ids

    async def _port_devices(self, port_ids: set[str]) -> dict[str, str | None]:
        """{port id: its device id, None when it has none}, one read."""
        if not port_ids:
            return {}
        result = await self.client.execute_graphql(query=_PORT_DEVICES_QUERY, variables={"ids": sorted(port_ids)})
        devices: dict[str, str | None] = dict.fromkeys(port_ids)
        for port in clean_data(result).get("DcimInterface") or []:
            if port.get("id") in devices:
                devices[port["id"]] = (port.get("device") or {}).get("id")
        return devices

    async def _reconcile_segment_port_rows(
        self,
        *,
        segment_name: str,
        ports: list[dict[str, Any]],
        port_devices: dict[str, str | None],
        requested: dict[str, str],
        rows_by_device: dict[str, tuple[str, str]],
    ) -> tuple[int, int]:
        """Make the segment's (port, row) edges exactly the desired ports (``port_devices``: {port id: device id});
        returns (added, removed). ``requested`` is {port id: deployment id}; a port with no row, or a row under another
        deployment, is reported and skipped.
        """
        desired: set[tuple[str, str]] = set()
        for port_id, device_id in sorted(port_devices.items()):
            row_id, deployment_id = rows_by_device.get(device_id or "", (None, None))
            if row_id is None:
                self.logger.warning(
                    f"Segment {segment_name}: port {port_id} sits on a device with no VLAN row of the segment (no "
                    "deployment of it there) — not tagged"
                )
            elif port_id in requested and requested[port_id] != deployment_id:
                self.logger.warning(
                    f"Segment {segment_name}: port {port_id} is requested in deployment {requested[port_id]}, but "
                    f"its switch's row is under {deployment_id} — not tagged"
                )
            else:
                desired.add((port_id, row_id))
        current = {(port["id"], port["row_id"]) for port in ports}
        to_add, to_remove = sorted(desired - current), sorted(current - desired)
        for port_id, row_id in to_add:
            await add_edges(self.client, node_id=port_id, relation="capabilities", peer_ids=[row_id])
        for port_id, row_id in to_remove:
            await remove_edge(self.client, node_id=port_id, relation="capabilities", peer_id=row_id)
        self.logger.info(f"Segment {segment_name}: tagged {len(to_add)}, untagged {len(to_remove)} port(s)")
        return len(to_add), len(to_remove)

    @staticmethod
    def _resolve_physical_device_id(instance: dict[str, Any]) -> str | None:
        """The physical device id an AppInstance is actually cabled through (a virtual instance walks its
        hosting_device).
        """
        host = physical_host(instance)
        return host.get("id") if host else None

    async def _customer_facing_target_ids(self, device_ids: list[str]) -> set[str]:
        """The ids of every switch interface the devices are cabled to: the uplink far end, or a lag NIC's switch-side
        port-channel. Read-only.
        """
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
        """The interface to tag for a NIC cabled to `far_end`: the far-end port, or its port-channel when it is a lag
        member (None if none yet).
        """
        if far_end.role.value != "lag":
            return far_end.id

        port_channel_peer = getattr(far_end.lag, "peer", None)
        if not port_channel_peer:
            self.logger.warning(f"  Far-end port {far_end.id} is role=lag but has no port-channel yet — skipping")
            return None
        return port_channel_peer.id
