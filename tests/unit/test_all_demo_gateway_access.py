"""The shared SD-WAN gateway has its own physical internet connection (30_all/08_interconnects/08_gateway_access).

The gateway EQX-FR2-SDWAN-GW1 has its own internet connection besides its trunk eth0: a second port, eth1, and an
internet circuit to Lumen's Frankfurt edge. It feeds no generator step (the overlay session is built from the
ports' own underlay); it models the gateway's real access. The trunk stays the gateway's single role=uplink port
(generators/topology/sdwan_edge.py refuses several), so eth1 must not be one.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from tests.integration.demo_data import declared, name
from tests.integration.test_constants import (
    ALL_DEMO_GATEWAY_ACCESS_CIRCUITS,
    ALL_DEMO_INTERNET_TRANSIT_CIRCUITS,
    ALL_DEMO_PHYSICAL_CIRCUIT_TYPES,
    ALL_DEMO_SDWAN_UNDERLAY_CIRCUITS,
)

_ROOT = Path(__file__).parents[2]
_ALL = _ROOT / "data" / "demos" / "30_all"
_GATEWAY_DIR = _ALL / "08_interconnects" / "08_gateway_access"
_TEMPLATES = _ROOT / "data" / "bootstrap" / "10_physical_devices_templates_velocloud.yaml"
_JUNOS_TEMPLATES = _ROOT / "data" / "bootstrap" / "10_physical_devices_templates_junos.yaml"
_GATEWAY = "EQX-FR2-SDWAN-GW1"
_CIRCUIT = "INET-LUMEN-EQXFR2-GW"
_ISP_PE = "EXT-LUMEN-FRA1"


def _entries(path: Path, kind: str) -> list[dict[str, Any]]:
    """The data entries of every document of `kind` in one YAML file."""
    return [
        entry
        for doc in yaml.safe_load_all(path.read_text())
        if doc and doc.get("spec", {}).get("kind") == kind
        for entry in doc["spec"]["data"]
    ]


def _circuit() -> dict[str, Any]:
    """The gateway access circuit as the data declares it."""
    (circuit,) = [c for path in sorted(_GATEWAY_DIR.glob("*.yml")) for c in _entries(path, "TopologyPhysicalCircuit")]
    return circuit


def _template_ports(path: Path, template: str) -> dict[str, dict[str, Any]]:
    """Port name -> template row, for the physical interfaces of one device template (`[0-3]` ranges expanded)."""
    (row,) = [r for r in _entries(path, "TemplateDcimPhysicalDevice") if r["template_name"] == template]
    ports: dict[str, dict[str, Any]] = {}
    for port in row["interfaces"]["data"]:
        match = re.fullmatch(r"(.*)\[(\d+)-(\d+)\](.*)", port["name"])
        names = (
            [f"{match[1]}{n}{match[4]}" for n in range(int(match[2]), int(match[3]) + 1)] if match else [port["name"]]
        )
        ports.update({name: port for name in names})
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
    ports = _template_ports(_TEMPLATES, "VCG-VA_GATEWAY")

    assert sorted(ports) == ["eth0", "eth1"]
    assert [name for name, port in ports.items() if port["role"] == "uplink"] == ["eth0"]
    assert ports["eth1"]["role"] == "customer"
    assert ports["eth1"]["template_name"] == "VCG-VA_GATEWAY_eth1"
    assert ports["eth1"]["interface_type"] == "1000base-t"


def test_gateway_access_circuit_is_an_unowned_internet_circuit_in_the_generator_group() -> None:
    """Shared underlay: internet, active, Lumen, no owner, in physical_circuits (add_circuit's target group)."""
    circuit = _circuit()

    assert circuit["circuit_id"] == circuit["name"] == _CIRCUIT
    assert circuit["circuit_type"] == "internet"
    assert circuit["status"] == "active"
    assert circuit["provider"] == "P014"
    assert "owner" not in circuit
    assert circuit["member_of_groups"] == ["physical_circuits"]


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
        for row in _entries(_ALL / "08_interconnects" / "06_internet" / "02_devices.yml", "DcimPhysicalDevice")
        if row["name"] == _ISP_PE
    )
    assert _template_ports(_JUNOS_TEMPLATES, pe_template)[port]["role"] == "customer"
    assert set(ALL_DEMO_GATEWAY_ACCESS_CIRCUITS[_CIRCUIT]) == set(ends)


def test_gateway_access_ports_are_used_by_no_other_circuit_peering_or_address() -> None:
    """Both ends are free: the pair appears once in all of 30_all (this circuit) and the ISP port is not et-0/0/0."""
    ends = [tuple(end) for end in _circuit()["capabilities"]]
    docs = [doc for path in sorted(_ALL.rglob("*.yml")) for doc in yaml.safe_load_all(path.read_text()) if doc]

    for device, port in ends:
        assert sum(_uses(doc, device, port) for doc in docs) == 1, f"{device} {port} is used more than once"
    assert ends[1][1] != "et-0/0/0"


def test_gateway_access_is_a_modelled_simplification_without_peering() -> None:
    """No peering_role, so add_circuit builds no session: nothing is addressed or peered for this access."""
    circuit = _circuit()

    assert "peering_role" not in circuit
    assert not any(
        _CIRCUIT in str(peering) for path in _ALL.rglob("*.yml") for peering in _entries(path, "ManagedBGPPeering")
    )


def test_gateway_access_is_not_in_the_transit_folder_nor_counted_as_transit() -> None:
    """06_internet pairs every circuit with a transit peering (and tests it); the gateway access is separate."""
    assert _CIRCUIT not in ALL_DEMO_INTERNET_TRANSIT_CIRCUITS
    assert not (_ALL / "08_interconnects" / "06_internet" / "01_circuit.yml").exists()
    assert not [
        c
        for path in (_ALL / "08_interconnects" / "06_internet").glob("*.yml")
        for c in _entries(path, "TopologyPhysicalCircuit")
        if c["circuit_id"] == _CIRCUIT
    ]


def test_internet_circuit_count_is_transit_plus_gateway_access_plus_one_per_office() -> None:
    """The expected internet count follows the data: the 06_internet transit, the gateway access, an INET-<office> each."""
    transit = {
        c["circuit_id"]
        for path in (_ALL / "08_interconnects" / "06_internet").glob("*.yml")
        for c in _entries(path, "TopologyPhysicalCircuit")
    }
    offices = {name(o) for o in declared("TopologyCustomerOffice", _ALL) if o.item.get("sdwan_gateway")}
    assert transit == ALL_DEMO_INTERNET_TRANSIT_CIRCUITS
    assert set(ALL_DEMO_SDWAN_UNDERLAY_CIRCUITS) == {f"INET-{office}" for office in offices}
    assert offices
    assert ALL_DEMO_PHYSICAL_CIRCUIT_TYPES["internet"] == len(transit) + 1 + len(offices)
