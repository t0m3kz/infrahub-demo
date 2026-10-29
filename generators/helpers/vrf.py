"""Converges the VRF instances (``ManagedVRF``) and segment SVIs of the devices a run touched.

What a device needs is derived once, by ``utils.vrf_demand`` (the same code the check and the transforms read); this
mixin runs the query ``queries/validation/vrf_instances.gql`` for the devices it was given and writes the difference.
It is the ONLY writer of an instance or an SVI.

One pass, ``reconcile_vrfs``, under a lock per device (``vrf-device-<id>``, taken in sorted order, once; after a
segment lock when the caller holds one, never while holding a firewall or load-balancer context lock). Per device:

1. create the instances the device needs and lacks, written from the VRF side (``capabilities`` names the device);
2. upsert the SVIs of its routed rows, then delete the ones it no longer needs (generators/helpers/svi.py);
3. only when ``prune``: delete the instances no demand remains for. The demand of this step ignores the SVIs step 2
   deleted, so an instance goes in the same pass as the last SVI that used it, after it.

``vrfs_held`` is the same pass without step 3 that KEEPS the device locks while its body saves the address, leg or
session that needs the instance (the transforms refuse a session in a VRF the device lacks): a concurrent pruning
pass waits for the lock, so it cannot delete the fresh instance before the write shows its demand. It never deletes
an instance. The locks are not re-entrant: nothing inside the body may reconcile a device it holds.

Instances and SVIs are untracked (``update_group_context=False``): they belong to no single run. An instance is found
by (device, namespace) from the query and never re-created; it is never referenced by HFID in the run that created
it. A re-run with nothing changed writes nothing. A device that cannot carry instances (a spine, a firewall, a party's
router) gets none, whatever a caller asks for.

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

from ..protocols import ManagedVRF
from .svi import SviMixin

if TYPE_CHECKING:
    from ..common import CommonGenerator
    from ..pools import PoolMixin

_VRF_INSTANCES_QUERY_PATH = Path(__file__).resolve().parents[2] / "queries/validation/vrf_instances.gql"


def vrf_device_lock_key(device_id: str) -> str:
    """The resource_lock key serializing the instances and SVIs of one device."""
    return f"vrf-device-{device_id}"


class VrfMixin(SviMixin):
    """Mixin converging the VRF instances and SVIs of devices.

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
        """Ensure each device ({device id: namespace names}) has its instances and keep its lock while the body runs.

        The body writes the address, leg or session that needs the instance: a concurrent ``reconcile_vrfs`` (which
        prunes) waits for the device lock, so it cannot delete the instance before that write is in the graph. The
        pass never deletes an instance. A name of the ``default`` namespace, or a device that carries no VRF, is
        ignored; empty ``needs`` locks nothing. The device locks are not re-entrant: the body must not reconcile a
        device it holds.
        """
        async with self._locked(needs.keys()) as ids:
            await self._converge_locked(ids, extra=needs, prune=False)
            yield

    async def reconcile_vrfs(self, device_ids: Iterable[str], *, prune: bool = True) -> None:
        """Converge the instances and SVIs of the devices to their demand; ``prune`` deletes the instances nobody
        needs any more."""
        async with self._locked(device_ids) as ids:
            await self._converge_locked(ids, extra={}, prune=prune)

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
            query=_VRF_INSTANCES_QUERY_PATH.read_text(), variables={"device_ids": ids}
        )
        cleaned = clean_data(result)
        namespaces = namespaces_by_name(cleaned)
        for node in cleaned.get("DcimDevice") or []:
            facts = device_vrf_facts(node)
            await self._converge_device(facts, namespaces, set(extra.get(facts.id) or ()), prune=prune)

    async def _converge_device(
        self, facts: DeviceVrfFacts, namespaces: Mapping[str, dict[str, Any]], extra: set[str], *, prune: bool
    ) -> None:
        """Steps 1-3 of the pass for one device; the caller holds its lock."""
        wanted = {name for name in extra if name != DEFAULT_NAMESPACE} if facts.carries_vrfs else set()
        # The demand once the stale SVIs are gone: only the desired SVIs' namespaces count.
        svi_namespaces = frozenset(row.namespace for row in facts.desired_svis().values())
        demand = vrf_demand(replace(facts.inputs, svi_namespaces=svi_namespaces)) | wanted

        instance_ids: dict[str, str] = {}
        for instance in facts.instances:
            if instance.namespace:
                instance_ids.setdefault(instance.namespace, instance.id)
        for namespace in sorted(demand - instance_ids.keys()):
            instance_ids[namespace] = await self._create_instance(facts, namespace, namespaces)

        await self.sync_svis(facts, instance_ids)

        if prune:
            for instance in facts.instances:
                if instance.namespace not in demand:
                    await self.client.delete(kind=ManagedVRF, id=instance.id)
                    self.logger.info(f"  {facts.name}: removed VRF instance {instance.name} (no demand left)")

    async def _create_instance(
        self, facts: DeviceVrfFacts, namespace: str, namespaces: Mapping[str, dict[str, Any]]
    ) -> str:
        """Create the device's instance of ``namespace`` (untracked); returns its id."""
        namespace_id = (namespaces.get(namespace) or {}).get("id")
        if not namespace_id:
            self.fail(f"{facts.name}: namespace {namespace} not found - cannot create its VRF instance")
        instance = await self.client.create(
            kind=ManagedVRF,
            data={
                "name": instance_name(facts.name, namespace),
                "namespace": {"id": namespace_id},
                "capabilities": [{"id": facts.id}],
                "status": "active",
            },
        )
        await instance.save(allow_upsert=True, update_group_context=False)
        self.logger.info(f"  {facts.name}: created VRF instance {instance_name(facts.name, namespace)}")
        return instance.id
