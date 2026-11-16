"""The VRF capability (schemas/extensions/capabilities/vrf.yml): the instances (``ManagedVRF``), their members and the
interfaces in them. The ONLY writer of those, of the SVIs (``generators/dcim/svi.py``) and of ``ManagedPBR``
(``generators/capabilities/pbr.py``). Members are written edge by edge. Untracked.

The builders are pure: ``vrf_instance_spec`` (an instance created with its first member) and ``vrf_membership_plan``
(an interface that moves to an instance). The convergence is a few locked entry points taking the generator (``gen``: ``client``, ``logger``, ``fail``,
``resource_lock``, ``apply_plan``):
``reconcile_vrfs`` adds memberships, upserts SVIs and PBR, then prunes; ``vrfs_held`` keeps the device locks while its
body runs and never deletes; ``prune_empty_vrfs`` deletes the instances nothing references. Lock order: segment lock,
device locks (sorted), instance locks (sorted); the locks are not re-entrant.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Iterable, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

from generators.capabilities.pbr import pbr_name, plan_pbr
from generators.dcim.svi import device_hfid, plan_l3vni_svis, plan_svis
from generators.plan import EdgesSpec, Hfid, NodeRef, NodeSpec, PruneEdgesSpec, TopologyPlan
from generators.protocols import DcimInterface, DcimVirtualInterface, ManagedVRF, TopologyExchangeGateway
from utils.data_cleaning import clean_data
from utils.limits import DEFAULT_NAMESPACE
from utils.vrf_demand import (
    DeviceVrfFacts,
    device_vrf_facts,
    instance_name,
    namespaces_by_name,
    vrf_demand,
)

_VRF_REFERENCES = ("capabilities", "session_circuits")
"""The edges of an instance that keep it alive besides its members: its interfaces and its session circuits."""

_VRF_INSTANCES_QUERY_PATH = Path(__file__).resolve().parents[2] / "queries/validation/vrf_instances.gql"

_RELATION = "capabilities"


def vrf_device_lock_key(device_id: str) -> str:
    """The resource_lock key serializing the memberships and SVIs of one device."""
    return f"vrf-device-{device_id}"


def vrf_instance_lock_key(deployment_id: str, namespace: str) -> str:
    """The resource_lock key serializing create, delete and membership of one instance."""
    return f"vrf-{deployment_id}-{namespace}"


# builders


def vrf_ids(peers: Iterable[Any]) -> list[str]:
    """The instances among the peers of an interface's loaded ``capabilities``."""
    return [peer.id for peer in peers if peer.typename == ManagedVRF.__name__ and peer.id]


def vrf_membership_plan(interface_id: str, vrf_id: str, peers: Iterable[Any]) -> TopologyPlan:
    """The plan that puts the interface in the instance ``vrf_id`` and takes it out of every other instance among its
    loaded ``capabilities`` ``peers`` (its other capabilities stay). Empty when it is in exactly that one. No root:
    the interface belongs to the run's target. The add is retried on NODE_NOT_FOUND (the instance may be new).
    """
    peers = list(peers)
    plan = TopologyPlan(root_id=None)
    if vrf_id not in {peer.id for peer in peers}:
        plan.add(EdgesSpec(interface_id, _RELATION, (vrf_id,), retry_not_found=True))
    if stale := frozenset(old for old in vrf_ids(peers) if old != vrf_id):
        plan.add(
            PruneEdgesSpec(
                DcimInterface, interface_id, _RELATION, keep_peer_ids=frozenset({vrf_id}), known_peer_ids=stale
            )
        )
    return plan


def plan_interface_vrf(
    plan: TopologyPlan, interface: str | NodeRef, vrf_id: str, *, stale_vrf_ids: Iterable[str] = ()
) -> None:
    """Put a virtual interface the plan writes (a ``NodeRef``) or found (its id) in the instance ``vrf_id``, and take it
    out of ``stale_vrf_ids``, the other instances the caller read it in. The add reads the interface and sends only what it
    lacks; it is retried on NODE_NOT_FOUND (the instance may be new). The interface's other capabilities stay.
    """
    plan.add(EdgesSpec(interface, _RELATION, (vrf_id,), retry_not_found=True, kind=DcimVirtualInterface))
    if stale := frozenset(stale_vrf_ids) - {vrf_id}:
        plan.add(
            PruneEdgesSpec(DcimInterface, interface, _RELATION, keep_peer_ids=frozenset({vrf_id}), known_peer_ids=stale)
        )


def vrf_instance_spec(
    facts: DeviceVrfFacts, deployment_id: str, deployment_name: str, namespace: Mapping[str, Any]
) -> NodeSpec:
    """The topology's instance of ``namespace`` created WITH its first member (the device): a RelationshipAdd on a
    just-created node can hit NODE_NOT_FOUND. Shared (untracked).
    """
    return NodeSpec(
        "instance",
        ManagedVRF,
        {
            "name": instance_name(deployment_name, namespace["name"]),
            "deployment": {"id": deployment_id},
            "security_zone": {"id": namespace["id"]},
            "shared_devices": [device_hfid(facts)],
            "status": "active",
        },
        hfid=Hfid("ManagedVRF", (deployment_name, namespace["name"])),
        ownership="shared",
    )


