"""OSPF as plan specs (schemas/extensions/capabilities/ospf.yml): the backbone area and the underlay of a layer pair:
a process per device, an interface config per cabled port, a peering per cable.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from generators.capabilities.common import RoutingPlanError, id_ref, pair_ends, password_ref
from generators.dcim.cabling_plan import Link
from generators.plan import Hfid, NodeRef, NodeSpec, TopologyPlan

OSPF_KIND = "ManagedOSPF"
AREA_KIND = "RoutingOSPFArea"


def ospf_area_name(fabric_name: str) -> str:
    return f"{fabric_name}-ospf-area-0"


def plan_ospf_area(plan: TopologyPlan, fabric_name: str) -> NodeRef:
    """The fabric's OSPF backbone area (area 0), upserted by name and owned by the run."""
    area_name = ospf_area_name(fabric_name)
    plan.add(
        NodeSpec(
            key="ospf-area",
            kind=AREA_KIND,
            data={
                "name": area_name,
                "area": 0,
                "area_type": "standard",
                "description": f"OSPF backbone area for {fabric_name}",
            },
            hfid=Hfid(AREA_KIND, (area_name,)),
            ownership="owned",
        )
    )
    return NodeRef("ospf-area")


def plan_ospf_underlay(builder: Any, pairs: list[tuple[Link, str, str, bool]], links: Sequence[Link]) -> None:
    """OSPF underlay: a process per device, an interface config per cabled port, a peering per cable."""
    shared = builder.shared
    if shared.ospf_area is None:
        raise RoutingPlanError("The shared OSPF area is not found: the DC generator must create it first")
    for name, device in builder.devices.items():
        router_id = builder.router_id(device, "OSPF")
        builder.process(
            name,
            "underlay",
            "ospf",
            f"OSPF process for {name} underlay",
            {
                "process_id": "1",
                "version": "ospf",
                "router_type": "internal",
                "router_id": id_ref(router_id),
            },
        )
    for link in links:
        for port in (link.bottom, link.top):
            if port.device_name not in builder.devices:
                continue
            name = f"{port.device_name}-{port.port_name}-ospf-underlay"
            builder.add_once(
                NodeSpec(
                    key=f"ospf-interface:{name}",
                    kind="RoutingOSPFInterface",
                    data={
                        "name": name,
                        "description": f"OSPF config for {port.device_name}:{port.port_name}",
                        "mode": "peer_to_peer",
                        "capabilities": [port.hfid],
                        **password_ref(shared.underlay_password),
                    },
                    ownership="owned",
                    hfid=Hfid("RoutingOSPFInterface", (name,)),
                )
            )
    for pair in pairs:
        _, a_name, b_name, _ = pair
        a, b = pair_ends(pair)
        a_label, b_label = a.port_name.replace("/", "_"), b.port_name.replace("/", "_")
        name = f"{a_name}--{a_label}--{b_name}--{b_label}-ospf-peering"
        builder.add_once(
            NodeSpec(
                key=f"ospf-peering:{name}",
                kind="ManagedOSPFPeering",
                data={
                    "name": name,
                    "description": f"OSPF peering: {a_name} ({a.port_name}) <-> {b_name} ({b.port_name})",
                    "network_type": "point-to-point",
                    "ospf_area": id_ref(shared.ospf_area),
                    "ospf_process": [
                        Hfid(OSPF_KIND, (f"{a_name}-ospf-underlay",)),
                        Hfid(OSPF_KIND, (f"{b_name}-ospf-underlay",)),
                    ],
                    "ospf_interface": [
                        Hfid("RoutingOSPFInterface", (f"{a_name}-{a.port_name}-ospf-underlay",)),
                        Hfid("RoutingOSPFInterface", (f"{b_name}-{b.port_name}-ospf-underlay",)),
                    ],
                },
                ownership="owned",
                hfid=Hfid("ManagedOSPFPeering", (name,)),
                retry_on_conflict=True,
            )
        )
