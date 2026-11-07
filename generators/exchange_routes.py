"""The static routes of a service context's exchange, written as ``ManagedStaticRoute`` objects on its border devices.

A firewall context joins the tenant VRF to INTERNET and MANAGEMENT; the border leaf (or the colocation edge) whose service
port faces a member carries the static routes of that exchange: the tenant default route and the return route of the
context's public address. A load-balancer context carries the route of its VIP range. The rule is one function
(``utils.exchange_transit.exchange_route_specs``) and the transform renders the objects this module writes, so there is
one path from model to configuration.

Ownership: ``maintained_by`` is the context; a context maintains exactly the routes that name it, on
whatever device they sit, and never updates or deletes a ``manual`` route, nor one another context or a person owns.
Order: the desired set is written first; the stale ones are deleted only when every write succeeded.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from utils.exchange_transit import exchange_route_specs, transit_addresses, transit_vlan

from .protocols import DcimDevice, DcimVirtualInterface, IpamIPAddress, IpamPrefix, ManagedStaticRoute, ManagedVRF

EDGE_ROLE = "edge"
"""A colocation edge keeps the tenant default a local static: it resolves the VIP over its own transit L2VNI, and
exporting it as type-5 would let a DC import another site's PROD default via a stretched segment. Every other border
device exports it into EVPN."""


def route_prefix(context: str) -> str:
    """The name prefix that marks the routes a context maintains."""
    return f"rt:{context}:".lower()


def route_name(context: str, device: str, namespace: str, prefix: str) -> str:
    """The name of a route: unique per context, device, VRF and prefix, stable across runs."""
    return f"{route_prefix(context)}{device}:{namespace}:{prefix}".lower()


@dataclass(frozen=True)
class RouteInputs:
    """What the reconcile of a context's routes needs from the step that built its legs."""

    spec: Any
    context_name: str
    members: list[Any]
    namespaces: Mapping[str, Any]
    leg_types: tuple[str, ...]
    shared: bool


