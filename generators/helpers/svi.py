"""The segment SVIs of a device: one ``DcimVirtualInterface`` (role ``svi``) per routed row, written untracked. The SVI
carries the anycast gateway, sits in the switch's VRF and is added to its VLAN row edge by edge after every save; demand
comes from utils.vrf_demand.
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


def plan_svis(facts: DeviceVrfFacts, instance_ids: Mapping[str, str]) -> SviPlan:
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


def plan_l3vni_svis(
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


def taken_svi_names(facts: DeviceVrfFacts) -> dict[str, str | None]:
    """Desired SVI names that an interface of another role already has (e.g. MLAG ``Vlan4094``) -> that role."""
    return {name: facts.other_interfaces[name] for name in facts.desired_svis() if name in facts.other_interfaces}


class SviMixin:
    """Writes the SVIs of one device; the host provides ``client`` and ``logger``."""

    client: Any
    logger: logging.Logger

    async def sync_l3vni_svis(
        self, facts: DeviceVrfFacts, namespaces: Mapping[str, Mapping[str, Any]], instance_ids: Mapping[str, str]
    ) -> None:
        """Create, move and delete the device's L3 VNI SVIs. The caller holds the device's VRF lock and has created the
        instances. The SVI has no address and no row. Untracked.
        """
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
        The caller holds the device's VRF lock and has created the instances."""
        plan = plan_svis(facts, instance_ids)
        taken = taken_svi_names(facts)
        for name, role in taken.items():
            # Warn, don't fail: the logger's error() raises and the other SVIs must converge; the check reports it
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
            # After the save, which replaces the edge set: the row exists from the segment reconcile
            await join_capabilities(self.client, self.logger, svi.id, [write.row_id])
        for svi_id, row_id in plan.leave:
            await remove_edge(self.client, node_id=svi_id, relation=_ROW_RELATION, peer_id=row_id)
        for svi_id in plan.stale_ids:
            await self.client.delete(kind=DcimVirtualInterface, id=svi_id)
        if plan:
            self.logger.info(
                f"  {facts.name}: SVIs written {len(plan.writes)}, deleted {len(plan.recreate_ids) + len(plan.stale_ids)}"
            )
