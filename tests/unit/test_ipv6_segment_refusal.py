"""A transform refuses what it cannot render: the IPv6 segment gates (utils/segment_addressing.py).

A segment has ONE gateway of ONE family. The SVI address line of an IPv6 gateway renders on every platform
(transforms/helpers/segments.py ``get_vlans`` -> ``ipv6 address``), but not what a routed segment needs around it: the
VRF's IPv6 address family, the IPv6 ACL, the PBR to the firewall, the zone members and rules of the firewall. Each
consumer that would give a wrong or silently incomplete config raises ``Ipv6SegmentNotRenderedError`` naming the
segment; the allowed path (the SVI address through the helpers) stays open. No demo segment is IPv6, so the rendered
goldens do not change (tests/smoke).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import jinja2
import pytest

from tests.smoke.fixture_builders.common import run_transform
from tests.unit.cisco_xe_platform_helpers import _golden_input
from tests.unit.segment_port_helpers import raw_port_segments
from transforms.config.access_leaf import AccessLeaf
from transforms.config.border_leaf import BorderLeaf
from transforms.config.firewall import Firewall
from transforms.config.leaf import Leaf
from transforms.helpers.policy import rule_endpoint
from transforms.helpers.segments import get_vlans
from transforms.topology.office_firewall_policy import build_office_policy
from utils.segment_addressing import Ipv6SegmentNotRenderedError, has_ipv6_gateway, refuse_ipv6_segment

_TEMPLATES = Path(__file__).resolve().parents[2] / "templates" / "configs" / "common"


def _segment(name: str = "web", address: str | None = "fd5a:1760::1/64", prefix: str | None = "fd5a:1760::/64") -> dict:
    """A cleaned segment with a gateway (None: an L2-only segment)."""
    gateway = {"address": address, "ip_prefix": {"prefix": prefix}} if address or prefix else None
    return {"id": f"seg-{name}", "name": name, "customer_name": name, "gateway": gateway}


class TestTheSignal:
    """The family of the gateway decides, not the ip_version attribute (a segment with no gateway has nothing to render)."""

    @pytest.mark.parametrize(
        ("address", "prefix", "expected"),
        [
            ("fd5a:1760::1/64", "fd5a:1760::/64", True),
            ("fd5a:1760::1/64", None, True),
            (None, "fd5a:1760::/64", True),
            ("10.1.0.1/24", "10.1.0.0/24", False),
            (None, None, False),
        ],
    )
    def test_an_ipv6_address_or_prefix_is_an_ipv6_gateway(
        self, address: str | None, prefix: str | None, expected: bool
    ) -> None:
        """The gateway address wins, else its prefix; none is IPv4-or-L2 only."""
        assert has_ipv6_gateway(_segment(address=address, prefix=prefix)) is expected

    def test_a_segment_without_a_gateway_key_is_not_ipv6(self) -> None:
        """An L2-only segment (or a query that does not select the gateway) has nothing to refuse."""
        assert has_ipv6_gateway({"name": "l2"}) is False
        assert has_ipv6_gateway({"name": "l2", "gateway": None}) is False

    def test_the_refusal_names_the_segment_and_the_consumer(self) -> None:
        """The message the owner reads: segment, renderer, 'not rendered ... yet'."""
        with pytest.raises(
            Ipv6SegmentNotRenderedError, match=r"Segment 'web': IPv6 segments are not rendered by the X yet"
        ):
            refuse_ipv6_segment(_segment(), "the X")
        refuse_ipv6_segment(_segment(address="10.1.0.1/24", prefix="10.1.0.0/24"), "the X")

    def test_the_refusal_is_a_value_error(self) -> None:
        """The transforms raise ValueError for what they refuse (VrfRealizationError, the LISP renderer): so does this."""
        assert issubclass(Ipv6SegmentNotRenderedError, ValueError)


class TestTheAllowedPath:
    """The SVI address of an IPv6 gateway still flows through the existing helpers and the NX-OS SVI template."""

    @staticmethod
    def _activation() -> dict[str, Any]:
        segment = _segment()
        segment["security_zone"] = {"name": "PROD", "l3_vni": 3901}
        return {"vlan_id": 120, "vni": 10120, "segment": segment}

    def test_get_vlans_sorts_the_single_gateway_into_gateway_ipv6(self) -> None:
        """One gateway of one family: gateway_ipv6 is set, gateway_ip is not."""
        (vlan,) = get_vlans([self._activation()], {120: "PROD"})
        assert vlan["gateway_ipv6"] == "fd5a:1760::1/64" and vlan["gateway_ip"] is None

    def test_the_nxos_svi_renders_the_ipv6_gateway(self) -> None:
        """`ipv6 address` under the SVI, no `ip address`: the existing template needs no change for the address."""
        environment = jinja2.Environment(loader=jinja2.FileSystemLoader(_TEMPLATES), autoescape=False)  # noqa: S701
        (vlan,) = get_vlans([self._activation()], {120: "PROD"})
        rendered = environment.get_template("cisco_nxos_svi.j2").render(vlans=[vlan], vxlan=None)
        assert "interface Vlan120" in rendered and "  ipv6 address fd5a:1760::1/64" in rendered
        assert "  ip address" not in rendered


class TestTheRuleResolver:
    """rule_endpoint is the one resolver behind the switch ACL, the firewall rule table and the office policy."""

    def test_a_rule_endpoint_on_an_ipv6_segment_is_refused_on_both_sides(self) -> None:
        """Without it the endpoint would resolve to no prefix and render as `any`: a widened rule."""
        v6 = _segment()
        v4 = _segment("db", "10.2.0.1/24", "10.2.0.0/24")
        with pytest.raises(Ipv6SegmentNotRenderedError, match="Segment 'web'"):
            rule_endpoint({"source_segment": v6, "destination_segment": v4}, "source")
        with pytest.raises(Ipv6SegmentNotRenderedError, match="Segment 'web'"):
            rule_endpoint({"source_segment": v4, "destination_segment": v6}, "destination")
        assert rule_endpoint({"source_segment": v6, "destination_segment": v4}, "destination") == "10.2.0.0/24"

    def test_a_rule_with_no_segment_on_a_side_is_untouched(self) -> None:
        """A prefix selector or `any` has no segment to refuse."""
        assert rule_endpoint({"destination_prefixes": [{"prefix": "10.9.0.0/16"}]}, "source") is None

    def test_the_office_policy_refuses_an_ipv6_segment(self) -> None:
        """The office policy artifact (JSON) lists each segment's prefix: an IPv6 one would be null."""
        with pytest.raises(Ipv6SegmentNotRenderedError, match="the office firewall policy"):
            build_office_policy({"name": "C005-OFFICE", "network_segments": [_segment()]})


