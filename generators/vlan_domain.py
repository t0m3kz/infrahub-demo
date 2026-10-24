"""Mixin realizing a segment's LOCAL VLAN ID per VLAN domain (MLAG pair or
standalone device).

Shared by VxlanSegmentGenerator (generators/topology/segment.py, border
gateway realization for a stretched segment) and AppInstanceSegmentGenerator
(generators/topology/app_instance_segment.py, customer-port realization
driven by an AppComponent's instances) — both need the same (segment, VLAN
domain) -> local VLAN ID allocation, just reached from different device sets.

Ownership: a (segment, VLAN domain) activation is reached by BOTH generators
and by every AppComponent on the segment, so it belongs to no single run.
Activations are therefore shared desired state, written untracked
(update_group_context=False) and reconciled explicitly per SEGMENT from what
the segment needs right now — see reconcile_segment_vlan_domains. The VLAN
domains themselves (a ManagedStandaloneVlanDomain or a ManagedMLAG) belong to
the generator that creates the device and are only read here, never saved.

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
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack
from pathlib import Path
from typing import Any, NoReturn

from utils.data_cleaning import clean_data

from .connections import BORDER_ROLE_FOR_SERVICES
from .domain_pairing import standalone_vlan_domain_name
from .helpers.pools import CUSTOMER_VLAN_ID_MAX, CUSTOMER_VLAN_ID_MIN
from .protocols import DcimPhysicalDevice, ManagedStandaloneVlanDomain, ManagedVlanDomainSegment

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
NON_BORDER_GATEWAY_GROUPS = frozenset({"sdwan_gateways", "provider_stubs"})
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
    # PoolMixin.resource_lock — declared as a plain Callable attribute, not a
    # method, so it never shadows the real implementation via MRO (same
    # convention as EndpointUplinkMixin's cross-mixin attributes).
    resource_lock: Callable[[str], AbstractAsyncContextManager[Any]]
    # CommonGenerator.fail — same convention.
    fail: Callable[[str], NoReturn]

    async def _resolve_vlan_domain(self, device: Any) -> tuple[str, str]:
        """Return (domain_kind, domain_id) for a leaf/tor device: its
        ManagedMLAG if paired, else the device itself is its own standalone
        VLAN domain. Requires device.capabilities to already be fetched
        (batch-included by the caller to avoid an N+1 query pattern)."""
        caps = getattr(device, "capabilities", None)
        if caps is not None:
            for peer in caps.peers:
                if peer.typename == "ManagedMLAG":
                    return "ManagedMLAG", peer.id
        return "DcimPhysicalDevice", device.id

    async def _resolve_vlan_domains(self, devices: list[Any]) -> dict[str, str]:
        """{device id: VLAN domain id} for every device, capabilities included.

        An MLAG-paired device's domain is its ManagedMLAG; every other device's
        is its ManagedStandaloneVlanDomain (``{device}-vlan-domain``), all found
        by ONE read on their names. Never saves a domain: it is created and owned
        by the generator that creates the device (rack/pod/dc/colocation), and a
        save here would claim it for this segment's run, whose cleanup would then
        delete it for every other segment on the device. A missing standalone
        domain is an error (raises): the device generator has not run.
        """
        domain_by_device: dict[str, str] = {}
        standalone_devices: dict[str, list[str]] = {}
        for device in devices:
            domain_kind, domain_id = await self._resolve_vlan_domain(device)
            if domain_kind == "ManagedMLAG":
                domain_by_device[device.id] = domain_id
            else:
                standalone_devices.setdefault(standalone_vlan_domain_name(device.name.value), []).append(device.id)
        if standalone_devices:
            found = await self.client.filters(kind=ManagedStandaloneVlanDomain, name__values=sorted(standalone_devices))
            domain_id_by_name = {domain.name.value: domain.id for domain in found}
            missing = sorted(set(standalone_devices) - set(domain_id_by_name))
            if missing:
                self.fail(
                    f"Standalone VLAN domain(s) {', '.join(missing)} not found — the generator that created the "
                    "device owns them and must run first"
                )
            for name, device_ids in standalone_devices.items():
                for device_id in device_ids:
                    domain_by_device[device_id] = domain_id_by_name[name]
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
        self, segment_id: str, segment_name: str, new_domains: list[str], kept_domains: list[str]
    ) -> None:
        """Create the segment's missing (segment, VLAN domain) pairs, choosing each vlan_id.

        Only called for pairs that do not exist yet, so an existing pair's
        vlan_id is never re-chosen. The new domains are locked in sorted order
        (inside the caller's segment lock, so two runs never hold one domain each
        while waiting for the other's), then ONE read returns the ids in use on
        them and the segment's own ids on its ``kept_domains`` — and the pairs are
        written before the locks are released, so the next segment sees them.

        Untracked (update_group_context=False): the pair is shared desired
        state of the segment, cleaned up explicitly by
        reconcile_segment_vlan_domains, never by a run's group.
        """
        async with AsyncExitStack() as stack:
            for domain_id in new_domains:
                await stack.enter_async_context(self.resource_lock(vlan_domain_lock_key(domain_id)))
            activations = await self.client.filters(
                kind=ManagedVlanDomainSegment,
                vlan_domain__ids=sorted({*new_domains, *kept_domains}),
                include=["segment", "vlan_domain"],
            )
            taken: dict[str, set[int]] = {domain_id: set() for domain_id in new_domains}
            own_ids: list[int] = []
            for activation in activations:
                domain_id = activation.vlan_domain.id
                if activation.segment.id == segment_id:
                    own_ids.append(activation.vlan_id.value)
                elif domain_id in taken:
                    taken[domain_id].add(activation.vlan_id.value)
            chosen = self._choose_vlan_ids(segment_name, new_domains, taken, min(own_ids, default=None))
            for domain_id, vlan_id in chosen.items():
                activation = await self.client.create(
                    kind=ManagedVlanDomainSegment,
                    data={"segment": {"id": segment_id}, "vlan_domain": {"id": domain_id}, "vlan_id": vlan_id},
                )
                await activation.save(allow_upsert=True, update_group_context=False)
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
        {"segment": cleaned segment, "activations": {domain_id: activation_id}};
        None when ``segment_id`` is not a ManagedVxlanSegment (a VLAN segment
        has a manual vlan_id and no VLAN domain activations)."""
        result = await self.client.execute_graphql(
            query=_SEGMENT_VLAN_DOMAINS_QUERY_PATH.read_text(), variables={"segment_id": segment_id}
        )
        cleaned = clean_data(result)
        segments = cleaned.get("ManagedVxlanSegment") or []
        if not segments:
            return None
        activations: dict[str, str] = {}
        for activation in cleaned.get("ManagedVlanDomainSegment") or []:
            domain_id = (activation.get("vlan_domain") or {}).get("id")
            if domain_id:
                activations[domain_id] = activation["id"]
        return {"segment": segments[0], "activations": activations}

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
                kind=DcimPhysicalDevice, ids=tagged_device_ids, include=["capabilities"]
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
                    include=["capabilities", "member_of_groups"],
                ):
                    if not NON_BORDER_GATEWAY_GROUPS.isdisjoint(self._group_names(device)):
                        continue
                    devices[device.id] = device
        return list(devices.values())

    @staticmethod
    def _group_names(device: Any) -> set[str]:
        """Names of the groups a device was fetched with (a group's HFID is its name)."""
        return {peer.hfid[0] for peer in device.member_of_groups.peers if peer.hfid}

    async def reconcile_segment_vlan_domains(self, segment_id: str, segment_name: str) -> None:
        """Bring the segment's ManagedVlanDomainSegment activations to desired
        state, under the per-segment lock. See
        _reconcile_segment_vlan_domains_locked."""
        async with self.resource_lock(segment_lock_key(segment_id)):
            await self._reconcile_segment_vlan_domains_locked(segment_id, segment_name)

    async def _reconcile_segment_vlan_domains_locked(
        self, segment_id: str, segment_name: str, state: dict[str, Any] | None = None
    ) -> None:
        """Desired domains = the VLAN domain of every switch with a port
        carrying the segment, plus every border gateway's domain when the
        segment is stretched. Activations on domains that no longer need the
        segment are deleted (freeing their ids); missing ones are created
        (untracked, vlan_id chosen — see _create_vlan_domain_segments); existing
        ones keep their vlan_id, so a re-run writes nothing.

        The caller must hold the segment lock (segment_lock_key). ``state``
        is a fresh _fetch_segment_vlan_state result, or None to read it here.
        """
        if state is None:
            state = await self._fetch_segment_vlan_state(segment_id)
        if state is None:
            self.logger.info(f"Segment {segment_name}: not a VXLAN segment — no VLAN domain activations")
            return

        devices = await self._segment_vlan_devices(state["segment"])
        # Raises (fail-on-error logger) before anything is deleted when a
        # standalone domain is missing, so an unresolved domain can never
        # look like one that stopped needing the segment.
        desired = set((await self._resolve_vlan_domains(devices)).values())
        existing: dict[str, str] = state["activations"]

        stale = sorted(activation_id for domain_id, activation_id in existing.items() if domain_id not in desired)
        for activation_id in stale:
            await self.client.delete(kind=ManagedVlanDomainSegment, id=activation_id)

        # After the deletes: the ids they held are free for the new pairs below.
        new_domains = sorted(desired - existing.keys())
        if new_domains:
            kept_domains = sorted(desired & existing.keys())
            await self._create_vlan_domain_segments(segment_id, segment_name, new_domains, kept_domains)

        self.logger.info(
            f"Segment {segment_name}: {len(desired)} VLAN domain(s) need it across {len(devices)} device(s) — "
            f"created {len(new_domains)}, removed {len(stale)} activation(s)"
        )
