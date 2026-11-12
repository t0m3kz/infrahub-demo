"""The physical carrier of a virtual circuit, derived and never declared (shared by the generator and the checks).

A port is carried by a carrier circuit (``port_underlay``) or a cable (``port_cabled``); ``has_carrier`` is either.
"""

from __future__ import annotations

from typing import Any

from utils.interface_encapsulation import parent_of

DECOMMISSIONED = "decommissioned"
CARRIER_CIRCUIT = "TopologyCarrierCircuit"
SESSION_CIRCUIT = "TopologySessionCircuit"
DCI_TYPE = "dci"
HANDOFF_TYPE = "handoff"
TUNNEL_TYPE = "tunnel"
VPN_TYPE = "vpn"
# Types with a generated session (a tunnel has none); a port is an end of at most one.
SESSION_CIRCUIT_TYPES = (DCI_TYPE, HANDOFF_TYPE, VPN_TYPE)

BGP_PROCESS = "ManagedBGP"
OVERLAY_ROLE = "overlay"
# A provider-hosted network function we do not administer.
NETWORK_APPLIANCE_KIND = "CloudNetworkAppliance"
# The cloud side of a circuit (authored, never generated): a peering, or a port of a transport.
CLOUD_PEERING_KIND = "CloudPeeringInterface"
CLOUD_TRANSPORT_KINDS = ("CloudDirectConnect", "CloudVPNConnection")
# THE INTERNET: a passive device that is also a facility; it is the location of its own carrier end.
INTERNET_KIND = "TopologyInternet"
# The one interface kind that may be an end of many carriers.
INTERNET_INTERFACE_KIND = "TopologyInternetInterface"
# A provider's own router has no place and no deployment: its provider is its ``owner``.
PROVIDER_KIND = "OrganizationProvider"


def is_provider_device(device: dict[str, Any]) -> bool:
    """The device is a provider's own router: owned by an ``OrganizationProvider`` and placed nowhere."""
    owner = device.get("owner")
    placed = device.get("deployment") or device.get("region") or device.get("saas_region") or device.get("location")
    return isinstance(owner, dict) and owner.get("typename") == PROVIDER_KIND and not placed


def overlay_processes(device: dict[str, Any]) -> list[dict[str, Any]]:
    """The ManagedBGP processes of process_role ``overlay`` among a cleaned device's ``capabilities``."""
    return [
        process
        for process in device.get("capabilities") or []
        if isinstance(process, dict)
        and process.get("typename") == BGP_PROCESS
        and process.get("process_role") == OVERLAY_ROLE
    ]


def is_cloud_end(port: dict[str, Any]) -> bool:
    """The cleaned end is a cloud peering interface, or a port of a cloud transport (a device with no deployment)."""
    return port.get("typename") == CLOUD_PEERING_KIND or (port.get("device") or {}).get("typename") in (
        CLOUD_TRANSPORT_KINDS
    )


def _port_and_parent(port: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """The port and, when it is a sub-interface, its parent interface (else an empty dict)."""
    return port, parent_of(port) or {}


def port_underlay(port: dict[str, Any]) -> list[dict[str, Any]]:
    """The carrier circuits on a port or its encapsulation parent, deduplicated by id, whatever their status."""
    circuits: dict[str, dict[str, Any]] = {}
    for interface in _port_and_parent(port):
        for cap in interface.get("capabilities") or []:
            if isinstance(cap, dict) and cap.get("typename") == CARRIER_CIRCUIT and cap.get("id"):
                circuits.setdefault(cap["id"], cap)
    return list(circuits.values())


def cabled_port(port: dict[str, Any]) -> dict[str, Any] | None:
    """The port carrying a cable, directly or through its parent (a sub-interface or a hosted VM NIC)."""
    return next((interface for interface in _port_and_parent(port) if interface.get("cable")), None)


def port_cabled(port: dict[str, Any]) -> bool:
    """A DcimCable terminates on the port, or on its parent interface (one level)."""
    return cabled_port(port) is not None


def has_carrier(port: dict[str, Any]) -> bool:
    """The port is carried: a carrier circuit (whatever its status) or a cable at the port or its parent."""
    return bool(port_underlay(port)) or port_cabled(port)


def derived_underlay(circuit: dict[str, Any]) -> list[dict[str, Any]]:
    """The carrier circuits at a virtual circuit's ports (cables are not entries); a LAG that is not the ``parent``
    derives nothing.
    """
    underlay: dict[str, dict[str, Any]] = {}
    for port in circuit.get("capabilities") or []:
        for carrier in port_underlay(port):
            underlay.setdefault(carrier["id"], carrier)
    return list(underlay.values())


async def session_circuits_on(client: Any, port_ids: list[str]) -> list[Any]:
    """Every session circuit (dci, handoff or vpn) ending on one of ``port_ids``; a tunnel shares ports, so it is out."""
    return await client.filters(
        kind=SESSION_CIRCUIT, capabilities__ids=port_ids, circuit_type__values=list(SESSION_CIRCUIT_TYPES)
    )
