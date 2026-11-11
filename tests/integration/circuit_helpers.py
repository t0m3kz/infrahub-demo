"""The interconnect inventory: physical and virtual circuits with their ends, locations and sessions (test_62)."""

import asyncio
import logging
from typing import Any

from infrahub_sdk import InfrahubClient

from utils.circuit_underlay import derived_underlay, port_cabled, port_underlay
from utils.data_cleaning import clean_data
from utils.interface_encapsulation import parent_of, vlan_of
from utils.ownership import circuit_owner, customer_owner

from .query_files import QUERY_GET_INTERCONNECT_INVENTORY
from .suite_constants import DATA_PROPAGATION_DELAY

logger = logging.getLogger(__name__)


async def fetch_interconnect_inventory(client: InfrahubClient, branch: str) -> dict[str, list[dict[str, Any]]]:
    """Fetch the physical and virtual circuit layer of a branch.

    Returns:
        {"physical": [{"id", "circuit_id", "circuit_type", "status", "provider",
                       "owner", "owner_id", "owner_org", "locations": [str],
                       "location_nodes": [location], "end_devices": [device],
                       "end_ids": [str], "ends": [(device, interface)]}],
         "virtual": [{"id", "name", "circuit_type", "owner", "owner_id", "owner_org", "locations": [str],
                      "location_nodes": [location], "end_devices": [device],
                      "ends": [(device, interface)], "end_nodes": [end],
                      "carrier": [str], "carriers": [carrier],
                      "security_zone", "password"}]}

        A circuit has no owner of its own: "owner" (the customer's name), "owner_id" and "owner_org" (its org_id) are
        derived by utils.ownership.circuit_owner from the offices among its locations; None for shared platform
        (a provider's or the operator's own account is no customer). A location is {"id", "kind", "name",
        "parent_id", "parent_kind", "parent_name", "owner_id", "owner_org"}: the parent of a colocation zone (its
        metro; likewise a virtual colocation's) or of a TopologyCustomer footprint (its facility), the customer
        owner of a footprint or an office, None where the kind has none. An end
        device is {"device", "device_id", "deployment_id", "deployment_kind",
        "deployment_name", "deployment_parent_id"}, one per circuit end, in end order; the parent is the metro
        of a zone or virtual colocation deployment, None otherwise. A cloud transport has no deployment: its region (or
        SaaS region) stands in the "deployment_*" keys.

        A session circuit's "circuit_type" is its role (tunnel, dci, handoff or vpn), "ends" its sorted (device, interface) pairs and "end_nodes" the same ends in query
        order, each {"id", "device", "device_id", "interface", "kind", "address", "vlan_id", "parent_id", "parent_name",
        "carriers", "cabled"}: the address ("10.255.128.0/31", None when unaddressed), the parent interface of a
        sub-interface, the carriers found on the port or on its parent (utils.circuit_underlay.port_underlay) and
        whether a cable terminates there (port_cabled). The "carrier" of the circuit is derived, never declared:
        the circuit_ids of the carrier circuits on its ends (derived_underlay), deduped and sorted, "carriers" the
        same circuits as {"id", "circuit_id", "status"}. A Handoff or a VPN also carries "security_zone" and "password" (its
        key's name); the other kinds have None for those.
    """
    client.default_branch = branch
    await asyncio.sleep(DATA_PROPAGATION_DELAY)

    raw = await client.execute_graphql(query=QUERY_GET_INTERCONNECT_INVENTORY)
    result = clean_data(raw)

    physical: list[dict[str, Any]] = [
        {
            "id": str(circuit.get("id") or ""),
            "circuit_id": str(circuit.get("circuit_id") or ""),
            "circuit_type": str(circuit.get("circuit_type") or ""),
            "status": str(circuit.get("status") or ""),
            "provider": (circuit.get("provider") or {}).get("name"),
            **_derived_owner(circuit),
            "locations": sorted(str(loc.get("name") or "") for loc in circuit.get("locations", []) or []),
            "location_nodes": _circuit_locations(circuit),
            "end_devices": _circuit_end_devices(circuit),
            "end_ids": sorted(str(end.get("id") or "") for end in circuit.get("capabilities", []) or []),
            "ends": sorted(
                (str((end.get("device") or {}).get("name") or ""), str(end.get("name") or ""))
                for end in circuit.get("capabilities", []) or []
            ),
        }
        for circuit in result.get("TopologyCarrierCircuit", []) or []
    ]

    virtual: list[dict[str, Any]] = [
        _virtual_circuit(circuit) for circuit in result.get("TopologySessionCircuit", []) or []
    ]

    logger.info(
        "Interconnects on branch '%s': %d physical, %d virtual",
        branch,
        len(physical),
        len(virtual),
    )
    for circuit in sorted(physical, key=lambda c: str(c["circuit_id"])):
        logger.info(
            "  PHY %s [%s] %s owner=%s ends=%s",
            circuit["circuit_id"],
            circuit["circuit_type"],
            circuit["locations"],
            circuit["owner"],
            circuit["ends"],
        )
    for circuit in sorted(virtual, key=lambda c: str(c["name"])):
        logger.info(
            "  VC  %s [%s] owner=%s locations=%s ends=%s carrier=%s ns=%s",
            circuit["name"],
            circuit["circuit_type"],
            circuit["owner"],
            circuit["locations"],
            circuit["ends"],
            circuit["carrier"],
            circuit["security_zone"],
        )
    return {"physical": physical, "virtual": virtual}


