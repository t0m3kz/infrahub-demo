"""Catalyst 9000 office fixtures: the LISP site (SD-Access style) and its WAN exit."""

from __future__ import annotations

from tests.smoke.fixture_builders.cisco_xe import _add_cisco_xe_acl_rules, _interface_segments
from tests.smoke.fixture_builders.common import (
    _af_edges,
    _edges,
    _make_bgp_peering,
    _make_interface,
    _node,
    _v,
    _write_data_fixture,
)
from tests.smoke.fixture_builders.fabric import build_device_data
from transforms.config.access_switch import AccessSwitch
from transforms.config.distribution_switch import DistributionSwitch

# Office LISP site (add_office_fabric, SD-Access style): an access-switch edge and a distribution-switch border-and-
# map-server pair on one office footprint. The cisco_xe ospf_ibgp inputs without the BGP capability, the uplinks re-wired
# to the pair, and the office's own deployment (queries/config/access_switch.gql): its name is the LISP site, its
# site key and the two distribution-switch map-servers (listed out of name order: the transform sorts them).
OFFICE_NAME = "C005-OFFICE"
OFFICE_MAP_SERVERS = (("ds-c005-office02", "10.250.5.2/32"), ("ds-c005-office01", "10.250.5.1/32"))
OFFICE_SITE_KEY = "0ff1c3-k3y"


def _rewire_uplink(iface: dict, *, role: str, peer: str) -> None:
    """Point one P2P interface of the base fixture at the office peer ``peer`` (description, role, cable far end)."""
    iface["role"] = _v(role)
    iface["description"] = _v(f"to {peer}")
    iface["cable"]["node"]["endpoints"]["edges"][1]["node"]["device"] = _node({"name": _v(peer)})


def build_office_lisp_device_data(
    *, device_name: str, role: str, include_segments: bool = True, include_acls: bool = False
) -> dict:
    """The raw cisco_xe device query response of an office access-switch or distribution-switch."""
    data = build_device_data(
        device_name=device_name,
        role=role,
        platform="cisco_xe",
        scenario="ospf_ibgp",
        include_segments=include_segments,
        include_acls=include_acls,
        vlan_segment_vrf=True,
    )
    node = data["DcimDevice"]["edges"][0]["node"]
    node["capabilities"]["edges"] = [
        edge for edge in node["capabilities"]["edges"] if edge["node"].get("__typename") != "ManagedBGP"
    ]
    by_name = {edge["node"]["name"]["value"]: edge["node"] for edge in node["interfaces"]["edges"]}
    if role == "access-switch":
        _rewire_uplink(by_name["Ethernet1"], role="uplink", peer="ds-c005-office01")
        _rewire_uplink(by_name["Ethernet2"], role="uplink", peer="ds-c005-office02")
    else:
        _rewire_uplink(by_name["Ethernet1"], role="downlink", peer="as-c005-office01")
        _rewire_uplink(by_name["Ethernet2"], role="interlink", peer="ds-c005-office02")
    deployment = node["deployment"]["node"]
    deployment.update(
        {
            "id": "office-1",
            "name": _v(OFFICE_NAME),
            "lisp_map_servers": _edges(
                [
                    {"name": _v(name), "interfaces": _edges([{"ip_address": _node({"address": _v(address)})}])}
                    for name, address in OFFICE_MAP_SERVERS
                ]
            ),
            "lisp_site_key": _node({"password": _v(OFFICE_SITE_KEY)}),
        }
    )
    # An office has no EVPN route-target ASN (unset on TopologyCustomerOffice) and no overlay flag.
    deployment.pop("evpn_rt_as", None)
    return data


def write_office_lisp_fixtures() -> tuple[int, int]:
    """Write the office access-switch and distribution-switch LISP fixtures (OSPF underlay, no BGP).

    Plus a hostless access-switch (an edge that hosts no segment yet still carries the edge LISP config) and one
    whose tenant SVIs carry the zero-trust segment ACLs.
    """
    generated = errors = 0
    acl_data = build_office_lisp_device_data(device_name="as-c005-office01", role="access-switch", include_acls=True)
    _add_cisco_xe_acl_rules(_interface_segments(acl_data))
    for dir_name, data in (
        (
            "access_switch_cisco_xe_lisp_hostless",
            build_office_lisp_device_data(device_name="as-c005-office01", role="access-switch", include_segments=False),
        ),
        ("access_switch_cisco_xe_lisp_with_acl", acl_data),
    ):
        g, e = _write_data_fixture(AccessSwitch, dir_name, data)
        generated += g
        errors += e
    for transform_cls, role, dir_name, device_name in (
        (AccessSwitch, "access-switch", "access_switch_cisco_xe_lisp_ospf", "as-c005-office01"),
        (DistributionSwitch, "distribution-switch", "distribution_switch_cisco_xe_lisp_ospf", "ds-c005-office01"),
    ):
        g, e = _write_data_fixture(
            transform_cls, dir_name, build_office_lisp_device_data(device_name=device_name, role=role)
        )
        generated += g
        errors += e
    return generated, errors


