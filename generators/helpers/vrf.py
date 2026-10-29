"""Converges the VRF instances (``ManagedVRF``, one per hosting deployment and namespace) and segment SVIs.

What a device needs is derived once, by ``utils.vrf_demand`` (the same code the check and the transforms read); this
mixin runs the query ``queries/validation/vrf_instances.gql`` for the devices it was given and writes the difference.
It is the ONLY writer of an instance, of its membership and of an SVI.

An instance is a namespace realized in one topology: the device's hosting deployment (a DC, a colocation metro or an
office). A device is a MEMBER of it (``devices``), written edge by edge (``add_relationships`` /
``remove_relationships``, never a save of the list: two writers would lose each other's members).

Locks, in this order, never the reverse (they are not re-entrant): the caller's segment lock, then the device locks
(``vrf-device-<id>``, sorted, once; never under a firewall or load-balancer context lock), then the instance locks
(``vrf-<deployment id>-<namespace>``, sorted; only the keys a pass changes, so a steady-state re-run takes none). A
device lock is never taken while an instance lock is held.

One pass, ``reconcile_vrfs``, per device (its lock held): ``demand`` is {namespace} of what it needs, in its hosting deployment,
``have`` the instances it is a member of.

1. ``add = want - have``: under the instance locks, find or create the instance (the [deployment, namespace]
   uniqueness turns a lost create race into an upsert) and add the device to it;
2. upsert the SVIs of its routed rows, then delete the ones it no longer needs (generators/helpers/svi.py). The
   membership of step 1 always precedes an SVI that points at the instance;
3. unless held: ``drop = have - want`` (also a membership in another topology after a move): under the
   instance lock remove the device and delete the instance when it has no member and no SVI left. The demand of this
   step ignores the SVIs step 2 deleted, so an instance goes in the same pass as the last SVI that used it, after it;
4. unless held: sweep the instances with no member and no SVI of the hosting deployments the pass touched
   (``prune_empty_vrfs``, which a pod decommission calls for its DC).

A device with demand but no hosting deployment gets a WARNING and no write (checks/vrf_instances.py reports it).

``vrfs_held`` is the same pass without 3 and 4 that KEEPS the device locks while its body saves the address, leg or
session that needs the instance (the transforms refuse a session in a VRF the device lacks): a concurrent pruning pass
waits for the lock, and a prune of ANOTHER member cannot delete the instance because this device is already a member.
It never deletes an instance. Nothing inside the body may reconcile a device it holds.

Instances and SVIs are untracked (``update_group_context=False``): they belong to no single run. A re-run with nothing
changed writes nothing. A device that cannot carry instances (a spine, a firewall, a party's router) gets none,
whatever a caller asks for.

Deviation from the SVI design: the SVI upsert key is (device, ``Vlan<id>``), not its row link, so a row moving
between domains needs no delete-before-create ordering (generators/helpers/svi.py).
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
from utils.vrf_demand import (
    DeviceVrfFacts,
    device_vrf_facts,
    instance_name,
    namespaces_by_name,
    vrf_demand,
)

from ..protocols import DcimVirtualInterface, ManagedVRF
from .svi import SviMixin

if TYPE_CHECKING:
    from ..common import CommonGenerator
    from ..pools import PoolMixin

_VRF_INSTANCES_QUERY_PATH = Path(__file__).resolve().parents[2] / "queries/validation/vrf_instances.gql"


def vrf_device_lock_key(device_id: str) -> str:
    """The resource_lock key serializing the memberships and SVIs of one device."""
    return f"vrf-device-{device_id}"


def vrf_instance_lock_key(deployment_id: str, namespace: str) -> str:
    """The resource_lock key serializing the create, the delete and the membership of one instance."""
    return f"vrf-{deployment_id}-{namespace}"


class VrfMixin(SviMixin):
    """Mixin converging the VRF instances, their members and the SVIs of devices.

    Expects the host class to provide ``client``, ``logger``, ``resource_lock`` (PoolMixin) and ``fail``
    (CommonGenerator).
    """

    client: Any
    logger: logging.Logger
    if TYPE_CHECKING:
        fail = CommonGenerator.fail
        resource_lock = PoolMixin.resource_lock

    @asynccontextmanager
    async def vrfs_held(self, needs: Mapping[str, set[str]]) -> AsyncGenerator[None]:
        """Ensure each device ({device id: namespace names}) is a member of its instances and keep its lock while the
        body runs.

        The body writes the address, leg or session that needs the instance: a concurrent ``reconcile_vrfs`` (which
        prunes) waits for the device lock, so it cannot remove the membership before that write is in the graph. The
        pass never deletes an instance. A name of the ``default`` namespace, or a device that carries no VRF, is
        ignored; empty ``needs`` locks nothing. The device locks are not re-entrant: the body must not reconcile a
        device it holds.
        """
        async with self._locked(needs.keys()) as ids:
            await self._converge_locked(ids, extra=needs, prune=False)
            yield

    async def reconcile_vrfs(self, device_ids: Iterable[str]) -> None:
        """Converge the memberships and SVIs of the devices to their demand, drop the memberships nobody needs any
        more and delete the instances left empty."""
        async with self._locked(device_ids) as ids:
            await self._converge_locked(ids, extra={}, prune=True)

    async def prune_empty_vrfs(self, deployment_ids: Iterable[str]) -> None:
        """Delete the instances of the deployments that have no member and no SVI (one read, each under its lock)."""
        ids = sorted(set(deployment_ids))
        if not ids:
            return
        nodes = await self.client.filters(kind=ManagedVRF, deployment__ids=ids, include=["devices", "namespace"])
        for node in nodes:
            if node.devices.peers:
                continue
            await self._delete_if_unused(node.deployment.id, node.namespace.peer.name.value, node.id)

    @asynccontextmanager
    async def _locked(self, device_ids: Iterable[str]) -> AsyncGenerator[list[str]]:
        """The sorted, deduplicated ids, with the lock of each device held (in that order)."""
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
            query=_VRF_INSTANCES_QUERY_PATH.read_text(), variables={"device_ids": ids, "with_instances": False}
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
        """Steps 1-3 of the pass for one device (the caller holds its lock); returns its hosting deployment id, None
        when it has none."""
        wanted = {name for name in extra if name != DEFAULT_NAMESPACE} if facts.carries_vrfs else set()
        # The demand once the stale SVIs are gone: only the desired SVIs' namespaces count.
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

        if prune:
            for key in drop:
                await self._delete_if_unused(key[0], key[1], have[key].id, leaving=facts)
        return facts.hosting_id

    async def _join_instance(
        self, facts: DeviceVrfFacts, deployment_id: str, deployment_name: str, namespace: Mapping[str, Any]
    ) -> str:
        """Find or create the topology's instance of the namespace and add the device to it; the caller holds the
        instance lock. Returns the instance id.

        A new instance is created WITH its first member in one write: a RelationshipAdd on a node created a moment
        before, in another request, can reach the other replica first and fail with an unretried NODE_NOT_FOUND."""
        found = await self.client.filters(
            kind=ManagedVRF, deployment__ids=[deployment_id], namespace__ids=[namespace["id"]], include=["devices"]
        )
        if found:
            instance = found[0]
            if not any(peer.id == facts.id for peer in instance.devices.peers):
                await instance.add_relationships("devices", [facts.id])
            return instance.id
        name = instance_name(deployment_name, namespace["name"])
        instance = await self.client.create(
            kind=ManagedVRF,
            data={
                "name": name,
                "deployment": {"id": deployment_id},
                "namespace": {"id": namespace["id"]},
                "devices": [{"id": facts.id}],
                "status": "active",
            },
        )
        await instance.save(allow_upsert=True, update_group_context=False)
        self.logger.info(f"  {facts.name}: created VRF instance {name}")
        return instance.id

    async def _delete_if_unused(
        self, deployment_id: str, namespace: str, instance_id: str, *, leaving: DeviceVrfFacts | None = None
    ) -> None:
        """Under the instance lock: remove ``leaving`` from the instance, then delete it when it has no member and no
        SVI. The members are read under the lock (every add holds it), so one read answers both questions."""
        async with self.resource_lock(vrf_instance_lock_key(deployment_id, namespace)):
            found = await self.client.filters(kind=ManagedVRF, ids=[instance_id], include=["devices"])
            if not found:
                return
            instance = found[0]
            members = {peer.id for peer in instance.devices.peers}
            if leaving and leaving.id in members:
                await instance.remove_relationships("devices", [leaving.id])
                members.discard(leaving.id)
                self.logger.info(f"  {leaving.name}: left VRF instance {instance.name.value}")
            if members or await self.client.filters(kind=DcimVirtualInterface, vrf__ids=[instance_id]):
                return
            await self.client.delete(kind=ManagedVRF, id=instance_id)
            self.logger.info(f"  removed VRF instance {instance.name.value} (no member left)")