class ExchangeRoutesMixin:
    """Reconcile the routes of one service context on the border devices whose ports face its members."""

    client: Any
    logger: logging.Logger
    resource_lock: Any

    def route_inputs(self) -> dict[str, RouteInputs]:
        """The inputs kept per context id for this run (a generator instance runs once)."""
        return self.__dict__.setdefault("_route_inputs", {})

    async def reconcile_routes_of(self, context_id: str) -> bool:
        """Reconcile the routes of the context ``_ensure_transit_legs`` just handled, under a lock of its own and on a
        fresh read of the context, so concurrent runs reconcile one snapshot after the other. False when it failed."""
        inputs = self.route_inputs().get(context_id)
        if inputs is None:
            return True
        spec = inputs.spec
        async with self.resource_lock(f"routes-{spec.key}-{inputs.context_name}"):
            (context,) = await self.client.filters(kind=spec.context_kind, ids=[context_id])
            return await self.reconcile_context_routes(
                context=context,
                members=inputs.members,
                namespaces=inputs.namespaces,
                leg_types=inputs.leg_types,
                shared=inputs.shared,
                load_balancer=spec.vip_pool_name is not None,
            )

    @staticmethod
    def exchange_specs(
        *,
        tenant_leg: Mapping[str, Any],
        peer_legs: Sequence[Mapping[str, Any]],
        shared: bool,
        snat: str | None,
        edge: bool,
    ) -> list[dict[str, Any]]:
        """The routes of a firewall context's exchanges (the one rule), for a border device of this kind."""
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

    async def reconcile_context_routes(
        self,
        *,
        context: Any,
        members: list[Any],
        namespaces: Mapping[str, Any],
        leg_types: tuple[str, ...],
        shared: bool,
        load_balancer: bool,
    ) -> bool:
        """Reconcile the routes of a context from what is stored: its legs' addresses, SNAT address and VIP prefix.

        Reads the sub-interface of the first member for each leg (the /29 gives the floating VIP), so a context that is
        complete and one that was just built go through the same code. False (logged) when something is missing.
        """
        vlan_id = context.vlan_id.value
        subs = {
            (sub.device.id, sub.name.value): sub
            for sub in await self.client.filters(kind=DcimVirtualInterface, capabilities__ids=[context.id])
        }
        first = members[0]
        leg_subs: dict[str, Any] = {}
        for leg_type in leg_types:
            sub = subs.get((first.device_id, f"{first.trunk.name.value}.{transit_vlan(vlan_id, leg_type)}"))
            if sub is None or not sub.ip_address.id:
                self.logger.error(f"{context.name.value}: no {leg_type} leg address to route to")
                return False
            leg_subs[leg_type] = sub
        snat_id = None if load_balancer else getattr(context.snat_address, "id", None)
        wanted = [sub.ip_address.id for sub in leg_subs.values()] + ([snat_id] if snat_id else [])
        addresses = {
            a.id: str(a.address.value) for a in await self.client.filters(kind=IpamIPAddress, ids=sorted(wanted))
        }
        legs: dict[str, dict[str, Any]] = {}
        for leg_type, sub in leg_subs.items():
            namespace = namespaces[leg_type]
            legs[leg_type] = {
                "namespace": namespace.name.value,
                "l3_vni": namespace.l3_vni.value,
                "ns_type": leg_type,
                "vip": transit_addresses(addresses[sub.ip_address.id])["vip"],
            }
        tenant_leg = legs[leg_types[0]]
        if load_balancer:
            vip_prefix = getattr(context.vip_prefix, "id", None)
            if not vip_prefix:
                return True
            (prefix,) = await self.client.filters(kind=IpamPrefix, ids=[vip_prefix])
            spec = self.vip_spec(tenant_leg, str(prefix.prefix.value))
            specs = {False: [spec], True: [spec]}
        else:
            snat = addresses[snat_id].split("/")[0] if snat_id else None
            peers = [legs[leg_type] for leg_type in leg_types[1:]]
            specs = {
                edge: self.exchange_specs(tenant_leg=tenant_leg, peer_legs=peers, shared=shared, snat=snat, edge=edge)
                for edge in (False, True)
            }
        return await self.reconcile_routes(
            context=context,
            device_ids=sorted({m.bl_port.device.id for m in members}),
            specs_for=specs,
        )

    async def reconcile_routes(
        self,
        *,
        context: Any,
        device_ids: list[str],
        specs_for: Mapping[bool, list[dict[str, Any]]],
    ) -> bool:
        """Make the routes ``context`` maintains equal ``specs_for[is_edge]`` on each device; False when a write failed.

        Runs on every pass, a complete context included: complete legs say nothing about the routes. Stale routes are
        deleted only after every desired one was written, so a failed write leaves the previous routes in place.
        The routes the context maintains are found by ``maintained_by`` (the context itself), on any device: a route left on a device
        the attachment moved away from is removed too, and a renamed context keeps ownership of its routes.
        """
        devices = await self.client.filters(kind=DcimDevice, ids=sorted(device_ids))
        instances = await self._vrf_instances([d.id for d in devices])
        desired: set[str] = set()
        for device in devices:
            is_edge = getattr(getattr(device, "role", None), "value", None) == EDGE_ROLE
            for spec in specs_for[is_edge]:
                name = route_name(context.name.value, device.name.value, spec["namespace"], spec["prefix"])
                vrf_id = instances.get((device.id, spec["namespace"]))
                if vrf_id is None:
                    self.logger.error(
                        f"{device.name.value} is in no VRF instance of {spec['namespace']}: cannot place route '{name}'"
                    )
                    return False
                if not await self._upsert_route(context, device, name, spec, vrf_id):
                    return False
                desired.add(name)
        for route in await self.client.filters(kind=ManagedStaticRoute, maintained_by__ids=[context.id]):
            if route.origin.value == "generated" and route.name.value not in desired:
                await route.delete()
        return True

    async def _vrf_instances(self, device_ids: list[str]) -> dict[tuple[str, str], str]:
        """(device id, namespace name) -> id of the VRF instance the device is a member of, one read.

        The instance follows the legs (``_reconcile_border_vrfs``), so this runs after it."""
        found = await self.client.filters(
            kind=ManagedVRF, shared_devices__ids=device_ids, include=["shared_devices", "namespace"]
        )
        return {(peer.id, vrf.namespace.peer.name.value): vrf.id for vrf in found for peer in vrf.shared_devices.peers}

    async def _upsert_route(self, context: Any, device: Any, name: str, spec: Mapping[str, Any], vrf_id: str) -> bool:
        # ``origin: manual`` is a declaration; the guard is this read: an upsert by name would overwrite it.
        existing = await self.client.filters(kind=ManagedStaticRoute, name__value=name)
        if any(route.origin.value == "manual" for route in existing):
            self.logger.warning(f"Static route '{name}' on {device.name.value} is manual: left as it is")
            return True
        exported = not spec["local_only"]
        if any(r.next_hop.value == spec["nexthop"] and r.exported.value == exported for r in existing):
            return True  # already as derived: a re-run writes nothing
        try:
            route = await self.client.create(
                kind=ManagedStaticRoute,
                data={
                    "name": name,
                    "prefix": spec["prefix"],
                    "next_hop": spec["nexthop"],
                    "exported": exported,
                    "origin": "generated",
                    "maintained_by": {"id": context.id},
                    "status": "active",
                    "vrf": {"id": vrf_id},
                    "capabilities": [{"id": device.id}],
                },
            )
            await route.save(allow_upsert=True, update_group_context=False)
        except Exception as exc:
            self.logger.error(f"Failed to upsert the static route '{name}' on {device.name.value}: {exc}")
            return False
        return True
