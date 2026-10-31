"""The route-protection filters rendered on an internal VRF session (NX-OS edge), staged and enforced.

The smoke goldens cover the global sessions of every platform (tests/smoke/configs/*_routing_protection_*); this file
covers what they cannot cheaply: an internal VRF session, whose far end is one of our own devices, derives its
prefixes from the namespace and attaches the filter inside its VRF (cisco_nxos_bgp.j2).
"""

from __future__ import annotations

import copy
import re
from typing import Any

from tests.smoke.fixture_builders.route_protection_roots import add_environment_roots
from tests.unit.test_external_vrf_peers_render import _NE_HOP, _fixture, _run_edge, _set_far_end_kind

_NEIGHBOR = "10.255.8.1"


def _internal(*, enforce: bool) -> dict[str, Any]:
    """The external-peers edge with the eg-am-ne01 hop turned into one of our own devices, PROD routed space known."""
    data = copy.deepcopy(_set_far_end_kind(_fixture(), _NE_HOP, "DcimPhysicalDevice"))
    node = data["DcimDevice"]["edges"][0]["node"]
    node["deployment"]["node"]["routing_filter_enforce"] = {"value": enforce}
    add_environment_roots(data, tenant_members=["eg-fr201"], internet_members=[])
    for edge in data["environment_prefixes_prod"]["edges"]:
        edge["node"]["ip_namespace"]["node"]["name"] = {"value": "PROD"}
    return data


def _block(output: str) -> str:
    match = re.search(rf"^    neighbor {re.escape(_NEIGHBOR)}\n(.*?)(?=^    neighbor |^  !)", output, re.M | re.S)
    assert match
    return match.group(1)


def test_an_internal_vrf_session_gets_its_namespace_filter_attached_when_enforced() -> None:
    """The far end is ours: its prefixes are the namespace's, the map is named inside the VRF, the limit is live."""
    output = _run_edge(_internal(enforce=True))
    block = _block(output)
    assert "        route-map RM-VRF-PROD-IPV4-IN in\n" in block
    assert "        route-map RM-VRF-PROD-IPV4-OUT out\n" in block
    assert re.search(r"^ {8}maximum-prefix \d+\n", block, re.M)
    assert "warning-only" not in block
    assert re.findall(r"^ip prefix-list PL-VRF-PROD-IPV4-IN seq \d+ permit (\S+)$", output, re.M) == [
        "10.100.0.0/24",
        "10.100.8.0/24",
    ]
    assert "route-map RM-VRF-PROD-IPV4-IN permit 10\n  match ip address prefix-list PL-VRF-PROD-IPV4-IN\n" in output


def test_a_staged_deployment_renders_the_objects_and_a_comment_not_the_attachment() -> None:
    """Staged: the same prefix-lists and maps exist, the neighbour does not name them, the limit only warns."""
    output = _run_edge(_internal(enforce=False))
    block = _block(output)
    assert "\n        route-map RM-VRF-PROD-IPV4-IN in\n" not in block
    assert (
        "        ! staged (routing_filter_enforce false): route-map RM-VRF-PROD-IPV4-IN in is not attached\n" in block
    )
    assert re.search(r"^ {8}maximum-prefix \d+ warning-only\n", block, re.M)
    assert "route-map RM-VRF-PROD-IPV4-IN permit 10" in output


# ---------------------------------------------------------------------------
# EOS and IOS XE: the VRF neighbour and the EVPN refusal, through a hand-built BGP profile
# ---------------------------------------------------------------------------


