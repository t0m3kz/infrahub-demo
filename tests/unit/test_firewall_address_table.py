"""The firewall address table: one named object per prefix, family-agnostic, rendered by every vendor.

An IPv4-only, an IPv6-only and a mixed firewall render through the same table (transforms/helpers/address_table.py).
An IPv6 rule endpoint is rendered by NAME; an endpoint that resolves to no address is an error, never ``any``.
"""

from __future__ import annotations

import pytest

from checks.internet_egress import internet_rules
from tests.smoke.fixture_builders.common import run_transform
from tests.smoke.fixture_builders.firewall import build_firewall_data
from tests.smoke.fixture_builders.ipv6 import build_firewall_ipv6_data
from transforms.config.firewall import Firewall
from transforms.helpers.address_table import (
    UnresolvedEndpointError,
    address_name,
    build_address_table,
    endpoint_ref,
)
from transforms.helpers.firewall import get_zone_policies

PLATFORMS = ["paloalto_panos", "checkpoint_gaia", "juniper_junos"]
V6_NAME = "addr-fd5a-1760-0-2---64"


class TestTheTable:
    """Names, families, order."""

    @pytest.mark.parametrize(
        ("cidr", "name"),
        [
            ("10.0.2.0/24", "addr-10-0-2-0-24"),
            ("fd5a:1760:0:2::/64", "addr-fd5a-1760-0-2---64"),
            ("2001:db8::1/128", "addr-2001-db8--1-128"),
        ],
    )
    def test_names_extend_the_ipv4_scheme(self, cidr: str, name: str) -> None:
        """IPv4 names are what the templates always wrote; ':' becomes '-' so an IPv6 name is valid everywhere."""
        assert address_name(cidr) == name

    def test_an_endpoint_that_is_no_network_is_an_error(self) -> None:
        """Not 'any', not skipped."""
        assert endpoint_ref(None) is None
        with pytest.raises(UnresolvedEndpointError, match="'web-servers'"):
            endpoint_ref("web-servers")

    def test_members_come_first_in_zone_order_then_the_other_endpoints_sorted(self) -> None:
        """Both families in one table; each entry knows its family and its zones."""
        zones = [
            {"name": "PROD", "member_cidrs": ["10.0.1.0/24", "fd5a::/64"]},
            {"name": "DMZ", "member_cidrs": ["10.0.1.0/24"]},
        ]
        rules = [{"rules": [{"src": "fd5b::/64", "dst": "10.0.1.0/24"}, {"src": "10.9.0.0/16", "dst": None}]}]
        table = build_address_table(zones, rules)
        assert [(e["cidr"], e["family"], e["zones"]) for e in table] == [
            ("10.0.1.0/24", 4, ["PROD", "DMZ"]),
            ("fd5a::/64", 6, ["PROD"]),
            ("10.9.0.0/16", 4, []),
            ("fd5b::/64", 6, []),
        ]


class TestRuleEndpoints:
    """get_zone_policies resolves a rule's endpoints through the table."""

    @staticmethod
    def _policy(segment: dict | None) -> list[dict]:
        rule = {
            "id": "r1",
            "index": 10,
            "name": "r",
            "action": "permit",
            "protocol": "tcp",
            "port_start": 22,
            "source_segment": segment,
            "destination_zone": {"name": "PROD"},
        }
        return [{"name": "p", "enabled": True, "default_action": "deny", "rules": [rule]}]

    def test_an_ipv6_segment_endpoint_carries_its_prefix_and_its_name(self) -> None:
        """src/src_ref: the CIDR and the table name; an IPv4 endpoint carries no ref (inline, byte-identical IPv4 output)."""
        segment = {"id": "s", "name": "s", "gateway": {"ip_prefix": {"prefix": "fd5a:1760::/64"}}}
        (policy,) = get_zone_policies(self._policy(segment))
        rule = policy["rules"][0]
        assert (rule["src"], rule["src_ref"]) == ("fd5a:1760::/64", "addr-fd5a-1760---64")
        v4 = {"id": "s", "name": "s", "gateway": {"ip_prefix": {"prefix": "10.1.0.0/24"}}}
        assert get_zone_policies(self._policy(v4))[0]["rules"][0]["src_ref"] is None

    def test_a_segment_with_no_prefix_is_an_error_not_any(self) -> None:
        """The endpoint would resolve to nothing and render as any: a widened rule."""
        with pytest.raises(UnresolvedEndpointError, match="has no prefix"):
            get_zone_policies(self._policy({"id": "s", "name": "s", "gateway": None}))


