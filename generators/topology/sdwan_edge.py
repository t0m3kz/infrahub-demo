"""SD-WAN edge automation for TopologyCustomerOffice.

Triggered on TopologyCustomerOffice creation (trigger rule
trigger-customer-office-sdwan-on-created in data/events/99_actions.yml) and
on a later sdwan_gateway update. Registered as add_sdwan_edge in
.infrahub.yml, targeting the customer_deployments_office group and querying
customer_office_sdwan.gql.

No-ops unless office.sdwan_gateway is set — that relationship is the opt-in
signal (see schemas/extensions/topology/topology_customer.yml), same shape
as design.dedicated_firewall gating dedicated provisioning elsewhere. When
set, this generator provisions the per-office slice of SD-WAN: an Edge
device, the internet-underlay TopologyCarrierCircuit INET-<office> (the
Edge's uplink to a free ``customer`` port of the ISP's access PE) and the
SD-WAN tunnel (a TopologySessionCircuit of type tunnel) <office>-SDWAN-<gateway zone> (owned by the
office's owner, ending on the Gateway's public port and the Edge's uplink) —
then attaches the new Edge to the Gateway's own Orchestrator (VCO) via
managed_devices. It is the only path: no office's Edge, tunnel or underlay is
hand-authored.

The tunnel is an inventory record: the orchestrator owns the overlay routing,
so no session is generated and the tunnel is not a member of the
``virtual_circuits`` generator group. Both ends are written with the circuit in
one save. The Gateway's public port is its one ``customer`` physical interface;
the trunk (role uplink) is not used here.

The underlay's far end is not authored per office. The ISP is the one
TopologyExternalProviders of the internet provider (data/demos/30_all/
04_internet), its one device the access PE; the circuit takes that PE's first
free ``customer`` port (a port that is an end of no carrier circuit), and
reuses its own port on a rerun. The pick and the circuit's single save (a
carrier circuit needs both ends at creation) run under one resource_lock,
because the offices board in one load and run concurrently. The circuit's
locations are the office and the ISP; the tunnel's are the office and the
Gateway's zone.

The office's LAN is allocated here too, when the office declares none: one /24 from
the PROD tenant pool (``Tenant-PROD-IPv4``, data/bootstrap/23b_tenant_pools.yml), keyed by
the office name, attached to its ``lan_prefixes`` (what the border registers behind the
gateway's session, utils/ownership.py). An office that lists a ``lan_prefixes`` (C002's
hand-authored one) or has ``fabric_templates`` (it hosts its segments instead, registered
behind the gateway) is left alone, and a rerun allocates and writes nothing.

The shared Gateway/VCO themselves stay hand-authored and load with the
foundation stage (data/demos/30_all/02_colo/equinix/fr/04_sdwan_gateway.yml),
so an office can opt in when it is created — this generator only ever reads
them, mirroring how CustomerDeploymentColocationExchangeGenerator
(generators/topology/customer_colocation.py) assumes the shared colocation fabric
already exists and only handles the per-tenant slice.

Deliberately does NOT reuse DeviceMixin.create_devices(): it unconditionally
resolves a "{fabric_name}-management-pool" (generators/devices.py) that no
office has, and is tailored to DC/pod-scale fabric devices; a branch-office
Edge is simple enough that a direct create+upsert is less code and less risk
than bending those assumptions.

Failures raise: a missing prerequisite is logged with self.logger.error and
raised as GeneratorError (CommonGenerator.fail), and an SDK error propagates as is,
so a run that could not provision the office's slice is a failed task, never a
green one that silently stopped half way.
"""

from __future__ import annotations

from typing import Any

from infrahub_sdk.protocols import CoreIPPrefixPool
from netutils.interface import sort_interface_list

from utils.data_cleaning import clean_data
from utils.limits import GROUP_CARRIER_CIRCUITS, GROUP_VIRTUAL_CIRCUITS

