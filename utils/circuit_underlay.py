"""The physical carrier of a virtual circuit, shared by the generator and the checks.

A virtual circuit's underlay is derived, never declared. A port is carried by

- a TopologyCarrierCircuit found in the ``capabilities`` of the port, or of its parent interface when the port is a
  sub-interface (``parent_interface``: a handoff VLAN on a cage edge port rides the circuit that terminates on the
  parent): :func:`port_underlay`, :func:`derived_underlay` over every end, or
- a DcimCable on the port or its parent: :func:`port_cabled`. A carrier circuit needs two distinct facilities, so a
  hop inside one facility (the gateway and the cage edge in one cage) is a cable, the project's rule for fibre
  pulled between two chassis in one facility. A cable has no status: a cabled port is always carried.

:func:`has_carrier` is either. Both ``generators/topology/virtual_circuit.py`` (waits, session status) and
``checks/circuit_placement.py`` (contiguity, carrier presence) read the same cleaned dicts. The cleaned port has
``cable`` (the cable's id, or None) when the query selects it on a DcimPhysicalInterface, and a sub-interface's
``parent_interface`` has its own ``cable``.

The session circuit kind and its ``circuit_type`` values live here once (strings, no protocols import): the
generators, the check and the planner mapping all read :data:`SESSION_CIRCUIT_TYPES`. The two rules of a session end that the generator and the check
must agree on are here too: the overlay BGP processes of a device (:func:`overlay_processes`) and the VLAN of a
``<port>.<vlan>`` sub-interface (:func:`subinterface_vlan`).
"""

from __future__ import annotations

import re
from typing import Any

DECOMMISSIONED = "decommissioned"
CARRIER_CIRCUIT = "TopologyCarrierCircuit"
# One kind for every circuit that rides a carrier; its role is its ``circuit_type``.
SESSION_CIRCUIT = "TopologySessionCircuit"
DCI_TYPE = "dci"
HANDOFF_TYPE = "handoff"
TUNNEL_TYPE = "tunnel"
# The circuit types that have a session generated (a tunnel has none): a port is an end of at most one.
SESSION_CIRCUIT_TYPES = (DCI_TYPE, HANDOFF_TYPE)

BGP_PROCESS = "ManagedBGP"
OVERLAY_ROLE = "overlay"
# A provider-hosted network function (Equinix Network Edge): a device kind we do not administer.
NETWORK_APPLIANCE_KIND = "CloudNetworkAppliance"
# A handoff end is a VLAN sub-interface, <parent>.<vlan>: the VLAN lives in the name.
_VLAN_SUBINTERFACE = re.compile(r".+\.(\d+)")


def overlay_processes(device: dict[str, Any]) -> list[dict[str, Any]]:
    """The ManagedBGP processes of process_role ``overlay`` among a cleaned device's ``capabilities``."""
    return [
        process
        for process in device.get("capabilities") or []
        if isinstance(process, dict)
        and process.get("typename") == BGP_PROCESS
        and process.get("process_role") == OVERLAY_ROLE
    ]


def subinterface_vlan(name: str) -> int | None:
    """The VLAN of a sub-interface named ``<port>.<vlan>``, None for any other name."""
    match = _VLAN_SUBINTERFACE.fullmatch(name)
    return int(match.group(1)) if match else None


def _port_and_parent(port: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """The port and, when it is a sub-interface, its parent interface (else an empty dict)."""
    parent = port.get("parent_interface")
    return port, parent if isinstance(parent, dict) else {}


def port_underlay(port: dict[str, Any]) -> list[dict[str, Any]]:
    """The carrier circuits terminating on one port, or on its parent interface when it is a sub-interface.

    Each port's own ``capabilities`` list the entries that terminate on it (segments, peerings, other circuits
    too); a sub-interface has no carrier of its own, so its ``parent_interface`` is read as well, one level. The
    TopologyCarrierCircuit entries, deduplicated by id in first-seen order, whatever their status.
    """
    circuits: dict[str, dict[str, Any]] = {}
    for interface in _port_and_parent(port):
        for cap in interface.get("capabilities") or []:
            if isinstance(cap, dict) and cap.get("typename") == CARRIER_CIRCUIT and cap.get("id"):
                circuits.setdefault(cap["id"], cap)
    return list(circuits.values())


def port_cabled(port: dict[str, Any]) -> bool:
    """A DcimCable terminates on the port, or on its parent interface when it is a sub-interface (one level)."""
    return any(interface.get("cable") for interface in _port_and_parent(port))


def has_carrier(port: dict[str, Any]) -> bool:
    """The port is carried: a carrier circuit (whatever its status) or a cable at the port or its parent."""
    return bool(port_underlay(port)) or port_cabled(port)


def derived_underlay(circuit: dict[str, Any]) -> list[dict[str, Any]]:
    """The physical carriers at a virtual circuit's ports: :func:`port_underlay` over every end, deduplicated by id.

    Only circuits: a cable is no entry here (nothing to wait for, nothing to chain). Limitations, all silent: a LAG
    or other logical port that is not a ``parent_interface`` of the port derives nothing; a middle hop of a chain that
    touches none of the circuit's ports is not found; and a port removed from a carrier circuit, or cabled after the
    virtual circuit exists, reruns nothing, so the virtual circuits that sat on it keep (or lack) their session until
    their next run.

    What reaches a session: it reruns on its own created / capabilities / status (a handoff also ip_namespace and
    password) and, through add_circuit's fan-out (generators/topology/circuit.py), when a physical
    circuit on its ports (or on a port's parent) is created, has its ends changed or its status changed. It does NOT
    rerun when a cable is added later, when a port is removed from a circuit, or when a circuit's type changes.
    """
    underlay: dict[str, dict[str, Any]] = {}
    for port in circuit.get("capabilities") or []:
        for carrier in port_underlay(port):
            underlay.setdefault(carrier["id"], carrier)
    return list(underlay.values())


async def session_circuits_on(client: Any, port_ids: list[str]) -> list[Any]:
    """Every session circuit (dci or handoff) with one of ``port_ids`` among its ends.

    A tunnel shares its ports by design (one gateway port serves many tunnels), so it stays out: the filter is on
    ``circuit_type``, not on the kind.
    """
    return await client.filters(
        kind=SESSION_CIRCUIT, capabilities__ids=port_ids, circuit_type__values=list(SESSION_CIRCUIT_TYPES)
    )
