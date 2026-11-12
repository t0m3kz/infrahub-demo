"""Converges the VRF instances (``ManagedVRF``), their members and the segment SVIs; the ONLY writer of those and of
``ManagedPBR``. Members are written edge by edge. Lock order: segment lock, device locks (sorted), instance locks
(sorted). Untracked. ``reconcile_vrfs`` adds memberships, upserts SVIs and PBR, then (unless held) prunes; ``vrfs_held``
keeps the device locks and never deletes.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Iterable, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from utils.data_cleaning import clean_data
from utils.limits import DEFAULT_NAMESPACE
from utils.query_flatten import load_flat_query
from utils.vrf_demand import (
    DeviceVrfFacts,
    device_vrf_facts,
    instance_name,
    namespaces_by_name,
    vrf_demand,
)

from ..protocols import ManagedPBR, ManagedVRF, TopologyExchangeGateway
from .svi import SviMixin

if TYPE_CHECKING:
    from ..common import CommonGenerator
    from ..pools import PoolMixin

_VRF_REFERENCES = ("capabilities", "session_circuits")
"""The edges of an instance that keep it alive besides its members: its interfaces and its session circuits."""

_VRF_INSTANCES_QUERY_PATH = Path(__file__).resolve().parents[2] / "queries/validation/vrf_instances.gql"


def vrf_device_lock_key(device_id: str) -> str:
    """The resource_lock key serializing the memberships and SVIs of one device."""
    return f"vrf-device-{device_id}"


def vrf_instance_lock_key(deployment_id: str, namespace: str) -> str:
    """The resource_lock key serializing create, delete and membership of one instance."""
    return f"vrf-{deployment_id}-{namespace}"


def pbr_name(device_name: str) -> str:
    """The name of a device's ``ManagedPBR``."""
    return f"{device_name}-pbr"


