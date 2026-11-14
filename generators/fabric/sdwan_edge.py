"""SD-WAN edge automation for TopologyCustomerOffice (add_sdwan_edge); no-op unless office.sdwan_gateway is set.
Failures raise. Provisions the Edge, the INET-<office> carrier circuit (to a free customer port of the ISP's access PE),
the SD-WAN tunnel and the Edge's attachment to the Orchestrator. The shared Gateway/VCO are only read; an office that
declares no LAN gets a /24 from Tenant-PROD-IPv4.
"""

from __future__ import annotations

import re
from typing import Any

from infrahub_sdk.protocols import CoreIPPrefixPool
from netutils.interface import sort_interface_list

from generators.helpers.naming import sdwan_edge_name
from generators.mixins.common import CommonGenerator
from generators.mixins.pools import PoolMixin
from generators.plan import DeviceSpec, TopologyPlan
from generators.protocols import (
    DcimPhysicalDevice,
    DcimPhysicalInterface,
    IpamPrefix,
    ManagedController,
    TopologyCarrierCircuit,
    TopologyCustomerOffice,
    TopologySessionCircuit,
)
from utils.data_cleaning import clean_data
from utils.limits import GROUP_CARRIER_CIRCUITS, GROUP_VIRTUAL_CIRCUITS
from utils.segment_addressing import tenant_pool_name

# Every office's internet underlay gets the same ISP (Orange S.A.): there is no per-office signal.
_INTERNET_PROVIDER_ORG_ID = "P019"
# The overlay operator is the SD-WAN provider (VMware VeloCloud), never the ISP.
_OVERLAY_PROVIDER_ORG_ID = "P035"
_EDGE_TEMPLATE_NAME = "VCE-610_EDGE"
# An office is always PROD (no environment): its LAN comes from the PROD tenant pool.
_OFFICE_LAN_NAMESPACE = "PROD"


def _target_group(name: str) -> dict[str, Any]:
    """A circuit generator target group (data/bootstrap/00_groups.yml), by HFID."""
    return {"hfid": [name], "kind": "CoreStandardGroup"}


