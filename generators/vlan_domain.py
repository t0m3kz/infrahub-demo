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
domains themselves (a ManagedStandaloneVlanDomain and its vlan_pool, or a
ManagedMLAG) belong to the generator that creates the device and are only
read here, never saved.

Independent VLAN domains may reuse the same numeric VLAN ID, since IEEE
802.1Q VLAN ID has only local significance (unlike VNI, which is the real
DC-wide/fabric-wide segment identifier allocated in segment.py).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any

from utils.data_cleaning import clean_data

from .connections import BORDER_ROLE_FOR_SERVICES
from .devices import standalone_vlan_domain_name
from .logger import GeneratorError
from .protocols import DcimPhysicalDevice, ManagedStandaloneVlanDomain, ManagedVlanDomainSegment

# Devices that act as a deployment's EVPN Multi-Site border gateway for a
# stretched segment: the DC's border leaves and the metro's edges. `edge` is
# also the colocation metro's on-ramp router — its only VTEP, so it is both
# the metro's border gateway and (via AppInstance resolution, not here) where
# a stretched segment's customer ports land.
BORDER_GATEWAY_ROLES = frozenset({"border-leaf", "edge"})
# Interface kinds whose segment tag puts the device's VLAN domain in scope. A
# DcimVirtualInterface carrying the segment is an inline sub-interface on a
# firewall/LB HA pair, which takes the facing border leaf's VLAN instead.
TAGGABLE_INTERFACE_KINDS = frozenset({"DcimPhysicalInterface", "DcimLAGInterface"})
# Border-leaf ports facing a firewall/load-balancer: tagged with a segment
# only by its inline termination (segment.py), never by its AppComponents.
SERVICE_PORT_ROLES = frozenset(BORDER_ROLE_FOR_SERVICES.values())

_SEGMENT_VLAN_DOMAINS_QUERY_PATH = Path(__file__).resolve().parents[1] / "queries/topology/add/segment_vlan_domains.gql"


def segment_lock_key(segment_id: str) -> str:
    """The resource_lock key serializing every per-segment reconciliation
    (VLAN domain activations and, in AppInstanceSegmentGenerator, interface
    tags) across both generators and all of the segment's components."""
    return f"segment-vlan-domains-{segment_id}"


