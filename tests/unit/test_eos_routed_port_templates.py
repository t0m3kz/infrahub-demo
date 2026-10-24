"""Inline renders of the Arista EOS routed-port and global-routing includes.

The smoke goldens carry only physical Ethernet underlay ports, so the sub-interface parent, the routed
Port-Channel and the pure-L2 paths are covered here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from jinja2 import Environment, FileSystemLoader

TEMPLATES = Path(__file__).resolve().parents[2] / "templates" / "configs"
ADDRESS = [{"address": "10.0.0.1/31"}]
LOOPBACK_ADDRESS = [{"address": "10.0.0.9/32"}]


def _env() -> Environment:
    """Return a Jinja environment over the config templates."""
    return Environment(loader=FileSystemLoader(str(TEMPLATES)), trim_blocks=False)


def _interface_block(interface: dict[str, Any], interfaces: list[dict[str, Any]], **extra: Any) -> str:
    """Render the interface IP include for one interface the way the role templates include it."""
    template = _env().from_string("interface {{ interface.name }}{% include 'common/arista_eos_interface_ip.j2' %}")
    return template.render(interface=interface, interfaces=interfaces, **extra)


def _iface(name: str, ip_addresses: list[dict[str, str]] | None = None) -> dict[str, Any]:
    """Build the interface dict shape the transform hands to the templates."""
    return {"name": name, "ip_addresses": ip_addresses or []}


def test_routed_ethernet_gets_no_switchport_before_the_address() -> None:
    """A routed physical port is a routed port: `no switchport` precedes `ip address`."""
    port = _iface("Ethernet1", ADDRESS)
    block = _interface_block(port, [port])
    assert block.index("no switchport") < block.index("ip address 10.0.0.1/31")


def test_sub_interface_parent_without_address_gets_no_switchport() -> None:
    """The parent of a routed sub-interface needs a routed parent, the sub-interface itself does not."""
    parent = _iface("Ethernet20")
    sub = _iface("Ethernet20.100", ADDRESS)
    interfaces = [parent, sub]
    assert "no switchport" in _interface_block(parent, interfaces)
    assert "no switchport" not in _interface_block(sub, interfaces)


def test_unrelated_port_without_address_stays_a_switchport() -> None:
    """A port with no address and no routed sub-interface is left alone, also next to another port's sub-interface."""
    host = _iface("Ethernet21")
    interfaces = [_iface("Ethernet20"), _iface("Ethernet20.100", ADDRESS), host]
    assert "no switchport" not in _interface_block(host, interfaces)


def test_routed_port_channel_gets_no_switchport() -> None:
    """A routed LAG gets `no switchport`, under its own name or the `eos_ifname` the leaf passes."""
    lag = _iface("ae5", ADDRESS)
    assert "no switchport" not in _interface_block(lag, [lag])
    assert "no switchport" in _interface_block(lag, [lag], eos_ifname="Port-Channel5")
    named = _iface("Port-Channel5", ADDRESS)
    assert "no switchport" in _interface_block(named, [named])


@pytest.mark.parametrize("name", ["Loopback0", "Vlan10", "Management1", "Vxlan1"])
def test_virtual_interfaces_never_get_no_switchport(name: str) -> None:
    """Loopback, SVI, Management and Vxlan are not switchports."""
    interface = _iface(name, LOOPBACK_ADDRESS)
    assert "no switchport" not in _interface_block(interface, [interface])


def _routing(interfaces: list[dict[str, Any]], bgp: Any = None, ospf: Any = None) -> str:
    """Render the global routing include."""
    template = _env().from_string("{% include 'common/arista_eos_ip_routing.j2' %}")
    return template.render(interfaces=interfaces, bgp=bgp, ospf=ospf)


def test_ip_routing_is_enabled_when_an_interface_is_addressed() -> None:
    """A device with an addressed interface routes, so it needs the global `ip routing`."""
    assert "\nip routing" in _routing([_iface("Ethernet1", ADDRESS)])


@pytest.mark.parametrize("protocol", ["bgp", "ospf"])
def test_ip_routing_is_enabled_for_a_routing_protocol(protocol: str) -> None:
    """BGP or OSPF alone also needs routing on."""
    assert "\nip routing" in _routing([], **{protocol: [{"anything": 1}]})


def test_pure_l2_device_omits_ip_routing() -> None:
    """A switch with no addressed interface, no BGP and no OSPF stays an L2 device."""
    assert "ip routing" not in _routing([_iface("Ethernet1"), _iface("Ethernet2")])


def test_l2_role_template_without_addresses_has_no_ip_routing() -> None:
    """The whole L2-leaf template, with only unaddressed ports, renders no global `ip routing`."""
    template = _env().get_template("l2_leafs/arista_eos.j2")
    rendered = template.render(
        hostname="l2-1",
        vlans=[],
        interfaces=[{**_iface("Ethernet1"), "role": "server", "vlans": [], "mode": None}],
    )
    assert "ip routing" not in rendered
    assert "no switchport" not in rendered


def test_bgp_route_maps_render_at_column_zero_before_router_bgp() -> None:
    """Per-neighbour route-maps are global configuration: column 0, ahead of `router bgp`."""
    session = {
        "name": "peer-a",
        "neighbor": "10.1.0.0",
        "remote_as": {"asn": 65100},
        "local_pref": 200,
        "med": 50,
    }
    bgp = [{"local_as": {"asn": 65000}, "router_ip": "10.0.0.1", "sessions": [session], "vrf_sessions": {}}]
    template = _env().from_string(
        "{% from 'common/arista_eos_bgp.j2' import render_bgp %}{{ render_bgp(bgp, 'Loopback0') }}"
    )
    lines = template.render(bgp=bgp).splitlines()
    for name in ("peer-a-LOCAL-PREF-IN", "peer-a-MED-OUT"):
        definition = f"route-map {name} permit 10"
        assert definition in lines
        assert lines.index(definition) < lines.index("router bgp 65000")
    assert not any(line.startswith(" ") and "permit 10" in line for line in lines)
