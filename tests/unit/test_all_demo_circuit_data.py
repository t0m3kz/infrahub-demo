"""Consistency of the circuit data the circuit generators run on.

add_circuit (physical circuits) and add_virtual_circuit (Interconnect and Handoff circuits) only run for members of
their target groups. The loader accepts a circuit outside its group, or a handoff the generator would refuse, so both
only surface as a missing session or a failed task after the 30_all load - and a hand-written peering left next to a
generated one is a duplicate session to the same neighbour. A TopologyTunnelCircuit builds nothing and stays out of
the group.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

_DEMOS = Path(__file__).parents[2] / "data" / "demos"
_BOOTSTRAP_GROUPS = Path(__file__).parents[2] / "data" / "bootstrap" / "00_groups.yml"
_TARGET_GROUPS = {
    "TopologyPhysicalCircuit": "physical_circuits",
    "TopologyInterconnectCircuit": "virtual_circuits",
    "TopologyHandoffCircuit": "virtual_circuits",
}
_ALL = _DEMOS / "30_all"
_ONRAMP = _ALL / "08_interconnects" / "01_colo_onramp"
_INTERNET = _ALL / "04_internet"
_JUNOS_TEMPLATES = Path(__file__).parents[2] / "data" / "bootstrap" / "10_physical_devices_templates_junos.yaml"
# One fibre per DC to each of the metro's two edges (both are Multi-Site border gateways), and the Interconnect that
# rides each (the DCI session is built on it).
_DCI_FIBRES = {
    "DF-DC10-EQXFR2": "ICX-DC10-EQXFR2",
    "DF-DC11-EQXFR2": "ICX-DC11-EQXFR2",
    "DF-DC12-EQXPA4": "ICX-DC12-EQXPA4",
    "DF-DC10-EQXFR2-B": "ICX-DC10-EQXFR2-B",
    "DF-DC11-EQXFR2-B": "ICX-DC11-EQXFR2-B",
    "DF-DC12-EQXPA4-B": "ICX-DC12-EQXPA4-B",
}
_DCI_CIRCUITS = set(_DCI_FIBRES)


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
    rows = _objects(_DEMOS, kind)
    assert rows
    outside = [
        f"{path.relative_to(_DEMOS)}: {_circuit_name(entry)}"
        for path, entry in rows
        if _TARGET_GROUPS[kind] not in (entry.get("member_of_groups") or [])
    ]
    assert not outside, f"{kind} outside {_TARGET_GROUPS[kind]}: {outside}"


def test_a_tunnel_circuit_is_in_no_generator_group() -> None:
    """The SD-WAN orchestrator owns a tunnel's routing: no session is generated, so it is not a group member."""
    rows = _objects(_DEMOS, "TopologyTunnelCircuit")
    assert [_circuit_name(entry) for _, entry in rows] == ["C001-PARTNER-ACME-FR2"]
    assert not [_circuit_name(entry) for _, entry in rows if entry.get("member_of_groups")]


def test_no_virtual_circuit_is_declared_as_the_generic() -> None:
    """The generic TopologyVirtualCircuit has no data row: the role of a virtual circuit is its kind."""
    assert not _objects(_DEMOS, "TopologyVirtualCircuit")


def test_circuit_target_groups_are_generator_groups() -> None:
    """Target groups are CoreStandardGroups, like the other generator targets (the SDK tracks runs in its own groups)."""
    generator_groups = {
        entry["name"]
        for doc in yaml.safe_load_all(_BOOTSTRAP_GROUPS.read_text())
        if doc and doc["spec"]["kind"] == "CoreStandardGroup"
        for entry in doc["spec"]["data"]
    }
    assert set(_TARGET_GROUPS.values()) <= generator_groups


_REMOVED_CIRCUIT_KEYS = ("peering_role", "link_type", "transport_mode", "tunnel_id", "bandwidth", "encryption", "vni")


@pytest.mark.parametrize("kind", ["TopologyTunnelCircuit", "TopologyInterconnectCircuit", "TopologyHandoffCircuit"])
def test_a_virtual_circuit_row_uses_no_removed_attribute(kind: str) -> None:
    """The role is the kind: no `peering_role`, `link_type`, `transport_mode`, `tunnel_id`, and no carrier key."""
    for _, entry in _objects(_DEMOS, kind):
        assert not [key for key in (*_REMOVED_CIRCUIT_KEYS, "physical_circuits") if key in entry], _circuit_name(entry)


def test_no_physical_circuit_names_a_peering_role() -> None:
    """A physical circuit is a pure carrier: the DCI session belongs to the Interconnect that rides it."""
    assert _objects(_DEMOS / "30_all", "TopologyPhysicalCircuit")
    assert not [
        _circuit_name(e) for _, e in _objects(_DEMOS / "30_all", "TopologyPhysicalCircuit") if "peering_role" in e
    ]


