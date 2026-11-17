"""The SD-WAN side of an office (built by add_office_fabric): its edge, one tunnel per gateway of ``sdwan_gateways``, the
edge's attachment to the controller that manages the gateways and, for an office with no fabric, its LAN prefix.

The edge comes from the office's ``sdwan-edge`` fabric_templates entry. What it rides on is authored data, not built
here: a TopologyCarrierCircuit of type ``internet`` from the edge's WAN port to the ISP's router (``INET-<office>``).
The tunnel is inventory only (no session). Nothing here names a provider, a template or an orchestrator: the overlay
provider is the owner of the gateway, the template the entry's and the orchestrator the controller of the gateways.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any, NamedTuple

from infrahub_sdk.protocols import CoreIPPrefixPool

from generators.helpers.naming import sdwan_edge_name
from generators.ipam import allocation
from generators.plan import DeviceSpec, TopologyPlan
from generators.protocols import (
    DcimPhysicalInterface,
    ManagedController,
    TopologyCustomerOffice,
    TopologySessionCircuit,
)
from utils.limits import GROUP_VIRTUAL_CIRCUITS
from utils.segment_addressing import tenant_pool_name

# An office is always PROD (no environment): its LAN comes from the PROD tenant pool.
_OFFICE_LAN_NAMESPACE = "PROD"
EDGE_WAN_PORT_ROLE = "uplink"
_PROVIDER_KIND = "OrganizationProvider"


class GatewayAttachment(NamedTuple):
    """One gateway of an office: its device, its zone, its public port (the far end of the office's tunnel) and the
    provider that operates the overlay (its owner)."""

    id: str
    name: str
    zone_id: str
    zone_name: str
    public_port_id: str
    provider_id: str


def plan_edge(*, office_name: str, office_id: str, owner_id: str, template_name: str) -> TopologyPlan:
    """The office's edge: from its template on first creation, its WAN ports (``EDGE_WAN_PORT_ROLE``) read back."""
    plan = TopologyPlan(root_id=office_id)
    plan.add(
        DeviceSpec(
            name=sdwan_edge_name(office_name),
            template_name=template_name,
            discover_interface_role=EDGE_WAN_PORT_ROLE,
            owner_id=owner_id,
            deployment_id=office_id,
            description=f"{office_name} SD-WAN branch edge",
        )
    )
    return plan


def gateway_attachments(office_name: str, gateways: Sequence[Mapping[str, Any]]) -> list[str | GatewayAttachment]:
    """The ids each gateway's tunnel needs, in gateway-name order; a gap is returned as a message (all of them are
    reported together by the caller, before anything is written)."""
    result: list[str | GatewayAttachment] = []
    for gateway in sorted((g for g in gateways if g.get("id")), key=lambda g: g.get("name") or ""):
        name: str = gateway.get("name") or gateway["id"]
        zone = gateway.get("deployment") or {}
        owner = gateway.get("owner") or {}
        ports = sorted(
            (
                iface
                for iface in gateway.get("interfaces") or []
                if iface.get("typename") == DcimPhysicalInterface.__name__ and iface.get("role") == "customer"
            ),
            key=lambda iface: iface.get("name") or "",
        )
        if not zone.get("id") or not zone.get("name"):
            result.append(f"{office_name}: gateway {name} has no deployment — cannot set the tunnel's locations")
        elif owner.get("typename") != _PROVIDER_KIND:
            result.append(
                f"{office_name}: gateway {name} has no owner that is an {_PROVIDER_KIND} — the tunnel's provider is "
                "the gateway's owner: set the SD-WAN overlay operator as `owner` of the gateway"
            )
        elif not ports:
            result.append(f"{office_name}: gateway {name} has no role=customer public interface")
        elif len(ports) > 1:
            result.append(
                f"{office_name}: gateway {name} has several role=customer public interfaces "
                f"{[iface.get('name') for iface in ports]}"
            )
        else:
            result.append(GatewayAttachment(gateway["id"], name, zone["id"], zone["name"], ports[0]["id"], owner["id"]))
    return result


def orchestrator_of(office_name: str, gateways: Sequence[GatewayAttachment], controllers: Sequence[Any]) -> Any | None:
    """The one controller that manages the office's gateways (``controllers``: those managing any of them, with their
    ``managed_devices`` loaded); None when none does. Gateways under different controllers raise before any write."""
    managers: dict[str, Any] = {}
    by_gateway: dict[str, set[str]] = defaultdict(set)
    gateway_ids = {gateway.id for gateway in gateways}
    for controller in controllers:
        for peer in getattr(controller, "managed_devices").peers:
            if peer.id in gateway_ids:
                managers[controller.id] = controller
                by_gateway[peer.id].add(controller.id)
    if len(managers) > 1:
        names = {gateway.id: gateway.name for gateway in gateways}
        detail = "; ".join(
            f"{names[gateway_id]} by {sorted(managers[c].name.value for c in ids)}"
            for gateway_id, ids in sorted(by_gateway.items(), key=lambda item: names[item[0]])
        )
        raise ValueError(
            f"{office_name}: the gateways are managed by several controllers ({detail}) — an office's edge attaches to "
            "one orchestrator: list only gateways of one controller in sdwan_gateways"
        )
    return next(iter(managers.values()), None)


async def ensure_tunnels(
    gen: Any, *, office_name: str, office_id: str, edge_port_id: str, gateways: Sequence[GatewayAttachment]
) -> None:
    """One tunnel (a TopologySessionCircuit of type ``tunnel``) per gateway, both ends in one save, tracked by the run:
    a gateway taken out of ``sdwan_gateways`` loses its tunnel in the group cleanup. No session: add_virtual_circuit
    builds none for it."""
    for gateway in gateways:
        tunnel_name = f"{office_name}-SDWAN-{gateway.name}"
        tunnel = await gen.client.create(
            kind=TopologySessionCircuit,
            data={
                "name": tunnel_name,
                "circuit_type": "tunnel",
                "status": "active",
                "provider": {"id": gateway.provider_id},
                "description": f"{office_name} SD-WAN tunnel to gateway {gateway.name} ({gateway.zone_name})",
                "locations": [{"id": office_id}, {"id": gateway.zone_id}],
                "capabilities": [{"id": gateway.public_port_id}, {"id": edge_port_id}],
                "member_of_groups": [{"hfid": [GROUP_VIRTUAL_CIRCUITS], "kind": "CoreStandardGroup"}],
            },
        )
        await tunnel.save(allow_upsert=True)
        gen.logger.info(f"Ensured SD-WAN tunnel circuit '{tunnel_name}'")


async def find_orchestrator(gen: Any, *, office_name: str, gateways: Sequence[GatewayAttachment]) -> Any | None:
    """The controller of the office's gateways, read before anything is written (None: no controller manages them)."""
    controllers = await gen.client.filters(
        kind=ManagedController, managed_devices__ids=[gateway.id for gateway in gateways], include=["managed_devices"]
    )
    try:
        controller = orchestrator_of(office_name, gateways, controllers)
    except ValueError as error:
        gen.fail(str(error))
    if controller is None:
        gen.logger.warning(f"{office_name}: no controller manages its gateways — the edge will not be group-managed")
    return controller


async def attach_to_orchestrator(gen: Any, controller: Any, edge_id: str) -> None:
    """Add the edge to the managed devices of ``controller`` (one RelationshipAdd of this edge, not a save of the peer
    list: concurrent offices share the controller, and a tracked save would get it deleted by delete_unused_nodes)."""
    if any(peer.id == edge_id for peer in getattr(controller, "managed_devices").peers):
        return
    await controller.add_relationships(relation_to_update="managed_devices", related_nodes=[edge_id])
    gen.logger.info(f"Attached edge to orchestrator '{controller.name.value}'")


async def ensure_office_lan(gen: Any, office: Mapping[str, Any], *, office_name: str, office_id: str) -> None:
    """Give an office with no LAN prefix its one /24 from the PROD tenant pool, once; both writes are untracked.
    (An office with a switch fabric hosts its LAN as a segment.)"""
    if office.get("lan_prefixes"):
        return
    pool_name = tenant_pool_name(_OFFICE_LAN_NAMESPACE)
    pools = await gen.client.filters(kind=CoreIPPrefixPool, name__value=pool_name)
    if not pools:
        gen.fail(f"{office_name}: tenant pool {pool_name} not found - load data/bootstrap/23b_tenant_pools.yml")
    async with gen.resource_lock(f"office-lan-{pool_name}"):
        prefix = await allocation.allocate_prefix(
            gen.client,
            pools[0],
            f"office-lan__{office_name}",
            None,
            member_type="address",
            data={"role": "customer", "status": "active", "description": f"{office_name} LAN"},
        )
        if prefix is None:
            gen.fail(f"{office_name}: tenant pool {pool_name} returned no prefix")
        office_node = await gen.client.create(
            kind=TopologyCustomerOffice, data={"id": office_id, "lan_prefixes": [{"id": prefix.id}]}
        )
        await office_node.save(allow_upsert=True, update_group_context=False)
    gen.logger.info(f"Allocated LAN {prefix.prefix.value} to office {office_name} from {pool_name}")
