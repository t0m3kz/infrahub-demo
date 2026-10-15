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
device, the SD-WAN overlay TopologyVirtualCircuit (with a pool-allocated
tunnel_id), the Gateway's matching tunnel sub-interface, and an
internet-underlay circuit — then attaches the new Edge to the Gateway's own
Orchestrator (VCO) via managed_devices. It is the only path: no office's
Edge, tunnel or underlay is hand-authored.

The shared Gateway/VCO themselves stay hand-authored and load with the
foundation stage (data/demos/30_all/02_colo/equinix/fr/04_sdwan_gateway.yml),
so an office can opt in when it is created — this generator only ever reads
them, mirroring how CustomerDeploymentColocationExchangeGenerator
(generators/topology/customer_colocation.py) assumes the shared metro fabric
already exists and only handles the per-tenant slice.

Deliberately does NOT reuse DeviceMixin.create_devices()/CablingMixin.
ensure_vlan_subinterface(): create_devices() unconditionally resolves a
"{fabric_name}-management-pool" (generators/devices.py) that no office has,
and ensure_vlan_subinterface() hardcodes role="service" for its
sub-interface (generators/connections.py), while an SD-WAN tunnel
termination on the Gateway's trunk is role="uplink" (the role the trunk
itself has, and the one the Gateway's other sub-interfaces use). Both
helpers are tailored to DC/pod-scale fabric devices; a branch-office Edge is
simple enough that a direct create+upsert is less code and less risk than
bending those assumptions.

Ordering note: the Gateway's tunnel sub-interface is named "<trunk>.<tunnel_id>",
so the overlay circuit is created FIRST (allocating tunnel_id from
GLOBAL-SDWAN-TUNNEL-ID via a from_pool dict on the attribute) and its
resolved value read back, THEN the sub-interface is created, THEN the
circuit's interface_capabilities is updated to include it — there is no
standalone "allocate a number" call for a CoreNumberPool, only from_pool
applied to a real node's attribute.

