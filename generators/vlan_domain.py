"""Mixin realizing a segment's LOCAL VLAN ID per VLAN domain (an MLAG pair, or
a device without MLAG, which is its own domain).

Shared by VxlanSegmentGenerator (generators/topology/segment.py, border
gateway realization for a stretched segment) and AppInstanceSegmentGenerator
(generators/topology/app_instance_segment.py, customer-port realization
driven by an AppComponent's instances) — both need the same (segment, VLAN
domain) -> local VLAN ID allocation, just reached from different device sets.

Ownership: a (segment deployment, VLAN domain) row is reached by BOTH
generators and by every AppComponent on the segment, so it belongs to no single
run. Rows are therefore shared desired state, written untracked
(update_group_context=False) and reconciled explicitly per SEGMENT from what
the segment needs right now — see reconcile_vxlan_segment (generators/topology/segment_ports.py). A row is a
child of the segment's realization (ManagedSegmentDeployment, the VNI) in the
DC or metro its switch sits in: a switch maps to that deployment through its own
`deployment` (the hosting parent or one of its children). The VLAN domains
themselves (a device, or a ManagedMLAG) belong to the generator that creates
the device and are only read here, never saved. A domain has ONE deployment
(an MLAG pair sits in one pod): a domain whose devices map to two, or a device
with no deployment of the segment, is reported (WARNING) and left as it is.

Local significance and consistency: an IEEE 802.1Q VLAN ID only has to be
unique within one VLAN domain (the real DC-wide segment identity is the VNI,
allocated in segment.py), so a domain never gives one id to two segments
(ManagedVlanDomainSegment is unique on [vlan_domain, vlan_id]). But a
virtualisation cluster spans racks served by different domains and wants ONE
VLAN id per port group, so the id is chosen, not drawn from a per-domain pool:
a segment keeps the id it already has on its other domains; a segment with
none gets the lowest id free on every domain it lands on. Only when a domain
already uses the preferred id for another segment does that domain fall back
to its own lowest free id (with a WARNING); a domain with no free id fails.
The domains' used ids are read, chosen and written under a lock per domain
(taken in sorted order, inside the segment lock), so two segments landing on
one domain never pick the same id.
"""

from __future__ import annotations

import logging
from contextlib import AsyncExitStack
from pathlib import Path
from typing import TYPE_CHECKING, Any

from utils.data_cleaning import clean_data
from utils.limits import CUSTOMER_VLAN_ID_MAX, CUSTOMER_VLAN_ID_MIN, GROUP_PROVIDER_STUBS, GROUP_SDWAN_GATEWAYS
from utils.vlan_domain import vlan_domain_id

from .connections import BORDER_ROLE_FOR_SERVICES
from .protocols import DcimPhysicalDevice, ManagedVlanDomainSegment

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
# Interface kinds whose segment tag puts the device's VLAN domain in scope. A
# DcimVirtualInterface carrying the segment is an inline sub-interface on a
# firewall/LB HA pair, which takes the facing border leaf's VLAN instead.
TAGGABLE_INTERFACE_KINDS = frozenset({"DcimPhysicalInterface", "DcimLAGInterface"})
# Border-leaf ports facing a firewall/load-balancer: tagged with a segment
# only by its inline termination (segment.py), never by its AppComponents.
SERVICE_PORT_ROLES = frozenset(BORDER_ROLE_FOR_SERVICES.values())

_SEGMENT_VLAN_DOMAINS_QUERY_PATH = Path(__file__).resolve().parents[1] / "queries/topology/add/segment_vlan_domains.gql"


def vlan_domain_lock_key(domain_id: str) -> str:
    """The resource_lock key serializing the choice of VLAN IDs on one VLAN
    domain (taken inside the segment lock, domains in sorted order)."""
    return f"vlan-domain-ids-{domain_id}"