class VlanDomainMixin:
    """Mixin providing (segment, VLAN domain) -> local VLAN ID realization.

    Expects the host class to provide: ``client``, ``logger``,
    ``resource_lock`` (PoolMixin — every host composes PoolMixin).
    """

    client: Any
    logger: logging.Logger
    # PoolMixin.resource_lock — declared as a plain Callable attribute, not a
    # method, so it never shadows the real implementation via MRO (same
    # convention as EndpointUplinkMixin's cross-mixin attributes).
    resource_lock: Callable[[str], AbstractAsyncContextManager[Any]]

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

    async def _ensure_standalone_vlan_domain(self, device: Any) -> tuple[str, str | None]:
        """Look up a non-MLAG device's ManagedStandaloneVlanDomain (by its
        name, ``{device}-vlan-domain``) and its vlan_pool. Returns (domain
        id, pool id or None).

        Never saves either: the domain and its pool are created and owned by
        the generator that creates the device (rack/pod/dc/colocation). A
        save here would claim them for this segment's run, whose cleanup
        would then delete them for every other segment on the device. A
        missing domain is an error (raises): the device generator has not run.
        """
        domain_name = standalone_vlan_domain_name(device.name.value)
        domain = await self.client.get(
            kind=ManagedStandaloneVlanDomain,
            name__value=domain_name,
            include=["vlan_pool"],
            raise_when_missing=False,
        )
        if domain is None:
            message = (
                f"Standalone VLAN domain {domain_name} not found — the generator that created device "
                f"{device.name.value} owns it and must run first"
            )
            self.logger.error(message)  # raises GeneratorError under the generator's fail-on-error logger
            raise GeneratorError(message)
        vlan_pool_rel = getattr(domain, "vlan_pool", None)
        pool_id = getattr(vlan_pool_rel, "id", None) if vlan_pool_rel else None
        return domain.id, pool_id

    async def _create_vlan_domain_segment(
        self, segment_id: str, segment_name: str, domain_id: str, pool_id: str | None = None
    ) -> None:
        """Create one ManagedVlanDomainSegment (segment, VLAN domain) pair,
        allocating vlan_id from that domain's own pool via from_pool. Only
        called for a pair that does not exist yet (the caller holds the
        segment lock and has just read the existing pairs), so an existing
        pair's vlan_id is never re-allocated.

        Untracked (update_group_context=False): the pair is shared desired
        state of the segment, cleaned up explicitly by
        reconcile_segment_vlan_domains, never by a run's group. A known
        pool_id (a standalone domain just looked up) skips reading it back.
        """
        if not pool_id:
            domain = await self.client.get(kind="ManagedGenericVlanDomain", id=domain_id, include=["vlan_pool"])
            vlan_pool_rel = getattr(domain, "vlan_pool", None)
            pool_id = getattr(vlan_pool_rel, "id", None) if vlan_pool_rel else None
        if not pool_id:
            self.logger.error(
                f"VLAN domain {domain_id} has no vlan_pool — cannot allocate VLAN ID for segment {segment_name}"
            )
            return

        vlan_identifier = f"{segment_id}-{domain_id}-vlan"
        activation = await self.client.create(
            kind=ManagedVlanDomainSegment,
            data={
                "segment": {"id": segment_id},
                "vlan_domain": {"id": domain_id},
                "vlan_id": {"from_pool": {"id": pool_id}, "identifier": vlan_identifier},
            },
        )
        await activation.save(allow_upsert=True, update_group_context=False)
        self.logger.info(f"  Allocated VLAN ID from domain {domain_id}'s pool for segment {segment_name}")

    async def _ensure_vlan_domains_for_devices(self, devices: list[Any]) -> dict[str, str | None]:
        """Resolve each device's VLAN domain and return {domain_id: pool_id}
        for every distinct domain touched.

        Each device's own resolution is independent — a standalone domain is
        keyed by that device's own name, and an MLAG-paired device only reads
        its shared peer id — so these run concurrently; the gather's results
        are merged into one dict afterwards, single-threaded.
        """
        resolved = await asyncio.gather(*(self._resolve_device_vlan_domain(device) for device in devices))
        domain_pools: dict[str, str | None] = {}
        for domain_id, pool_id in resolved:
            domain_pools[domain_id] = domain_pools.get(domain_id) or pool_id
        return domain_pools

    async def _resolve_device_vlan_domain(self, device: Any) -> tuple[str, str | None]:
        """One device's (domain_id, pool_id) — the per-device body gathered by
        _ensure_vlan_domains_for_devices."""
        domain_kind, domain_id = await self._resolve_vlan_domain(device)
        pool_id = None
        if domain_kind == "DcimPhysicalDevice":
            domain_id, pool_id = await self._ensure_standalone_vlan_domain(device)
        return domain_id, pool_id

    @staticmethod
    def _device_deployment_ids(hosting_parent: dict[str, Any]) -> list[str]:
        """Deployment ids whose devices a segment can land on: the hosting
        parent itself (border leaves, a metro's edges) plus, for a DC, each of
        its pods — leafs and ToRs are deployed into the pod, not the DC. The
        queries select ids only on TopologyPod children."""
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
            for iface in segment.get("interface_capabilities") or []
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
                for iface in segment.get("interface_capabilities") or []
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
                    include=["capabilities"],
                ):
                    devices[device.id] = device
        return list(devices.values())

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
        segment is stretched. Missing activations are created (untracked,
        vlan_id from the domain's pool); existing ones keep their vlan_id;
        activations on domains that no longer need the segment are deleted.

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
        desired = await self._ensure_vlan_domains_for_devices(devices)
        existing: dict[str, str] = state["activations"]

        created = 0
        for domain_id, pool_id in desired.items():
            if domain_id not in existing:
                await self._create_vlan_domain_segment(segment_id, segment_name, domain_id, pool_id)
                created += 1

        stale = sorted(activation_id for domain_id, activation_id in existing.items() if domain_id not in desired)
        for activation_id in stale:
            await self.client.delete(kind=ManagedVlanDomainSegment, id=activation_id)

        self.logger.info(
            f"Segment {segment_name}: {len(desired)} VLAN domain(s) need it across {len(devices)} device(s) — "
            f"created {created}, removed {len(stale)} activation(s)"
        )
