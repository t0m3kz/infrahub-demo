"""Build the static routes of a service context on the border devices its service ports are tagged on.

The target is the context (group ``service_contexts``), so each context owns its routes: they are saved tracked, and the
run's group deletes the ones the derivation no longer yields, on whatever device they sat. A person's route is not in the
group and is never touched; the names ``rt:<context>:...`` are the generator's. Starts when a context joins the group and
whenever its ``capabilities`` change (a leg or a border port added), so a context built in steps is routed once complete.
A run over an incomplete context (no leg or no tagged port yet) writes nothing.
"""

from __future__ import annotations

from typing import Any

from utils.data_cleaning import clean_data
from utils.exchange_transit import EXCHANGE_PEERS, transit_addresses

from ..common import CommonGenerator
from ..exchange_routes import ExchangeRoutesMixin
from ..helpers.vrf import VrfMixin
from ..pools import PoolMixin

FIREWALL_KIND = "ManagedFirewallContext"
LOADBALANCER_KIND = "ManagedLoadbalancerContext"
_LEG_KIND = "DcimVirtualInterface"


class ContextRoutesGenerator(ExchangeRoutesMixin, VrfMixin, PoolMixin, CommonGenerator):
    """The routes of one firewall or load-balancer context (see the module docstring)."""

    async def generate(self, data: dict[str, Any]) -> None:
        cleaned = clean_data(data)
        context = next((c for kind in (FIREWALL_KIND, LOADBALANCER_KIND) for c in cleaned.get(kind) or []), None)
        if context is None:
            self.fail("No service context in the GraphQL response")
        load_balancer = LOADBALANCER_KIND in cleaned and bool(cleaned[LOADBALANCER_KIND])
        legs, devices, leg_interfaces = self._legs_and_devices(context)
        if not legs or not devices:
            self.logger.info(f"{context['name']}: no leg or no tagged border port yet - no routes")
            return
        tenant_type = next((t for t in legs if t in EXCHANGE_PEERS), None)
        if tenant_type is None:
            self.fail(f"{context['name']}: no tenant leg among {sorted(legs)}")
        tenant_leg = legs[tenant_type]
        if load_balancer:
            vip_prefix = (context.get("vip_prefix") or {}).get("prefix")
            if not vip_prefix:
                self.logger.info(f"{context['name']}: no VIP prefix yet - no routes")
                return
            spec = self.vip_spec(tenant_leg, vip_prefix)
            specs = {False: [spec], True: [spec]}
        else:
            snat = ((context.get("snat_address") or {}).get("address") or "").split("/")[0] or None
            peers = [leg for leg_type, leg in legs.items() if leg_type != tenant_type]
            shared = not (context.get("tenant") or {}).get("id")
            specs = {
                edge: self.exchange_specs(tenant_leg=tenant_leg, peer_legs=peers, shared=shared, snat=snat, edge=edge)
                for edge in (False, True)
            }
        # The devices are in their VRF instances before a route points at one (idempotent; no context lock is held).
        await self.reconcile_vrfs(sorted(devices))
        async with self.resource_lock(f"context-routes-{context['id']}"):
            await self.write_routes(
                context=context, device_ids=sorted(devices), specs_for=specs, leg_interfaces=leg_interfaces
            )

    @staticmethod
    def _legs_and_devices(
        context: dict[str, Any],
    ) -> tuple[dict[str, dict[str, Any]], set[str], dict[str, str]]:
        """{namespace type: leg}, the ids of the border devices of the other ports, and {VRF name: interface id}.

        The interface is the leg of the first member (by device name) in that VRF: the interface that owns the next hop
        of a route there (the pair's floating address has no interface of its own), where a path through the
        firewall enters."""
        legs: dict[str, dict[str, Any]] = {}
        interfaces: dict[str, tuple[str, str]] = {}
        devices: set[str] = set()
        for cap in context.get("capabilities") or []:
            if cap.get("typename") == _LEG_KIND:
                address = (cap.get("ip_address") or {}).get("address")
                namespace = (cap.get("ip_address") or {}).get("ip_namespace") or {}
                if address and namespace.get("namespace_type"):
                    legs.setdefault(
                        namespace["namespace_type"],
                        {
                            "namespace": namespace["name"],
                            "l3_vni": namespace.get("l3_vni"),
                            "ns_type": namespace["namespace_type"],
                            "vip": transit_addresses(address)["vip"],
                        },
                    )
                    member = (cap.get("device") or {}).get("name") or ""
                    if namespace["name"] not in interfaces or member < interfaces[namespace["name"]][0]:
                        interfaces[namespace["name"]] = (member, cap["id"])
            elif (cap.get("device") or {}).get("id"):
                devices.add(cap["device"]["id"])
        return legs, devices, {name: leg_id for name, (_, leg_id) in interfaces.items()}
