"""Consistency of the circuit data the circuit generators run on.

add_circuit / add_virtual_circuit (generators/topology/circuit.py) only run
for members of their target groups, and build the session a circuit's
peering_role asks for. The loader accepts a circuit outside its group or a
dci circuit the generator would refuse, so both only surface as a missing
session or a failed task after the 30_all load — and a hand-written peering
left next to a generated one is a duplicate session to the same neighbour.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

_DEMOS = Path(__file__).parents[2] / "data" / "demos"
_BOOTSTRAP_GROUPS = Path(__file__).parents[2] / "data" / "bootstrap" / "00_groups.yml"
_TARGET_GROUPS = {"TopologyPhysicalCircuit": "physical_circuits", "TopologyVirtualCircuit": "virtual_circuits"}
_ALL = _DEMOS / "30_all"
_ONRAMP = _ALL / "08_interconnects" / "01_colo_onramp"
_INTERNET = _ALL / "04_internet"
_JUNOS_TEMPLATES = Path(__file__).parents[2] / "data" / "bootstrap" / "10_physical_devices_templates_junos.yaml"
# One fibre per DC to each of the metro's two edges (both are Multi-Site border gateways).
_DCI_CIRCUITS = {
    "DF-DC10-EQXFR2",
    "DF-DC11-EQXFR2",
    "DF-DC12-EQXPA4",
    "DF-DC10-EQXFR2-B",
    "DF-DC11-EQXFR2-B",
    "DF-DC12-EQXPA4-B",
}


def _objects(root: Path, kind: str) -> list[tuple[Path, dict[str, Any]]]:
    """(file, entry) for every data entry of `kind` under root (a directory or one file)."""
    entries: list[tuple[Path, dict[str, Any]]] = []
    for path in [root] if root.is_file() else sorted(root.rglob("*.yml")):
        for doc in yaml.safe_load_all(path.read_text()):
            if isinstance(doc, dict) and doc.get("kind") == "Object" and doc["spec"]["kind"] == kind:
                entries.extend((path, entry) for entry in doc["spec"]["data"])
    return entries


def _nested(node: Any, key: str) -> list[dict[str, Any]]:
    """Every mapping holding `key`, at any depth of a loaded document (nested children included)."""
    if isinstance(node, dict):
        found = [node] if key in node else []
        return found + [hit for value in node.values() for hit in _nested(value, key)]
    if isinstance(node, list):
        return [hit for value in node for hit in _nested(value, key)]
    return []


def _circuit_name(entry: dict[str, Any]) -> str:
    return entry.get("circuit_id") or entry["name"]


@pytest.mark.parametrize("kind", sorted(_TARGET_GROUPS))
def test_every_circuit_is_in_its_generator_target_group(kind: str) -> None:
    """Without membership the generator never runs for the circuit."""
    outside = [
        f"{path.relative_to(_DEMOS)}: {_circuit_name(entry)}"
        for path, entry in _objects(_DEMOS, kind)
        if _TARGET_GROUPS[kind] not in (entry.get("member_of_groups") or [])
    ]
    assert not outside, f"{kind} outside {_TARGET_GROUPS[kind]}: {outside}"


def test_circuit_target_groups_are_generator_groups() -> None:
    """Target groups are CoreGeneratorGroups, like the other generator targets."""
    generator_groups = {
        entry["name"]
        for doc in yaml.safe_load_all(_BOOTSTRAP_GROUPS.read_text())
        if doc and doc["spec"]["kind"] == "CoreGeneratorGroup"
        for entry in doc["spec"]["data"]
    }
    assert set(_TARGET_GROUPS.values()) <= generator_groups


def test_dark_fibres_are_the_dci_circuits() -> None:
    """peering_role is explicit: exactly the six DC <-> cage fibres are dci."""
    dci = {
        _circuit_name(e)
        for _, e in _objects(_DEMOS / "30_all", "TopologyPhysicalCircuit")
        if e.get("peering_role") == "dci"
    }
    assert dci == _DCI_CIRCUITS


@pytest.mark.parametrize("circuit_id", sorted(_DCI_CIRCUITS))
def test_dci_circuit_has_a_border_leaf_and_a_cage_edge_end(circuit_id: str) -> None:
    """add_circuit refuses anything but two endpoints on two devices, one of them a DC device."""
    circuit = next(e for _, e in _objects(_ONRAMP, "TopologyPhysicalCircuit") if e["circuit_id"] == circuit_id)
    ends = [tuple(i) for i in circuit["interface_capabilities"]]
    devices = sorted(device for device, _ in ends)

    assert len(set(ends)) == 2
    assert devices[0].startswith("bl-dc") and devices[1].startswith("eg-")


def test_no_hand_written_dci_session_or_addressing_is_left() -> None:
    """The generator owns the DCI sessions, their /127s and the interface addressing."""
    assert not [
        e["name"] for _, e in _objects(_DEMOS / "30_all", "ManagedBGPPeering") if e.get("peering_role") == "dci"
    ]
    assert not _objects(_ONRAMP, "DcimPhysicalInterface")
    assert not [e["prefix"] for _, e in _objects(_ONRAMP, "IpamPrefix") if e["prefix"].endswith("/127")]


def test_generated_peering_names_do_not_collide_with_data() -> None:
    """DCI-<circuit_id> / <virtual circuit>-bgp share the peering/circuit name space."""
    names = {e["name"] for _, e in _objects(_DEMOS, "ManagedBGPPeering")}
    names |= {_circuit_name(e) for kind in _TARGET_GROUPS for _, e in _objects(_DEMOS, kind)}
    generated = {f"DCI-{c}" for c in _DCI_CIRCUITS}
    generated |= {f"{e['name']}-bgp" for _, e in _objects(_DEMOS, "TopologyVirtualCircuit")}

    assert not generated & names


def test_every_dci_fibre_lands_on_its_own_free_port() -> None:
    """No two fibres share a port: each DC border leaf uses Ethernet1/35 and 1/36, each cage edge its dci ports."""
    ends = [tuple(i) for _, e in _objects(_ONRAMP, "TopologyPhysicalCircuit") for i in e["interface_capabilities"]]
    assert len(ends) == len(set(ends))
    border_leaf_ports = {port for device, port in ends if device.startswith("bl-dc")}
    assert border_leaf_ports == {"Ethernet1/35", "Ethernet1/36"}


def test_both_edges_of_a_metro_share_one_multisite_vip_address() -> None:
    """The two edges of a metro are one Multi-Site site: same /128, so the loader upserts one IpamIPAddress."""
    vips: dict[str, set[str]] = {}
    for _, entry in _objects(_ONRAMP, "DcimVirtualInterface"):
        if entry["role"] == "multisite-vip" and entry["device"].startswith("eg-"):
            vips.setdefault(entry["device"][:-2], set()).add(entry["ip_address"]["data"]["address"])
    assert vips == {"eg-fr": {"fd00:2500:ff::f1/128"}, "eg-pa": {"fd00:2500:ff::f2/128"}}
    devices = [e["device"] for _, e in _objects(_ONRAMP, "DcimVirtualInterface") if e["device"].startswith("eg-")]
    assert sorted(devices) == ["eg-fr01", "eg-fr02", "eg-pa01", "eg-pa02"]


def test_generated_fabric_peering_names_do_not_collide_with_data() -> None:
    """FABRIC-<a>-<b> shares the peering/circuit name space."""
    names = {e["name"] for _, e in _objects(_DEMOS, "ManagedBGPPeering")}
    names |= {_circuit_name(e) for kind in _TARGET_GROUPS for _, e in _objects(_DEMOS, kind)}
    assert not {"FABRIC-eg-fr01-eg-fr02", "FABRIC-eg-pa01-eg-pa02"} & names


def test_every_physical_circuit_row_has_two_distinct_ends_and_no_port_is_in_two_rows() -> None:
    """A physical circuit terminates on exactly two ports (the schema's min/max count), each port on one circuit.

    The INET-<office> circuits are not data rows: add_sdwan_edge creates them with both ends in one write.
    """
    rows = _objects(_ALL, "TopologyPhysicalCircuit")
    assert rows
    seen: dict[tuple[str, str], str] = {}
    for _, row in rows:
        ends = [tuple(end) for end in row["interface_capabilities"]]
        assert len(ends) == 2 and len(set(ends)) == 2, f"{_circuit_name(row)}: {ends}"
        for end in ends:
            assert end not in seen, f"{end} is an end of {seen[end]} and {_circuit_name(row)}"
            seen[end] = _circuit_name(row)


def test_every_cross_connect_far_end_is_a_declared_cloud_provider_edge() -> None:
    """A cross-connect's second port sits on a device the 01_colo_onramp cloud provider edges declare."""
    declared = {e["name"] for _, e in _objects(_ONRAMP / "02_cloud_provider_edges.yml", "DcimPhysicalDevice")}
    cross_connects = [
        e for _, e in _objects(_ONRAMP, "TopologyPhysicalCircuit") if e["circuit_type"] == "cross_connect"
    ]
    assert sorted(_circuit_name(e) for e in cross_connects) == ["XC-EQXFR2-AWS-EUC1", "XC-EQXPA4-AZURE-WEU"]
    for circuit in cross_connects:
        cage_end, cloud_end = circuit["interface_capabilities"]
        assert cage_end[0].startswith("eg-")
        assert cloud_end[0] in declared, f"{_circuit_name(circuit)}: {cloud_end[0]} is not declared"


def _template_ports(name: str, role: str) -> list[str]:
    """The expanded interface names of `role` on a bootstrap device template."""
    (doc,) = [d for d in yaml.safe_load_all(_JUNOS_TEMPLATES.read_text()) if d]
    (template,) = [t for t in doc["spec"]["data"] if t["template_name"] == name]
    ports: list[str] = []
    for iface in template["interfaces"]["data"]:
        if iface["role"] != role:
            continue
        match = re.fullmatch(r"(.*)\[(\d+)-(\d+)\]", iface["name"])
        if match:
            prefix, low, high = match.groups()
            ports.extend(f"{prefix}{n}" for n in range(int(low), int(high) + 1))
        else:
            ports.append(iface["name"])
    return ports


def test_the_orange_access_pe_has_a_customer_port_for_every_sdwan_office() -> None:
    """add_sdwan_edge takes a free `customer` port of the one access PE per office: 8 ports cover the 5 offices."""
    (device,) = [e for _, e in _objects(_INTERNET, "DcimPhysicalDevice")]
    assert (device["name"], device["object_template"], device["deployment"]) == (
        "EXT-ORANGE",
        "MX204_ISP_ACCESS",
        "ISP-ORANGE",
    )
    ports = _template_ports("MX204_ISP_ACCESS", "customer")
    assert ports == [f"xe-0/1/{n}" for n in range(8)]
    sdwan_offices = [
        office
        for path in (_ALL / "06_customer_boarding").rglob("*.yml")
        for doc in yaml.safe_load_all(path.read_text())
        for office in _nested(doc, "sdwan_gateway")
    ]
    assert len(sdwan_offices) == 5
    assert len(ports) >= len(sdwan_offices)


def test_the_orange_isp_topology_is_the_foundation_isp_of_its_access_pe() -> None:
    """The generator resolves the ISP topology of the internet provider (Orange, P019); its one device is the PE."""
    (topology,) = [e for _, e in _objects(_INTERNET / "01_topology.yml", "TopologyExternal")]
    isps = {c["name"]: c for c in topology["children"]["data"]}
    assert isps["ISP-ORANGE"]["provider"] == "P019"
    (device,) = [e for _, e in _objects(_INTERNET, "DcimPhysicalDevice")]
    assert device["deployment"] == "ISP-ORANGE" and device["owner"] == "P019"
