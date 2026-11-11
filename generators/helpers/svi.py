"""The segment SVIs of a device: one ``DcimVirtualInterface`` (role ``svi``) per routed row of the device.

The SVI carries the segment's anycast gateway as its ``ip_address`` (THE ``segment.gateway`` object, the same address on
the SVI of every switch with a routed row of the segment: ``ip_address`` is a plain edge with no cascade) and is an
interface of the VRF the switch routes it in (the ``ManagedVRF`` among its ``capabilities``). It is a MEMBER of the
VLAN row it realizes, through the row's interface edge (``ManagedSegmentVlan.capabilities``, the edge a switch port is a
member of its row through): the row is read from the membership, never from the name ``Vlan<vlan id>``. It is a plain
generic edge, not a Component of the row (a Component under a row whose ``device`` is its Parent broke the server's
object-template walk, measured on 1.11.9). An SVI's only capabilities are its VRF and its row (checks/vrf_instances.py
S4). The save of the SVI writes its ``capabilities`` as the one-instance VRF list (generators/helpers/interface_vrf.py
``vrf_capability``): one write, which is safe even right after the instance was created in the same pass, where a
RelationshipAdd on the new node can reach the other replica first. A list save replaces the edge set, so the row is
added edge by edge AFTER every save (``join_capabilities``, one RelationshipAdd, a no-op when the edge exists), and an
extra row is removed edge by edge. What a device needs is derived by ``utils.vrf_demand.device_vrf_facts`` (a border leaf has none
for a stretched segment: it carries it L2 only, utils/segment_addressing.py ``l2_only_on``); this module only turns
the difference with what exists into writes.

Identity: an existing SVI is the member of a routed row of its device (the row it realizes); a new one is created as
``Vlan<vlan id>``, the interface's own name (unique on its device). Nothing deletes an SVI with its row: this pass
deletes every role-svi interface of the device that is a member of no routed row of it (the row is gone, it became L2
only, its segment lost its gateway, or the device carries no VRF any more), and one that is a member of no row at
all and bears the name a new SVI needs is deleted first; a deleted device takes its interfaces, a deleted row
its edges.

Written untracked (``update_group_context=False``), like the rows: an SVI belongs to no single run.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from utils.limits import DEFAULT_NAMESPACE
from utils.vrf_demand import L3VNI_ROLE, SVI_ROLE, DeviceVrfFacts, RoutedRow, SviFacts

from ..protocols import DcimVirtualInterface
from .edges import remove_edge
from .interface_vrf import join_capabilities, vrf_capability

_ROW_RELATION = "capabilities"
"""The interface edge an SVI is a member of its VLAN row through."""


@dataclass(frozen=True)
class SviWrite:
    """One SVI to write: ``existing_id`` is set for an in-place update, None for a create."""

    name: str
    row_id: str
    address_id: str | None
    vrf_id: str | None
    existing_id: str | None = None


@dataclass(frozen=True)
class SviPlan:
    """What to do to a device's SVIs; ``recreate_ids`` are deleted before the writes, ``stale_ids`` after them.

    ``leave`` are the (SVI id, row id) memberships removed edge by edge: an SVI that is a member of a row besides the
    one it realizes."""

    writes: tuple[SviWrite, ...] = ()
    recreate_ids: tuple[str, ...] = ()
    stale_ids: tuple[str, ...] = ()
    leave: tuple[tuple[str, str], ...] = ()

    def __bool__(self) -> bool:
        return bool(self.writes or self.recreate_ids or self.stale_ids or self.leave)


def desired_vrf_id(row: RoutedRow, instance_ids: Mapping[str, str]) -> str | None:
    """The instance an SVI of the row routes in: None for a ``default`` gateway (the global table)."""
    return None if row.namespace == DEFAULT_NAMESPACE else instance_ids[row.namespace]


def plan_svis(facts: DeviceVrfFacts, instance_ids: Mapping[str, str]) -> SviPlan:
    """The writes that make the device's role-svi interfaces exactly its desired ones.

    ``instance_ids`` maps a namespace name to the id of the instance of it in the device's hosting deployment (every
    non-default namespace of a desired SVI must be in it). An SVI whose address (the segment's gateway moved) or VRF
    changes to another one is updated in place. One that would have to lose its VRF or its address is deleted and
    created again, because a relationship cannot be cleared through a save of a node read without it.
    """
    desired = facts.desired_svis()
    # Each SVI realizes the first desired row it is a member of; a row has one SVI (a second is stale).
    matched: dict[str, SviFacts] = {}
    leave: list[tuple[str, str]] = []
    unmatched: list[SviFacts] = []
    for svi in sorted(facts.svis, key=lambda item: item.id):
        row = next((row for row in desired.values() if row.id in svi.row_ids and row.id not in matched), None)
        if row is None:
            unmatched.append(svi)
            continue
        matched[row.id] = svi
        leave.extend((svi.id, row_id) for row_id in svi.row_ids if row_id != row.id)
    writes: list[SviWrite] = []
    recreate: list[str] = []
    for name, row in desired.items():
        vrf_id = desired_vrf_id(row, instance_ids)
        current = matched.get(row.id)
        if current is None:
            writes.append(SviWrite(name, row.id, row.gateway_id, vrf_id))
        elif current.name == name and current.address_id == row.gateway_id and current.vrf_id == vrf_id:
            continue
        elif (vrf_id is None and current.vrf_id is not None) or (row.gateway_id is None and current.address_id):
            recreate.append(current.id)
            writes.append(SviWrite(name, row.id, row.gateway_id, vrf_id))
        else:
            writes.append(SviWrite(name, row.id, row.gateway_id, vrf_id, existing_id=current.id))
    # An SVI of no wanted row goes after the writes, unless it bears a name an SVI is written with: the name is
    # unique on the device, so it goes first.
    created = {write.name for write in writes}
    recreate.extend(svi.id for svi in unmatched if svi.name in created)
    stale = sorted(svi.id for svi in unmatched if svi.name not in created)
    return SviPlan(tuple(writes), tuple(sorted(recreate)), tuple(stale), tuple(sorted(leave)))


@dataclass(frozen=True)
class L3vniWrite:
    """One L3 VNI SVI to write: ``existing_id`` is set for an in-place update (a VRF instance that moved)."""

    name: str
    vrf_id: str
    existing_id: str | None = None


@dataclass(frozen=True)
class L3vniPlan:
    """What to do to a device's L3 VNI SVIs: the writes, and the ids deleted after them."""

    writes: tuple[L3vniWrite, ...] = ()
    stale_ids: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.writes or self.stale_ids)


