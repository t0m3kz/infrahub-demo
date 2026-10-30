"""The shared SD-WAN gateway has its own physical internet connection (30_all/08_interconnects/08_gateway_access).

The gateway EQX-FR2-SDWAN-GW1 has its own internet connection besides its cable eth0: a second port, eth1, and an
internet circuit to Lumen's Frankfurt edge. It feeds no generator step; it is the carrier of the gateway's public port
eth1, which every tunnel ends on (checks/circuit_placement.py wants each tunnel end on an internet circuit with an ISP
end). eth0 stays the gateway's single role=uplink port, so eth1 must not be one.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from tests.integration.all_demo_circuit_constants import (
    ALL_DEMO_CARRIER_CIRCUIT_TYPES,
    ALL_DEMO_GATEWAY_ACCESS_CIRCUITS,
    ALL_DEMO_INTERNET_TRANSIT_CIRCUITS,
    ALL_DEMO_SDWAN_UNDERLAY_CIRCUITS,
)
from tests.integration.demo_data import ROOT, declared, name, object_documents, object_templates

_ALL = ROOT / "data" / "demos" / "30_all"
_GATEWAY_DIR = _ALL / "08_interconnects" / "08_gateway_access"
_INTERNET_DIR = _ALL / "08_interconnects" / "06_internet"
_GATEWAY = "EQX-FR2-SDWAN-GW1"
_CIRCUIT = "INET-LUMEN-EQXFR2-GW"
_ISP_PE = "EXT-LUMEN-FRA1"


def _entries(kind: str, *paths: Path) -> list[dict[str, Any]]:
    """The data entries of `kind` the object files under `paths` (directories or single files) declare."""
    return [obj.item for obj in declared(kind, *paths)]


def _circuit() -> dict[str, Any]:
    """The gateway access circuit as the data declares it."""
    (circuit,) = _entries("TopologyCarrierCircuit", _GATEWAY_DIR)
    return circuit


def _template_ports(template: str) -> dict[str, dict[str, Any]]:
    """Port name -> template row, for the physical interfaces of one device template (`[0-3]` ranges expanded)."""
    ports: dict[str, dict[str, Any]] = {}
    for port in object_templates()[template]["interfaces"]["data"]:
        match = re.fullmatch(r"(.*)\[(\d+)-(\d+)\](.*)", port["name"])
        names = (
            [f"{match[1]}{n}{match[4]}" for n in range(int(match[2]), int(match[3]) + 1)] if match else [port["name"]]
        )
        ports.update({port_name: port for port_name in names})
    return ports


def _uses(node: Any, device: str, port: str) -> int:
    """How many times the (device, port) pair appears in a loaded document: a [device, port] list, or an
    interface/address entry naming the device and the port."""
    if isinstance(node, list):
        here = int(len(node) == 2 and node[0] == device and node[1] == port)
        return here + sum(_uses(item, device, port) for item in node)
    if isinstance(node, dict):
        here = int(node.get("device") == device and port in (node.get("name"), node.get("interface")))
        return here + sum(_uses(value, device, port) for value in node.values())
    return 0


def test_gateway_template_has_exactly_one_uplink_and_the_new_eth1() -> None:
    """eth0 stays the single role=uplink trunk; eth1 is the gateway's own internet port and not an uplink."""
    ports = _template_ports("VCG-VA_GATEWAY")

    assert sorted(ports) == ["eth0", "eth1"]
    assert [name for name, port in ports.items() if port["role"] == "uplink"] == ["eth0"]
    assert ports["eth1"]["role"] == "customer"
    assert ports["eth1"]["template_name"] == "VCG-VA_GATEWAY_eth1"
    assert ports["eth1"]["interface_type"] == "1000base-t"


