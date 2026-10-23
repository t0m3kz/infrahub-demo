"""The raw response of ``tests/integration/queries/get_interconnect_inventory.gql`` over the 30_all data.

Built on the simulator of the placement check (``circuit_data_payloads``, shaped like ``circuit_placement.gql``) and
completed with what the inventory query selects on top: the physical circuits' ``circuit_id`` and status, owner names,
each end device's deployment, the interface kind, the parent of a sub-interface, a handoff's key and route limit. No
address is set (nothing generated it offline): ``addresses`` puts them on named ports.
"""

from __future__ import annotations

from typing import Any

from tests.integration.demo_data import declared, distinct, name
from tests.unit.simulators.circuit_data_payloads import (
    ALL_DEMO,
    HANDOFF,
    INTERCONNECT,
    PHYSICAL,
    TUNNEL,
    _Model,
    build_payloads,
)


def _edges(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    return {"edges": [{"node": node} for node in nodes]}


def _with_circuit_id(carriers: list[dict[str, Any]]) -> None:
    """The inventory query selects a physical circuit's `circuit_id`, the check query its `name`."""
    for carrier in carriers:
        if carrier.get("__typename") == PHYSICAL:
            carrier["circuit_id"] = carrier["name"]


def inventory_payload(
    without_cables: bool = False,
    without_circuits: tuple[str, ...] = (),
    addresses: dict[tuple[str, str], str] | None = None,
) -> dict[str, Any]:
    """The inventory response; ``addresses`` maps (device, interface) to the address a generator put on it."""
    model = _Model(without_cables, frozenset(without_circuits))
    payloads = build_payloads(without_cables, without_circuits)
    organisations = {
        str(org.value("org_id")): str(org.value("name")) for org in distinct(declared("OrganizationCustomer", ALL_DEMO))
    }
    status = {row["name"]: row["status"] for row in model.physical}

    def named_owner(circuit: dict[str, Any]) -> None:
        owner = circuit["owner"]["node"]
        if owner:
            owner["name"] = {"value": organisations[owner["org_id"]["value"]]}

    physical = []
    for edge in payloads[PHYSICAL]["edges"]:
        circuit = edge["node"]
        circuit["circuit_id"] = circuit["name"]
        circuit["circuit_type"] = {"value": "dark_fiber"}
        circuit["status"] = {"value": status[circuit["name"]["value"]]}
        circuit["provider"] = {"node": {"name": {"value": "P016"}}}
        named_owner(circuit)
        physical.append(circuit)

    maximum_routes = {name(h): h.item["maximum_routes"] for h in distinct(declared(HANDOFF, ALL_DEMO))}
    passwords = {name(h): h.item["password"] for h in distinct(declared(HANDOFF, ALL_DEMO))}
    virtual = []
    for kind in (TUNNEL, INTERCONNECT, HANDOFF):
        for edge in payloads[kind]["edges"]:
            circuit = edge["node"]
            circuit["__typename"] = kind
            if "owner" in circuit:
                named_owner(circuit)
            if kind == HANDOFF:
                circuit["maximum_routes"] = {"value": maximum_routes[circuit["name"]["value"]]}
                circuit["password"] = {"node": {"name": {"value": passwords[circuit["name"]["value"]]}}}
            for port_edge in circuit["capabilities"]["edges"]:
                port = port_edge["node"]
                device = port["device"]["node"]["name"]["value"]
                port["device"]["node"]["deployment"] = model.deployment(device)
                port["__typename"] = "DcimVirtualInterface" if "parent_interface" in port else "DcimPhysicalInterface"
                address = (addresses or {}).get((device, port["name"]["value"]))
                port["ip_address"] = {"node": {"address": {"value": address}} if address else None}
                _with_circuit_id([e["node"] for e in port["capabilities"]["edges"]])
                parent = port.get("parent_interface")
                if parent:
                    parent_name = port["name"]["value"].rpartition(".")[0]
                    parent["node"]["id"] = f"{device}:{parent_name}"
                    parent["node"]["name"] = {"value": parent_name}
                    _with_circuit_id([e["node"] for e in parent["node"]["capabilities"]["edges"]])
            virtual.append(circuit)

    return {"TopologyPhysicalCircuit": _edges(physical), "TopologyVirtualCircuit": _edges(virtual)}