def test_the_dark_fibres_are_the_six_dci_carriers() -> None:
    """Exactly the six DC <-> cage fibres are dark fibre, and each has one Interconnect with the same two ports."""
    fibres = {
        _circuit_name(e): e
        for _, e in _objects(_DEMOS / "30_all", "TopologyPhysicalCircuit")
        if e["circuit_type"] == "dark_fiber"
    }
    assert set(fibres) == _DCI_CIRCUITS
    interconnects = {e["name"]: e for _, e in _objects(_DEMOS / "30_all", "TopologyInterconnectCircuit")}
    assert set(interconnects) == set(_DCI_FIBRES.values())
    for fibre_id, interconnect in _DCI_FIBRES.items():
        assert interconnects[interconnect]["capabilities"] == fibres[fibre_id]["capabilities"]
        # the fibre lists its cage (the DC is derived), the Interconnect both facilities
        assert fibres[fibre_id]["locations"][0] in interconnects[interconnect]["locations"]
        assert len(set(interconnects[interconnect]["locations"])) == 2
        assert interconnects[interconnect]["provider"] == fibres[fibre_id]["provider"]
        assert "owner" not in interconnects[interconnect]


@pytest.mark.parametrize("circuit_id", sorted(_DCI_CIRCUITS))
def test_dci_circuit_has_a_border_leaf_and_a_cage_edge_end(circuit_id: str) -> None:
    """add_circuit refuses anything but two endpoints on two devices, one of them a DC device."""
    circuit = next(e for _, e in _objects(_ONRAMP, "TopologyPhysicalCircuit") if e["circuit_id"] == circuit_id)
    ends = [tuple(i) for i in circuit["capabilities"]]
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


def test_generated_peering_and_key_names_do_not_collide_with_data() -> None:
    """The session generator names its objects after the circuit: `<name>-underlay` and `<name>-evpn` (Interconnect),
    `<name>-bgp` (Handoff), `<name>-dci-key`. They share the peering/circuit name space, so no row may reuse them."""
    names = {e["name"] for _, e in _objects(_DEMOS, "ManagedBGPPeering")}
    names |= {_circuit_name(e) for kind in _TARGET_GROUPS for _, e in _objects(_DEMOS, kind)}
    names |= {e["name"] for _, e in _objects(_DEMOS, "TopologyTunnelCircuit")}
    names |= {e["name"] for _, e in _objects(_DEMOS, "RoutingPassword")}
    generated: set[str] = set()
    for _, e in _objects(_DEMOS, "TopologyInterconnectCircuit"):
        generated |= {f"{e['name']}-underlay", f"{e['name']}-evpn", f"{e['name']}-dci-key"}
    generated |= {f"{e['name']}-bgp" for _, e in _objects(_DEMOS, "TopologyHandoffCircuit")}

    assert generated
    assert not generated & names


def test_every_dci_fibre_lands_on_its_own_free_port() -> None:
    """No two fibres share a port: each DC border leaf uses Ethernet1/35 and 1/36, each cage edge its dci ports."""
    ends = [tuple(i) for _, e in _objects(_ONRAMP, "TopologyPhysicalCircuit") for i in e["capabilities"]]
    assert len(ends) == len(set(ends))
    border_leaf_ports = {port for device, port in ends if device.startswith("bl-dc")}
    assert border_leaf_ports == {"Ethernet1/35", "Ethernet1/36"}


def test_both_edges_of_a_metro_share_one_multisite_vip_address() -> None:
    """The two edges of a metro are one Multi-Site site: same /128, so the loader upserts one IpamIPAddress."""
    vips: dict[str, set[str]] = {}
    for _, entry in _objects(_ONRAMP, "DcimVirtualInterface"):
        if entry["role"] == "multisite-vip" and entry["device"].startswith("eg-"):
            vips.setdefault(entry["device"][:-2], set()).add(entry["ip_address"]["data"]["address"])
    assert vips == {"eg-fr2": {"fd00:2500:ff::f1/128"}, "eg-pa4": {"fd00:2500:ff::f2/128"}}
    devices = [e["device"] for _, e in _objects(_ONRAMP, "DcimVirtualInterface") if e["device"].startswith("eg-")]
    assert sorted(devices) == ["eg-fr201", "eg-fr202", "eg-pa401", "eg-pa402"]