# convergence


@asynccontextmanager
async def vrfs_held(gen: Any, needs: Mapping[str, set[str]]) -> AsyncGenerator[None]:
    """Ensure each device ({device id: namespace names}) is a member of its instances and keep its lock while the body
    runs. The body writes what needs the instance, so a concurrent ``reconcile_vrfs`` cannot prune it first. Never
    deletes an instance; ``default`` names and VRF-less devices are ignored. The locks are not re-entrant.
    """
    async with _locked(gen, needs.keys()) as ids:
        await _converge_locked(gen, ids, extra=needs, prune=False)
        yield


async def reconcile_vrfs(gen: Any, device_ids: Iterable[str]) -> None:
    """Converge memberships and SVIs of the devices to their demand, drop unneeded memberships and delete empty
    instances.
    """
    async with _locked(gen, device_ids) as ids:
        await _converge_locked(gen, ids, extra={}, prune=True)


async def prune_empty_vrfs(gen: Any, deployment_ids: Iterable[str]) -> None:
    """Delete the instances of the deployments that have no member and no reference (an interface, a session circuit,
    an exchange); each is checked under its instance lock.
    """
    ids = sorted(set(deployment_ids))
    if not ids:
        return
    nodes = await gen.client.filters(kind=ManagedVRF, deployment__ids=ids, include=["shared_devices", "security_zone"])
    for node in nodes:
        if node.shared_devices.peers:
            continue
        await _delete_if_unused(gen, node.deployment.id, node.security_zone.peer.name.value, node.id)