class VrfMixin(SviMixin):
    """Mixin converging VRF instances, their members and device SVIs; the host provides ``client``, ``logger``,
    ``resource_lock`` and ``fail``.
    """

    client: Any
    logger: logging.Logger
    if TYPE_CHECKING:
        fail = CommonGenerator.fail
        resource_lock = PoolMixin.resource_lock

    @asynccontextmanager
    async def vrfs_held(self, needs: Mapping[str, set[str]]) -> AsyncGenerator[None]:
        """Ensure each device ({device id: namespace names}) is a member of its instances and keep its lock while the
        body runs. The body writes what needs the instance, so a concurrent ``reconcile_vrfs`` cannot prune it first.
        Never deletes an instance; ``default`` names and VRF-less devices are ignored. The locks are not re-entrant.
        """
        async with self._locked(needs.keys()) as ids:
            await self._converge_locked(ids, extra=needs, prune=False)
            yield

    async def reconcile_vrfs(self, device_ids: Iterable[str]) -> None:
        """Converge memberships and SVIs of the devices to their demand, drop unneeded memberships and delete empty
        instances.
        """
        async with self._locked(device_ids) as ids:
            await self._converge_locked(ids, extra={}, prune=True)

    async def prune_empty_vrfs(self, deployment_ids: Iterable[str]) -> None:
        """Delete the instances of the deployments that have no member and no SVI."""
        ids = sorted(set(deployment_ids))
        if not ids:
            return
        nodes = await self.client.filters(
            kind=ManagedVRF, deployment__ids=ids, include=["shared_devices", "security_zone"]
        )
        for node in nodes:
            if node.shared_devices.peers:
                continue
            await self._delete_if_unused(node.deployment.id, node.security_zone.peer.name.value, node.id)

    @asynccontextmanager
    async def _locked(self, device_ids: Iterable[str]) -> AsyncGenerator[list[str]]:
        """The sorted, deduplicated ids, with each device's lock held in that order."""
        ids = sorted(set(device_ids))
        async with AsyncExitStack() as stack:
            for device_id in ids:
                await stack.enter_async_context(self.resource_lock(vrf_device_lock_key(device_id)))
            yield ids

    async def _converge_locked(self, ids: list[str], *, extra: Mapping[str, set[str]], prune: bool) -> None:
        """One pass over the devices; the caller holds their locks."""
        if not ids:
            return
        result = await self.client.execute_graphql(
            query=load_flat_query(_VRF_INSTANCES_QUERY_PATH), variables={"device_ids": ids, "with_instances": False}
        )
        cleaned = clean_data(result)
        namespaces = namespaces_by_name(cleaned)
        touched: set[str] = set()
        for node in cleaned.get("DcimDevice") or []:
            facts = device_vrf_facts(node)
            if hosting_id := await self._converge_device(
                facts, namespaces, set(extra.get(facts.id) or ()), prune=prune
            ):
                touched.add(hosting_id)
        if prune and touched:
            await self.prune_empty_vrfs(touched)

    async def _converge_device(
        self, facts: DeviceVrfFacts, namespaces: Mapping[str, dict[str, Any]], extra: set[str], *, prune: bool
    ) -> str | None:
        """Steps 1-3 of the pass for one device (lock held); returns its hosting deployment id, or None."""
        wanted = {name for name in extra if name != DEFAULT_NAMESPACE} if facts.carries_vrfs else set()
        # Demand once the stale SVIs are gone
        svi_namespaces = frozenset(row.namespace for row in facts.desired_svis().values())
        demand = vrf_demand(replace(facts.inputs, svi_namespaces=svi_namespaces)) | wanted
        if demand and not facts.hosting_id:
            self.logger.warning(
                f"  {facts.name}: needs VRF instances {sorted(demand)} but has no hosting deployment - skipped"
            )
            return None

        hosting = (facts.hosting_id or "", facts.hosting_name or "")
        have = {(inst.deployment_id or "", inst.namespace): inst for inst in facts.instances if inst.namespace}
        add = sorted(name for name in demand if (hosting[0], name) not in have)
        drop = sorted(key for key in have if key[0] != hosting[0] or key[1] not in demand)

        instance_ids: dict[str, str] = {
            name: have[(hosting[0], name)].id for name in sorted(demand) if (hosting[0], name) in have
        }
        missing = [name for name in add if not (namespaces.get(name) or {}).get("id")]
        if missing:
            self.fail(f"{facts.name}: namespace {missing[0]} not found - cannot create its VRF instance")
        if add:
            async with AsyncExitStack() as stack:
                for name in add:
                    await stack.enter_async_context(self.resource_lock(vrf_instance_lock_key(hosting[0], name)))
                for name in add:
                    instance_ids[name] = await self._join_instance(facts, hosting[0], hosting[1], namespaces[name])

        await self.sync_svis(facts, instance_ids)
        await self.sync_l3vni_svis(facts, namespaces, instance_ids)
        # The device has a ManagedPBR exactly when one of its SVIs is steered
        await self.sync_pbr(facts)

        if prune:
            for key in drop:
                await self._delete_if_unused(key[0], key[1], have[key].id, leaving=facts)
        return facts.hosting_id

    async def sync_pbr(self, facts: DeviceVrfFacts) -> None:
        """Create the device's ``ManagedPBR`` when it steers a tenant SVI and has none, delete it when it steers none.
        The steering is derived at render time; the capability only says the device does it."""
        steers = facts.steers()
        if steers and not facts.pbr_ids:
            pbr = await self.client.create(
                kind=ManagedPBR,
                data={"name": pbr_name(facts.name), "status": "active", "capabilities": [{"id": facts.id}]},
            )
            await pbr.save(allow_upsert=True, update_group_context=False)
            self.logger.info(f"  {facts.name}: steers its tenant SVIs - created {pbr_name(facts.name)}")
        elif not steers:
            for pbr_id in facts.pbr_ids:
                await self.client.delete(kind=ManagedPBR, id=pbr_id)
                self.logger.info(f"  {facts.name}: steers no SVI any more - removed its PBR")

    async def _join_instance(
        self, facts: DeviceVrfFacts, deployment_id: str, deployment_name: str, namespace: Mapping[str, Any]
    ) -> str:
        """Find or create the topology's instance of the namespace and add the device to it (instance lock held);
        returns its id. A new instance is created WITH its first member in one write, since a RelationshipAdd on a
        just-created node can hit NODE_NOT_FOUND.
        """
        found = await self.client.filters(
            kind=ManagedVRF,
            deployment__ids=[deployment_id],
            security_zone__ids=[namespace["id"]],
            include=["shared_devices"],
        )
        if found:
            instance = found[0]
            if not any(peer.id == facts.id for peer in instance.shared_devices.peers):
                await instance.add_relationships("shared_devices", [facts.id])
            return instance.id
        name = instance_name(deployment_name, namespace["name"])
        instance = await self.client.create(
            kind=ManagedVRF,
            data={
                "name": name,
                "deployment": {"id": deployment_id},
                "security_zone": {"id": namespace["id"]},
                "shared_devices": [{"id": facts.id}],
                "status": "active",
            },
        )
        await instance.save(allow_upsert=True, update_group_context=False)
        self.logger.info(f"  {facts.name}: created VRF instance {name}")
        return instance.id

    async def _delete_if_unused(
        self, deployment_id: str, namespace: str, instance_id: str, *, leaving: DeviceVrfFacts | None = None
    ) -> None:
        """Under the instance lock: remove ``leaving``, then delete the instance when it has no member and no SVI."""
        async with self.resource_lock(vrf_instance_lock_key(deployment_id, namespace)):
            found = await self.client.filters(kind=ManagedVRF, ids=[instance_id], include=["shared_devices"])
            if not found:
                return
            instance = found[0]
            members = {peer.id for peer in instance.shared_devices.peers}
            if leaving and leaving.id in members:
                await instance.remove_relationships("shared_devices", [leaving.id])
                members.discard(leaving.id)
                self.logger.info(f"  {leaving.name}: left VRF instance {instance.name.value}")
            references = await self.client.get(kind=ManagedVRF, id=instance_id, include=list(_VRF_REFERENCES))
            if (
                members
                or any(getattr(references, name).peers for name in _VRF_REFERENCES)
                or await self._exchanged(instance_id)
            ):
                return
            await self.client.delete(kind=ManagedVRF, id=instance_id)
            self.logger.info(f"  removed VRF instance {instance.name.value} (no member left)")

    async def _exchanged(self, instance_id: str) -> bool:
        """Whether a routed exchange joins the instance (``vrf_a`` / ``vrf_z``)."""
        for end in ("vrf_a__ids", "vrf_z__ids"):
            if await self.client.filters(kind=TopologyExchangeGateway, **{end: [instance_id]}):
                return True
        return False

    async def local_vrf_ids(self, device_ids: Iterable[str], names: Iterable[str]) -> dict[tuple[str, str], str]:
        """Read scoped instances already provisioned under ``vrfs_held``; never assign a remote instance."""
        devices = sorted(set(device_ids))
        zones = set(names)
        found = await self.client.filters(
            kind=ManagedVRF, shared_devices__ids=devices, include=["shared_devices", "security_zone"]
        )
        result: dict[tuple[str, str], str] = {}
        for instance in found:
            name = instance.security_zone.peer.name.value
            if name not in zones:
                continue
            for peer in instance.shared_devices.peers:
                key = (peer.id, name)
                if key in result and result[key] != instance.id:
                    self.fail(f"{peer.id}: ambiguous VRF instance for {name}")
                result[key] = instance.id
        return result