from ..common import CommonGenerator
from ..helpers.naming import sdwan_edge_name
from ..pools import PoolMixin
from ..protocols import (
    DcimPhysicalDevice,
    DcimPhysicalInterface,
    IpamPrefix,
    ManagedController,
    TopologyCarrierCircuit,
    TopologyCustomerOffice,
    TopologyExternalProviders,
    TopologySessionCircuit,
)
from .segment import tenant_pool_name

# Who sells each office its internet access. Not modelled per office (no
# "chosen ISP" signal on TopologyCustomerOffice), so every office's underlay
# gets the same provider: Orange S.A. Its TopologyExternalProviders (the ISP
# location, with the access PE as its one device) is the underlay's far end,
# resolved from this org. A per-office knob is added only when a transform or
# check reads it.
_INTERNET_PROVIDER_ORG_ID = "P019"
# Who operates the overlay riding that underlay: the SD-WAN service provider
# (VMware VeloCloud), never the ISP — the tunnel is not bought from the ISP.
_OVERLAY_PROVIDER_ORG_ID = "P035"
_EDGE_TEMPLATE_NAME = "VCE-610_EDGE"
# An office is always PROD (no environment): its LAN comes from the PROD tenant pool.
_OFFICE_LAN_NAMESPACE = "PROD"


def _target_group(name: str) -> dict[str, Any]:
    """A circuit generator target group (data/bootstrap/00_groups.yml), by HFID.

    Membership is what lets add_circuit run for the INET circuits this
    generator creates, as for the data-declared ones.
    """
    return {"hfid": [name], "kind": "CoreStandardGroup"}