def _flip_to_ipv6(data: dict[str, Any]) -> None:
    """Make the segment carried by Ethernet10 of a golden input an IPv6 one (address and prefix)."""
    interfaces = data["DcimDevice"]["edges"][0]["node"]["interfaces"]["edges"]
    port = next(e["node"] for e in interfaces if e["node"]["name"]["value"] == "Ethernet10")
    gateway = raw_port_segments(port)[0]["gateway"]["node"]
    gateway["address"]["value"] = "fd5a:1760::1/64"
    gateway["ip_prefix"]["node"]["prefix"]["value"] = "fd5a:1760::/64"


# (golden, transform): every platform of the fabric roles; the platform is in the message.
_SWITCHES = [
    ("leaf_cisco_nxos_ebgp_ebgp", Leaf, "cisco_nxos leaf"),
    ("leaf_arista_eos_ebgp_ebgp", Leaf, "arista_eos leaf"),
    ("leaf_sonic_ebgp_ebgp", Leaf, "sonic leaf"),
    ("leaf_dell_sonic_ebgp_ebgp", Leaf, "dell_sonic leaf"),
    ("leaf_cisco_xe_ebgp_ebgp", Leaf, "cisco_xe leaf"),
    ("border_leaf_cisco_nxos_ebgp_ebgp", BorderLeaf, "cisco_nxos border-leaf"),
    ("access_leaf_arista_eos_ebgp_ebgp", AccessLeaf, "arista_eos access-leaf"),
]


class TestTheSwitchGate:
    """BaseDeviceTransform._extra_config: a routed IPv6 segment on a fabric switch is refused, on every platform."""

    @pytest.mark.parametrize(("golden", "transform", "consumer"), _SWITCHES, ids=[s[0] for s in _SWITCHES])
    def test_a_switch_carrying_an_ipv6_segment_is_refused(self, golden: str, transform: type, consumer: str) -> None:
        """The IPv4 golden renders; the same device with an IPv6 gateway on the segment raises, naming platform and role."""
        data = _golden_input(golden)
        assert run_transform(transform, copy.deepcopy(data))
        _flip_to_ipv6(data)
        with pytest.raises(
            Ipv6SegmentNotRenderedError,
            match=rf"IPv6 segments are not rendered by the {consumer} \(VRF, ACL, PBR\) yet",
        ):
            run_transform(transform, data)


class TestTheFirewallGate:
    """The firewall transform: zone members, rules and context routes are IPv4 prefixes, an IPv6 segment is refused."""

    @pytest.mark.parametrize(
        "golden",
        [
            "firewall_checkpoint_gaia_with_ha",
            "firewall_checkpoint_gaia_with_policy",
            "firewall_paloalto_panos_with_policy",
        ],
    )
    def test_a_firewall_serving_an_ipv6_segment_is_refused(self, golden: str) -> None:
        """Every prefix of one served segment turned IPv6: the IPv4 golden renders, this one raises (zones or rules)."""
        text = json.dumps(_golden_input(golden))
        assert run_transform(Firewall, json.loads(text))
        assert "10.0.2.0/24" in text
        data = json.loads(text.replace("10.0.2.0/24", "fd5a:1760::/64"))
        with pytest.raises(Ipv6SegmentNotRenderedError, match="IPv6 segments are not rendered by the"):
            run_transform(Firewall, data)
