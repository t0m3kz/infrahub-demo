"""SD-WAN edge automation for TopologyCustomerOffice.

Triggered on TopologyCustomerOffice creation (new trigger rule
trigger-customer-office-sdwan-on-created in data/events/99_actions.yml).
Registered as add_sdwan_edge in .infrahub.yml, targeting the
customer_offices_sdwan group and querying customer_office_sdwan.gql.

No-ops unless office.sdwan_gateway is set — that relationship is the opt-in
signal (see schemas/extensions/topology/topology_customer.yml), same shape
as design.dedicated_firewall gating dedicated provisioning elsewhere. When
set, this generator provisions the per-office slice of SD-WAN: an Edge
device, the SD-WAN overlay TopologyVirtualCircuit (with a pool-allocated
tunnel_id), the Gateway's matching tunnel sub-interface, and an
internet-underlay circuit — then attaches the new Edge to the Gateway's own
Orchestrator (VCO) via managed_devices.

The shared Gateway/VCO themselves stay hand-authored
(data/demos/30_all/08_interconnects/04_sdwan/01_gateway.yml) — this
generator only ever reads them, mirroring how
CustomerDeploymentColocationExchangeGenerator (generators/topology/
customer_colocation.py) assumes the shared metro fabric already exists and
only handles the per-tenant slice.

Deliberately does NOT reuse DeviceMixin.create_devices()/CablingMixin.
ensure_vlan_subinterface(): create_devices() unconditionally resolves a
"{fabric_name}-management-pool" (generators/devices.py) that no office has,
and ensure_vlan_subinterface() hardcodes role="service" for its
sub-interface (generators/connections.py), while every existing hand-authored
SD-WAN sub-interface uses role="uplink" (data/demos/30_all/08_interconnects/
04_sdwan/02_gateway_interfaces.yml). Both helpers are tailored to DC/pod-scale
fabric devices; a branch-office Edge is simple enough that a direct
create+upsert is less code and less risk than bending those assumptions.

Ordering note: the Gateway's tunnel sub-interface is named "<trunk>.<tunnel_id>"
(matching the hand-authored convention), so the overlay circuit is created
FIRST (allocating tunnel_id from GLOBAL-SDWAN-TUNNEL-ID via a from_pool dict
on the attribute) and its resolved value read back, THEN the sub-interface
is created, THEN the circuit's interface_capabilities is updated to include
it — there is no standalone "allocate a number" call for a CoreNumberPool,
only from_pool applied to a real node's attribute.
"""

from __future__ import annotations

from typing import Any

from utils.data_cleaning import clean_data

from ..common import CommonGenerator
from ..protocols import (
    DcimPhysicalDevice,
    DcimPhysicalInterface,
    DcimVirtualInterface,
    ManagedController,
    TopologyPhysicalCircuit,
    TopologyVirtualCircuit,
)

# Cross-connect/ISP details aren't modeled per-office anywhere yet (no
# "chosen ISP" signal on TopologyCustomerOffice) — every generator-provisioned
# office gets the same default internet provider. Orange S.A (org_id P019)
# already provides two of the three hand-authored offices' internet underlay
# (data/demos/30_all/08_interconnects/04_sdwan/04_internet_underlay.yml).
# org_id, not id — resolved to a real id via _resolve_provider_id() before
# use, the same way generators/topology/interconnect.py never passes a bare
# HFID string into a relationship field, only ever a resolved {"id": ...}.
_DEFAULT_INTERNET_PROVIDER_ORG_ID = "P019"
_TUNNEL_ID_POOL_NAME = "GLOBAL-SDWAN-TUNNEL-ID"