@pytest.mark.parametrize("platform", PLATFORMS)
class TestEveryVendorRendersTheTable:
    """IPv4-only (the golden input), mixed and IPv6-only firewalls."""

    def test_an_ipv4_only_firewall_has_no_ipv6_reference(self, platform: str) -> None:
        """Nothing changes for IPv4: no IPv6 object, no reference."""
        text = run_transform(Firewall, build_firewall_data(device_name="dc1-fw-01", platform=platform))
        assert "fd5a" not in text and "ipv6" not in text.lower().replace("ipv6-address", "")

    def test_a_mixed_firewall_names_the_ipv6_endpoint_and_keeps_the_ipv4_inline(self, platform: str) -> None:
        """The zone member object, the rule reference (by name) and the IPv4 destination inline."""
        text = run_transform(Firewall, build_firewall_ipv6_data(device_name="dc1-fw-01", platform=platform))
        assert V6_NAME in text and text.count(V6_NAME) >= 3
        assert "10.0.1.0/24" in text
        assert "fd5a:1760:0:2::/64" in text

    def test_an_ipv6_only_firewall_renders_no_ipv4_prefix_in_a_rule(self, platform: str) -> None:
        """Every endpoint of the rules is a name of the table."""
        text = run_transform(
            Firewall, build_firewall_ipv6_data(device_name="dc1-fw-01", platform=platform, ipv6_only=True)
        )
        assert "addr-fd5a-1760-0-1---64" in text and "addr-fd5a-1760-0-10---64" in text
        assert "10.0.1.0/24" not in text and "10.0.2.0/24" not in text


class TestPlatformSyntax:
    """Each vendor's form of the IPv6 object and route."""

    def test_pan_os(self) -> None:
        """ip-netmask takes the IPv6 prefix; the route is in the ipv6 table with an ipv6-address next hop."""
        text = run_transform(Firewall, build_firewall_ipv6_data(device_name="dc1-fw-01", platform="paloalto_panos"))
        assert f"set address {V6_NAME} ip-netmask fd5a:1760:0:2::/64" in text
        assert "routing-table ipv6 static-route NONPROD-1 nexthop ipv6-address fd5a:1760:0:2::2" in text

    def test_junos(self) -> None:
        """Zone address book entry, and the route in the instance's inet6 table."""
        text = run_transform(Firewall, build_firewall_ipv6_data(device_name="dc1-fw-01", platform="juniper_junos"))
        assert f"address-book address {V6_NAME} fd5a:1760:0:2::/64" in text
        assert "rib NONPROD.inet6.0 static route fd5a:1760:0:2::/64" in text

    def test_check_point(self) -> None:
        """A network object with subnet6 / mask-length6, and the ipv6 static route."""
        text = run_transform(Firewall, build_firewall_ipv6_data(device_name="dc1-fw-01", platform="checkpoint_gaia"))
        assert f'add network name "{V6_NAME}" subnet6 "fd5a:1760:0:2::" mask-length6 64' in text
        assert "set ipv6 static-route fd5a:1760:0:2::/64" in text


class TestInternetEgressAcceptedGap:
    """An IPv6 source is not translated, so the egress check demands no snat_address for it."""

    @staticmethod
    def _rule(version: str) -> dict:
        return {
            "id": "r",
            "name": "web",
            "destination_zone": {"name": "INTERNET"},
            "source_segment": {"id": "s", "name": "s", "ip_version": version},
        }

    def test_an_ipv6_source_is_not_judged(self) -> None:
        """The IPv4 source is."""
        assert [r["name"] for r in internet_rules([self._rule("ipv4"), self._rule("ipv6")])] == ["web"]
        assert internet_rules([self._rule("ipv6")]) == []
