"""The static routes of a service context's exchange, written as ``ManagedStaticRoute`` objects on its border devices.
The rule is ``utils.exchange_transit.exchange_route_specs``; the routes are tracked output of
generators/connectivity/context_routes.py. ``plan_routes`` is the pure builder: a route that cannot be placed fails the
plan before the first write.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from generators.model.errors import GeneratorError
from generators.plan import NodeSpec, TopologyPlan
from generators.protocols import DcimDeviceManaged, ManagedStaticRoute, ManagedVRF
from utils.exchange_transit import exchange_route_specs

EDGE_ROLE = "edge"
"""A colocation edge keeps the tenant default a local static: it resolves the VIP over its own transit L2VNI, and
exporting it as type-5 would let a DC import another site's PROD default via a stretched segment. Every other border
device exports it into EVPN."""


@dataclass(frozen=True)
class BorderDevice:
    """A border device a context's routes sit on: ``is_edge`` picks which route set it gets."""

    id: str
    name: str
    is_edge: bool


def route_name(context: str, device: str, namespace: str, prefix: str) -> str:
    """Route name: unique per context, device, VRF and prefix, stable across runs."""
    return f"rt:{context}:{device}:{namespace}:{prefix}".lower()


def exchange_specs(
    *,
    tenant_leg: Mapping[str, Any],
    peer_legs: Sequence[Mapping[str, Any]],
    shared: bool,
    snat: str | None,
    edge: bool,
) -> list[dict[str, Any]]:
    """Routes of a firewall context's exchanges, for a border device of this kind."""
    return [
        spec
        for peer_leg in peer_legs
        for spec in exchange_route_specs(tenant_leg, peer_leg, shared=shared, snat=snat, export_tenant_default=not edge)
    ]


def vip_spec(leg: Mapping[str, Any], vip_prefix: str) -> dict[str, Any]:
    """The route of a load-balancer context's VIP range to its leg's floating address."""
    return {"namespace": leg["namespace"], "prefix": vip_prefix, "nexthop": leg["vip"], "local_only": False}


async def read_border_devices(client: Any, device_ids: list[str]) -> list[BorderDevice]:
    """The devices with their role (an edge keeps its tenant default local): DcimDeviceManaged, not the slim DcimDevice,
    carries it.
    """
    devices = await client.filters(kind=DcimDeviceManaged, ids=device_ids)
    return [
        BorderDevice(device.id, device.name.value, getattr(getattr(device, "role", None), "value", None) == EDGE_ROLE)
        for device in devices
    ]


async def read_vrf_instances(client: Any, device_ids: list[str]) -> dict[tuple[str, str], str]:
    """(device id, namespace name) -> id of the VRF instance the device is a member of."""
    found = await client.filters(
        kind=ManagedVRF, shared_devices__ids=device_ids, include=["shared_devices", "security_zone"]
    )
    return {(peer.id, vrf.security_zone.peer.name.value): vrf.id for vrf in found for peer in vrf.shared_devices.peers}


def plan_routes(
    *,
    context_name: str,
    devices: Sequence[BorderDevice],
    instances: Mapping[tuple[str, str], str],
    specs_for: Mapping[bool, list[dict[str, Any]]],
    leg_interfaces: Mapping[str, str],
) -> TopologyPlan:
    """One tracked ``ManagedStaticRoute`` per device and route spec, in the device's VRF instance. A route name listed
    twice is one upsert, the later spec winning. A device in no instance of a route's VRF raises ``GeneratorError``.
    """
    nodes: dict[str, NodeSpec] = {}
    for device in devices:
        for spec in specs_for[device.is_edge]:
            name = route_name(context_name, device.name, spec["namespace"], spec["prefix"])
            vrf_id = instances.get((device.id, spec["namespace"]))
            if vrf_id is None:
                raise GeneratorError(
                    f"{device.name} is in no VRF instance of {spec['namespace']}: cannot place route '{name}'"
                )
            nodes[name] = NodeSpec(
                name,
                ManagedStaticRoute,
                {
                    "name": name,
                    "prefix": spec["prefix"],
                    "next_hop": spec["nexthop"],
                    "exported": not spec["local_only"],
                    "status": "active",
                    "vrf": {"id": vrf_id},
                    **(
                        {"next_hop_interface": {"id": leg_interfaces[spec["namespace"]]}}
                        if spec["namespace"] in leg_interfaces
                        else {}
                    ),
                    "capabilities": [{"id": device.id}],
                },
                track=True,
            )
    return TopologyPlan(root_id=None, specs=list(nodes.values()))