def test_generated_fabric_peering_names_do_not_collide_with_data() -> None:
    """FABRIC-<a>-<b> shares the peering/circuit name space."""
    names = {e["name"] for _, e in _objects(_DEMOS, "ManagedBGPPeering")}
    names |= {_circuit_name(e) for kind in _TARGET_GROUPS for _, e in _objects(_DEMOS, kind)}
    assert not {"FABRIC-eg-fr201-eg-fr202", "FABRIC-eg-pa401-eg-pa402"} & names


def test_every_physical_circuit_row_has_two_distinct_ends_and_no_port_is_in_two_rows() -> None:
    """A physical circuit terminates on exactly two ports (the schema's min/max count), each port on one circuit.

    The INET-<office> circuits are not data rows: add_sdwan_edge creates them with both ends in one write.
    """
    rows = _objects(_ALL, "TopologyPhysicalCircuit")
    assert rows
    seen: dict[tuple[str, str], str] = {}
    for _, row in rows:
        ends = [tuple(end) for end in row["capabilities"]]
        assert len(ends) == 2 and len(set(ends)) == 2, f"{_circuit_name(row)}: {ends}"
        for end in ends:
            assert end not in seen, f"{end} is an end of {seen[end]} and {_circuit_name(row)}"
            seen[end] = _circuit_name(row)


def test_every_cross_connect_far_end_is_a_declared_cloud_provider_edge() -> None:
    """A cross-connect's second port sits on a stub device the 01_colo_onramp provider edges declare."""
    declared = {e["name"] for _, e in _objects(_ONRAMP / "02_cloud_provider_edges.yml", "DcimPhysicalDevice")}
    cross_connects = [
        e for _, e in _objects(_ONRAMP, "TopologyPhysicalCircuit") if e["circuit_type"] == "cross_connect"
    ]
    assert sorted(_circuit_name(e) for e in cross_connects) == [
        "XC-EQXFR2-AWS-EUC1",
        "XC-EQXPA4-AZURE-WEU",
        "XC-EQXPA4-MEGAPORT",
    ]
    for circuit in cross_connects:
        cage_end, cloud_end = circuit["capabilities"]
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
    isps = {e["name"]: e for _, e in _objects(_INTERNET / "01_topology.yml", "TopologyExternalProviders")}
    assert {isp["parent"] for isp in isps.values()} == {"Internet"}
    assert isps["ISP-ORANGE"]["provider"] == "P019"
    (device,) = [e for _, e in _objects(_INTERNET, "DcimPhysicalDevice")]
    assert device["deployment"] == "ISP-ORANGE" and device["owner"] == "P019"


# ---------------------------------------------------------------------------
# What add_virtual_circuit reads for a Handoff
# ---------------------------------------------------------------------------

_SUBINTERFACE = re.compile(r"^(?P<parent>.+)\.(?P<vlan>\d+)$")
_HANDOFF_POOLS = _DEMOS.parent / "bootstrap" / "26_handoff_pools.yml"
_NAMESPACES = _DEMOS.parent / "bootstrap" / "22_namespaces.yml"
_STUB_LOOPBACKS = "10.255.64."


def _handoffs() -> list[dict[str, Any]]:
    return [entry for _, entry in _objects(_ALL, "TopologyHandoffCircuit")]


def _sub_interfaces() -> dict[tuple[str, str], dict[str, Any]]:
    return {(e["device"], e["name"]): e for _, e in _objects(_ALL, "DcimVirtualInterface")}


def _declared_devices() -> set[str]:
    """Devices the 30_all files declare by hand (the generated ones, edges and NE routers, are not in the data)."""
    return {e["name"] for _, e in _objects(_ALL, "DcimPhysicalDevice")}


def test_every_handoff_names_a_namespace_with_a_pool() -> None:
    """The /31 comes from `Handoff-<namespace>-IPv4` (virtual_circuit.handoff_pool_name): both must exist."""
    namespaces = {e["name"] for _, e in _objects(_NAMESPACES, "IpamNamespace")}
    pools = {e["name"] for _, e in _objects(_HANDOFF_POOLS, "CoreIPPrefixPool")}
    assert _handoffs()
    for handoff in _handoffs():
        assert handoff["ip_namespace"] in namespaces, handoff["name"]
        assert f"Handoff-{handoff['ip_namespace']}-IPv4" in pools, handoff["name"]


def test_every_handoff_end_is_a_declared_sub_interface_of_one_vlan_and_carries_no_address() -> None:
    """Both ends are `<parent>.<vlan>` DcimVirtualInterfaces on the same VLAN whose parent is the named physical port;
    the addresses are generated, so none is authored."""
    sub_interfaces = _sub_interfaces()
    for handoff in _handoffs():
        vlans = set()
        assert len(handoff["capabilities"]) == 2
        for device, port in handoff["capabilities"]:
            match = _SUBINTERFACE.match(port)
            assert match, f"{handoff['name']}: {device} {port} is not <parent>.<vlan>"
            vlans.add(match["vlan"])
            sub_interface = sub_interfaces[(device, port)]
            assert sub_interface["parent_interface"] == [device, match["parent"]], handoff["name"]
            assert "ip_address" not in sub_interface, f"{handoff['name']}: {device} {port} is addressed by hand"
        assert len(vlans) == 1, handoff["name"]


