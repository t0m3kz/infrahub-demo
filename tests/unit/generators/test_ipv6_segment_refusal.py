"""What a transform still refuses for an IPv6 segment (utils/segment_addressing.py)."""

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
from transforms.config.firewall import Firewall, apply_context_routes
from transforms.config.leaf import Leaf
from transforms.helpers.policy import rule_endpoint
from transforms.helpers.segments import get_vlans
from transforms.topology.office_firewall_policy import build_office_policy
from utils.segment_addressing import Ipv6SegmentNotRenderedError, has_ipv6_gateway, refuse_ipv6_segment

_TEMPLATES = Path(__file__).resolve().parents[3] / "templates" / "configs" / "common"


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

    def test_a_rule_endpoint_on_an_ipv6_segment_resolves_to_its_prefix(self) -> None:
        """The switch ACLs render either family: the segment's one prefix, whichever it is."""
        v6 = _segment()
        v4 = _segment("db", "10.2.0.1/24", "10.2.0.0/24")
        assert rule_endpoint({"source_segment": v6, "destination_segment": v4}, "source") == "fd5a:1760::/64"
        assert rule_endpoint({"source_segment": v6, "destination_segment": v4}, "destination") == "10.2.0.0/24"

    def test_a_rule_with_no_segment_on_a_side_is_untouched(self) -> None:
        """A prefix selector or `any` has no segment to refuse."""
        assert rule_endpoint({"destination_prefixes": [{"prefix": "10.9.0.0/16"}]}, "source") is None

    def test_the_office_policy_renders_an_ipv6_segment_with_its_address_table(self) -> None:
        """The office policy artifact lists the segment's IPv6 prefix and the table of the prefixes its rules name."""
        office = build_office_policy({"name": "C005-OFFICE", "network_segments": [_segment()]})
        assert office["segments"][0]["prefix"] == "fd5a:1760::/64"
        assert office["addresses"] == []


def _flip_to_ipv6(data: dict[str, Any]) -> None:
    """Make the segment carried by Ethernet10 of a golden input an IPv6 one (address and prefix)."""
    interfaces = data["DcimDevice"]["edges"][0]["node"]["interfaces"]["edges"]
    port = next(e["node"] for e in interfaces if e["node"]["name"]["value"] == "Ethernet10")
    gateway = raw_port_segments(port)[0]["gateway"]["node"]
    gateway["address"]["value"] = "fd5a:1760::1/64"
    gateway["ip_prefix"]["node"]["prefix"]["value"] = "fd5a:1760::/64"


# (golden, transform, expected text of the rendered IPv6 gateway): every platform of the fabric roles.
_SWITCHES = [
    ("leaf_cisco_nxos_ebgp_ebgp", Leaf, "ipv6 address fd5a:1760::1/64"),
    ("leaf_arista_eos_ebgp_ebgp", Leaf, "ipv6 address virtual fd5a:1760::1/64"),
    ("leaf_sonic_ebgp_ebgp", Leaf, "Vlan100|fd5a:1760::1/64"),
    ("leaf_dell_sonic_ebgp_ebgp", Leaf, "Vlan100|fd5a:1760::1/64"),
    ("leaf_cisco_xe_ebgp_ebgp", Leaf, "ipv6 address fd5a:1760::1/64"),
    ("border_leaf_cisco_nxos_ebgp_ebgp", BorderLeaf, "ipv6 address fd5a:1760::1/64"),
    ("access_leaf_arista_eos_ebgp_ebgp", AccessLeaf, "ipv6 address virtual fd5a:1760::1/64"),
]


class TestTheSwitchRendersIpv6:
    """A routed IPv6 segment renders on a fabric switch of every platform (SVI, VRF family, ACL); see tests/smoke goldens."""

    @pytest.mark.parametrize(("golden", "transform", "expected"), _SWITCHES, ids=[s[0] for s in _SWITCHES])
    def test_a_switch_carrying_an_ipv6_segment_renders_its_gateway(
        self, golden: str, transform: type, expected: str
    ) -> None:
        """The IPv4 golden renders; the same device with an IPv6 gateway renders the IPv6 form of the SVI."""
        data = _golden_input(golden)
        assert run_transform(transform, copy.deepcopy(data))
        _flip_to_ipv6(data)
        assert expected in run_transform(transform, data)

    def test_a_segment_that_would_be_steered_to_a_firewall_context_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PBR needs the context's IPv4 leg: an IPv6 segment that gets a PBR rule is refused, never left unsteered."""
        data = _golden_input("leaf_cisco_nxos_ebgp_ebgp")
        _flip_to_ipv6(data)
        monkeypatch.setattr(
            "transforms.common.get_customer_pbr_rules",
            lambda *args, **kwargs: [{"vlan_id": 100, "fw_nexthop": "1.1.1.1"}],
        )
        with pytest.raises(Ipv6SegmentNotRenderedError, match="PBR to a firewall context"):
            run_transform(Leaf, data)


_FIREWALLS = [
    "firewall_checkpoint_gaia_with_ha",
    "firewall_checkpoint_gaia_with_policy",
    "firewall_paloalto_panos_with_policy",
]


class TestTheFirewallRendersIpv6:
    """The firewall transform names every prefix through the address table; only the transit legs stay IPv4."""

    @pytest.mark.parametrize("golden", _FIREWALLS)
    def test_a_firewall_serving_an_ipv6_segment_renders_it(self, golden: str) -> None:
        """Every prefix of one served segment turned IPv6: the IPv4 golden renders, this one names the IPv6 prefix."""
        text = json.dumps(_golden_input(golden))
        assert run_transform(Firewall, json.loads(text))
        assert "10.0.2.0/24" in text
        data = json.loads(text.replace("10.0.2.0/24", "fd5a:1760::/64"))
        rendered = run_transform(Firewall, data)
        assert "addr-fd5a-1760---64" in rendered

    def test_a_context_with_transit_legs_refuses_an_ipv6_segment(self) -> None:
        """The legs, their VIPs and anycast next hops are IPv4: an IPv6 segment has no next hop to be routed through."""
        context = {
            "legs": [{"ns_type": "prod", "zone": "PROD", "anycast": "100.66.0.1", "interface": "e1.1", "vrf": "PROD"}],
            "segments": [_segment()],
        }
        with pytest.raises(Ipv6SegmentNotRenderedError, match="transit legs of a firewall context"):
            apply_context_routes(context, {})