def _profile(*, enforce: bool) -> list[dict[str, Any]]:
    """A BGP profile with an underlay, an EVPN overlay and an internal PROD VRF session, policy composed as the transform does."""
    from jinja2 import Environment  # noqa: F401  (rendered by the callers)

    from transforms.helpers.bgp import add_template_fields
    from transforms.helpers.bgp_facts import annotate_bgp_policy

    def session(name: str, remote: str, **extra: Any) -> dict[str, Any]:
        return {
            "name": name,
            "remote_ip": {"address": f"{remote}/31"},
            "remote_as": {"asn": 65100},
            "remote_device": f"far-{name}",
            "enabled": True,
            **extra,
        }

    sessions = [
        session("u1", "10.1.0.0", ttl=1, peering_role="underlay", address_families=["ipv4"]),
        session("o1", "10.0.0.100", ttl=255, peering_role="overlay", address_families=["evpn"]),
        session("v1", "10.255.8.1", ttl=1, vrf="PROD", address_families=["ipv4"]),
    ]
    config: dict[str, Any] = {
        "local_as": {"asn": 65001},
        "router_id": {"address": "10.0.0.1/32"},
        "sessions": sessions,
        "peer_groups": [],
    }
    add_template_fields(config)
    device = {
        "name": "leaf1",
        "role": "leaf",
        "interfaces": [{"role": "loopback", "ip_address": {"address": "10.0.0.1/32"}}],
        "deployment": {"routing_filter_enforce": enforce, "loopback_pool": {"resources": [{"prefix": "10.0.0.0/24"}]}},
        "shared_capabilities": [{"typename": "ManagedVRF", "namespace": {"name": "PROD"}}],
    }
    roots = {
        "environment_prefixes_prod": [
            {"prefix": "10.100.0.0/24", "ip_namespace": {"name": "PROD", "namespace_type": "prod"}}
        ]
    }
    annotate_bgp_policy({"bgp": [config]}, device, roots)
    return [config]


def test_eos_attaches_the_unicast_filters_in_their_address_family_and_renders_no_evpn_filter() -> None:
    """EOS: `neighbor <peer> route-map` inside `address-family ipv4` and inside the VRF; the EVPN one is a comment."""
    from tests.unit.test_eos_routed_port_templates import _env

    template = _env().from_string(
        "{% from 'common/arista_eos_bgp.j2' import render_bgp %}{{ render_bgp(bgp, 'Loopback0') }}"
    )
    enforced = template.render(bgp=_profile(enforce=True))
    assert "ip prefix-list PL-UNDERLAY-IPV4-IN seq 10 permit 10.0.0.0/24 le 32" in enforced
    assert "      neighbor 10.1.0.0 route-map RM-UNDERLAY-IPV4-IN in\n" in enforced
    assert "         neighbor 10.255.8.1 route-map RM-VRF-PROD-IPV4-IN in\n" in enforced
    assert "match evpn" not in enforced
    assert "! staged or unsupported: the derived EVPN filter RM-OVERLAY-EVPN-IN (in) is not rendered (U)" in enforced
    staged = template.render(bgp=_profile(enforce=False))
    assert "neighbor 10.1.0.0 route-map RM-UNDERLAY-IPV4-IN in" not in staged
    assert "! staged (routing_filter_enforce false): route-map RM-UNDERLAY-IPV4-IN in is not attached" in staged
    assert "neighbor 10.1.0.0 maximum-routes 256 warning-only" in staged


def test_ios_xe_attaches_inside_the_address_family_and_defines_the_objects_at_column_zero() -> None:
    """IOS XE: the objects precede `router bgp` at column 0, the neighbour names them in its address family."""
    from tests.unit.test_eos_routed_port_templates import _env

    rendered = _env().get_template("common/cisco_ios_bgp.j2").render(bgp=_profile(enforce=True))
    assert rendered.index("route-map RM-UNDERLAY-IPV4-IN permit 10") < rendered.index("router bgp 65001")
    assert "\nroute-map RM-UNDERLAY-IPV4-IN permit 10\n" in rendered
    assert "    neighbor 10.1.0.0 route-map RM-UNDERLAY-IPV4-IN in\n" in rendered
    assert "    neighbor 10.255.8.1 route-map RM-VRF-PROD-IPV4-IN in\n" in rendered
    assert "match evpn" not in rendered