def test_gateway_access_circuit_is_an_unowned_internet_circuit_in_the_generator_group() -> None:
    """Shared underlay: internet, active, Lumen, no owner, in carrier_circuits (add_circuit's target group)."""
    circuit = _circuit()

    assert circuit["circuit_id"] == circuit["name"] == _CIRCUIT
    assert circuit["circuit_type"] == "internet"
    assert circuit["status"] == "active"
    assert circuit["provider"] == "P014"
    assert "owner" not in circuit
    assert circuit["member_of_groups"] == ["carrier_circuits"]


def test_gateway_access_circuit_lists_only_the_cage() -> None:
    """add_circuit derives the ISP end from the provider edge's deployment, so only the cage is authored."""
    assert _circuit()["locations"] == ["FR2"]


def test_gateway_access_circuit_ends_on_the_gateway_eth1_and_a_customer_port_of_the_isp_edge() -> None:
    """One end is the gateway's eth1, the other an et-0/0/N port of the Lumen edge, a customer-role port of its template."""
    ends = [tuple(end) for end in _circuit()["capabilities"]]
    assert len(set(ends)) == 2
    assert ends[0] == (_GATEWAY, "eth1")
    device, port = ends[1]
    assert device == _ISP_PE

    pe_template = next(
        row["object_template"]
        for row in _entries("DcimPhysicalDevice", _INTERNET_DIR / "02_devices.yml")
        if row["name"] == _ISP_PE
    )
    assert _template_ports(pe_template)[port]["role"] == "customer"
    assert set(ALL_DEMO_GATEWAY_ACCESS_CIRCUITS[_CIRCUIT]) == set(ends)


def test_gateway_access_ports_are_used_by_no_other_circuit_peering_or_address() -> None:
    """Both ends are free: the pair appears once in all of 30_all (this circuit) and the ISP port is not et-0/0/0.

    A tunnel circuit is no other user: the gateway's public port eth1 is the end every tunnel shares.
    """
    ends = [tuple(end) for end in _circuit()["capabilities"]]
    docs = [
        doc
        for _, doc in object_documents(_ALL)
        if not (
            doc["spec"]["kind"] == "TopologySessionCircuit"
            and any(entry.get("circuit_type") == "tunnel" for entry in doc["spec"]["data"])
        )
    ]

    for device, port in ends:
        assert sum(_uses(doc, device, port) for doc in docs) == 1, f"{device} {port} is used more than once"
    assert ends[1][1] != "et-0/0/0"


def test_gateway_access_is_a_modelled_simplification_without_peering() -> None:
    """No peering_role, so add_circuit builds no session: nothing is addressed or peered for this access."""
    circuit = _circuit()

    assert "peering_role" not in circuit
    assert not any(_CIRCUIT in str(peering) for peering in _entries("ManagedBGPPeering", _ALL))


def test_gateway_access_is_not_in_the_transit_folder_nor_counted_as_transit() -> None:
    """06_internet pairs every circuit with a transit peering (and tests it); the gateway access is separate."""
    assert _CIRCUIT not in ALL_DEMO_INTERNET_TRANSIT_CIRCUITS
    assert not (_INTERNET_DIR / "01_circuit.yml").exists()
    assert not [c for c in _entries("TopologyCarrierCircuit", _INTERNET_DIR) if c["circuit_id"] == _CIRCUIT]


def test_internet_circuit_count_is_transit_plus_two_accesses_plus_one_per_office() -> None:
    """The expected internet count follows the data: the 06_internet transit, the gateway's and the partner's access,
    an INET-<office> each."""
    transit = {c["circuit_id"] for c in _entries("TopologyCarrierCircuit", _INTERNET_DIR)}
    offices = {name(o) for o in declared("TopologyCustomerOffice", _ALL) if o.item.get("sdwan_gateway")}
    assert transit == ALL_DEMO_INTERNET_TRANSIT_CIRCUITS
    assert set(ALL_DEMO_SDWAN_UNDERLAY_CIRCUITS) == {f"INET-{office}" for office in offices}
    assert offices
    assert ALL_DEMO_CARRIER_CIRCUIT_TYPES["internet"] == len(transit) + 1 + 1 + len(offices)
