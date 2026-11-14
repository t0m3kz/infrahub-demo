"""The static routes of a service context's exchange, written as ``ManagedStaticRoute`` objects on its border devices.
The rule is ``utils.exchange_transit.exchange_route_specs``; the routes are tracked output of
generators/topology/context_routes.py.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from generators.mixins.connections import tracked_save_kwargs
from generators.protocols import DcimDeviceManaged, ManagedStaticRoute, ManagedVRF
from utils.exchange_transit import exchange_route_specs

EDGE_ROLE = "edge"
"""A colocation edge keeps the tenant default a local static: it resolves the VIP over its own transit L2VNI, and
exporting it as type-5 would let a DC import another site's PROD default via a stretched segment. Every other border
device exports it into EVPN."""


def route_name(context: str, device: str, namespace: str, prefix: str) -> str:
    """Route name: unique per context, device, VRF and prefix, stable across runs."""
    return f"rt:{context}:{device}:{namespace}:{prefix}".lower()


class ExchangeRoutesMixin:
    """Route specs of a service context and their write as tracked objects."""

    client: Any
    logger: logging.Logger

    @staticmethod
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
            for spec in exchange_route_specs(
                tenant_leg, peer_leg, shared=shared, snat=snat, export_tenant_default=not edge
            )
        ]

    @staticmethod
    def vip_spec(leg: Mapping[str, Any], vip_prefix: str) -> dict[str, Any]:
        """The route of a load-balancer context's VIP range to its leg's floating address."""
        return {"namespace": leg["namespace"], "prefix": vip_prefix, "nexthop": leg["vip"], "local_only": False}

    async def write_routes(
        self,
        *,
        context: Mapping[str, Any],
        device_ids: list[str],
        specs_for: Mapping[bool, list[dict[str, Any]]],
        leg_interfaces: Mapping[str, str],
    ) -> None:
        """Save each route on its device in its VRF instance, tracked by the run.
        A device in no instance of the route's VRF fails the run."""
        # DcimDeviceManaged, not the slim DcimDevice: the role (an edge keeps its tenant default local) is on it
        devices = await self.client.filters(kind=DcimDeviceManaged, ids=device_ids)
        instances = await self._vrf_instances([d.id for d in devices])
        for device in devices:
            is_edge = getattr(getattr(device, "role", None), "value", None) == EDGE_ROLE
            for spec in specs_for[is_edge]:
                name = route_name(context["name"], device.name.value, spec["namespace"], spec["prefix"])
                vrf_id = instances.get((device.id, spec["namespace"]))
                if vrf_id is None:
                    self.logger.error(
                        f"{device.name.value} is in no VRF instance of {spec['namespace']}: cannot place route '{name}'"
                    )
                    return
                try:
                    route = await self.client.create(
                        kind=ManagedStaticRoute,
                        data={
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
                    )
                    await route.save(**tracked_save_kwargs(True))
                except Exception as exc:
                    self.logger.error(f"Failed to upsert the static route '{name}' on {device.name.value}: {exc}")
                    return

    async def _vrf_instances(self, device_ids: list[str]) -> dict[tuple[str, str], str]:
        """(device id, namespace name) -> id of the VRF instance the device is a member of."""
        found = await self.client.filters(
            kind=ManagedVRF, shared_devices__ids=device_ids, include=["shared_devices", "security_zone"]
        )
        return {
            (peer.id, vrf.security_zone.peer.name.value): vrf.id for vrf in found for peer in vrf.shared_devices.peers
        }