def segment_lock_key(segment_id: str) -> str:
    """The resource_lock key serializing every per-segment reconciliation
    (VLAN domain activations and, in AppInstanceSegmentGenerator, interface
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

    @staticmethod
    def _resolve_vlan_domains(devices: list[Any]) -> dict[str, str]:
        """{device id: VLAN domain id} for every device, capabilities included.

        The rule is utils/vlan_domain.vlan_domain_id (the MLAG first, else the
        device). Never saves a domain: a ManagedMLAG is created and owned by the generator
        that creates the device (rack/pod/dc/colocation), and a save here would
        claim it for this segment's run, whose cleanup would then delete it for
        every other segment on the device.
        """
        domain_by_device: dict[str, str] = {}
        for device in devices:
            capabilities = getattr(device, "capabilities", None)
            peers = capabilities.peers if capabilities else []
            domain_by_device[device.id] = (
                vlan_domain_id(device.id, ((peer.typename, peer.id) for peer in peers)) or device.id
            )
        return domain_by_device

    def _choose_vlan_ids(
        self, segment_name: str, new_domains: list[str], taken: dict[str, set[int]], preferred: int | None
    ) -> dict[str, int]:
        """The vlan_id for each domain that needs a new activation of the segment.

        ``taken`` is the ids other segments use on each new domain; ``preferred``
        is the id the segment already has on its other domains (None when it has
        none). One id is used everywhere when it is free on every domain: the
        preferred one, else the lowest free on all of them. A domain where it is
        taken (or when no id is free on all) gets its own lowest free id, with a
        WARNING; a domain with no free id in the customer band fails the run.
        """
        band = range(CUSTOMER_VLAN_ID_MIN, CUSTOMER_VLAN_ID_MAX + 1)
        wanted = preferred
        if wanted is None:
            busy = set().union(*(taken[domain_id] for domain_id in new_domains))
            wanted = next((vlan_id for vlan_id in band if vlan_id not in busy), None)
        chosen: dict[str, int] = {}
        for domain_id in new_domains:
            if wanted is not None and wanted not in taken[domain_id]:
                chosen[domain_id] = wanted
                continue
            own = next((vlan_id for vlan_id in band if vlan_id not in taken[domain_id]), None)
            if own is None:
                self.fail(
                    f"VLAN domain {domain_id} has no free VLAN ID in {CUSTOMER_VLAN_ID_MIN}-{CUSTOMER_VLAN_ID_MAX} "
                    f"for segment {segment_name}"
                )
            reason = (
                f"VLAN ID {wanted} is already used by another segment on VLAN domain {domain_id}"
                if wanted is not None
                else f"no VLAN ID is free on every VLAN domain of the segment (VLAN domain {domain_id} included)"
            )
            self.logger.warning(
                f"Segment {segment_name}: {reason} — using {own} there, so the segment has different VLAN IDs "
                "across domains"
            )
            chosen[domain_id] = own
        return chosen

    async def _create_vlan_domain_segments(
        self,
        segment_name: str,
        segment_deployment_ids: list[str],
        new: dict[str, str],
        kept_domains: list[str],
    ) -> None:
        """Create the missing rows of the segment, choosing each vlan_id.

        ``new`` is {VLAN domain id: id of the segment deployment its row goes
        under}; only rows that do not exist yet, so an existing row's vlan_id
        is never re-chosen. The new domains are locked in sorted order (inside
        the caller's segment lock, so two runs never hold one domain each while
        waiting for the other's), then ONE read returns the ids in use on them
        and the segment's own ids on its ``kept_domains`` (a row of the segment
        is one under any of ``segment_deployment_ids``) — and the rows are
        written before the locks are released, so the next segment sees them.

        Untracked (update_group_context=False): the row is shared desired
        state of the segment, cleaned up explicitly by
        the segment reconcile, never by a run's group.
        """
        new_domains = sorted(new)
        async with AsyncExitStack() as stack:
            for domain_id in new_domains:
                await stack.enter_async_context(self.resource_lock(vlan_domain_lock_key(domain_id)))
            rows = await self.client.filters(
                kind=ManagedVlanDomainSegment,
                vlan_domain__ids=sorted({*new_domains, *kept_domains}),
                include=["segment_deployment", "vlan_domain"],
            )
            taken: dict[str, set[int]] = {domain_id: set() for domain_id in new_domains}
            own_ids: list[int] = []
            for row in rows:
                domain_id = row.vlan_domain.id
                if row.segment_deployment.id in segment_deployment_ids:
                    own_ids.append(row.vlan_id.value)
                elif domain_id in taken:
                    taken[domain_id].add(row.vlan_id.value)
            chosen = self._choose_vlan_ids(segment_name, new_domains, taken, min(own_ids, default=None))
            for domain_id, vlan_id in chosen.items():
                row = await self.client.create(
                    kind=ManagedVlanDomainSegment,
                    data={
                        "segment_deployment": {"id": new[domain_id]},
                        "vlan_domain": {"id": domain_id},
                        "vlan_id": vlan_id,
                    },
                )
                await row.save(allow_upsert=True, update_group_context=False)
                self.logger.info(f"  Allocated VLAN ID {vlan_id} on VLAN domain {domain_id} for segment {segment_name}")

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
        "rows": {domain id: {"id", "vlan_id"}}}], "domain_devices": {domain id:
        [device ids]}}, one entry per ManagedSegmentDeployment; None when ``segment_id`` is not a
        ManagedVxlanSegment."""
        result = await self.client.execute_graphql(
            query=_SEGMENT_VLAN_DOMAINS_QUERY_PATH.read_text(), variables={"segment_id": segment_id}
        )
        cleaned = clean_data(result)
        segments = cleaned.get("ManagedVxlanSegment") or []
        if not segments:
            return None
        deployments: list[dict[str, Any]] = []
        domain_devices: dict[str, list[str]] = {}
        for segment_deployment in cleaned.get("ManagedSegmentDeployment") or []:
            rows = {}
            for row in segment_deployment.get("vlan_domain_segments") or []:
                domain = row.get("vlan_domain") or {}
                if domain_id := domain.get("id"):
                    rows[domain_id] = {"id": row["id"], "vlan_id": row.get("vlan_id")}
                    members = domain.get("capabilities") if domain.get("typename") == "ManagedMLAG" else [domain]
                    domain_devices[domain_id] = [member["id"] for member in members or [] if member.get("id")]
            deployments.append(
                {
                    "id": segment_deployment["id"],
                    "deployment_id": (segment_deployment.get("deployment") or {}).get("id"),
                    "rows": rows,
                }
            )
        return {"segment": segments[0], "deployments": deployments, "domain_devices": domain_devices}

    async def segment_vlan_by_domain(self, segment_id: str) -> dict[str, int]:
        """{VLAN domain id: vlan_id} of the segment's rows, whatever deployment they sit under."""
        state = await self._fetch_segment_vlan_state(segment_id)
        return {
            domain_id: row["vlan_id"]
            for segment_deployment in (state or {}).get("deployments", [])
            for domain_id, row in segment_deployment["rows"].items()
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
            for iface in segment.get("capabilities") or []
            if iface.get("typename") in TAGGABLE_INTERFACE_KINDS
            and iface.get("id")
            and (iface.get("role") in SERVICE_PORT_ROLES) == service_ports
        }

    async def _segment_vlan_devices(self, segment: dict[str, Any]) -> list[Any]:
        """Every device whose VLAN domain needs the segment, capabilities
        included: the devices of the switch ports tagged with it, plus — for
        a stretched segment — every border gateway of each hosting parent."""
        tagged_device_ids = sorted(
            {
                device_id
                for iface in segment.get("capabilities") or []
                if iface.get("typename") in TAGGABLE_INTERFACE_KINDS
                and (device_id := (iface.get("device") or {}).get("id"))
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
        return list(devices.values())

    @staticmethod
    def _group_names(device: Any) -> set[str]:
        """Names of the groups a device was fetched with (a group's HFID is its name)."""
        return {peer.hfid[0] for peer in device.member_of_groups.peers if peer.hfid}

    async def _reconcile_segment_vlan_domains_locked(
        self, segment_id: str, segment_name: str, state: dict[str, Any] | None = None
    ) -> bool:
        """Desired rows = the VLAN domain of every switch with a port carrying
        the segment, plus every border gateway's domain when the segment is
        stretched, each under the segment's deployment its switch sits in.
        Rows on domains that no longer need the segment (or that moved to
        another deployment) are deleted (freeing their ids); missing ones are
        created (untracked, vlan_id chosen — see _create_vlan_domain_segments);
        existing ones keep their vlan_id, so a re-run writes nothing.

        A device the segment has no deployment for (a port in a DC the segment
        is not activated in, or the deployment not created yet), and a domain
        whose devices sit in two deployments, are reported and skipped, and
        the rows of their domains are left as they are: add_vxlan_segment
        reconciles again once the deployment exists.

        The caller must hold the segment lock (segment_lock_key). ``state``
        is a fresh _fetch_segment_vlan_state result, or None to read it here.

        Returns whether it created or deleted a row.
        """
        if state is None:
            state = await self._fetch_segment_vlan_state(segment_id)
        if state is None:
            self.logger.info(f"Segment {segment_name}: not a VXLAN segment — no VLAN domain rows")
            return False

        devices = await self._segment_vlan_devices(state["segment"])
        domain_by_device = self._resolve_vlan_domains(devices)
        deployment_by_device_deployment = self._deployment_by_device_deployment(state)

        desired: dict[str, str] = {}
        undecided: set[str] = set()
        for device in devices:
            domain_id = domain_by_device[device.id]
            deployment_id = getattr(getattr(device, "deployment", None), "id", None)
            segment_deployment_id = deployment_by_device_deployment.get(deployment_id)
            if segment_deployment_id is None:
                undecided.add(domain_id)
                self.logger.warning(
                    f"Segment {segment_name}: device {device.name.value} sits in no deployment the segment is "
                    "activated in — no VLAN ID for it"
                )
            elif desired.setdefault(domain_id, segment_deployment_id) != segment_deployment_id:
                undecided.add(domain_id)
                self.logger.warning(
                    f"Segment {segment_name}: VLAN domain {domain_id} has devices in two deployments — left as it is"
                )
        desired = {domain_id: dep_id for domain_id, dep_id in desired.items() if domain_id not in undecided}

        existing: dict[str, tuple[str, str]] = {
            domain_id: (row["id"], item["id"])
            for item in state["deployments"]
            for domain_id, row in item["rows"].items()
        }
        stale = sorted(
            row_id
            for domain_id, (row_id, deployment_id) in existing.items()
            if domain_id not in undecided and desired.get(domain_id) != deployment_id
        )
        for row_id in stale:
            await self.client.delete(kind=ManagedVlanDomainSegment, id=row_id)

        # After the deletes: the ids they held are free for the new rows below.
        new = {
            domain_id: deployment_id
            for domain_id, deployment_id in desired.items()
            if existing.get(domain_id, (None, None))[1] != deployment_id
        }
        if new:
            kept_domains = sorted(set(desired) & existing.keys() - new.keys())
            await self._create_vlan_domain_segments(
                segment_name, [item["id"] for item in state["deployments"]], new, kept_domains
            )

        self.logger.info(
            f"Segment {segment_name}: {len(desired)} VLAN domain(s) need it across {len(devices)} device(s) — "
            f"created {len(new)}, removed {len(stale)} row(s)"
        )
        return bool(new or stale)
