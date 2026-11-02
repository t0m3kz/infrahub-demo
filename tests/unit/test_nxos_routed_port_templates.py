"""Inline renders of the NX-OS routed-port, sub-interface parent, feature and route-map includes.

The smoke goldens carry only physical Ethernet underlay ports and no spine with a BGP policy, so the routed
port-channel, the absent sub-interface parent and the per-session route-maps are covered here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader

from transforms.helpers.bgp_policy import compose_policy

TEMPLATES = Path(__file__).resolve().parents[2] / "templates" / "configs"
ADDRESS = [{"address": "10.0.0.1/31"}]


def _env() -> Environment:
    """Return a Jinja environment over the config templates."""
    return Environment(loader=FileSystemLoader(str(TEMPLATES)), trim_blocks=False)


def _iface(name: str, ip_addresses: list[dict[str, str]] | None = None) -> dict[str, Any]:
    """Build the interface dict shape the transform hands to the templates."""
    return {"name": name, "ip_addresses": ip_addresses or []}


def _routed_port(interface: dict[str, Any], interfaces: list[dict[str, Any]]) -> str:
    """Render the routed-port include the way the role templates include it."""
    template = _env().from_string("interface {{ interface.name }}{% include 'common/cisco_nxos_routed_port.j2' %}")
    return template.render(interface=interface, interfaces=interfaces)


def test_routed_ethernet_and_port_channel_get_no_switchport() -> None:
    """A routed physical port and a routed port-channel start with `no switchport`."""
    for name in ("Ethernet1/1", "port-channel10"):
        port = _iface(name, ADDRESS)
        assert _routed_port(port, [port]) == f"interface {name}\n  no switchport"


def test_sub_interface_loopback_and_vlan_never_get_no_switchport() -> None:
    """Sub-interfaces (invalid), loopbacks and SVIs are not switchport candidates."""
    for name in ("Ethernet1/13.1000", "Loopback0", "loopback0", "Vlan100", "mgmt0"):
        port = _iface(name, ADDRESS)
        assert "no switchport" not in _routed_port(port, [port])


def test_parent_of_a_routed_sub_interface_in_the_list_gets_no_switchport() -> None:
    """The addressless parent is routed too; an unrelated addressless port stays a switchport."""
    parent, sub, other = _iface("Ethernet1/13"), _iface("Ethernet1/13.1000", ADDRESS), _iface("Ethernet1/14")
    interfaces = [parent, sub, other]
    assert "no switchport" in _routed_port(parent, interfaces)
    assert "no switchport" not in _routed_port(other, interfaces)


def test_absent_sub_interface_parent_is_rendered_once() -> None:
    """A parent that only exists in the sub-interface name is rendered once as a routed port."""
    interfaces = [_iface("Ethernet1/13.1000", ADDRESS), _iface("Ethernet1/13.1001", ADDRESS), _iface("Ethernet1/20")]
    rendered = _env().get_template("common/cisco_nxos_subif_parents.j2").render(interfaces=interfaces)
    assert rendered.strip().splitlines() == ["interface Ethernet1/13", "  no switchport", "!"]


def test_listed_sub_interface_parent_is_not_rendered_twice() -> None:
    """A parent already in the interface list is left to the interface loop."""
    interfaces = [_iface("Ethernet1/13"), _iface("Ethernet1/13.1000", ADDRESS)]
    assert _env().get_template("common/cisco_nxos_subif_parents.j2").render(interfaces=interfaces).strip() == ""


def test_spine_with_evpn_session_gets_only_nv_overlay_evpn() -> None:
    """A device with an EVPN session but no VXLAN block enables `nv overlay evpn` and no NVE feature."""
    bgp = [{"sessions": [{"address_families": ["ipv4"]}, {"address_families": ["evpn"]}]}]
    rendered = _env().get_template("common/cisco_nxos_feature_evpn.j2").render(bgp=bgp)
    assert rendered.strip() == "nv overlay evpn"


def test_device_without_evpn_gets_no_overlay_lines() -> None:
    """No EVPN session and no VXLAN block render nothing."""
    bgp = [{"sessions": [{"address_families": ["ipv4"]}]}]
    assert _env().get_template("common/cisco_nxos_feature_evpn.j2").render(bgp=bgp).strip() == ""


def test_session_route_maps_are_global_and_precede_the_connected_map() -> None:
    """Per-session local-preference and MED maps are column-0 route-maps, rendered with RM-CONN-2-BGP."""
    sessions = [{"name": "peer-a", "local_pref": 200, "med": 50}, {"name": "peer-b"}]
    bgp = [{"sessions": [{**s, "policy": compose_policy(s)} for s in sessions]}]
    rendered = _env().get_template("common/cisco_nxos_route_maps.j2").render(bgp=bgp, loopback_name="loopback0")
    assert "\nroute-map peer-a-LOCAL-PREF-IN permit 10\n  set local-preference 200\n" in rendered
    assert "\nroute-map peer-a-MED-OUT permit 10\n  set metric 50\n" in rendered
    assert "peer-b" not in rendered
    assert rendered.index("peer-a-MED-OUT") < rendered.index("route-map RM-CONN-2-BGP permit 10")
    assert "  match interface loopback0" in rendered


def test_spine_without_bgp_policy_still_defines_the_connected_map() -> None:
    """RM-CONN-2-BGP is defined for a spine, which has no vxlan, no vrf routes and no transit prefixes."""
    bgp = [{"sessions": [{"name": "peer-a", "policy": compose_policy({"name": "peer-a"})}]}]
    rendered = _env().get_template("common/cisco_nxos_route_maps.j2").render(bgp=bgp, loopback_name="loopback0")
    assert "route-map RM-CONN-2-BGP permit 10\n  match interface loopback0" in rendered
    assert "RM-VRF-DIRECT-2-EVPN" not in rendered
