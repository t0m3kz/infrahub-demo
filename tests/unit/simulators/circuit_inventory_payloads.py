"""The raw response of ``tests/integration/queries/get_interconnect_inventory.gql`` over the 30_all data.

Built on the simulator of the placement check (``circuit_data_payloads``, shaped like ``circuit_placement.gql``) and
completed with what the inventory query selects on top: the carrier circuits' ``circuit_id`` and status, the owners (a
circuit has none: the office locations and the provider connection carry them, as customers), the connection's VLAN,
each end device's deployment, the interface kind, the parent of a sub-interface, a handoff's key (its own, else its connection's) and route limit. No
address is set (nothing generated it offline): ``addresses`` puts them on named ports.
"""

from __future__ import annotations

from typing import Any

from tests.integration.demo_data import declared, distinct, name
from tests.unit.simulators.circuit_data_payloads import (
    ALL_DEMO,
    CARRIER,
    HANDOFF,
    INTERCONNECT,
    SESSION,
    TUNNEL,
    _Model,
    build_payloads,
    session_circuits,
)


def _edges(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    return {"edges": [{"node": node} for node in nodes]}


def _with_circuit_id(carriers: list[dict[str, Any]]) -> None:
    """The inventory query selects a carrier circuit's `circuit_id`, the check query its `name`."""
    for carrier in carriers:
        if carrier.get("__typename") == CARRIER:
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
    status = {row["name"]: row["status"] for row in model.carriers}

    connections = {name(c): c for c in distinct(declared("TopologyProviderConnection", ALL_DEMO))}
    accounts = {name(a): a for a in distinct(declared("CloudAccount", ALL_DEMO))}

    def customer(org_id: str | None) -> dict[str, Any]:
        """An owner as CircuitOwner selects it: only an OrganizationCustomer carries a name (the operator's own
        account belongs to a provider, which is no customer)."""
        if org_id is None:
            return {"node": None}
        if org_id in organisations:
            return {
                "node": {
                    "id": f"id-{org_id}",
                    "__typename": "OrganizationCustomer",
                    "org_id": {"value": org_id},
                    "name": {"value": organisations[org_id]},
                }
            }
        return {"node": {"id": f"id-{org_id}", "__typename": "OrganizationProvider", "org_id": {"value": org_id}}}

    def named_owners(circuit: dict[str, Any]) -> None:
        """A circuit has no owner of its own: drop the leftover key and name the owners of its office locations."""
        circuit.pop("owner", None)
        for location in circuit["locations"]["edges"]:
            owner = location["node"].get("owner")
            if owner and owner["node"]:
                location["node"]["owner"] = customer(owner["node"]["org_id"]["value"])

    def full_connection(circuit: dict[str, Any]) -> None:
        """The connection of a generated session circuit with what the inventory query selects of it."""
        node = (circuit.get("connection") or {}).get("node")
        if not node:
            return
        row = connections[node["name"]["value"]]
        account = row.item.get("account")
        node["vlan_id"] = {"value": row.item.get("vlan_id")}
        node["owner"] = customer(str(row.item["owner"]) if row.item.get("owner") else None)
        node["account"] = {"node": {"owner": customer(str(accounts[str(account)].item["owner"]))} if account else None}
        if row.item.get("password"):  # the connection puts its key on every session circuit it generates
            circuit["password"] = {"node": {"name": {"value": str(row.item["password"])}}}

    physical = []
    for edge in payloads[CARRIER]["edges"]:
        circuit = edge["node"]
        circuit["circuit_id"] = circuit["name"]
        circuit["circuit_type"] = {"value": "dark_fiber"}
        circuit["status"] = {"value": status[circuit["name"]["value"]]}
        circuit["provider"] = {"node": {"name": {"value": "P016"}}}
        named_owners(circuit)
        circuit["provider_connections"] = _edges([])
        physical.append(circuit)

    passwords = {name(h): h.item["password"] for h in session_circuits(HANDOFF)}
    virtual = []
    for circuit_type in (TUNNEL, INTERCONNECT, HANDOFF):
        for edge in payloads[SESSION]["edges"]:
            circuit = edge["node"]
            if circuit["circuit_type"]["value"] != circuit_type:
                continue
            circuit["__typename"] = SESSION
            named_owners(circuit)
            full_connection(circuit)
            if circuit_type == HANDOFF:
                # An authored handoff names its key; a generated one takes its connection's (full_connection above).
                key = passwords.get(circuit["name"]["value"])
                if key:
                    circuit["password"] = {"node": {"name": {"value": key}}}
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

    return {"TopologyCarrierCircuit": _edges(physical), "TopologySessionCircuit": _edges(virtual)}
