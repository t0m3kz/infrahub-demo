"""Build the static routes of a service context on the border devices its service ports are tagged on. Routes are saved
tracked (stale ones deleted, a person's never touched); an incomplete context writes nothing. See
utils/service_contexts.py.
"""

from __future__ import annotations

from typing import Any

from generators.base import BaseGenerator
from generators.capabilities.vrf import reconcile_vrfs
from generators.connectivity.exchange_routes import (
    exchange_specs,
    plan_routes,
    read_border_devices,
    read_vrf_instances,
    vip_spec,
)
from generators.plan import NodeSpec
from utils.data_cleaning import clean_data
from utils.exchange_transit import BORDER_ROLE_FOR_SERVICES, EXCHANGE_PEERS, transit_addresses
from utils.routing_domain import interface_vrf
from utils.service_contexts import PHYSICAL_INTERFACE, leg_interfaces, member_border_ports

FIREWALL_KIND = "ManagedFirewallContext"
LOADBALANCER_KIND = "ManagedLoadbalancerContext"


class ContextRoutesGenerator(BaseGenerator):
    """The routes of one firewall or load-balancer context."""

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
            spec = vip_spec(tenant_leg, vip_prefix)
            specs = {False: [spec], True: [spec]}
        else:
            snat = ((context.get("snat_address") or {}).get("address") or "").split("/")[0] or None
            peers = [leg for leg_type, leg in legs.items() if leg_type != tenant_type]
            shared = not (context.get("tenant") or {}).get("id")
            specs = {
                edge: exchange_specs(tenant_leg=tenant_leg, peer_legs=peers, shared=shared, snat=snat, edge=edge)
                for edge in (False, True)
            }
        # Devices are in their VRF instances before a route points at one (idempotent; no context lock held)
        device_ids = sorted(devices)
        await reconcile_vrfs(self, device_ids)
        async with self.plan_locks(f"context-routes-{context['id']}"):
            border = await read_border_devices(self.client, device_ids)
            plan = plan_routes(
                context_name=context["name"],
                devices=border,
                instances=await read_vrf_instances(self.client, [device.id for device in border]),
                specs_for=specs,
                leg_interfaces=leg_interfaces,
            )
            if plan.specs:
                try:
                    await self.apply_plan(plan)
                except Exception as exc:
                    names = ", ".join(spec.key for spec in plan.of(NodeSpec))
                    self.fail(f"Failed to upsert the static route(s) {names}: {exc}")

    @staticmethod
    def _legs_and_devices(
        context: dict[str, Any],
    ) -> tuple[dict[str, dict[str, Any]], set[str], dict[str, str]]:
        """{namespace type: leg}, the border device ids facing the members, and {VRF name: interface id}. A leg is a
        member's sub-interface of its first uplink with the transit VLAN; the first member's interface (by name) in a
        VRF owns the next hop.
        """
        legs: dict[str, dict[str, Any]] = {}
        interfaces: dict[str, tuple[str, str]] = {}
        devices: set[str] = set()
        for member in context.get("shared_devices") or []:
            for leg in leg_interfaces(member).get(str(context.get("id")), []):
                address = (leg.get("ip_address") or {}).get("address")
                namespace = interface_vrf(leg).get("security_zone") or {}
                if address and namespace.get("vrf_type"):
                    legs.setdefault(
                        namespace["vrf_type"],
                        {
                            "namespace": namespace["name"],
                            "l3_vni": namespace.get("l3_vni"),
                            "ns_type": namespace["vrf_type"],
                            "vip": transit_addresses(address)["vip"],
                        },
                    )
                    name = str(member.get("name") or "")
                    if namespace["name"] not in interfaces or name < interfaces[namespace["name"]][0]:
                        interfaces[namespace["name"]] = (name, leg["id"])
            port_role = BORDER_ROLE_FOR_SERVICES.get(str(member.get("role") or ""))
            devices |= {
                far["device"]["id"]
                for far in member_border_ports(member)
                if far.get("typename") == PHYSICAL_INTERFACE
                and far.get("role") == port_role
                and (far.get("device") or {}).get("id")
            }
        return legs, devices, {name: leg_id for name, (_, leg_id) in interfaces.items()}