def _virtual_circuit(circuit: dict[str, Any]) -> dict[str, Any]:
    """One cleaned virtual circuit of the inventory query, flattened (see fetch_interconnect_inventory)."""
    ports = [port for port in circuit.get("capabilities", []) or [] if isinstance(port, dict)]
    end_nodes = [_circuit_end(port) for port in ports]
    carriers = [
        {
            "id": str(carrier.get("id") or ""),
            "circuit_id": str(carrier.get("circuit_id") or ""),
            "status": str(carrier.get("status") or ""),
        }
        for carrier in derived_underlay(circuit)
    ]
    return {
        "id": str(circuit.get("id") or ""),
        "name": str(circuit.get("name") or ""),
        "circuit_type": str(circuit.get("circuit_type") or ""),
        **_derived_owner(circuit),
        "locations": sorted(str(loc.get("name") or "") for loc in circuit.get("locations", []) or []),
        "location_nodes": _circuit_locations(circuit),
        "end_devices": _circuit_end_devices(circuit),
        "ends": sorted((end["device"], end["interface"]) for end in end_nodes),
        "end_nodes": end_nodes,
        "carrier": sorted({carrier["circuit_id"] for carrier in carriers}),
        "carriers": carriers,
        "security_zone": ((circuit.get("vrf") or {}).get("security_zone") or {}).get("name"),
        "password": (circuit.get("password") or {}).get("name"),
    }


def _customer(owner: Any) -> dict[str, Any] | None:
    """A cleaned owner as {"id", "name", "org_id"} when it is a customer, None for a provider or no owner at all."""
    key = customer_owner(owner)
    if key is None:
        return None
    return {"id": owner.get("id"), "name": owner.get("name"), "org_id": key}


def _derived_owner(circuit: dict[str, Any]) -> dict[str, Any]:
    """{"owner", "owner_id", "owner_org"} of a circuit, derived like checks/circuit_placement.py does (a circuit has no
    owner relationship): the customer of the offices among its locations. Raises CircuitOwnerError for offices of two customers."""
    offices = [
        owner
        for location in circuit.get("locations", []) or []
        if location.get("typename") == "TopologyCustomerOffice" and (owner := _customer(location.get("owner")))
    ]
    by_key = {owner["org_id"]: owner for owner in offices}
    key = circuit_owner([owner["org_id"] for owner in offices])
    owner = by_key.get(key) if key else None
    return {
        "owner": owner["name"] if owner else None,
        "owner_id": owner["id"] if owner else None,
        "owner_org": key,
    }


def _circuit_end(port: dict[str, Any]) -> dict[str, Any]:
    """One end port of a virtual circuit: where it is, its address and what carries it."""
    device = port.get("device") or {}
    parent = parent_of(port) or {}
    return {
        "id": str(port.get("id") or ""),
        "device": str(device.get("name") or ""),
        "device_id": device.get("id"),
        "interface": str(port.get("name") or ""),
        "kind": str(port.get("typename") or ""),
        "address": (port.get("ip_address") or {}).get("address"),
        "vlan_id": vlan_of(port),
        "parent_id": parent.get("id"),
        "parent_name": parent.get("name"),
        "carriers": [
            {
                "id": str(carrier.get("id") or ""),
                "circuit_id": str(carrier.get("circuit_id") or ""),
                "status": str(carrier.get("status") or ""),
            }
            for carrier in port_underlay(port)
        ],
        "cabled": port_cabled(port),
    }


def _circuit_locations(circuit: dict[str, Any]) -> list[dict[str, Any]]:
    """The cleaned CircuitLocation nodes of one circuit, flattened (see fetch_interconnect_inventory)."""
    locations = []
    for location in circuit.get("locations", []) or []:
        parent = location.get("parent") or {}
        owner = _customer(location.get("owner")) or {}
        locations.append(
            {
                "id": str(location.get("id") or ""),
                "kind": str(location.get("typename") or ""),
                "name": str(location.get("name") or ""),
                "parent_id": parent.get("id"),
                "parent_kind": parent.get("typename"),
                "parent_name": parent.get("name"),
                "owner_id": owner.get("id"),
                "owner_org": owner.get("org_id"),
            }
        )
    return sorted(locations, key=lambda location: location["name"])


def _circuit_end_devices(circuit: dict[str, Any]) -> list[dict[str, Any]]:
    """The device behind each end (capability) of one circuit, with its deployment."""
    devices = []
    for end in circuit.get("capabilities", []) or []:
        device = end.get("device") or {}
        # A cloud transport has no deployment: its facility is its region (or SaaS region), and a provider connect's is its
        # `location`. THE INTERNET is a facility itself (a device that is one), so it is its own place.
        deployment = (
            device.get("deployment")
            or device.get("region")
            or device.get("saas_region")
            or device.get("location")
            or {}
        )
        if not deployment and device.get("typename") == "TopologyInternet":
            deployment = device
        devices.append(
            {
                "device": str(device.get("name") or ""),
                "device_id": device.get("id"),
                "deployment_id": deployment.get("id"),
                "deployment_kind": deployment.get("typename"),
                "deployment_name": deployment.get("name"),
                "deployment_parent_id": (deployment.get("parent") or {}).get("id"),
            }
        )
    return devices


def _deployed_dc_names(devices: list[dict[str, Any]] | None) -> list[str]:
    """Return the sorted TopologyDataCenter names a list of devices is deployed in."""
    names = set()
    for device in devices or []:
        deployment = device.get("deployment") or {}
        if deployment.get("typename") == "TopologyDataCenter" and deployment.get("name"):
            names.add(str(deployment["name"]))
    return sorted(names)
