"""Mixin realizing a segment's LOCAL VLAN ID per device (a ManagedVlanDomainSegment row is on ONE device).

Shared by VxlanSegmentGenerator (generators/topology/segment.py, border
gateway realization for a stretched segment) and AppInstanceSegmentGenerator
(generators/topology/app_instance_segment.py, customer-port realization
driven by an AppComponent's instances) — both need the same (segment, device)
-> local VLAN ID allocation, just reached from different device sets.

Ownership: a (segment deployment, device) row is reached by BOTH
generators and by every AppComponent on the segment, so it belongs to no single
run. Rows are therefore shared desired state, written untracked
(update_group_context=False) and reconciled explicitly per SEGMENT from what
the segment needs right now — see reconcile_vxlan_segment (generators/topology/segment_ports.py). A row is a
child of the segment's realization (ManagedSegmentDeployment, the VNI) in the
DC or metro its switch sits in: a switch maps to that deployment through its own
`deployment` (the hosting parent or one of its children). The devices
belong to the generator that creates them and are only read here, never saved.
A device with no deployment the segment is activated in is reported (WARNING)
and left as it is.

MLAG pairs: the two peers of a pair must carry the SAME VLAN ID for a segment (a dual-homed port-channel is one L2
broadcast domain), so every MLAG peer of a device that needs the segment gets a row too, even with no port of the
segment, and a pair shares one id. If the two members of a pair map to different segment deployments (or one to none)
the pair is reported and left as it is.

Local significance and consistency: an IEEE 802.1Q VLAN ID only has to be
unique within one device (the real DC-wide segment identity is the VNI,
allocated in segment.py), so a device never gives one id to two segments
(ManagedVlanDomainSegment is unique on [device, vlan_id]). But a
virtualisation cluster spans racks served by different switches and wants ONE
VLAN id per port group, so the id is chosen, not drawn from a per-device pool:
a segment keeps the id it already has elsewhere (a pair follows its existing member); a segment with
none gets the lowest id free on every device it lands on. Only when a device
already uses the preferred id for another segment does that group fall back
to the lowest id free on its members (with a WARNING); a device with no free id fails.
A row's id is never re-chosen: a pair whose existing rows differ (a late pairing) is reported (WARNING) and left
alone; checks/mlag_domains.py reports it too.
The devices' used ids are read, chosen and written under a lock per device
(taken in sorted order, inside the segment lock), so two segments landing on
one device never pick the same id.
"""

from __future__ import annotations

import logging
from contextlib import AsyncExitStack
from pathlib import Path
from typing import TYPE_CHECKING, Any

from utils.data_cleaning import clean_data
from utils.limits import CUSTOMER_VLAN_ID_MAX, CUSTOMER_VLAN_ID_MIN, GROUP_PROVIDER_STUBS, GROUP_SDWAN_GATEWAYS
from utils.vlan_domain import mlag_id

from .connections import BORDER_ROLE_FOR_SERVICES
from .protocols import DcimPhysicalDevice, ManagedMLAG, ManagedVlanDomainSegment

if TYPE_CHECKING:
    from .common import CommonGenerator
    from .pools import PoolMixin

# Devices that act as a deployment's EVPN Multi-Site border gateway for a
# stretched segment: the DC's border leaves and the colocation edges. `edge` is
# also the on-ramp router of a colocation zone or virtual colocation — its only
# VTEP, so it is both the metro site's border gateway and (via AppInstance resolution, not here) where
# a stretched segment's customer ports land.
BORDER_GATEWAY_ROLES = frozenset({"border-leaf", "edge"})
# An SD-WAN gateway, and a provider stub modelled inside the zone (an Equinix Fabric port, a Megaport MCR:
# `provider_stubs`), also carries the `edge` role and is deployed into the same colocation zone as the on-ramp
# router, so the zone lookup finds it. Neither terminates VXLAN segments and no generator gives it a VLAN domain,
# so it is never a border gateway. (The missing-domain error for a real border gateway stays.)
NON_BORDER_GATEWAY_GROUPS = frozenset({GROUP_SDWAN_GATEWAYS, GROUP_PROVIDER_STUBS})
# Border-leaf ports facing a firewall/load-balancer: tagged with a segment
# only by its inline termination (segment.py), never by its AppComponents.
SERVICE_PORT_ROLES = frozenset(BORDER_ROLE_FOR_SERVICES.values())

