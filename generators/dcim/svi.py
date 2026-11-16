"""The SVIs of a device as plans: one ``DcimVirtualInterface`` (role ``svi``) per routed VLAN row, one (role ``l3vni``)
per VRF the device terminates; untracked (shared). The SVI carries the anycast gateway, sits in the switch's VRF and is
added to its VLAN row edge by edge after every save; demand comes from utils.vrf_demand. ``plan_svi_writes`` and
``plan_l3vni_writes`` are pure; the VRF convergence (generators/capabilities/vrf.py) reads the facts and applies them.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from generators.plan import EdgesSpec, Hfid, NodeRef, NodeSpec, PruneEdgesSpec, PruneSpec, TopologyPlan
from generators.protocols import DcimVirtualInterface
from utils.limits import DEFAULT_NAMESPACE
from utils.vrf_demand import L3VNI_ROLE, SVI_ROLE, DeviceVrfFacts, RoutedRow, SviFacts

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
    """What to do to a device's SVIs: ``recreate_ids`` are deleted before the writes, ``stale_ids`` after them,
    ``leave`` are (SVI id, row id) memberships removed edge by edge."""

    writes: tuple[SviWrite, ...] = ()
    recreate_ids: tuple[str, ...] = ()
    stale_ids: tuple[str, ...] = ()
    leave: tuple[tuple[str, str], ...] = ()

    def __bool__(self) -> bool:
        return bool(self.writes or self.recreate_ids or self.stale_ids or self.leave)


def desired_vrf_id(row: RoutedRow, instance_ids: Mapping[str, str]) -> str | None:
    """The instance an SVI of the row routes in: None for a ``default`` gateway."""
    return None if row.namespace == DEFAULT_NAMESPACE else instance_ids[row.namespace]


def svi_changes(facts: DeviceVrfFacts, instance_ids: Mapping[str, str]) -> SviPlan:
    """The writes that make the device's role-svi interfaces exactly its desired ones. ``instance_ids`` maps a namespace
    name to its instance id. A changed address or VRF updates in place; losing a VRF or address means delete and
    recreate.
    """
    desired = facts.desired_svis()
    # Each SVI realizes the first desired row it is a member of; a row has one SVI (a second is stale)
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
    # An SVI of no wanted row goes after the writes, unless its name is needed by a written SVI: then it goes first
    created = {write.name for write in writes}
    recreate.extend(svi.id for svi in unmatched if svi.name in created)
    stale = sorted(svi.id for svi in unmatched if svi.name not in created)
    return SviPlan(tuple(writes), tuple(sorted(recreate)), tuple(stale), tuple(sorted(leave)))


@dataclass(frozen=True)
class L3vniWrite:
    """One L3 VNI SVI to write: ``existing_id`` is set for an in-place update."""

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


def l3vni_changes(
    facts: DeviceVrfFacts, namespaces: Mapping[str, Mapping[str, Any]], instance_ids: Mapping[str, str]
) -> L3vniPlan:
    """The writes that make the device's role-l3vni interfaces exactly the VRFs it terminates (moved, deleted or left
    alone).
    """
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


def _vrf_capability(vrf_id: str | None) -> dict[str, Any]:
    """Create data that puts a NEW interface in the instance ``vrf_id`` as its only capability (nothing for None)."""
    return {"capabilities": [{"id": vrf_id}]} if vrf_id else {}


def device_hfid(facts: DeviceVrfFacts) -> Hfid:
    """The device a VRF member or SVI belongs to: only a switch (a physical device) carries VRFs."""
    return Hfid("DcimPhysicalDevice", (facts.name,))


def svi_hfid(facts: DeviceVrfFacts, name: str) -> Hfid:
    """The SVI ``name`` of the device."""
    return Hfid("DcimVirtualInterface", (facts.name, name))


def taken_svi_names(facts: DeviceVrfFacts) -> dict[str, str | None]:
    """Desired SVI names that an interface of another role already has (e.g. MLAG ``Vlan4094``) -> that role."""
    return {name: facts.other_interfaces[name] for name in facts.desired_svis() if name in facts.other_interfaces}


def stale_svi_prune(kind_ids: tuple[str, ...], stale_ids: tuple[str, ...]) -> PruneSpec | None:
    """The prune of the SVIs the device no longer wants: of its ``kind_ids`` (the ones it has), those not in
    ``stale_ids`` stay. None when nothing is stale.
    """
    if not stale_ids:
        return None
    return PruneSpec(
        kind=DcimVirtualInterface,
        scope={"ids": sorted(kind_ids)},
        keep=frozenset(kind_ids) - frozenset(stale_ids),
        key="id",
    )


@dataclass(frozen=True)
class PlannedSvis:
    """The plan of a device's SVIs, what it changes (``changes``) and the names it skipped because an interface of
    another role holds them (``taken``: name -> that role, to be reported).
    """

    plan: TopologyPlan
    taken: Mapping[str, str | None]
    changes: SviPlan


@dataclass(frozen=True)
class PlannedL3vniSvis:
    """The plan of a device's L3 VNI SVIs, as ``PlannedSvis``."""

    plan: TopologyPlan
    taken: Mapping[str, str | None]
    changes: L3vniPlan