class SdwanEdgeGenerator(PoolMixin, CommonGenerator):
    """add_sdwan_edge — per-office SD-WAN Edge, internet underlay circuit and tunnel circuit provisioning."""

    # Widens the type for the type checker only; tests replace the client with a MagicMock.
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

        internet_provider_id, isp_name, overlay_provider_id = await self._resolve_provider_ids(office_name)
        plan = self.build_plan(office_name=office_name, office_id=office_id, owner_id=owner_id)
        reconciled = await self.apply_plan(plan)
        edge_name = sdwan_edge_name(office_name)
        edge_id = reconciled.device_ids[edge_name]
        uplink_candidates = sorted(
            iface_name
            for (dev_name, iface_name), role in reconciled.interface_roles.items()
            if dev_name == edge_name and role == "uplink"
        )
        if not uplink_candidates:
            self.fail(f"{office_name}: edge device has no role=uplink interface")
        edge_uplink_id = reconciled.interface_ids[(edge_name, uplink_candidates[0])]
        isp_device_id = await self._resolve_isp_access(office_name, internet_provider_id)

        await self._ensure_internet_circuit(
            office_name=office_name,
            office_id=office_id,
            provider_id=internet_provider_id,
            edge_uplink_id=edge_uplink_id,
            isp_name=isp_name,
            isp_device_id=isp_device_id,
        )
        assert gateway_zone_id is not None and gateway_zone_name is not None
        await self._ensure_tunnel_circuit(
            office_name=office_name,
            office_id=office_id,
            provider_id=overlay_provider_id,
            gateway_zone_id=gateway_zone_id,
            gateway_zone_name=gateway_zone_name,
            end_ids=[gateway_public_port_id, edge_uplink_id],
        )
        await self._attach_to_orchestrator(gateway_id=gateway_id, gateway_name=gateway_name, edge_id=edge_id)
        await self._ensure_office_lan(office, office_name=office_name, office_id=office_id)

    async def _resolve_provider_ids(self, office_name: str) -> tuple[str, str, str]:
        """(internet provider id, its circuit-name slug, overlay provider id), all looked up in one query."""
        org_ids = [_INTERNET_PROVIDER_ORG_ID, _OVERLAY_PROVIDER_ORG_ID]
        providers = await self.client.filters(kind="OrganizationProvider", org_id__values=org_ids)
        by_org_id = {provider.org_id.value: provider for provider in providers}
        missing = [org_id for org_id in org_ids if org_id not in by_org_id]
        if missing:
            self.fail(f"{office_name}: provider org(s) {missing} not found")
        isp = by_org_id[_INTERNET_PROVIDER_ORG_ID]
        isp_slug = re.sub(r"[^a-z0-9]+", "-", isp.name.value.lower()).strip("-")
        return isp.id, isp_slug, by_org_id[_OVERLAY_PROVIDER_ORG_ID].id

    @staticmethod
    def build_plan(*, office_name: str, office_id: str, owner_id: str) -> TopologyPlan:
        """Declarative device plan for an office SD-WAN Edge."""
        plan = TopologyPlan()
        plan.add_device(
            DeviceSpec(
                name=sdwan_edge_name(office_name),
                kind=DcimPhysicalDevice,
                template_name=_EDGE_TEMPLATE_NAME,
                discover_interface_kind=DcimPhysicalInterface,
                discover_interface_role="uplink",
                owner_id=owner_id,
                deployment_id=office_id,
                description=f"{office_name} SD-WAN branch edge",
            )
        )
        return plan

    async def _resolve_isp_access(self, office_name: str, internet_provider_id: str) -> str:
        """The access PE device id: the one device the internet provider owns."""
        devices = await self.client.filters(kind=DcimPhysicalDevice, owner__ids=[internet_provider_id])
        if len(devices) != 1:
            self.fail(f"{office_name}: the internet provider must own exactly 1 access device, found {len(devices)}")
        return devices[0].id

    async def _ensure_internet_circuit(
        self,
        *,
        office_name: str,
        office_id: str,
        provider_id: str,
        edge_uplink_id: str,
        isp_name: str,
        isp_device_id: str,
    ) -> None:
        """Create/upsert INET-<office> from the Edge uplink to the ISP access PE's port, in one save; the port is picked
        under one lock.
        """
        circuit_id = f"INET-{office_name}"
        circuit_name = f"{office_name}-inet-{isp_name}".lower()
        async with self.resource_lock(f"isp-access-port-{isp_device_id}"):
            isp_port_id = await self._pick_isp_port(office_name, circuit_id, isp_device_id)
            circuit = await self.client.create(
                kind=TopologyCarrierCircuit,
                data={
                    "circuit_id": circuit_id,
                    "name": circuit_name,
                    "circuit_type": "internet",
                    "status": "active",
                    "provider": {"id": provider_id},
                    "description": f"{office_name} internet access (SD-WAN underlay)",
                    # locations: the office itself (not the edge device); the ISP's router has no place.
                    "locations": [{"id": office_id}],
                    "capabilities": [{"id": edge_uplink_id}, {"id": isp_port_id}],
                    "member_of_groups": [_target_group(GROUP_CARRIER_CIRCUITS)],
                },
            )
            await circuit.save(allow_upsert=True)
        self.logger.info(f"Ensured internet-underlay circuit '{circuit_name}'")

    async def _pick_isp_port(self, office_name: str, circuit_id: str, isp_device_id: str) -> str:
        """The access PE port for this circuit: the one it ends on, else the first free ``customer`` port; fails before
        any write when none is free.
        """
        circuits = await self.client.filters(
            kind=TopologyCarrierCircuit, circuit_id__value=circuit_id, include=["capabilities"]
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
            self.fail(f"{office_name}: the ISP access device has no free customer port for {circuit_id}")
        return free[sort_interface_list(list(free))[0]]

    async def _ensure_tunnel_circuit(
        self,
        *,
        office_name: str,
        office_id: str,
        provider_id: str,
        gateway_zone_id: str,
        gateway_zone_name: str,
        end_ids: list[str],
    ) -> None:
        """Create/upsert the office's tunnel (a TopologySessionCircuit of type ``tunnel``), both ends in one save;
        add_virtual_circuit builds no session for it.
        """
        tunnel_name = f"{office_name}-SDWAN-{gateway_zone_name}"
        tunnel = await self.client.create(
            kind=TopologySessionCircuit,
            data={
                "name": tunnel_name,
                "circuit_type": "tunnel",
                "status": "active",
                "provider": {"id": provider_id},
                "description": f"{office_name} SD-WAN tunnel to {gateway_zone_name} gateway",
                "locations": [{"id": office_id}, {"id": gateway_zone_id}],
                "capabilities": [{"id": end_id} for end_id in end_ids],
                "member_of_groups": [_target_group(GROUP_VIRTUAL_CIRCUITS)],
            },
        )
        await tunnel.save(allow_upsert=True)
        self.logger.info(f"Ensured SD-WAN tunnel circuit '{tunnel_name}'")

    async def _ensure_office_lan(self, office: dict[str, Any], *, office_name: str, office_id: str) -> None:
        """Give an office with no LAN and no fabric its one /24 from the PROD tenant pool, once; both writes are
        untracked.
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
                data={
                    "role": "customer",
                    "status": "active",
                    "description": f"{office_name} LAN",
                },
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
        # RelationshipAdd of this one edge, not a save of the peer list: concurrent offices share the orchestrator, and
        # a tracked save would get it deleted by delete_unused_nodes.
        await orchestrator.add_relationships(relation_to_update="managed_devices", related_nodes=[edge_id])
        self.logger.info(f"Attached edge to orchestrator '{orchestrator.name.value}'")
