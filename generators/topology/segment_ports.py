"""Reconciles the switch ports a VXLAN segment is tagged on, then its VLAN rows.

The ports are derived from two places: the instances of every AppComponent on the segment
(queries/topology/add/segment_components.gql): the interfaces each instance's
physical host is cabled to (the port-channel for a bonded one); and, for a segment a firewall context terminates
(``terminated_on``), the border service ports tagged with that context, the ones facing its HA pair. The segment owns
them, so the same reconcile runs from both directions that change the answer:

  - add_app_component_segment, when a component's instances or segment change;
  - add_vxlan_segment, when the segment itself is written — creation and its footprints.

A port carries the segment as a member of the segment's DEPLOYMENT in its device's topology: the
``ManagedSegmentDeployment`` among the port's interface ``capabilities``, the deployment whose hosting deployment
(a DC, a metro, an office) the port's device sits in or under (``_deployment_by_device_deployment``). A derived port
whose device sits in no deployment of the segment is reported and skipped (the next run, once the deployment exists,
converges). Provenance is the edge's ``source`` property (generators/helpers/edge_source.py): this reconcile writes
the (port, deployment) edges its components derive with its own account as the source, and removes an edge ONLY when
its source is that account and the pair is no longer derived (an instance moved, a component left, the segment has no
component any more, the port's device moved to another deployment: the edge moves with it). An edge with no source was set by a person from the port: no generator removes or changes
it, whatever the components say. A person's edge of the segment on a port that is also derived stays as it is (source
unset: it is not re-stamped, so it survives a later component leaving); a port a person adds gets its VLAN row at the
next run of the segment's generators.

Writes RelationshipAdd/RelationshipRemove on the port's ``capabilities``, edge by edge, untracked: a tagged port belongs to the device's generator, not
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
from typing import TYPE_CHECKING, Any

from infrahub_sdk.protocols import CoreIPPrefixPool

from utils.app_instances import physical_host
from utils.data_cleaning import clean_data
from utils.exchange_transit import FIREWALL_CONTEXT_KIND, SERVICE_CONTEXT_ROLES
from utils.graphql_queries import load_query
from utils.limits import DEFAULT_NAMESPACE, OFFSET_ANYCAST
from utils.segment_addressing import segment_namespace, tenant_pool_name
from utils.service_contexts import member_border_ports

from ..far_end import far_end_interfaces
from ..helpers.edge_source import add_edge_with_source, current_account_id, remove_edge
from ..helpers.vrf import VrfMixin
from ..protocols import DcimPhysicalInterface, IpamPrefix
from ..segment_vlan import SWITCHED_PORT_KINDS, SegmentVlanMixin, segment_lock_key
from .endpoint import ENDPOINT_NIC_ROLES

if TYPE_CHECKING:
    from ..connections import CablingMixin

# The device's deployment of each port, one read: the port joins the segment's deployment in that topology.
_PORT_DEVICE_DEPLOYMENTS_QUERY = """
query port_device_deployments($ids: [ID]) {
  DcimInterface(ids: $ids) {
    edges { node { id device { node { id ... on DcimPhysicalDevice { deployment { node { id } } } } } } }
  }
}
"""

_SEGMENT_COMPONENTS_QUERY_PATH = Path(__file__).resolve().parents[2] / "queries/topology/add/segment_components.gql"


class SegmentPortTagMixin(VrfMixin, SegmentVlanMixin):
    """Mixin providing the segment's port tags and, for a VXLAN segment, its VLAN rows and what routes on them.

    Expects the host class to provide what SegmentVlanMixin does.
    """

    _account_id_cache: str | None = None
    if TYPE_CHECKING:
        upsert_prefix_addresses = CablingMixin.upsert_prefix_addresses

    async def ensure_segment_addressing(self, segment: dict[str, Any]) -> None:
        """Allocate a missing gateway once, using explicit zone authority and default IPAM."""
        if segment.get("gateway"):
            return
        segment_id, segment_name = segment["id"], segment["name"]
        pool_name = tenant_pool_name(segment_namespace(segment)["name"])
        pools = await self.client.filters(kind=CoreIPPrefixPool, name__value=pool_name)
        if not pools:
            self.logger.error(f"Segment {segment_name}: tenant pool {pool_name} not found")
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
                kind=segment["typename"],
                data={"id": segment_id, "gateway": {"id": gateway.id}},
            )
            await segment_obj.save(allow_upsert=True, update_group_context=False)
        segment["gateway"] = {
            "id": gateway.id,
            "address": gateway.address.value,
            "ip_prefix": {"id": prefix.id, "prefix": prefix.prefix.value, "ip_namespace": {"name": DEFAULT_NAMESPACE}},
        }
        self.logger.info(f"Segment {segment_name}: gateway {gateway.address.value} from {pool_name}")

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
                device_deployments=self._deployment_by_device_deployment(state),
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

        The ports with the firewall service role that the context's members' uplinks are cabled to: the border leaf
        ports facing the HA pair (utils/service_contexts.py ``member_border_ports``: a context has no interface edge).
        ``segment`` is the cleaned state segment (queries/topology/add/segment_vlans.gql).
        """
        context = segment.get("terminated_on") or {}
        return {
            port["id"]
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

    async def _segment_components(self, segment_id: str) -> list[dict[str, Any]]:
        """Every AppComponent on the segment, with its instances (segment_components.gql)."""
        result = await self.client.execute_graphql(
            query=load_query(_SEGMENT_COMPONENTS_QUERY_PATH), variables={"segment_id": segment_id}
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
        device_deployments: dict[str, str],
    ) -> tuple[int, int]:
        """Make the (port, deployment) edges this reconcile owns exactly the customer-facing targets of the instances
        of EVERY component on the segment plus ``terminated_port_ids`` (the border service ports of the firewall
        context terminating it), each joined to the segment's deployment its device sits in
        (``device_deployments``: {deployment id a device sits in: segment deployment id}).

        Adds a missing pair with this run's account as the edge source and removes an edge whose source is that
        account and whose pair is no longer derived. ``ports`` are the segment's port edges today,
        ``{"id", "device_id", "deployment_id", "source_id"}`` (SegmentVlanMixin._segment_ports). An edge with another
        source or none is never removed, and a port that already has a person's edge of the segment is not written
        (it keeps its empty source). A derived port whose device sits in no deployment of the segment is reported
        and its edges are left as they are. RelationshipAdd/RelationshipRemove on the port's ``capabilities``, one
        edge each, never in this run's tracking group. The account is resolved only when there is something to
        compare or write.

        Returns (edges added, edges removed).
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
        targets = await self._port_deployment_targets(desired_ids, device_deployments)
        undecided = sorted(port_id for port_id, deployment_id in targets.items() if deployment_id is None)
        for port_id in undecided:
            self.logger.warning(
                f"Segment {segment_name}: port {port_id} sits on a device in no deployment of the segment — not tagged"
            )
        desired = {(port_id, deployment_id) for port_id, deployment_id in targets.items() if deployment_id}

        current = {(port["id"], port["deployment_id"]) for port in ports}
        by_person = {port["id"] for port in ports if not port.get("source_id")}
        to_add = sorted(pair for pair in desired - current if pair[0] not in by_person)
        owned = [
            port
            for port in ports
            if port.get("source_id")
            and (port["id"], port["deployment_id"]) not in desired
            and port["id"] not in undecided
        ]
        to_remove: list[tuple[str, str]] = []
        if to_add or owned:
            account_id = await self._generator_account_id()
            to_remove = sorted((port["id"], port["deployment_id"]) for port in owned if port["source_id"] == account_id)
            for port_id, deployment_id in to_add:
                await add_edge_with_source(
                    self.client, node_id=port_id, relation="capabilities", peer_id=deployment_id, source_id=account_id
                )
            for port_id, deployment_id in to_remove:
                await remove_edge(self.client, node_id=port_id, relation="capabilities", peer_id=deployment_id)
        self.logger.info(
            f"Segment {segment_name}: {len(desired_ids)} port(s) from {len(components)} "
            f"component(s) and {len(terminated_port_ids)} firewall service port(s) — tagged {len(to_add)}, untagged {len(to_remove)}"
        )
        return len(to_add), len(to_remove)

    async def _port_deployment_targets(
        self, port_ids: set[str], device_deployments: dict[str, str]
    ) -> dict[str, str | None]:
        """{port id: the segment deployment the port's device sits in, None when it sits in none of them}, one read
        of the ports' devices and their ``deployment``."""
        if not port_ids:
            return {}
        result = await self.client.execute_graphql(
            query=_PORT_DEVICE_DEPLOYMENTS_QUERY, variables={"ids": sorted(port_ids)}
        )
        targets: dict[str, str | None] = dict.fromkeys(port_ids)
        for port in clean_data(result).get("DcimInterface") or []:
            deployment_id = (((port.get("device") or {}).get("deployment")) or {}).get("id")
            if port.get("id") in targets:
                targets[port["id"]] = device_deployments.get(deployment_id) if deployment_id else None
        return targets

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