# The office's WAN exit (add_office_fabric): the office has its SD-WAN edge, so the distribution-switch is the exit (a
# routed port with a dot1q sub-interface in the tenant VRF, an eBGP session to the edge, proxy-ETR/ITR) and the
# access-switch uses it (use-petr). Same inputs as the two fixtures above plus what the generator writes: the edge on the
# deployment, and on the border the port, its sub-interface and the BGP process with its peering. The tenant VRF is the
# fixture's own (VRF_A, L3 instance-id 50001), the one its segment is in.
OFFICE_EDGE_NAME = "C005-OFFICE-EDGE1"
OFFICE_EXIT_PORT = "GigabitEthernet1/0/26"
OFFICE_EXIT_VLAN = 3800
OFFICE_EXIT_VRF = "VRF_A"
OFFICE_EXIT_SWITCH_ASN = 4200000001
OFFICE_EXIT_EDGE_ASN = 4200000002
OFFICE_EXIT_SWITCH_ADDRESS = "100.66.0.0/31"
OFFICE_EXIT_EDGE_ADDRESS = "100.66.0.1/31"
OFFICE_EXIT_LOOPBACK = OFFICE_MAP_SERVERS[1][1]


def build_office_wan_exit_data(*, device_name: str, role: str) -> dict:
    """The raw device query response of an office access-switch or distribution-switch whose office has an SD-WAN edge."""
    data = build_office_lisp_device_data(device_name=device_name, role=role)
    node = data["DcimDevice"]["edges"][0]["node"]
    node["deployment"]["node"]["lisp_wan_edges"] = _edges([{"name": _v(OFFICE_EDGE_NAME)}])
    if role != "distribution-switch":
        return data

    namespace = _node(
        {"name": _v(OFFICE_EXIT_VRF), "l3_vni": _v(50001), "namespace_type": _v("prod")},
    )
    # Loopback0 is the RLOC the edges register toward and the map-server address the other switches use.
    loopback_address = _node(
        {"address": _v(OFFICE_EXIT_LOOPBACK), "ip_namespace": _node({"name": _v("default")})},
    )
    for edge in node["interfaces"]["edges"]:
        if edge["node"]["name"]["value"] == "Loopback0":
            edge["node"]["ip_address"] = loopback_address
    sub_interface = f"{OFFICE_EXIT_PORT}.{OFFICE_EXIT_VLAN}"
    node["interfaces"]["edges"].append(
        {
            "node": _make_interface(
                name=OFFICE_EXIT_PORT,
                device_name=device_name,
                description=f"{device_name}-{OFFICE_EXIT_PORT}__{OFFICE_EDGE_NAME}-GE3",
                role="uplink",
            )
        }
    )
    node["interfaces"]["edges"].append(
        {
            "node": {
                **_make_interface(
                    name=sub_interface, device_name=device_name, role="uplink", typename="DcimVirtualInterface"
                ),
                "parent_interface": _node({"name": _v(OFFICE_EXIT_PORT)}),
                "ip_address": _node({"address": _v(OFFICE_EXIT_SWITCH_ADDRESS), "ip_namespace": namespace}),
            }
        }
    )
    peering = _make_bgp_peering(
        device_name=device_name,
        device_ip=OFFICE_EXIT_SWITCH_ADDRESS,
        device_asn=OFFICE_EXIT_SWITCH_ASN,
        remote_name=OFFICE_EDGE_NAME,
        remote_ip=OFFICE_EXIT_EDGE_ADDRESS,
        remote_asn=OFFICE_EXIT_EDGE_ASN,
        ttl=1,
        local_iface_name=sub_interface,
        remote_iface_name=f"GE3.{OFFICE_EXIT_VLAN}",
        send_extended_community=True,
        password="0ff1c3-w4n-k3y",
    )
    peering["name"] = _v(f"WAN-EXIT-{device_name}-{OFFICE_EDGE_NAME}")
    peering["peering_role"] = _v("regular")
    peering["address_families"] = _af_edges(("ipv4", "unicast"))
    for iface_edge in peering["capabilities"]["edges"]:
        iface_edge["node"]["ip_address"]["node"]["ip_namespace"] = namespace
    node["capabilities"]["edges"].append(
        {
            "node": {
                "__typename": "ManagedBGP",
                "name": _v(f"{device_name}-bgp-wan-exit"),
                "status": _v("active"),
                "multipath": _v(False),
                "graceful_restart": _v(True),
                "confederation_identifier": _v(None),
                "process_role": _v("regular"),
                "local_as": _node({"asn": _v(OFFICE_EXIT_SWITCH_ASN)}),
                "router_id": _node({"address": _v(OFFICE_EXIT_LOOPBACK)}),
                "peerings": _edges([peering]),
            }
        }
    )
    return data


def write_office_wan_exit_fixtures() -> tuple[int, int]:
    """Write the office access-switch and distribution-switch fixtures of an office with a WAN exit."""
    generated = errors = 0
    for transform_cls, role, dir_name, device_name in (
        (AccessSwitch, "access-switch", "access_switch_cisco_xe_lisp_wan_exit", "as-c005-office01"),
        (DistributionSwitch, "distribution-switch", "distribution_switch_cisco_xe_lisp_wan_exit", "ds-c005-office01"),
    ):
        g, e = _write_data_fixture(
            transform_cls, dir_name, build_office_wan_exit_data(device_name=device_name, role=role)
        )
        generated += g
        errors += e
    return generated, errors
