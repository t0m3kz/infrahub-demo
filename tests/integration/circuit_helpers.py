"""The interconnect inventory: physical and virtual circuits with their ends, locations and sessions (test_62)."""

import asyncio
import logging
from typing import Any

from infrahub_sdk import InfrahubClient

from utils.circuit_underlay import derived_underlay, port_cabled, port_underlay
from utils.data_cleaning import clean_data

from .query_files import QUERY_GET_INTERCONNECT_INVENTORY
from .suite_constants import DATA_PROPAGATION_DELAY

logger = logging.getLogger(__name__)


async def fetch_interconnect_inventory(client: InfrahubClient, branch: str) -> dict[str, list[dict[str, Any]]]:
    """Fetch the physical and virtual circuit layer of a branch.

    Returns:
        {"physical": [{"id", "circuit_id", "circuit_type", "status", "provider",
                       "owner", "owner_id", "locations": [str],
                       "location_nodes": [location], "end_devices": [device],
                       "end_ids": [str], "ends": [(device, interface)]}],
         "virtual": [{"id", "name", "circuit_type", "owner", "owner_id", "locations": [str],
                      "location_nodes": [location], "end_devices": [device],
                      "ends": [(device, interface)], "end_nodes": [end],
                      "carrier": [str], "carriers": [carrier],
                      "ip_namespace", "password",
                      "cloud_endpoint", "cloud_endpoint_vlan"}]}

        A location is {"id", "kind", "name", "parent_id", "parent_kind",
        "parent_name", "owner_id"}: the parent of a colocation zone (its
        metro; likewise a virtual colocation's) or of a TopologyCustomer footprint (its facility), the owner
        of a footprint or an office, None where the kind has none. An end
        device is {"device", "device_id", "deployment_id", "deployment_kind",
        "deployment_name", "deployment_parent_id"}, one per circuit end, in end order; the parent is the metro
        of a zone or virtual colocation deployment, None otherwise.

        A session circuit's "circuit_type" is its role (tunnel, dci or handoff), "ends" its sorted (device, interface) pairs and "end_nodes" the same ends in query
        order, each {"id", "device", "device_id", "interface", "kind", "address", "parent_id", "parent_name",
        "carriers", "cabled"}: the address ("10.255.128.0/31", None when unaddressed), the parent interface of a
        sub-interface, the carriers found on the port or on its parent (utils.circuit_underlay.port_underlay) and
        whether a cable terminates there (port_cabled). The "carrier" of the circuit is derived, never declared:
        the circuit_ids of the carrier circuits on its ends (derived_underlay), deduped and sorted, "carriers" the
        same circuits as {"id", "circuit_id", "status"}. A Handoff also carries "ip_namespace",
        "password" (its key's name), "cloud_endpoint" (the VIF's name) and the "cloud_endpoint_vlan" the VIF states;
        the other kinds have None for those.
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
            "owner": (circuit.get("owner") or {}).get("name"),
            "owner_id": (circuit.get("owner") or {}).get("id"),
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
            "  VC  %s [%s] owner=%s locations=%s ends=%s carrier=%s ns=%s cloud=%s",
            circuit["name"],
            circuit["circuit_type"],
            circuit["owner"],
            circuit["locations"],
            circuit["ends"],
            circuit["carrier"],
            circuit["ip_namespace"],
            circuit["cloud_endpoint"],
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
    endpoint = circuit.get("cloud_endpoint") or {}
    return {
        "id": str(circuit.get("id") or ""),
        "name": str(circuit.get("name") or ""),
        "circuit_type": str(circuit.get("circuit_type") or ""),
        "owner": (circuit.get("owner") or {}).get("name"),
        "owner_id": (circuit.get("owner") or {}).get("id"),
        "locations": sorted(str(loc.get("name") or "") for loc in circuit.get("locations", []) or []),
        "location_nodes": _circuit_locations(circuit),
        "end_devices": _circuit_end_devices(circuit),
        "ends": sorted((end["device"], end["interface"]) for end in end_nodes),
        "end_nodes": end_nodes,
        "carrier": sorted({carrier["circuit_id"] for carrier in carriers}),
        "carriers": carriers,
        "ip_namespace": (circuit.get("ip_namespace") or {}).get("name"),
        "password": (circuit.get("password") or {}).get("name"),
        "cloud_endpoint": endpoint.get("name") if endpoint else None,
        "cloud_endpoint_vlan": endpoint.get("vlan_id") if endpoint else None,
    }


def _circuit_end(port: dict[str, Any]) -> dict[str, Any]:
    """One end port of a virtual circuit: where it is, its address and what carries it."""
    device = port.get("device") or {}
    parent = port.get("parent_interface") or {}
    return {
        "id": str(port.get("id") or ""),
        "device": str(device.get("name") or ""),
        "device_id": device.get("id"),
        "interface": str(port.get("name") or ""),
        "kind": str(port.get("typename") or ""),
        "address": (port.get("ip_address") or {}).get("address"),
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
        owner = location.get("owner") or {}
        locations.append(
            {
                "id": str(location.get("id") or ""),
                "kind": str(location.get("typename") or ""),
                "name": str(location.get("name") or ""),
                "parent_id": parent.get("id"),
                "parent_kind": parent.get("typename"),
                "parent_name": parent.get("name"),
                "owner_id": owner.get("id"),
            }
        )
    return sorted(locations, key=lambda location: location["name"])


def _circuit_end_devices(circuit: dict[str, Any]) -> list[dict[str, Any]]:
    """The device behind each end (capability) of one circuit, with its deployment."""
    devices = []
    for end in circuit.get("capabilities", []) or []:
        device = end.get("device") or {}
        deployment = device.get("deployment") or {}
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