Failures raise: a missing prerequisite is logged with self.logger.error and
raised as GeneratorError (_fail), and an SDK error propagates as is, so a
run that could not provision the office's slice is a failed task, never a
green one that silently stopped half way.
"""

from __future__ import annotations

from typing import Any, NoReturn

from utils.data_cleaning import clean_data

from ..common import CommonGenerator
from ..logger import GeneratorError
from ..protocols import (
    DcimPhysicalDevice,
    DcimPhysicalInterface,
    DcimVirtualInterface,
    ManagedController,
    TopologyPhysicalCircuit,
    TopologyVirtualCircuit,
)

# Who sells each office its internet access. Not modelled per office (no
# "chosen ISP" signal on TopologyCustomerOffice), so every office's underlay
# gets the same provider: Orange S.A. A per-office knob is added only when a
# transform or check reads it.
_INTERNET_PROVIDER_ORG_ID = "P019"
# Who operates the overlay riding that underlay: the SD-WAN service provider
# (VMware VeloCloud), never the ISP — the tunnel is not bought from the ISP.
_OVERLAY_PROVIDER_ORG_ID = "P035"
_TUNNEL_ID_POOL_NAME = "GLOBAL-SDWAN-TUNNEL-ID"
_EDGE_TEMPLATE_NAME = "VCE-610_EDGE"


def _target_group(name: str) -> dict[str, Any]:
    """A circuit generator target group (data/bootstrap/00_groups.yml), by HFID.

    Membership is what lets add_circuit/add_virtual_circuit run for the
    circuits this generator creates, as for the data-declared ones.
    """
    return {"hfid": [name], "kind": "CoreGeneratorGroup"}


class SdwanEdgeGenerator(CommonGenerator):
    """add_sdwan_edge — per-office SD-WAN Edge, tunnel, and circuit provisioning."""

    # Widens the type for the type checker only (mirrors generators/devices.py's
    # DeviceMixin.client: Any) — the real InfrahubGenerator.client property
    # returns InfrahubClient, which unit tests replace with a MagicMock; without
    # this, ty resolves .create/.filters/.get back to InfrahubClient's own
    # overloaded signatures and flags every mock assertion as invalid.
    client: Any

    def _fail(self, message: str) -> NoReturn:
        """Log ``message`` as an error and raise it: the run is a failed task.

        self.logger.error already raises under the generator's fail-on-error
        logger; the explicit raise keeps that true for any logger and tells
        the type checker the caller does not continue.
        """
        self.logger.error(message)
        raise GeneratorError(message)

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
            self._fail("Office missing id — cannot proceed")

        gateway = office.get("sdwan_gateway")
        if not gateway or not gateway.get("id"):
            self.logger.info(f"{office_name}: no sdwan_gateway set — SD-WAN edge provisioning not requested")
            return

        owner_org = (office.get("parent") or {}).get("owner") or {}
        owner_id: str | None = owner_org.get("id")
        if not owner_id:
            self._fail(f"{office_name}: no owner resolved via parent — cannot proceed")

        self.logger.info(f"Processing SD-WAN edge for office {office_name}")

        gateway_id: str = gateway["id"]
        gateway_name: str = gateway.get("name", gateway_id)
        gateway_zone = gateway.get("deployment") or {}
        gateway_zone_id: str | None = gateway_zone.get("id")
        gateway_zone_name: str | None = gateway_zone.get("name")
        if not gateway_zone_id or not gateway_zone_name:
            self._fail(f"{office_name}: gateway {gateway_name} has no deployment — cannot set circuit locations")

        trunk_iface = next(
            (iface for iface in gateway.get("interfaces") or [] if iface.get("role") == "uplink"),
            None,
        )
        if trunk_iface is None:
            self._fail(f"{office_name}: gateway {gateway_name} has no role=uplink trunk interface")

        internet_provider_id, overlay_provider_id = await self._resolve_provider_ids(office_name)
        edge = await self._ensure_edge_device(office_name, office_id, owner_id)
        edge_uplink = await self._find_edge_uplink(edge.id, office_name)

        internet_circuit = await self._ensure_internet_circuit(
            office_name=office_name,
            office_id=office_id,
            owner_id=owner_id,
            provider_id=internet_provider_id,
            edge_uplink_id=edge_uplink.id,
            gateway_zone_id=gateway_zone_id,
        )
        overlay_circuit = await self._ensure_overlay_circuit(
            office_name=office_name,
            office_id=office_id,
            owner_id=owner_id,
            provider_id=overlay_provider_id,
            gateway_zone_id=gateway_zone_id,
            gateway_zone_name=gateway_zone_name,
            internet_circuit_id=internet_circuit.id,
            edge_uplink_id=edge_uplink.id,
        )

        tunnel_id_attr = getattr(overlay_circuit, "tunnel_id", None)
        tunnel_id = getattr(tunnel_id_attr, "value", None) if tunnel_id_attr else None
        if tunnel_id is None:
            self._fail(f"{office_name}: overlay circuit has no resolved tunnel_id after save")

        gateway_sub_iface = await self._ensure_gateway_subinterface(
            gateway_id=gateway_id,
            gateway_name=gateway_name,
            trunk_iface_id=trunk_iface["id"],
            trunk_iface_name=trunk_iface.get("name", "eth0"),
            tunnel_id_value=int(tunnel_id),
        )

        await self._link_overlay_to_gateway_subinterface(overlay_circuit, gateway_sub_iface.id, office_name)
        await self._attach_to_orchestrator(gateway_id=gateway_id, gateway_name=gateway_name, edge_id=edge.id)

    async def _resolve_provider_ids(self, office_name: str) -> tuple[str, str]:
        """(internet provider id, overlay provider id), both looked up in one query."""
        org_ids = [_INTERNET_PROVIDER_ORG_ID, _OVERLAY_PROVIDER_ORG_ID]
        providers = await self.client.filters(kind="OrganizationProvider", org_id__values=org_ids)
        by_org_id = {provider.org_id.value: provider.id for provider in providers}
        missing = [org_id for org_id in org_ids if org_id not in by_org_id]
        if missing:
            self._fail(f"{office_name}: provider org(s) {missing} not found")
        return by_org_id[_INTERNET_PROVIDER_ORG_ID], by_org_id[_OVERLAY_PROVIDER_ORG_ID]

    async def _ensure_edge_device(self, office_name: str, office_id: str, owner_id: str) -> Any:
        """Create/upsert the Edge, mirroring generators/devices.py's
        create_devices() convention: object_template is only sent on first
        creation (resending it on an existing device triggers a server-side
        re-instantiation that fails with "device is mandatory for
        DcimPhysicalInterface") — an existing device is instead upserted by
        id, never re-sent its template.
        """
        edge_name = f"{office_name}-EDGE1"
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
                self._fail(f"Template '{_EDGE_TEMPLATE_NAME}' not found — cannot create Edge device")
            data["object_template"] = {"id": templates[0].id}

        edge = await self.client.create(kind=DcimPhysicalDevice, data=data)
        await edge.save(allow_upsert=True)
        self.logger.info(f"Ensured Edge device '{edge_name}'")
        return edge

    async def _find_edge_uplink(self, edge_id: str, office_name: str) -> Any:
        ifaces = await self.client.filters(kind=DcimPhysicalInterface, device__ids=[edge_id], role__value="uplink")
        if not ifaces:
            self._fail(f"{office_name}: edge device has no role=uplink interface")
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
    ) -> Any:
        circuit_name = f"INET-{office_name}"
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
                # itself (not the edge device) and the gateway's zone.
                "locations": [{"id": office_id}, {"id": gateway_zone_id}],
                "customer_interfaces": [{"id": edge_uplink_id}],
                "member_of_groups": [_target_group("physical_circuits")],
            },
        )
        await circuit.save(allow_upsert=True)
        self.logger.info(f"Ensured internet-underlay circuit '{circuit_name}'")
        return circuit

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
    ) -> Any:
        """Create/upsert the overlay VC with only the edge-side interface
        capability set — the gateway sub-interface doesn't exist yet (its
        name depends on the tunnel_id this same call allocates), so it's
        linked in a follow-up save by _link_overlay_to_gateway_subinterface.
        """
        vc_name = f"{office_name}-SDWAN-{gateway_zone_name}"
        pool = await self.client.get(kind="CoreNumberPool", name__value=_TUNNEL_ID_POOL_NAME)
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
                "member_of_groups": [_target_group("virtual_circuits")],
            },
        )
        await circuit.save(allow_upsert=True)
        self.logger.info(f"Ensured SD-WAN overlay circuit '{vc_name}'")
        return circuit

    async def _ensure_gateway_subinterface(
        self,
        *,
        gateway_id: str,
        gateway_name: str,
        trunk_iface_id: str,
        trunk_iface_name: str,
        tunnel_id_value: int,
    ) -> Any:
        sub_iface_name = f"{trunk_iface_name}.{tunnel_id_value}"
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
        refreshed = await self.client.filters(
            kind=TopologyVirtualCircuit, ids=[overlay_circuit.id], include=["interface_capabilities"]
        )
        if not refreshed:
            self._fail(f"{office_name}: overlay circuit vanished before linking gateway sub-interface")
        circuit = refreshed[0]
        iface_capabilities = getattr(circuit, "interface_capabilities")
        await iface_capabilities.fetch()
        if any(peer.id == gateway_sub_iface_id for peer in iface_capabilities.peers):
            return
        await self._safe_rel_add(iface_capabilities, {"id": gateway_sub_iface_id})
        await circuit.save(allow_upsert=True)

    async def _attach_to_orchestrator(self, *, gateway_id: str, gateway_name: str, edge_id: str) -> None:
        orchestrators = await self.client.filters(kind=ManagedController, managed_devices__ids=[gateway_id])
        if not orchestrators:
            self.logger.warning(f"No orchestrator manages {gateway_name} — new edge will not be group-managed")
            return

        orchestrator = orchestrators[0]
        managed_devices = getattr(orchestrator, "managed_devices")
        await managed_devices.fetch()
        if any(peer.id == edge_id for peer in managed_devices.peers):
            return
        # A RelationshipAdd of this one edge, not a save of the whole peer
        # list: every office's run attaches its edge to the same shared
        # orchestrator, and the offices board in one load, so a save would let
        # concurrent runs overwrite each other's edge. It also never claims the
        # hand-authored orchestrator for this run's group (a tracked save would
        # get it deleted by delete_unused_nodes on the next run).
        await orchestrator.add_relationships(relation_to_update="managed_devices", related_nodes=[edge_id])
        self.logger.info(f"Attached edge to orchestrator '{orchestrator.name.value}'")
