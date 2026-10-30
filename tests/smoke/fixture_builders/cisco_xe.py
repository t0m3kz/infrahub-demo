"""Catalyst 9000 (IOS-XE) fixtures: the leaf roles, segment ACLs and inter-VRF statics."""

from __future__ import annotations

from tests.smoke.fixture_builders.common import (
    _edges,
    _make_policy_rule,
    _make_security_policy,
    _node,
    _v,
    _write_data_fixture,
)
from tests.smoke.fixture_builders.fabric import SCENARIOS, _write_fixture, build_device_data
from tests.smoke.fixture_builders.firewall import _make_rule_segment, _make_segment_rule
from tests.smoke.fixture_builders.transit import build_exchange_transit_bl_data
from transforms.config.access_leaf import AccessLeaf
from transforms.config.border_leaf import BorderLeaf
from transforms.config.leaf import Leaf

# Catalyst 9000 (IOS-XE): an EVPN-VXLAN leaf, access-leaf and border-leaf only. Every other role has no
# cisco_xe fabric template, so cisco_xe is not in FABRIC_PLATFORMS.
CISCO_XE_DEVICE_CONFIGS: list[tuple[type, str, str]] = [
    (Leaf, "leaf", "leaf"),
    (AccessLeaf, "access-leaf", "access_leaf"),
    (BorderLeaf, "border-leaf", "border_leaf"),
]


def write_cisco_xe_fixtures() -> tuple[int, int]:
    """Write the Catalyst 9000 leaf / access-leaf / border-leaf fixtures for every scenario."""
    generated = errors = 0
    for transform_cls, role, type_prefix in CISCO_XE_DEVICE_CONFIGS:
        for scenario in SCENARIOS:
            g, e = _write_fixture(
                transform_cls,
                dir_name=f"{type_prefix}_cisco_xe_{scenario}",
                dev_name=f"dc1-{type_prefix.replace('_', '-')}-01",
                role=role,
                platform="cisco_xe",
                scenario=scenario,
                include_segments=True,
            )
            generated += g
            errors += e
    return generated, errors


# Segment ACL + inter-VRF statics on a Catalyst 9000. The segments' security_policy is in the query, so every hosted
# routed segment gets an implicit-deny SVI ACL (get_acls). The default ACL fixtures only hold tcp `eq` rules; this adds
# one of each other shape IOS-XE writes differently: a udp port range towards a zoned prefix, icmp, a host destination
# with `log`, and the return leg (tcp `established`, source port) of a permit from another segment.
def _add_cisco_xe_acl_rules(segments: list[dict]) -> None:
    """Extend the policy of seg-100 (and the inbound rules of seg-200) of the `include_acls` segments."""
    by_name = {seg["name"]["value"]: seg for seg in segments}
    web = _make_rule_segment(cidr="10.200.0.0/24", zone_name="web")
    rules = by_name["seg-100"]["security_policy"]["node"]["rules"]["edges"]
    rules.append(
        {
            "node": {
                **_make_policy_rule(index=30, name="allow-rtp", protocol="udp", port_start=5000, port_end=5010),
                "destination_segment": _node(web),
            }
        }
    )
    rules.append({"node": _make_policy_rule(index=40, name="allow-ping", protocol="icmp")})
    rules.append(
        {
            "node": {
                **_make_policy_rule(index=50, name="allow-db-logged", protocol="tcp", port_start=5432, log=True),
                "destination_ip_addresses": _edges([{"address": _v("10.9.9.9/32")}]),
            }
        }
    )
    inbound = _make_segment_rule(
        policy="policy-vxlan-seg",
        index=60,
        name="prod-to-web",
        protocol="tcp",
        port_start=8443,
        src_segment=_make_rule_segment(cidr="10.100.0.0/24", zone_name="prod"),
        dst_segment=web,
    )
    by_name["seg-200"]["inbound_rules"] = _edges([{**inbound, "policy": _node({"enabled": _v(True)})}])


def _interface_segments(data: dict, interface_name: str = "Ethernet10") -> list[dict]:
    """The segment nodes carried by one interface of the first device of a raw device response."""
    node = data["DcimDevice"]["edges"][0]["node"]
    port = next(e["node"] for e in node["interfaces"]["edges"] if e["node"]["name"]["value"] == interface_name)
    return [edge["node"] for edge in port["segments"]["edges"]]


def build_cisco_xe_acl_data(*, device_name: str, role: str = "leaf") -> dict:
    """A cisco_xe leaf hosting seg-100 (VRF_A, VXLAN) and seg-200 (VLAN), each behind a zero-trust SVI ACL."""
    data = build_device_data(
        device_name=device_name,
        role=role,
        platform="cisco_xe",
        scenario="ebgp_ibgp",
        include_segments=True,
        include_acls=True,
    )
    _add_cisco_xe_acl_rules(_interface_segments(data))
    return data


def build_cisco_xe_exchange_data(*, device_name: str, role: str, with_acl: bool) -> dict:
    """The exchange-transit wiring (firewall port tagged with two contexts) on a cisco_xe leaf or border leaf.

    Its inter-VRF statics (PROD 0/0 to the shared context's VIP, the SNAT /32s in INTERNET) are the
    `ip route vrf` lines. With `with_acl` the customer-port segment seg-shared also carries a zero-trust policy.
    """
    data = build_exchange_transit_bl_data(device_name=device_name, platform="cisco_xe", role=role)
    if with_acl:
        shared = _interface_segments(data)[0]
        shared["security_policy"] = _node(
            _make_security_policy(
                name="policy-shared",
                rules=[
                    _make_policy_rule(index=10, name="allow-https", protocol="tcp", port_start=443),
                    _make_policy_rule(index=20, name="allow-ping", protocol="icmp"),
                ],
            )
        )
    return data


def write_cisco_xe_acl_fixtures() -> tuple[int, int]:
    """Write the Catalyst 9000 segment-ACL and exchange-static fixtures (EVPN-VXLAN)."""
    generated = errors = 0
    fixtures = (
        (Leaf, "leaf_cisco_xe_with_acl", build_cisco_xe_acl_data(device_name="dc1-leaf-01")),
        (
            Leaf,
            "leaf_cisco_xe_exchange_acl",
            build_cisco_xe_exchange_data(device_name="dc1-leaf-01", role="leaf", with_acl=True),
        ),
        (
            BorderLeaf,
            "border_leaf_cisco_xe_exchange_transit",
            build_cisco_xe_exchange_data(device_name="dc1-border-leaf-01", role="border-leaf", with_acl=False),
        ),
    )
    for transform_cls, dir_name, data in fixtures:
        g, e = _write_data_fixture(transform_cls, dir_name, data)
        generated += g
        errors += e
    return generated, errors