def plan_l3vni_svis(
    facts: DeviceVrfFacts, namespaces: Mapping[str, Mapping[str, Any]], instance_ids: Mapping[str, str]
) -> PlannedL3vniSvis:
    """The plan of the device's L3 VNI SVIs: the writes (a name an interface of another role holds is skipped) and the
    prune of the stale ones. The SVI has no address and no row. Pure.
    """
    svis = l3vni_changes(facts, namespaces, instance_ids)
    taken = {w.name: facts.other_interfaces[w.name] for w in svis.writes if w.name in facts.other_interfaces}
    plan = TopologyPlan(root_id=None)
    for write in svis.writes:
        if write.name in taken:
            continue
        data: dict[str, Any] = {
            "name": write.name,
            "device": device_hfid(facts),
            "role": L3VNI_ROLE,
            "status": "active",
            **_vrf_capability(write.vrf_id),
        }
        if write.existing_id:
            data["id"] = write.existing_id
        plan.add(
            NodeSpec(
                f"l3vni-{write.name}", DcimVirtualInterface, data, hfid=svi_hfid(facts, write.name), ownership="shared"
            )
        )
    if prune := stale_svi_prune(tuple(svi.id for svi in facts.l3vni_svis), svis.stale_ids):
        plan.add(prune)
    return PlannedL3vniSvis(plan, taken, svis)


def plan_svis(facts: DeviceVrfFacts, instance_ids: Mapping[str, str]) -> PlannedSvis:
    """The plan of the device's segment SVIs: the writes (a name an interface of another role holds is skipped; those
    names and roles are returned to be reported), each SVI's row edge after the saves, the rows an SVI leaves and the
    prune of the stale SVIs. The SVIs in ``SviPlan.recreate_ids`` must be deleted before the plan is applied (the
    caller does, see ``reconcile_svis``). Pure.
    """
    svis = svi_changes(facts, instance_ids)
    taken = taken_svi_names(facts)
    svis = replace(svis, writes=tuple(write for write in svis.writes if write.name not in taken))
    plan = TopologyPlan(root_id=None)
    for write in svis.writes:
        data: dict[str, Any] = {
            "name": write.name,
            "device": device_hfid(facts),
            "role": SVI_ROLE,
            "status": "active",
            **_vrf_capability(write.vrf_id),
        }
        if write.address_id:
            data["ip_address"] = {"id": write.address_id}
        if write.existing_id:
            data["id"] = write.existing_id
        plan.add(
            NodeSpec(
                f"svi-{write.name}", DcimVirtualInterface, data, hfid=svi_hfid(facts, write.name), ownership="shared"
            )
        )
    for write in svis.writes:
        # After the saves, which replace the edge set: the row exists from the segment reconcile
        plan.add(EdgesSpec(NodeRef(f"svi-{write.name}"), _ROW_RELATION, (write.row_id,), retry_not_found=True))
    leaving: dict[str, set[str]] = defaultdict(set)
    for svi_id, row_id in svis.leave:
        leaving[svi_id].add(row_id)
    for svi_id, row_ids in sorted(leaving.items()):
        plan.add(
            PruneEdgesSpec(
                DcimVirtualInterface,
                svi_id,
                _ROW_RELATION,
                keep_peer_ids=frozenset(),
                known_peer_ids=frozenset(row_ids),
            )
        )
    if prune := stale_svi_prune(tuple(svi.id for svi in facts.svis), svis.stale_ids):
        plan.add(prune)
    return PlannedSvis(plan, taken, svis)