async def local_vrf_ids(gen: Any, device_ids: Iterable[str], names: Iterable[str]) -> dict[tuple[str, str], str]:
    """Read scoped instances already provisioned under ``vrfs_held``; never assign a remote instance."""
    devices = sorted(set(device_ids))
    zones = set(names)
    found = await gen.client.filters(
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
                gen.fail(f"{peer.id}: ambiguous VRF instance for {name}")
            result[key] = instance.id
    return result


@asynccontextmanager
async def _locked(gen: Any, device_ids: Iterable[str]) -> AsyncGenerator[list[str]]:
    """The sorted, deduplicated ids, with each device's lock held in that order."""
    ids = sorted(set(device_ids))
    async with AsyncExitStack() as stack:
        for device_id in ids:
            await stack.enter_async_context(gen.resource_lock(vrf_device_lock_key(device_id)))
        yield ids


async def _converge_locked(gen: Any, ids: list[str], *, extra: Mapping[str, set[str]], prune: bool) -> None:
    """One pass over the devices; the caller holds their locks."""
    if not ids:
        return
    result = await gen.client.execute_graphql(
        query=_VRF_INSTANCES_QUERY_PATH.read_text(), variables={"device_ids": ids, "with_instances": False}
    )
    cleaned = clean_data(result)
    namespaces = namespaces_by_name(cleaned)
    touched: set[str] = set()
    for node in cleaned.get("DcimDevice") or []:
        facts = device_vrf_facts(node)
        if hosting_id := await _converge_device(gen, facts, namespaces, set(extra.get(facts.id) or ()), prune=prune):
            touched.add(hosting_id)
    if prune and touched:
        await prune_empty_vrfs(gen, touched)


async def _converge_device(
    gen: Any,
    facts: DeviceVrfFacts,
    namespaces: Mapping[str, dict[str, Any]],
    extra: set[str],
    *,
    prune: bool,
) -> str | None:
    """Steps 1-3 of the pass for one device (lock held); returns its hosting deployment id, or None."""
    wanted = {name for name in extra if name != DEFAULT_NAMESPACE} if facts.carries_vrfs else set()
    # Demand once the stale SVIs are gone
    svi_namespaces = frozenset(row.namespace for row in facts.desired_svis().values())
    demand = vrf_demand(replace(facts.inputs, svi_namespaces=svi_namespaces)) | wanted
    if demand and not facts.hosting_id:
        gen.logger.warning(
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
        gen.fail(f"{facts.name}: namespace {missing[0]} not found - cannot create its VRF instance")
    if add:
        async with AsyncExitStack() as stack:
            for name in add:
                await stack.enter_async_context(gen.resource_lock(vrf_instance_lock_key(hosting[0], name)))
            for name in add:
                instance_ids[name] = await _join_instance(gen, facts, hosting[0], hosting[1], namespaces[name])

    await _converge_svis(gen, facts, instance_ids)
    await _converge_l3vni_svis(gen, facts, namespaces, instance_ids)
    # The device has a ManagedPBR exactly when one of its SVIs is steered
    await _converge_pbr(gen, facts)

    if prune:
        for key in drop:
            await _delete_if_unused(gen, key[0], key[1], have[key].id, leaving=facts)
    return facts.hosting_id


async def _converge_svis(gen: Any, facts: DeviceVrfFacts, instance_ids: Mapping[str, str]) -> None:
    """Create, move and delete the device's SVIs (nothing when they already match); the caller holds the device's VRF
    lock and has created the instances.
    """
    planned = plan_svis(facts, instance_ids)
    for name, role in planned.taken.items():
        # Warn, don't fail: the logger's error() raises and the other SVIs must converge; the check reports it
        gen.logger.warning(
            f"  {facts.name}: cannot create SVI {name}: an interface of that name (role {role}) exists - skipped"
        )
    changes = planned.changes
    # A name a written SVI needs is freed first: the delete precedes the recreate (a direct delete: the SVI is
    # untracked, which ``NodeSpec.replaces`` does not delete, and it is the same HFID the plan writes again)
    for svi_id in changes.recreate_ids:
        await gen.client.delete(kind=DcimVirtualInterface, id=svi_id)
    await gen.apply_plan(planned.plan)
    if changes:
        gen.logger.info(
            f"  {facts.name}: SVIs written {len(changes.writes)}, deleted {len(changes.recreate_ids) + len(changes.stale_ids)}"
        )


async def _converge_l3vni_svis(
    gen: Any,
    facts: DeviceVrfFacts,
    namespaces: Mapping[str, Mapping[str, Any]],
    instance_ids: Mapping[str, str],
) -> None:
    """Create, move and delete the device's L3 VNI SVIs; the caller holds the device's VRF lock and has created the
    instances.
    """
    planned = plan_l3vni_svis(facts, namespaces, instance_ids)
    for name in sorted(planned.taken):
        gen.logger.warning(
            f"  {facts.name}: cannot create L3 VNI SVI {name}: an interface of that name exists - skipped"
        )
    await gen.apply_plan(planned.plan)
    changes = planned.changes
    if changes:
        gen.logger.info(f"  {facts.name}: L3 VNI SVIs written {len(changes.writes)}, deleted {len(changes.stale_ids)}")


async def _converge_pbr(gen: Any, facts: DeviceVrfFacts) -> None:
    """Create the device's ``ManagedPBR`` when it steers a tenant SVI and has none, delete it when it steers none."""
    plan = plan_pbr(facts)
    if not plan.specs:
        return
    await gen.apply_plan(plan)
    if facts.steers():
        gen.logger.info(f"  {facts.name}: steers its tenant SVIs - created {pbr_name(facts.name)}")
    else:
        gen.logger.info(f"  {facts.name}: steers no SVI any more - removed its PBR")


async def _join_instance(
    gen: Any, facts: DeviceVrfFacts, deployment_id: str, deployment_name: str, namespace: Mapping[str, Any]
) -> str:
    """Find or create the topology's instance of the namespace and add the device to it (instance lock held); returns
    its id.
    """
    found = await gen.client.filters(
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
    spec = vrf_instance_spec(facts, deployment_id, deployment_name, namespace)
    created = await gen.apply_plan(TopologyPlan(root_id=None, specs=[spec]))
    gen.logger.info(f"  {facts.name}: created VRF instance {spec.data['name']}")
    return created.nodes["instance"].id


async def _delete_if_unused(
    gen: Any,
    deployment_id: str,
    namespace: str,
    instance_id: str,
    *,
    leaving: DeviceVrfFacts | None = None,
) -> None:
    """Under the instance lock: remove ``leaving``, then delete the instance when it has no member and nothing
    references it (an interface, a session circuit, an exchange).
    """
    async with gen.resource_lock(vrf_instance_lock_key(deployment_id, namespace)):
        found = await gen.client.filters(kind=ManagedVRF, ids=[instance_id], include=["shared_devices"])
        if not found:
            return
        instance = found[0]
        members = {peer.id for peer in instance.shared_devices.peers}
        if leaving and leaving.id in members:
            await instance.remove_relationships("shared_devices", [leaving.id])
            members.discard(leaving.id)
            gen.logger.info(f"  {leaving.name}: left VRF instance {instance.name.value}")
        references = await gen.client.get(kind=ManagedVRF, id=instance_id, include=list(_VRF_REFERENCES))
        if (
            members
            or any(getattr(references, name).peers for name in _VRF_REFERENCES)
            or await _exchanged(gen, instance_id)
        ):
            return
        await gen.client.delete(kind=ManagedVRF, id=instance_id)
        gen.logger.info(f"  removed VRF instance {instance.name.value} (no member left)")


async def _exchanged(gen: Any, instance_id: str) -> bool:
    """Whether a routed exchange joins the instance (``vrf_a`` / ``vrf_z``)."""
    for end in ("vrf_a__ids", "vrf_z__ids"):
        if await gen.client.filters(kind=TopologyExchangeGateway, **{end: [instance_id]}):
            return True
    return False
