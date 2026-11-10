"""The raw response of ``tests/integration/queries/get_interconnect_inventory.gql`` over the 30_all data.

Built on the simulator of the placement check (``circuit_data_payloads``, shaped like ``circuit_placement.gql``) and
completed with what the inventory query selects on top: the carrier circuits' ``circuit_id`` and status, the owners (a
circuit has none: the office locations carry them, as customers),
each end device's deployment, the interface kind, the parent of a sub-interface, a handoff's key and a declared
tunnel's VRF. No
address is set (nothing generated it offline): ``addresses`` puts them on named ports.
"""

from __future__ import annotations

from typing import Any

from tests.integration.demo_data import declared, distinct, name
from tests.unit.external_vrf_peers_helpers import _raw_vrf
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

    physical = []
    for edge in payloads[CARRIER]["edges"]:
        circuit = edge["node"]
        circuit["circuit_id"] = circuit["name"]
        circuit["circuit_type"] = {"value": "dark_fiber"}
        circuit["status"] = {"value": status[circuit["name"]["value"]]}
        circuit["provider"] = {"node": {"name": {"value": "P016"}}}
        named_owners(circuit)
        physical.append(circuit)

    passwords = {name(h): h.item["password"] for h in session_circuits(HANDOFF)}
    tunnel_vrfs = {name(t): t.item.get("vrf") for t in session_circuits(TUNNEL)}
    virtual = []
    for circuit_type in (TUNNEL, INTERCONNECT, HANDOFF):
        for edge in payloads[SESSION]["edges"]:
            circuit = edge["node"]
            if circuit["circuit_type"]["value"] != circuit_type:
                continue
            circuit["__typename"] = SESSION
            named_owners(circuit)
            vrf = tunnel_vrfs.get(circuit["name"]["value"]) if circuit_type == TUNNEL else None
            if vrf:
                # A declared tunnel that names its VRF (the partner tunnel: its registered range is routed there).
                circuit["vrf"] = {"node": _raw_vrf(str(vrf[1]), str(vrf[0]))}
            if circuit_type == HANDOFF:
                # An authored handoff names its key.
                key = passwords.get(circuit["name"]["value"])
                if key:
                    circuit["password"] = {"node": {"name": {"value": key}}}
            for port_edge in circuit["capabilities"]["edges"]:
                port = port_edge["node"]
                device = port["device"]["node"]["name"]["value"]
                port["device"]["node"].update(model.placement(device))
                if port.get("__typename") != "CloudPeeringInterface":
                    port["__typename"] = (
                        "DcimVirtualInterface" if "parent_interface" in port else "DcimPhysicalInterface"
                    )
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