_SEGMENT_VLAN_DOMAINS_QUERY_PATH = Path(__file__).resolve().parents[1] / "queries/topology/add/segment_vlan_domains.gql"


def vlan_device_lock_key(device_id: str) -> str:
    """The resource_lock key serializing the choice of VLAN IDs on one device
    (taken inside the segment lock, devices in sorted order)."""
    return f"vlan-device-ids-{device_id}"


def segment_lock_key(segment_id: str) -> str:
    """The resource_lock key serializing every per-segment reconciliation
    (VLAN row activations and, in AppInstanceSegmentGenerator, interface
    tags) across both generators and all of the segment's components."""
    return f"segment-vlan-domains-{segment_id}"


class VlanDomainMixin:
    """Mixin providing (segment, VLAN domain) -> local VLAN ID realization.

    Expects the host class to provide: ``client``, ``logger``,
    ``resource_lock`` (PoolMixin — every host composes PoolMixin) and ``fail`` (CommonGenerator).
    """

    client: Any
    logger: logging.Logger
    # Type checking only: the real members of the host's other bases, aliased so nothing is defined at runtime and
    # nothing shadows them in the MRO.
    if TYPE_CHECKING:
        fail = CommonGenerator.fail
        resource_lock = PoolMixin.resource_lock

    def _choose_vlan_ids(
        self,
        segment_name: str,
        groups: list[tuple[str, ...]],
        new: set[str],
        taken: dict[str, set[int]],
        held: dict[str, int],
        preferred: int | None,
    ) -> dict[str, int]:
        """The vlan_id for each device in ``new`` (a device that needs a new row of the segment).

        ``groups`` are the devices that share one id (an MLAG pair, or a single device), every member included even
        when it already has its row; ``taken`` is the ids other segments use on each member; ``held`` is the id the
        segment already has on a member's row; ``preferred`` is the id the segment has on its other devices (None
        when it has none). A group uses ``held`` of an existing member, else ``preferred``, else the lowest id free
        on every device of every group: when that is free on all its members. Otherwise the group takes the lowest
        id free on its members, with a WARNING; a member left with no common free id takes its own lowest free id
        (WARNING; the pair's rows then differ and the check reports it); a device with no free id fails the run.
        """
        band = range(CUSTOMER_VLAN_ID_MIN, CUSTOMER_VLAN_ID_MAX + 1)
        default = preferred
        if default is None:
            busy = set().union(*(taken[device_id] for group in groups for device_id in group))
            default = next((vlan_id for vlan_id in band if vlan_id not in busy), None)
        chosen: dict[str, int] = {}
        for group in groups:
            wanted = next((held[device_id] for device_id in group if device_id in held), default)
            busy = set().union(*(taken[device_id] for device_id in group))
            if wanted is not None and wanted not in busy:
                common: int | None = wanted
            else:
                common = next((vlan_id for vlan_id in band if vlan_id not in busy), None)
                reason = (
                    f"VLAN ID {wanted} is already used by another segment on {', '.join(group)}"
                    if wanted is not None
                    else f"no VLAN ID is free on every device of the segment ({', '.join(group)} included)"
                )
                self.logger.warning(
                    f"Segment {segment_name}: {reason} — using {common if common is not None else 'its own lowest'} "
                    "there, so the segment has different VLAN IDs across devices"
                )
            for device_id in group:
                if device_id not in new:
                    continue
                if common is not None:
                    chosen[device_id] = common
                    continue
                own = next((vlan_id for vlan_id in band if vlan_id not in taken[device_id]), None)
                if own is None:
                    self.fail(
                        f"Device {device_id} has no free VLAN ID in {CUSTOMER_VLAN_ID_MIN}-{CUSTOMER_VLAN_ID_MAX} "
                        f"for segment {segment_name}"
                    )
                self.logger.warning(
                    f"Segment {segment_name}: no VLAN ID is free on both members of {', '.join(group)} — "
                    f"using {own} on {device_id}, so the pair's VLAN IDs differ"
                )
                chosen[device_id] = own
        return chosen

    async def _create_vlan_domain_segments(
        self,
        segment_name: str,
        segment_deployment_ids: list[str],
        new: dict[str, str],
        groups: list[tuple[str, ...]],
        kept_devices: list[str],
    ) -> None:
        """Create the missing rows of the segment, choosing each vlan_id.

        ``new`` is {device id: id of the segment deployment its row goes
        under}; only rows that do not exist yet, so an existing row's vlan_id
        is never re-chosen. ``groups`` are the devices sharing one id (an MLAG pair or a single
        device) for every group with a new row, the peers included. The members are locked in sorted order (inside
        the caller's segment lock, so two runs never hold one device each while
        waiting for the other's), then ONE read returns the ids in use on them
        and the segment's own ids on its ``kept_devices`` (a row of the segment
        is one under any of ``segment_deployment_ids``) — and the rows are
        written before the locks are released, so the next segment sees them.

        Untracked (update_group_context=False): the row is shared desired
        state of the segment, cleaned up explicitly by
        the segment reconcile, never by a run's group.
        """
        members = sorted({device_id for group in groups for device_id in group})
        async with AsyncExitStack() as stack:
            for device_id in members:
                await stack.enter_async_context(self.resource_lock(vlan_device_lock_key(device_id)))
            rows = await self.client.filters(
                kind=ManagedVlanDomainSegment,
                device__ids=sorted({*members, *kept_devices}),
                include=["segment_deployment", "device"],
            )
            taken: dict[str, set[int]] = {device_id: set() for device_id in members}
            held: dict[str, int] = {}
            own_ids: list[int] = []
            for row in rows:
                device_id = row.device.id
                if row.segment_deployment.id in segment_deployment_ids:
                    own_ids.append(row.vlan_id.value)
                    held[device_id] = row.vlan_id.value
                elif device_id in taken:
                    taken[device_id].add(row.vlan_id.value)
            chosen = self._choose_vlan_ids(segment_name, groups, set(new), taken, held, min(own_ids, default=None))
            for device_id in sorted(chosen):
                vlan_id = chosen[device_id]
                row = await self.client.create(
                    kind=ManagedVlanDomainSegment,
                    data={
                        "segment_deployment": {"id": new[device_id]},
                        "device": {"id": device_id},
                        "vlan_id": vlan_id,
                    },
                )
                await row.save(allow_upsert=True, update_group_context=False)
                self.logger.info(f"  Allocated VLAN ID {vlan_id} on device {device_id} for segment {segment_name}")

    @staticmethod
    def _device_deployment_ids(hosting_parent: dict[str, Any]) -> list[str]:
        """Deployment ids whose devices a segment can land on: the hosting
        parent itself (a DC's border leaves) plus its children that deploy
        devices: each pod of a DC (leafs and ToRs are deployed into the pod, not
        the DC), each zone and virtual colocation of a metro (the edges are
        deployed into those, the metro owns none). The queries select ids only
        on TopologyPod, TopologyColocationZone and TopologyVirtualColocation
        children."""
        pod_ids = [child["id"] for child in hosting_parent.get("children") or [] if child.get("id")]
        return [hosting_parent["id"], *pod_ids]

    async def _fetch_segment_vlan_state(self, segment_id: str) -> dict[str, Any] | None:
        """The segment's reconciliation inputs (segment_vlan_domains.gql), as
        {"segment": cleaned segment, "deployments": [{"id", "deployment_id",
        "rows": {device id: {"id", "vlan_id"}}}]}, one entry per ManagedSegmentDeployment; None when ``segment_id`` is not a
        ManagedVxlanSegment."""
        result = await self.client.execute_graphql(
            query=_SEGMENT_VLAN_DOMAINS_QUERY_PATH.read_text(), variables={"segment_id": segment_id}
        )
        cleaned = clean_data(result)
        segments = cleaned.get("ManagedVxlanSegment") or []
        if not segments:
            return None
        deployments: list[dict[str, Any]] = []
        for segment_deployment in cleaned.get("ManagedSegmentDeployment") or []:
            rows = {
                device_id: {"id": row["id"], "vlan_id": row.get("vlan_id")}
                for row in segment_deployment.get("vlan_domain_segments") or []
                if (device_id := (row.get("device") or {}).get("id"))
            }
            deployments.append(
                {
                    "id": segment_deployment["id"],
                    "deployment_id": (segment_deployment.get("deployment") or {}).get("id"),
                    "rows": rows,
                }
            )
        return {"segment": segments[0], "deployments": deployments}

    async def segment_vlan_by_device(self, segment_id: str) -> dict[str, int]:
        """{device id: vlan_id} of the segment's rows, whatever deployment they sit under."""
        state = await self._fetch_segment_vlan_state(segment_id)
        return {
            device_id: row["vlan_id"]
            for segment_deployment in (state or {}).get("deployments", [])
            for device_id, row in segment_deployment["rows"].items()
        }

    @classmethod
    def _deployment_by_device_deployment(cls, state: dict[str, Any]) -> dict[str, str]:
        """{deployment id a device sits in: id of the segment's deployment there}.

        The segment's deployment is on the hosting parent (a DC, a metro, an
        office); a device sits in the parent itself or in one of the children
        that deploy devices (cls._device_deployment_ids)."""
        by_hosting = {item["deployment_id"]: item["id"] for item in state["deployments"] if item["deployment_id"]}
        mapping = dict(by_hosting)
        for customer_deployment in state["segment"].get("customer_deployments") or []:
            parent = customer_deployment.get("parent") or {}
            if segment_deployment_id := by_hosting.get(parent.get("id")):
                for deployment_id in cls._device_deployment_ids(parent):
                    mapping.setdefault(deployment_id, segment_deployment_id)
        return mapping

    @staticmethod
    def tagged_interface_ids(segment: dict[str, Any], *, service_ports: bool = False) -> set[str]:
        """Ids of the switch ports (physical or port-channel) carrying the
        segment: its customer-facing ports, or with service_ports the
        border-leaf ports facing its inline-terminating HA pair."""
        return {
            iface["id"]
            for iface in segment.get("switched_interfaces") or []
            if iface.get("id") and (iface.get("role") in SERVICE_PORT_ROLES) == service_ports
        }

    async def _segment_vlan_devices(self, segment: dict[str, Any]) -> list[Any]:
        """Every device that needs a row of the segment, capabilities and deployment
        included: the devices of the switch ports tagged with it, plus — for
        a stretched segment — every border gateway of each hosting parent, plus the MLAG peer of each of them
        (a peer with no port of the segment still carries its VLAN: a dual-homed port-channel is on both)."""
        tagged_device_ids = sorted(
            {
                device_id
                for iface in segment.get("switched_interfaces") or []
                if (device_id := (iface.get("device") or {}).get("id"))
            }
        )
        devices: dict[str, Any] = {}
        if tagged_device_ids:
            for device in await self.client.filters(
                kind=DcimPhysicalDevice, ids=tagged_device_ids, include=["capabilities", "deployment"]
            ):
                devices[device.id] = device

        if (segment.get("stretch_scope") or "local") != "local":
            deployment_ids: list[str] = []
            for customer_deployment in segment.get("customer_deployments") or []:
                parent = customer_deployment.get("parent") or {}
                if parent.get("id"):
                    deployment_ids.extend(self._device_deployment_ids(parent))
            if deployment_ids:
                for device in await self.client.filters(
                    kind=DcimPhysicalDevice,
                    deployment__ids=list(dict.fromkeys(deployment_ids)),
                    role__values=sorted(BORDER_GATEWAY_ROLES),
                    include=["capabilities", "member_of_groups", "deployment"],
                ):
                    if not NON_BORDER_GATEWAY_GROUPS.isdisjoint(self._group_names(device)):
                        continue
                    devices[device.id] = device

        mlag_ids = sorted({mlag for device in devices.values() if (mlag := self._mlag_of(device))})
        if mlag_ids:
            mlags = await self.client.filters(kind=ManagedMLAG, ids=mlag_ids, include=["capabilities"])
            peer_ids = sorted(
                {peer.id for mlag in mlags for peer in mlag.capabilities.peers if peer.id and peer.id not in devices}
            )
            if peer_ids:
                for device in await self.client.filters(
                    kind=DcimPhysicalDevice, ids=peer_ids, include=["capabilities", "deployment"]
                ):
                    devices[device.id] = device
        return list(devices.values())

    @staticmethod
    def _mlag_of(device: Any) -> str | None:
        """The id of the MLAG the device (fetched with its capabilities) is a member of."""
        capabilities = getattr(device, "capabilities", None)
        return mlag_id((peer.typename, peer.id) for peer in capabilities.peers) if capabilities else None

    @staticmethod
    def _group_names(device: Any) -> set[str]:
        """Names of the groups a device was fetched with (a group's HFID is its name)."""
        return {peer.hfid[0] for peer in device.member_of_groups.peers if peer.hfid}

    async def _reconcile_segment_vlan_domains_locked(
        self, segment_id: str, segment_name: str, state: dict[str, Any] | None = None
    ) -> bool:
        """Desired rows = every switch with a port carrying the segment, plus every border gateway when the
        segment is stretched, plus the MLAG peer of each (a pair shares one VLAN id), each under the segment's
        deployment its switch sits in. Rows on devices that no longer need the segment (or that moved to
        another deployment) are deleted (freeing their ids); missing ones are
        created (untracked, vlan_id chosen — see _create_vlan_domain_segments);
        existing ones keep their vlan_id, so a re-run writes nothing.

        A device the segment has no deployment for (a port in a DC the segment
        is not activated in, or the deployment not created yet), and a pair
        whose members map to two deployments or to none, are reported and skipped, and
        the rows of their devices are left as they are: add_vxlan_segment
        reconciles again once the deployment exists. A pair whose existing rows
        carry different ids (a late pairing) is reported and not renumbered.

        The caller must hold the segment lock (segment_lock_key). ``state``
        is a fresh _fetch_segment_vlan_state result, or None to read it here.

        Returns whether it created or deleted a row.
        """
        if state is None:
            state = await self._fetch_segment_vlan_state(segment_id)
        if state is None:
            self.logger.info(f"Segment {segment_name}: not a VXLAN segment — no VLAN rows")
            return False

        devices = await self._segment_vlan_devices(state["segment"])
        deployment_by_device_deployment = self._deployment_by_device_deployment(state)

        by_group: dict[str, list[Any]] = {}
        for device in devices:
            by_group.setdefault(self._mlag_of(device) or device.id, []).append(device)

        desired: dict[str, str] = {}
        undecided: set[str] = set()
        groups: list[tuple[str, ...]] = []
        for members in by_group.values():
            segment_deployments = {
                device.id: deployment_by_device_deployment.get(getattr(getattr(device, "deployment", None), "id", None))
                for device in members
            }
            for device in members:
                if segment_deployments[device.id] is None:
                    self.logger.warning(
                        f"Segment {segment_name}: device {device.name.value} sits in no deployment the segment is "
                        "activated in — no VLAN ID for it"
                    )
            if len(set(segment_deployments.values())) > 1:
                undecided.update(segment_deployments)
                if None not in segment_deployments.values():
                    self.logger.warning(
                        f"Segment {segment_name}: the MLAG pair {', '.join(sorted(segment_deployments))} sits in two "
                        "deployments — left as it is"
                    )
            elif None in segment_deployments.values():
                undecided.update(segment_deployments)
            else:
                desired.update(segment_deployments)
                groups.append(tuple(sorted(segment_deployments)))

        existing: dict[str, tuple[str, str, int]] = {
            device_id: (row["id"], item["id"], row["vlan_id"])
            for item in state["deployments"]
            for device_id, row in item["rows"].items()
        }
        for group in groups:
            ids = {existing[device_id][2] for device_id in group if device_id in existing}
            if len(group) > 1 and len(ids) > 1:
                self.logger.warning(
                    f"Segment {segment_name}: the MLAG pair {' and '.join(group)} has different VLAN IDs "
                    f"({', '.join(f'{device_id}: {existing[device_id][2]}' for device_id in group if device_id in existing)}) "
                    "— not renumbered, fix one row by hand"
                )

        stale = sorted(
            row_id
            for device_id, (row_id, deployment_id, _) in existing.items()
            if device_id not in undecided and desired.get(device_id) != deployment_id
        )
        for row_id in stale:
            await self.client.delete(kind=ManagedVlanDomainSegment, id=row_id)

        # After the deletes: the ids they held are free for the new rows below.
        new = {
            device_id: deployment_id
            for device_id, deployment_id in desired.items()
            if existing.get(device_id, (None, None, None))[1] != deployment_id
        }
        if new:
            kept_devices = sorted(set(desired) & existing.keys() - new.keys())
            await self._create_vlan_domain_segments(
                segment_name,
                [item["id"] for item in state["deployments"]],
                new,
                [group for group in groups if not new.keys().isdisjoint(group)],
                kept_devices,
            )

        self.logger.info(
            f"Segment {segment_name}: {len(desired)} device(s) need it (peers included) — "
            f"created {len(new)}, removed {len(stale)} row(s)"
        )
        return bool(new or stale)