def plan_l3vni_svis(
    facts: DeviceVrfFacts, namespaces: Mapping[str, Mapping[str, Any]], instance_ids: Mapping[str, str]
) -> L3vniPlan:
    """The writes that make the device's role-l3vni interfaces exactly the VRFs it terminates.

    ``instance_ids`` maps a namespace name to the id of its instance in the device's hosting deployment. An SVI
    that is on the right instance is left alone, one on another instance is moved, one for a VRF the device no longer
    terminates is deleted."""
    desired = facts.desired_l3vni_svis(namespaces)
    existing = {svi.name: svi for svi in facts.l3vni_svis}
    writes: list[L3vniWrite] = []
    for name, namespace in desired.items():
        vrf_id = instance_ids.get(namespace)
        if vrf_id is None:
            continue
        current = existing.get(name)
        if current is None:
            writes.append(L3vniWrite(name, vrf_id))
        elif current.vrf_id != vrf_id:
            writes.append(L3vniWrite(name, vrf_id, existing_id=current.id))
    stale = sorted(svi.id for name, svi in existing.items() if name not in desired)
    return L3vniPlan(tuple(writes), tuple(stale))


def taken_svi_names(facts: DeviceVrfFacts) -> dict[str, str | None]:
    """The desired SVI names an interface of another role already has (the MLAG control ``Vlan4094``, a hand-made
    ``VlanN``) -> that role. Creating the SVI by name would overwrite that interface."""
    return {name: facts.other_interfaces[name] for name in facts.desired_svis() if name in facts.other_interfaces}


