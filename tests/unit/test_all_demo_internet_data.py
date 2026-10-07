"""Consistency of the 30_all internet transit data (data/demos/30_all/08_interconnects/06_internet).

The loader accepts these files as long as every reference resolves, so a
peering wired to the wrong circuit's interfaces, a missing MD5 key, or an
address outside the INTERNET namespace would only surface as a broken config
artifact or a failed edge check in the Proposed Change.
"""

from __future__ import annotations

import ipaddress
from pathlib import Path
from typing import Any

import pytest
import yaml

_DIR = Path(__file__).parents[2] / "data" / "demos" / "30_all" / "08_interconnects" / "06_internet"
_CAGE_EDGES = {"eg-fr01", "eg-fr02", "eg-pa01", "eg-pa02"}


def _objects(kind: str) -> list[dict[str, Any]]:
    """Return the data entries of every document of `kind` in the directory."""
    entries: list[dict[str, Any]] = []
    for path in sorted(_DIR.glob("*.yml")):
        for doc in yaml.safe_load_all(path.read_text()):
            if doc and doc["spec"]["kind"] == kind:
                entries.extend(doc["spec"]["data"])
    return entries


def _interface_ips() -> dict[tuple[str, str], ipaddress.IPv4Interface]:
    """Map (device, interface) to its transit address."""
    ips = {}
    for iface in _objects("DcimPhysicalInterface"):
        assert iface["ip_address"]["data"]["ip_namespace"] == "INTERNET", iface
        ips[(iface["device"], iface["name"])] = ipaddress.IPv4Interface(iface["ip_address"]["data"]["address"])
    return ips


def _peerings() -> dict[str, dict[str, Any]]:
    """Map circuit id to its peering: TRANSIT-<isp>-<site> rides INET-<isp>-<site>."""
    return {p["name"].replace("TRANSIT-", "INET-", 1): p for p in _objects("ManagedBGPPeering")}


def _circuits() -> dict[str, dict[str, Any]]:
    return {c["circuit_id"]: c for c in _objects("TopologyPhysicalCircuit")}


def test_every_circuit_has_a_peering_and_vice_versa() -> None:
    """One transit eBGP session per transit circuit, named after it."""
    assert set(_peerings()) == set(_circuits())
    assert all(p["name"].startswith("TRANSIT-") for p in _objects("ManagedBGPPeering"))


def test_peering_names_do_not_collide_with_circuits() -> None:
    """Peerings and circuits share the ManagedGeneric name HFID, so their names must differ."""
    names = {p["name"] for p in _objects("ManagedBGPPeering")}
    assert not names & {c["name"] for c in _objects("TopologyPhysicalCircuit")}
    assert len(_circuits()) == 4


@pytest.mark.parametrize("circuit_id", sorted(_circuits()))
def test_peering_rides_its_circuit(circuit_id: str) -> None:
    """The session's two interfaces are the circuit's cage and ISP ends."""
    circuit = _circuits()[circuit_id]
    peering = _peerings()[circuit_id]
    ends = [tuple(i) for i in circuit["customer_interfaces"] + circuit["provider_interfaces"]]
    assert [tuple(i) for i in peering["interface_capabilities"]] == ends


@pytest.mark.parametrize("circuit_id", sorted(_circuits()))
def test_transit_link_is_one_31_with_isp_on_even(circuit_id: str) -> None:
    """Both ends share a /31 in INTERNET and the ISP takes the even address."""
    circuit = _circuits()[circuit_id]
    ips = _interface_ips()
    cage = ips[tuple(circuit["customer_interfaces"][0])]
    isp = ips[tuple(circuit["provider_interfaces"][0])]
    assert cage.network == isp.network
    assert cage.network.prefixlen == 31
    assert isp.ip == cage.network.network_address
    assert circuit["customer_interfaces"][0][0] in _CAGE_EDGES


def test_transit_networks_do_not_overlap() -> None:
    """Every /31 is used by exactly one circuit and declared as a prefix."""
    networks = [ip.network for ip in _interface_ips().values()]
    assert len(set(networks)) == len(_circuits())
    declared = {ipaddress.IPv4Network(p["prefix"]) for p in _objects("IpamPrefix")}
    assert set(networks) <= declared


@pytest.mark.parametrize("circuit_id", sorted(_circuits()))
def test_peering_has_a_declared_password(circuit_id: str) -> None:
    """checks/edge.py requires a password on every peering of a cage edge."""
    passwords = {p["name"] for p in _objects("RoutingPassword")}
    assert _peerings()[circuit_id]["password"] in passwords


def test_isp_router_id_is_its_transit_address() -> None:
    """Each ISP BGP process uses its own transit address as router_id."""
    by_device = {device: ip for (device, _), ip in _interface_ips().items() if device not in _CAGE_EDGES}
    for bgp in _objects("ManagedBGP"):
        (device,) = bgp["capabilities"]
        assert bgp["router_id"] == [str(by_device[device]), "INTERNET"], bgp["name"]


def test_transit_circuits_are_unowned() -> None:
    """Shared transit carries every tenant's traffic, so no customer owns it."""
    assert all("owner" not in c for c in _circuits().values())
