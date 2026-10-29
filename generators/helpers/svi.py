"""The segment SVIs of a device: one ``DcimVirtualInterface`` (role ``svi``) per routed row of its VLAN domain.

The SVI carries no address (the anycast gateway stays ``segment.gateway``); it is the object a row is realized by on a
switch (``vlan_domain_segment``) and the VRF the switch routes it in (``vrf``). What a device needs is derived by
``utils.vrf_demand.device_vrf_facts``; this module only turns the difference with what exists into writes.

Identity: an SVI is found by its own uniqueness, (device, ``Vlan<vlan id>``), among the device's role-svi interfaces.
That is not the row link of the design (a row moving between domains then needs no delete-before-create ordering): a
row keeps its VLAN id for life, a different row gets a different name or, on the same name, the SVI is moved to the
new row in place.

Written untracked (``update_group_context=False``), like the rows: an SVI belongs to no single run.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from utils.limits import DEFAULT_NAMESPACE
from utils.vrf_demand import SVI_ROLE, DeviceVrfFacts, RoutedRow

from ..protocols import DcimVirtualInterface


@dataclass(frozen=True)
class SviWrite:
    """One SVI to write: ``existing_id`` is set for an in-place update, None for a create."""

    name: str
    row_id: str
    vrf_id: str | None
    existing_id: str | None = None


@dataclass(frozen=True)
class SviPlan:
    """What to do to a device's SVIs; ``recreate_ids`` are deleted before the writes, ``stale_ids`` after them."""

    writes: tuple[SviWrite, ...] = ()
    recreate_ids: tuple[str, ...] = ()
    stale_ids: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.writes or self.recreate_ids or self.stale_ids)


def desired_vrf_id(row: RoutedRow, instance_ids: Mapping[str, str]) -> str | None:
    """The instance an SVI of the row routes in: None for a ``default`` gateway (the global table)."""
    return None if row.namespace == DEFAULT_NAMESPACE else instance_ids[row.namespace]


def plan_svis(facts: DeviceVrfFacts, instance_ids: Mapping[str, str]) -> SviPlan:
    """The writes that make the device's role-svi interfaces exactly its desired ones.

    ``instance_ids`` maps a namespace name to the id of the device's instance of it (every non-default namespace of a
    desired SVI must be in it). An SVI that differs only in its row or in a VRF it still has is updated in place; one
    that would have to lose its VRF is deleted and created again (a relationship cannot be cleared through a save of
    a node read without it).
    """
    desired = facts.desired_svis()
    existing = {svi.name: svi for svi in facts.svis}
    writes: list[SviWrite] = []
    recreate: list[str] = []
    for name, row in desired.items():
        vrf_id = desired_vrf_id(row, instance_ids)
        current = existing.get(name)
        if current is None:
            writes.append(SviWrite(name, row.id, vrf_id))
        elif current.row_id == row.id and current.vrf_id == vrf_id:
            continue
        elif vrf_id is None and current.vrf_id is not None:
            recreate.append(current.id)
            writes.append(SviWrite(name, row.id, vrf_id))
        else:
            writes.append(SviWrite(name, row.id, vrf_id, existing_id=current.id))
    stale = sorted(svi.id for name, svi in existing.items() if name not in desired)
    return SviPlan(tuple(writes), tuple(sorted(recreate)), tuple(stale))


def taken_svi_names(facts: DeviceVrfFacts) -> dict[str, str | None]:
    """The desired SVI names an interface of another role already has (the MLAG control ``Vlan4094``, a hand-made
    ``VlanN``) -> that role. Creating the SVI by name would overwrite that interface."""
    return {name: facts.other_interfaces[name] for name in facts.desired_svis() if name in facts.other_interfaces}


class SviMixin:
    """Writes the SVIs of one device. Expects the host to provide ``client`` and ``logger``."""

    client: Any
    logger: logging.Logger

    async def sync_svis(self, facts: DeviceVrfFacts, instance_ids: Mapping[str, str]) -> SviPlan:
        """Create, move and delete the device's SVIs; returns the plan it applied (falsy when nothing changed).

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
                "vlan_domain_segment": {"id": write.row_id},
            }
            if write.vrf_id:
                data["vrf"] = {"id": write.vrf_id}
            if write.existing_id:
                data["id"] = write.existing_id
            svi = await self.client.create(kind=DcimVirtualInterface, data=data)
            await svi.save(allow_upsert=True, update_group_context=False)
        for svi_id in plan.stale_ids:
            await self.client.delete(kind=DcimVirtualInterface, id=svi_id)
        if plan:
            self.logger.info(
                f"  {facts.name}: SVIs written {len(plan.writes)}, deleted {len(plan.recreate_ids) + len(plan.stale_ids)}"
            )
        return plan
