"""What the rack generator reads before it plans, and what it keeps of the plan to build the rest of it (the MLAG pairs
of the devices, planned once all the roles are)."""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from generators.capabilities.mlag import MlagReads, read_mlag_reads
from generators.dc.routing_inputs import RoutingInputs
from generators.dcim.cabling_plan import Link, PortFacts
from generators.dcim.device_plan import DeviceReads, read_device_state
from generators.dcim.rack_layout import read_device_positions, read_rack_positions
from generators.dcim.template_interfaces import TemplatePort, read_template_ports
from generators.helpers.cable_attributes import RackPosition


@dataclass(frozen=True)
class RowLeafs:
    """The leafs of a compute rack's row (another rack's, created by its own run) and their downlink ports, as they
    exist: the target of its l2-leaf and access-leaf cables.
    """

    names: list[str] = field(default_factory=list)
    downlinks: list[PortFacts] = field(default_factory=list)


@dataclass(frozen=True)
class RackReads:
    """What exists before the run and the plan needs: the devices of earlier runs and the role groups, the ports of
    every template involved (by template id), the rack position of each device that is cabled (by device name), the
    leafs of the row a compute rack cables to, the routing state (None: the rack has no routing design) and what the MLAG
    wiring of its pairs finds.
    """

    devices: DeviceReads = field(default_factory=DeviceReads)
    template_ports: Mapping[str, list[TemplatePort]] = field(default_factory=dict)
    template_owners: Mapping[str, str | None] = field(default_factory=dict)
    positions: Mapping[str, RackPosition | None] = field(default_factory=dict)
    row_leafs: RowLeafs = field(default_factory=RowLeafs)
    routing: RoutingInputs | None = None
    mlag: MlagReads = field(default_factory=MlagReads)


@dataclass(frozen=True)
class RoutedLayer:
    """A layer pair that was cabled (or, ``overlay_only``, peers its overlay only): routed in the plan once all its
    devices are planned.
    """

    bottom_devices: tuple[str, ...]
    top_devices: tuple[str, ...]
    bottom_role: str
    top_role: str
    links: tuple[Link, ...]
    overlay_only: bool = False


@dataclass(frozen=True)
class MlagGroup:
    """Devices of one role entry that pair into MLAG domains (``plan_mlag_domains``)."""

    names: tuple[str, ...]
    role: str
    template: Mapping[str, Any]
    mlag_create: Literal["back-to-back", "virtual"]
    supports_virtual: bool = True


async def read_rack_reads(
    client: Any,
    *,
    rack_id: str,
    device_names: list[str],
    group_names: Collection[str],
    template_ids: Collection[str],
    existing_names: list[str],
    row_leafs: RowLeafs | None = None,
    routing: RoutingInputs | None = None,
    mlag_names: Sequence[str] = (),
    mlag_domains: Sequence[str] = (),
) -> RackReads:
    """The reads of a rack's plan: existing devices, template ports, rack positions (the new devices sit in the rack
    itself, the ``existing_names`` (the pod's spines, the row's leafs) in their own racks).
    """
    devices = await read_device_state(client, device_names, group_names)
    templates = await read_template_ports(client, template_ids)
    positions: dict[str, RackPosition | None] = {}
    if existing_names:
        positions.update(await read_device_positions(client, existing_names))
    own_positions = await read_rack_positions(client, [rack_id], racks={}, suites={})
    positions.update({name: own_positions.get(rack_id) for name in device_names})
    return RackReads(
        devices=devices,
        template_ports=templates.ports,
        template_owners=templates.owners,
        positions=positions,
        row_leafs=row_leafs or RowLeafs(),
        routing=routing,
        mlag=await read_mlag_reads(client, device_names=mlag_names, domain_names=mlag_domains),
    )