def test_every_authored_handoff_password_is_a_declared_key() -> None:
    """A handoff's optional key refers to a RoutingPassword the data declares."""
    keys = {e["name"] for _, e in _objects(_ALL, "RoutingPassword")}
    for handoff in _handoffs():
        assert handoff.get("password") in keys, handoff["name"]


def test_every_party_device_of_a_handoff_has_exactly_one_bgp_process_with_a_declared_asn_and_router_id() -> None:
    """The session generator takes the party's ONLY ManagedBGP process (a stub has no overlay one); its local_as is the
    party ASN and its router_id a loopback of the device. Our edge and the NE routers are generated, not declared."""
    declared_devices = _declared_devices()
    processes = _objects(_ALL, "ManagedBGP")
    asns = {f"AS{e['asn']}" for _, e in _objects(_ALL, "RoutingAutonomousSystem")}
    loopbacks = {
        (e["device"], e["ip_address"]["data"]["address"], e["ip_address"]["data"]["ip_namespace"])
        for _, e in _objects(_ALL, "DcimVirtualInterface")
        if e.get("role") == "loopback"
    }
    parties = {device for handoff in _handoffs() for device, _ in handoff["capabilities"] if device in declared_devices}
    assert parties == {"EXT-AWS-DX-FR2", "EQX-FR2-SDWAN-GW1", "EXT-MEGAPORT-MCR1"}
    for device in parties:
        own = [entry for _, entry in processes if device in entry["capabilities"]]
        assert len(own) == 1, f"{device}: {[e['name'] for e in own]}"
        assert own[0]["local_as"] in asns, device
        address, namespace = own[0]["router_id"]
        assert (device, address, namespace) in loopbacks, device
        assert address.startswith(_STUB_LOOPBACKS) and address.endswith("/32"), device


def test_the_stub_loopbacks_are_distinct_and_in_the_reserved_range() -> None:
    """10.255.64.0/24 holds the router-ids of the party stubs, clear of the handoff pools and every authored range."""
    addresses = [
        e["ip_address"]["data"]["address"]
        for _, e in _objects(_ALL, "DcimVirtualInterface")
        if e["device"].startswith(("EXT-", "EQX-FR2-SDWAN-GW")) and e.get("role") == "loopback"
    ]
    assert sorted(addresses) == ["10.255.64.1/32", "10.255.64.2/32", "10.255.64.3/32"]


def test_a_cloud_endpoint_is_set_from_its_vif_and_mirrors_the_sub_interface_vlan() -> None:
    """A VIF names its one handoff (`virtual_circuit`); the handoff cannot name the VIF back. The VIF's vlan_id is the
    sub-interface VLAN, and a handoff has at most one VIF."""
    handoffs = {h["name"]: h for h in _handoffs()}
    vifs = [e for _, e in _objects(_ALL, "CloudVirtualInterface")]
    assert vifs
    circuits = [vif["virtual_circuit"] for vif in vifs]
    assert len(circuits) == len(set(circuits))
    for vif in vifs:
        handoff = handoffs[vif["virtual_circuit"]]
        assert "cloud_endpoint" not in handoff
        for _, port in handoff["capabilities"]:
            assert port.rpartition(".")[2] == str(vif["vlan_id"]), vif["name"]


def test_provider_stubs_deployed_to_a_zone_are_in_the_provider_stubs_group() -> None:
    """A stub router in a colocation zone has the `edge` role of its template: only the group keeps it out of the
    border-gateway lookup of generators/vlan_domain.py (a stretched segment would demand a VLAN domain for it)."""
    from tests.integration.demo_data import ROOT, declared, name

    root = ROOT / "data" / "demos" / "30_all"
    zones = {
        name(zone) for kind in ("TopologyColocationZone", "TopologyVirtualColocation") for zone in declared(kind, root)
    }
    stubs = [
        device
        for device in declared("DcimPhysicalDevice", root)
        if name(device).startswith("EXT-") and device.item.get("deployment") in zones
    ]
    assert {name(device) for device in stubs} == {"EXT-EQX-FABRIC-FR2", "EXT-MEGAPORT-MCR1"}
    assert [name(device) for device in stubs if "provider_stubs" not in device.groups()] == []