class SdwanEdgeGenerator(CommonGenerator):
    """add_sdwan_edge — per-office SD-WAN Edge, tunnel, and circuit provisioning."""

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
            self.logger.error("Office missing id — cannot proceed")
            return

        gateway = office.get("sdwan_gateway")
        if not gateway or not gateway.get("id"):
            self.logger.info(f"{office_name}: no sdwan_gateway set — SD-WAN edge provisioning not requested")
            return

        owner_org = (office.get("parent") or {}).get("owner") or {}
        owner_id: str | None = owner_org.get("id")
        if not owner_id:
            self.logger.error(f"{office_name}: no owner resolved via parent — cannot proceed")
            return

        self.logger.info(f"Processing SD-WAN edge for office {office_name}")

        gateway_id: str = gateway["id"]
        gateway_name: str = gateway.get("name", gateway_id)
        gateway_zone = gateway.get("deployment") or {}
        gateway_zone_id: str | None = gateway_zone.get("id")
        gateway_zone_name: str | None = gateway_zone.get("name")
        if not gateway_zone_id or not gateway_zone_name:
            self.logger.error(f"{office_name}: gateway {gateway_name} has no deployment — cannot set circuit locations")
            return

        trunk_iface = next(
            (iface for iface in gateway.get("interfaces") or [] if iface.get("role") == "uplink"),
            None,
        )
        if trunk_iface is None:
            self.logger.error(f"{office_name}: gateway {gateway_name} has no role=uplink trunk interface")
            return

        provider_id = await self._resolve_provider_id(office_name)
        if provider_id is None:
            return

        edge = await self._ensure_edge_device(office_name, office_id, owner_id)
        if edge is None:
            return

        edge_uplink = await self._find_edge_uplink(edge.id, office_name)
        if edge_uplink is None:
            return

        internet_circuit = await self._ensure_internet_circuit(
            office_name=office_name,
            office_id=office_id,
            owner_id=owner_id,
            provider_id=provider_id,
            edge_uplink_id=edge_uplink.id,
            gateway_zone_id=gateway_zone_id,
        )
        if internet_circuit is None:
            return

        overlay_circuit = await self._ensure_overlay_circuit(
            office_name=office_name,
            office_id=office_id,
            owner_id=owner_id,
            provider_id=provider_id,
            gateway_zone_id=gateway_zone_id,
            gateway_zone_name=gateway_zone_name,
            internet_circuit_id=internet_circuit.id,
            edge_uplink_id=edge_uplink.id,
        )
        if overlay_circuit is None:
            return

        tunnel_id_attr = getattr(overlay_circuit, "tunnel_id", None)
        tunnel_id = getattr(tunnel_id_attr, "value", None) if tunnel_id_attr else None
        if tunnel_id is None:
            self.logger.error(f"{office_name}: overlay circuit has no resolved tunnel_id after save")
            return

        gateway_sub_iface = await self._ensure_gateway_subinterface(
            gateway_id=gateway_id,
            gateway_name=gateway_name,
            trunk_iface_id=trunk_iface["id"],
            trunk_iface_name=trunk_iface.get("name", "eth0"),
            tunnel_id_value=int(tunnel_id),
        )
        if gateway_sub_iface is None:
            return

        await self._link_overlay_to_gateway_subinterface(overlay_circuit, gateway_sub_iface.id, office_name)
        await self._attach_to_orchestrator(gateway_id=gateway_id, gateway_name=gateway_name, edge_id=edge.id)

    async def _resolve_provider_id(self, office_name: str) -> str | None:
        try:
            providers = await self.client.filters(
                kind="OrganizationProvider", org_id__value=_DEFAULT_INTERNET_PROVIDER_ORG_ID
            )
        except Exception as exc:
            self.logger.error(
                f"{office_name}: error looking up provider org '{_DEFAULT_INTERNET_PROVIDER_ORG_ID}': {exc}"
            )
            return None
        if not providers:
            self.logger.error(f"{office_name}: provider org '{_DEFAULT_INTERNET_PROVIDER_ORG_ID}' not found")
            return None
        return providers[0].id

    async def _ensure_edge_device(self, office_name: str, office_id: str, owner_id: str) -> Any | None:
        """Create/upsert the Edge, mirroring generators/devices.py's
        create_devices() convention: object_template is only sent on first
        creation (resending it on an existing device triggers a server-side
        re-instantiation that fails with "device is mandatory for
        DcimPhysicalInterface") — an existing device is instead upserted by
        id, never re-sent its template.
        """
        edge_name = f"{office_name}-EDGE1"
        try:
            existing = await self.client.filters(kind=DcimPhysicalDevice, name__values=[edge_name])
        except Exception as exc:
            self.logger.error(f"Error looking up existing Edge device '{edge_name}': {exc}")
            return None

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
            try:
                templates = await self.client.filters(
                    kind="TemplateDcimPhysicalDevice", template_name__value="VCE-610_EDGE"
                )
            except Exception as exc:
                self.logger.error(f"Error looking up VCE-610_EDGE template: {exc}")
                return None
            if not templates:
                self.logger.error("Template 'VCE-610_EDGE' not found — cannot create Edge device")
                return None
            data["object_template"] = {"id": templates[0].id}

        try:
            edge = await self.client.create(kind=DcimPhysicalDevice, data=data)
            await edge.save(allow_upsert=True)
            self.logger.info(f"Ensured Edge device '{edge_name}'")
            return edge
        except Exception as exc:
            self.logger.error(f"Failed to create Edge device '{edge_name}': {exc}")
            return None

    async def _find_edge_uplink(self, edge_id: str, office_name: str) -> Any | None:
        try:
            ifaces = await self.client.filters(kind=DcimPhysicalInterface, device__ids=[edge_id], role__value="uplink")
        except Exception as exc:
            self.logger.error(f"{office_name}: error looking up edge uplink interface: {exc}")
            return None
        if not ifaces:
            self.logger.error(f"{office_name}: edge device has no role=uplink interface")
            return None
        return sorted(ifaces, key=lambda i: i.name.value)[0]

    async def _ensure_internet_circuit(
        self,
        *,
        office_name: str,
        office_id: str,
        owner_id: str,
        provider_id: str,
        edge_uplink_id: str,
        gateway_zone_id: str,
    ) -> Any | None:
        circuit_name = f"INET-{office_name}"
        try:
            circuit = await self.client.create(
                kind=TopologyPhysicalCircuit,
                data={
                    "circuit_id": circuit_name,
                    "name": circuit_name,
                    "circuit_type": "internet",
                    "status": "active",
                    "provider": {"id": provider_id},
                    "owner": {"id": owner_id},
                    "description": f"{office_name} internet access (SD-WAN underlay)",
                    # locations peers TopologyConnectableLocation — the office
                    # itself (not the edge device), matching the hand-authored
                    # convention in 04_internet_underlay.yml (locations:
                    # ["C001-P", "FR2"], the office's own computed name).
                    "locations": [{"id": office_id}, {"id": gateway_zone_id}],
                    "customer_interfaces": [{"id": edge_uplink_id}],
                },
            )
            await circuit.save(allow_upsert=True)
            self.logger.info(f"Ensured internet-underlay circuit '{circuit_name}'")
            return circuit
        except Exception as exc:
            self.logger.error(f"Failed to create internet-underlay circuit '{circuit_name}': {exc}")
            return None

    async def _ensure_overlay_circuit(
        self,
        *,
        office_name: str,
        office_id: str,
        owner_id: str,
        provider_id: str,
        gateway_zone_id: str,
        gateway_zone_name: str,
        internet_circuit_id: str,
        edge_uplink_id: str,
    ) -> Any | None:
        """Create/upsert the overlay VC with only the edge-side interface
        capability set — the gateway sub-interface doesn't exist yet (its
        name depends on the tunnel_id this same call allocates), so it's
        linked in a follow-up save by _link_overlay_to_gateway_subinterface.
        """
        vc_name = f"{office_name}-SDWAN-{gateway_zone_name}"
        try:
            pool = await self.client.get(kind="CoreNumberPool", name__value=_TUNNEL_ID_POOL_NAME)
        except Exception as exc:
            self.logger.error(f"Could not find pool '{_TUNNEL_ID_POOL_NAME}': {exc}")
            return None
        try:
            circuit = await self.client.create(
                kind=TopologyVirtualCircuit,
                data={
                    "name": vc_name,
                    "link_type": "sd_wan",
                    "transport_mode": "internet_backed",
                    "status": "active",
                    "encryption": True,
                    "tunnel_id": {"from_pool": {"id": pool.id}, "identifier": f"{office_name}-sdwan-tunnel_id"},
                    "provider": {"id": provider_id},
                    "owner": {"id": owner_id},
                    "description": f"{office_name} SD-WAN tunnel to {gateway_zone_name} gateway",
                    "locations": [{"id": office_id}, {"id": gateway_zone_id}],
                    "physical_circuits": [{"id": internet_circuit_id}],
                    "interface_capabilities": [{"id": edge_uplink_id}],
                },
            )
            await circuit.save(allow_upsert=True)
            self.logger.info(f"Ensured SD-WAN overlay circuit '{vc_name}'")
            return circuit
        except Exception as exc:
            self.logger.error(f"Failed to create SD-WAN overlay circuit '{vc_name}': {exc}")
            return None

    async def _ensure_gateway_subinterface(
        self,
        *,
        gateway_id: str,
        gateway_name: str,
        trunk_iface_id: str,
        trunk_iface_name: str,
        tunnel_id_value: int,
    ) -> Any | None:
        sub_iface_name = f"{trunk_iface_name}.{tunnel_id_value}"
        try:
            sub_iface = await self.client.create(
                kind=DcimVirtualInterface,
                data={
                    "name": sub_iface_name,
                    "device": {"id": gateway_id},
                    "parent_interface": {"id": trunk_iface_id},
                    "role": "uplink",
                    "status": "active",
                },
            )
            await sub_iface.save(allow_upsert=True)
            self.logger.info(f"Ensured Gateway sub-interface '{sub_iface_name}' on {gateway_name}")
            return sub_iface
        except Exception as exc:
            self.logger.error(f"Failed to create Gateway sub-interface '{sub_iface_name}' on {gateway_name}: {exc}")
            return None

    async def _link_overlay_to_gateway_subinterface(
        self, overlay_circuit: Any, gateway_sub_iface_id: str, office_name: str
    ) -> None:
        """overlay_circuit comes straight out of _ensure_overlay_circuit's own
        client.create()+save(allow_upsert=True) — its interface_capabilities
        relationship manager only carries the id given at creation time, no
        __typename, so calling .fetch() on it directly raises the same
        "id and/or typename are not defined" error generators/devices.py's
        _ensure_ha_interfaces docstring documents for a freshly-created node.
        Re-fetching the node via filters(..., include=[...]) first — the same
        fix devices.py's own "existing domain" path relies on — returns real
        typenames from the server, so .fetch() on THAT copy works.
        """
        try:
            refreshed = await self.client.filters(
                kind=TopologyVirtualCircuit, ids=[overlay_circuit.id], include=["interface_capabilities"]
            )
            if not refreshed:
                self.logger.error(f"{office_name}: overlay circuit vanished before linking gateway sub-interface")
                return
            circuit = refreshed[0]
            iface_capabilities = getattr(circuit, "interface_capabilities")
            await iface_capabilities.fetch()
            if any(peer.id == gateway_sub_iface_id for peer in iface_capabilities.peers):
                return
            await self._safe_rel_add(iface_capabilities, {"id": gateway_sub_iface_id})
            await circuit.save(allow_upsert=True)
        except Exception as exc:
            self.logger.error(f"{office_name}: failed to link overlay circuit to gateway sub-interface: {exc}")

    async def _attach_to_orchestrator(self, *, gateway_id: str, gateway_name: str, edge_id: str) -> None:
        try:
            orchestrators = await self.client.filters(kind=ManagedController, managed_devices__ids=[gateway_id])
        except Exception as exc:
            self.logger.error(f"Error looking up orchestrator managing {gateway_name}: {exc}")
            return
        if not orchestrators:
            self.logger.warning(f"No orchestrator manages {gateway_name} — new edge will not be group-managed")
            return

        orchestrator = orchestrators[0]
        managed_devices = getattr(orchestrator, "managed_devices")
        await managed_devices.fetch()
        if any(peer.id == edge_id for peer in managed_devices.peers):
            return
        await self._safe_rel_add(managed_devices, {"id": edge_id})
        # update_group_context=False: the orchestrator is hand-authored shared
        # infrastructure, not this generator's. Tracked, it would join the
        # run's group on the run that attaches the edge and be deleted by
        # delete_unused_nodes on the next, which finds the edge attached and
        # skips the save.
        await orchestrator.save(allow_upsert=True, update_group_context=False)
        self.logger.info(f"Attached edge to orchestrator '{orchestrator.name.value}'")