class SviMixin:
    """Writes the SVIs of one device. Expects the host to provide ``client`` and ``logger``."""

    client: Any
    logger: logging.Logger

    async def sync_l3vni_svis(
        self, facts: DeviceVrfFacts, namespaces: Mapping[str, Mapping[str, Any]], instance_ids: Mapping[str, str]
    ) -> None:
        """Create, move and delete the device's L3 VNI SVIs (nothing when they already match).

        The caller holds the device's VRF lock and has created the instances. The SVI has no address and no row: it is
        the realization of the VRF on the device (symmetric IRB), not of a segment. Written untracked, like the SVIs."""
        plan = plan_l3vni_svis(facts, namespaces, instance_ids)
        taken = {w.name for w in plan.writes if w.name in facts.other_interfaces}
        for name in sorted(taken):
            self.logger.warning(
                f"  {facts.name}: cannot create L3 VNI SVI {name}: an interface of that name exists - skipped"
            )
        for write in plan.writes:
            if write.name in taken:
                continue
            data: dict[str, Any] = {
                "name": write.name,
                "device": {"id": facts.id},
                "role": L3VNI_ROLE,
                "status": "active",
                **vrf_capability(write.vrf_id),
            }
            if write.existing_id:
                data["id"] = write.existing_id
            svi = await self.client.create(kind=DcimVirtualInterface, data=data)
            await svi.save(allow_upsert=True, update_group_context=False)
        for svi_id in plan.stale_ids:
            await self.client.delete(kind=DcimVirtualInterface, id=svi_id)
        if plan:
            self.logger.info(f"  {facts.name}: L3 VNI SVIs written {len(plan.writes)}, deleted {len(plan.stale_ids)}")

    async def sync_svis(self, facts: DeviceVrfFacts, instance_ids: Mapping[str, str]) -> None:
        """Create, move and delete the device's SVIs (nothing when they already match).

        The caller holds the device's VRF lock and has created the instances ``instance_ids`` names.
        """
        plan = plan_svis(facts, instance_ids)
        taken = taken_svi_names(facts)
        for name, role in taken.items():
            # Skipped with a warning, not failed: the generator logger's error() raises, and the device's other SVIs
            # must still converge. The check reports the missing SVI.
            self.logger.warning(
                f"  {facts.name}: cannot create SVI {name}: an interface of that name (role {role}) exists - skipped"
            )
        plan = replace(plan, writes=tuple(write for write in plan.writes if write.name not in taken))
        for svi_id in plan.recreate_ids:
            await self.client.delete(kind=DcimVirtualInterface, id=svi_id)
        for write in plan.writes:
            data: dict[str, Any] = {
                "name": write.name,
                "device": {"id": facts.id},
                "role": SVI_ROLE,
                "status": "active",
                **vrf_capability(write.vrf_id),
            }
            if write.address_id:
                data["ip_address"] = {"id": write.address_id}
            if write.existing_id:
                data["id"] = write.existing_id
            svi = await self.client.create(kind=DcimVirtualInterface, data=data)
            await svi.save(allow_upsert=True, update_group_context=False)
            # After the save, which replaces the edge set: the row (created by the segment reconcile before this pass).
            await join_capabilities(self.client, self.logger, svi.id, [write.row_id])
        for svi_id, row_id in plan.leave:
            await remove_edge(self.client, node_id=svi_id, relation=_ROW_RELATION, peer_id=row_id)
        for svi_id in plan.stale_ids:
            await self.client.delete(kind=DcimVirtualInterface, id=svi_id)
        if plan:
            self.logger.info(
                f"  {facts.name}: SVIs written {len(plan.writes)}, deleted {len(plan.recreate_ids) + len(plan.stale_ids)}"
            )