class SdwanEdgeGenerator(PoolMixin, CommonGenerator):
    """add_sdwan_edge — per-office SD-WAN Edge, internet underlay circuit and tunnel circuit provisioning."""

    # Widens the type for the type checker only (mirrors generators/devices.py's
    # DeviceMixin.client: Any) — the real InfrahubGenerator.client property
    # returns InfrahubClient, which unit tests replace with a MagicMock; without
    # this, ty resolves .create/.filters/.get back to InfrahubClient's own
    # overloaded signatures and flags every mock assertion as invalid.
    client: Any

    async def generate(self, data: dict[str, Any]) -> None:
        cleaned = clean_data(data)

        entries = cleaned.get("TopologyCustomerOffice", [])
        if not entries:
            self.logger.info("No TopologyCustomerOffice data in GraphQL response — not this generator's kind")
            return
        office = entries[0]

        office_id: str = office.get("id", "")
        office_name: str = office.get("name", office_id)
        if not office_id:
            self.fail("Office missing id — cannot proceed")

        gateway = office.get("sdwan_gateway")
        if not gateway or not gateway.get("id"):
            self.logger.info(f"{office_name}: no sdwan_gateway set — SD-WAN edge provisioning not requested")
            return

        owner_id: str | None = (office.get("owner") or {}).get("id")
        if not owner_id:
            self.fail(f"{office_name}: office has no owner — cannot proceed")

        self.logger.info(f"Processing SD-WAN edge for office {office_name}")

        gateway_id: str = gateway["id"]
        gateway_name: str = gateway.get("name", gateway_id)
        gateway_zone = gateway.get("deployment") or {}
        gateway_zone_id: str | None = gateway_zone.get("id")
        gateway_zone_name: str | None = gateway_zone.get("name")
        if not gateway_zone_id or not gateway_zone_name:
            self.fail(f"{office_name}: gateway {gateway_name} has no deployment — cannot set circuit locations")

        # The public port: the gateway's one physical customer-role interface.
        public_ports = sorted(
            (
                iface
                for iface in gateway.get("interfaces") or []
                if iface.get("typename") == DcimPhysicalInterface.__name__ and iface.get("role") == "customer"
            ),
            key=lambda iface: iface.get("name") or "",
        )
        if not public_ports:
            self.fail(f"{office_name}: gateway {gateway_name} has no role=customer public interface")
        if len(public_ports) > 1:
            names = [iface.get("name") for iface in public_ports]
            self.fail(f"{office_name}: gateway {gateway_name} has several role=customer public interfaces {names}")
        gateway_public_port_id: str = public_ports[0]["id"]

        internet_provider_id, overlay_provider_id = await self._resolve_provider_ids(office_name)
        edge = await self._ensure_edge_device(office_name, office_id, owner_id)
        edge_uplink = await self._find_edge_uplink(edge.id, office_name)
        isp_location_id, isp_device_id = await self._resolve_isp_access(office_name, internet_provider_id)

        await self._ensure_internet_circuit(
            office_name=office_name,
            office_id=office_id,
            owner_id=owner_id,
            provider_id=internet_provider_id,
            edge_uplink_id=edge_uplink.id,
            isp_location_id=isp_location_id,
            isp_device_id=isp_device_id,
        )
        await self._ensure_tunnel_circuit(
            office_name=office_name,
            office_id=office_id,
            owner_id=owner_id,
            provider_id=overlay_provider_id,
            gateway_zone_id=gateway_zone_id,
            gateway_zone_name=gateway_zone_name,
            end_ids=[gateway_public_port_id, edge_uplink.id],
        )
        await self._attach_to_orchestrator(gateway_id=gateway_id, gateway_name=gateway_name, edge_id=edge.id)
        await self._ensure_office_lan(office, office_name=office_name, office_id=office_id)

    async def _resolve_provider_ids(self, office_name: str) -> tuple[str, str]:
        """(internet provider id, overlay provider id), both looked up in one query."""
        org_ids = [_INTERNET_PROVIDER_ORG_ID, _OVERLAY_PROVIDER_ORG_ID]
        providers = await self.client.filters(kind="OrganizationProvider", org_id__values=org_ids)
        by_org_id = {provider.org_id.value: provider.id for provider in providers}
        missing = [org_id for org_id in org_ids if org_id not in by_org_id]
        if missing:
            self.fail(f"{office_name}: provider org(s) {missing} not found")
        return by_org_id[_INTERNET_PROVIDER_ORG_ID], by_org_id[_OVERLAY_PROVIDER_ORG_ID]

    async def _ensure_edge_device(self, office_name: str, office_id: str, owner_id: str) -> Any:
        """Create/upsert the Edge, mirroring generators/devices.py's
        create_devices() convention: object_template is only sent on first
        creation (resending it on an existing device triggers a server-side
        re-instantiation that fails with "device is mandatory for
        DcimPhysicalInterface") — an existing device is instead upserted by
        id, never re-sent its template.
        """
        edge_name = sdwan_edge_name(office_name)
        existing = await self.client.filters(kind=DcimPhysicalDevice, name__values=[edge_name])

        data: dict[str, Any] = {
            "name": edge_name,
            "status": "active",
            "owner": {"id": owner_id},
            "deployment": {"id": office_id},
            "description": f"{office_name} SD-WAN branch edge",
        }
        if existing:
            data["id"] = existing[0].id
        else:
            templates = await self.client.filters(
                kind="TemplateDcimPhysicalDevice", template_name__value=_EDGE_TEMPLATE_NAME
            )
            if not templates:
                self.fail(f"Template '{_EDGE_TEMPLATE_NAME}' not found — cannot create Edge device")
            data["object_template"] = {"id": templates[0].id}

        edge = await self.client.create(kind=DcimPhysicalDevice, data=data)
        await edge.save(allow_upsert=True)
        self.logger.info(f"Ensured Edge device '{edge_name}'")
        return edge

    async def _find_edge_uplink(self, edge_id: str, office_name: str) -> Any:
        ifaces = await self.client.filters(kind=DcimPhysicalInterface, device__ids=[edge_id], role__value="uplink")
        if not ifaces:
            self.fail(f"{office_name}: edge device has no role=uplink interface")
        return sorted(ifaces, key=lambda i: i.name.value)[0]

    async def _resolve_isp_access(self, office_name: str, internet_provider_id: str) -> tuple[str, str]:
        """(ISP location id, access PE device id): the internet provider's one topology and its one device."""
        isps = await self.client.filters(
            kind=TopologyExternalProviders, provider__ids=[internet_provider_id], include=["devices"]
        )
        if len(isps) != 1:
            self.fail(f"{office_name}: expected exactly 1 ISP topology for the internet provider, found {len(isps)}")
        isp = isps[0]
        device_ids = [peer.id for peer in getattr(isp, "devices").peers]
        if len(device_ids) != 1:
            self.fail(f"{office_name}: ISP {isp.name.value} must host exactly 1 access device, found {len(device_ids)}")
        return isp.id, device_ids[0]

    async def _ensure_internet_circuit(
        self,
        *,
        office_name: str,
        office_id: str,
        owner_id: str,
        provider_id: str,
        edge_uplink_id: str,
        isp_location_id: str,
        isp_device_id: str,
    ) -> None:
        """Create/upsert INET-<office> from the Edge uplink to the ISP access PE's port, in one save.

        A carrier circuit needs both ends when it is written, so the port is
        picked and the circuit saved inside one lock: two offices running at
        once would otherwise both see the same port free.
        """
        circuit_name = f"INET-{office_name}"
        async with self.resource_lock(f"isp-access-port-{isp_device_id}"):
            isp_port_id = await self._pick_isp_port(office_name, circuit_name, isp_device_id)
            circuit = await self.client.create(
                kind=TopologyCarrierCircuit,
                data={
                    "circuit_id": circuit_name,
                    "name": circuit_name,
                    "circuit_type": "internet",
                    "status": "active",
                    "provider": {"id": provider_id},
                    "owner": {"id": owner_id},
                    "description": f"{office_name} internet access (SD-WAN underlay)",
                    # locations peers TopologyFacility, and both the office itself
                    # (not the edge device) and the ISP the far end sits in are facilities.
                    "locations": [{"id": office_id}, {"id": isp_location_id}],
                    "capabilities": [{"id": edge_uplink_id}, {"id": isp_port_id}],
                    "member_of_groups": [_target_group(GROUP_CARRIER_CIRCUITS)],
                },
            )
            await circuit.save(allow_upsert=True)
        self.logger.info(f"Ensured internet-underlay circuit '{circuit_name}'")

    async def _pick_isp_port(self, office_name: str, circuit_name: str, isp_device_id: str) -> str:
        """The access PE port for this circuit: the one it already ends on, else the first free ``customer`` port.

        Free = no carrier circuit among the port's capabilities
        (a virtual circuit's tunnel port may be shared, a physical end may
        not). Fails, before anything is written, when none is left. Call
        inside the isp-access-port lock.
        """
        circuits = await self.client.filters(
            kind=TopologyCarrierCircuit, name__value=circuit_name, include=["capabilities"]
        )
        own_ends = {peer.id for circuit in circuits for peer in getattr(circuit, "capabilities").peers}
        ports = await self.client.filters(
            kind=DcimPhysicalInterface,
            device__ids=[isp_device_id],
            role__value="customer",
            include=["capabilities"],
        )
        for port in ports:
            if port.id in own_ends:
                return port.id
        free = {
            port.name.value: port.id
            for port in ports
            if not any(peer.typename == TopologyCarrierCircuit.__name__ for peer in getattr(port, "capabilities").peers)
        }
        if not free:
            self.fail(f"{office_name}: the ISP access device has no free customer port for {circuit_name}")
        return free[sort_interface_list(list(free))[0]]

    async def _ensure_tunnel_circuit(
        self,
        *,
        office_name: str,
        office_id: str,
        owner_id: str,
        provider_id: str,
        gateway_zone_id: str,
        gateway_zone_name: str,
        end_ids: list[str],
    ) -> None:
        """Create/upsert the office's tunnel (a TopologySessionCircuit of type ``tunnel``), both ends in one save.

        Ends: the gateway's public port and the edge uplink. An upsert replaces the
        cardinality-many ``capabilities``, so a rerun over unchanged data resends the
        same two ends. A member of the ``virtual_circuits`` group like every session circuit (its created-trigger
        needs it); the orchestrator owns the overlay routing, so add_virtual_circuit builds no session for it.
        """
        tunnel_name = f"{office_name}-SDWAN-{gateway_zone_name}"
        tunnel = await self.client.create(
            kind=TopologySessionCircuit,
            data={
                "name": tunnel_name,
                "circuit_type": "tunnel",
                "status": "active",
                "provider": {"id": provider_id},
                "owner": {"id": owner_id},
                "description": f"{office_name} SD-WAN tunnel to {gateway_zone_name} gateway",
                "locations": [{"id": office_id}, {"id": gateway_zone_id}],
                "capabilities": [{"id": end_id} for end_id in end_ids],
                "member_of_groups": [_target_group(GROUP_VIRTUAL_CIRCUITS)],
            },
        )
        await tunnel.save(allow_upsert=True)
        self.logger.info(f"Ensured SD-WAN tunnel circuit '{tunnel_name}'")

    async def _ensure_office_lan(self, office: dict[str, Any], *, office_name: str, office_id: str) -> None:
        """Give an office with no LAN and no fabric its one /24 from the PROD tenant pool, once.

        An office that lists ``lan_prefixes`` keeps them and one with ``fabric_templates`` registers
        through the segments it hosts, so neither is allocated a LAN (nothing is read or written
        for them). The prefix is allocated with ``office-lan__<office>`` as identifier: the office
        name survives a delete and recreate, and an allocation retried after a failed attach returns
        the same prefix. The pool and the attach run under one lock keyed by the pool, the way
        segment addressing serializes its allocations.

        Both writes are untracked: the prefix is a pool mutation, and the office (this run's own
        target) is saved with ``update_group_context=False``, so no later run's cleanup deletes
        either. Only ``lan_prefixes`` is sent, so no trigger rule of the office fires.
        """
        if office.get("lan_prefixes") or office.get("fabric_templates"):
            return
        pool_name = tenant_pool_name(_OFFICE_LAN_NAMESPACE)
        pools = await self.client.filters(kind=CoreIPPrefixPool, name__value=pool_name)
        if not pools:
            self.fail(f"{office_name}: tenant pool {pool_name} not found - load data/bootstrap/23b_tenant_pools.yml")
        async with self.resource_lock(f"office-lan-{pool_name}"):
            prefix = await self.client.allocate_next_ip_prefix(
                resource_pool=pools[0],
                kind=IpamPrefix,
                identifier=f"office-lan__{office_name}",
                member_type="address",
                data={"role": "customer", "status": "active", "description": f"{office_name} LAN"},
            )
            if prefix is None:
                self.fail(f"{office_name}: tenant pool {pool_name} returned no prefix")
            office_node = await self.client.create(
                kind=TopologyCustomerOffice, data={"id": office_id, "lan_prefixes": [{"id": prefix.id}]}
            )
            await office_node.save(allow_upsert=True, update_group_context=False)
        self.logger.info(f"Allocated LAN {prefix.prefix.value} to office {office_name} from {pool_name}")

    async def _attach_to_orchestrator(self, *, gateway_id: str, gateway_name: str, edge_id: str) -> None:
        orchestrators = await self.client.filters(
            kind=ManagedController, managed_devices__ids=[gateway_id], include=["managed_devices"]
        )
        if not orchestrators:
            self.logger.warning(f"No orchestrator manages {gateway_name} — new edge will not be group-managed")
            return

        orchestrator = orchestrators[0]
        if any(peer.id == edge_id for peer in getattr(orchestrator, "managed_devices").peers):
            return
        # A RelationshipAdd of this one edge, not a save of the whole peer
        # list: every office's run attaches its edge to the same shared
        # orchestrator, and the offices board in one load, so a save would let
        # concurrent runs overwrite each other's edge. It also never claims the
        # hand-authored orchestrator for this run's group (a tracked save would
        # get it deleted by delete_unused_nodes on the next run).
        await orchestrator.add_relationships(relation_to_update="managed_devices", related_nodes=[edge_id])
        self.logger.info(f"Attached edge to orchestrator '{orchestrator.name.value}'")
