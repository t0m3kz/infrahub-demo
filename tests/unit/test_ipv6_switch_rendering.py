"""IPv6 segments on the fabric switches: the family-aware helpers and what the templates write for them.

A segment has ONE gateway of ONE family (``ip_version``); a switch that carries one renders it in IPv6 form: the
anycast flag, the VRF's IPv6 address family with its route-targets, the IPv6 ACL with its internet band, the
routing-identity origination. A device may carry both families at once (the ``leaf_cisco_nxos_dual_stack`` golden).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from transforms.helpers.acl import get_acls
from transforms.helpers.cisco_xe import get_cisco_xe_acls
from transforms.helpers.overlay import build_overlay_node
from utils.internet_space import NON_INTERNET_PREFIXES, NON_INTERNET_PREFIXES_IPV6, non_internet_prefixes
from utils.segment_addressing import (
    AddressFamilyMismatchError,
    own_prefix,
    prefix_family,
    segment_family,
)

CONFIGS = Path(__file__).resolve().parents[1] / "smoke" / "configs"
V6_PREFIX = "fd5a:1760:0:100::/64"


def _segment(prefix: str | None, address: str | None = None, **extra: object) -> dict:
    """A cleaned segment with a gateway (None: L2 only) and an empty security policy."""
    gateway = {"address": address, "ip_prefix": {"prefix": prefix}} if prefix else None
    return {
        "id": "seg-a",
        "name": "seg-a",
        "customer_name": "seg-a",
        "gateway": gateway,
        "security_policy": {"name": "p", "enabled": True, "default_action": "deny", "rules": []},
        **extra,
    }


def _activation(segment: dict, vlan_id: int = 100) -> dict:
    return {"vlan_id": vlan_id, "vni": 10000 + vlan_id, "segment": segment}


class TestFamilyHelpers:
    """The one place that says which family a prefix or a segment is in."""

    @pytest.mark.parametrize(
        ("value", "family"), [("10.0.0.0/24", "ipv4"), ("fd5a::/64", "ipv6"), ("2001:db8::1", "ipv6"), ("any", "ipv4")]
    )
    def test_prefix_family(self, value: str, family: str) -> None:
        """A colon makes it IPv6."""
        assert prefix_family(value) == family

    @pytest.mark.parametrize(
        ("segment", "family"),
        [
            (_segment(V6_PREFIX, "fd5a:1760:0:100::1/64"), "ipv6"),
            (_segment("10.1.0.0/24", "10.1.0.1/24"), "ipv4"),
            (_segment(None), None),
            ({"name": "cloud", "cidr_block": "10.9.0.0/16"}, "ipv4"),
        ],
    )
    def test_segment_family_follows_the_gateway(self, segment: dict, family: str | None) -> None:
        """The gateway decides; an L2-only segment has no family, a cloud segment is IPv4."""
        assert segment_family(segment) == family

    def test_own_prefix_is_the_segments_one_prefix(self) -> None:
        """IPv4 first, else IPv6: a segment has only one."""
        assert own_prefix(_segment(V6_PREFIX)) == V6_PREFIX
        assert own_prefix(_segment("10.1.0.0/24")) == "10.1.0.0/24"

    def test_non_internet_prefixes_per_family(self) -> None:
        """IPv6 gets its own list: ULA, link-local, multicast, loopback; the IPv4 list is untouched."""
        assert non_internet_prefixes("ipv4") == NON_INTERNET_PREFIXES
        assert non_internet_prefixes("ipv6") == NON_INTERNET_PREFIXES_IPV6
        assert {"fc00::/7", "fe80::/10", "ff00::/8", "::1/128"} <= set(NON_INTERNET_PREFIXES_IPV6)
        assert all(":" in prefix for prefix in NON_INTERNET_PREFIXES_IPV6)


class TestIpv6Acl:
    """get_acls writes the ACL of the segment's family."""

    def test_an_ipv6_segment_gets_an_ipv6_acl_closed_by_ipv6_deny(self) -> None:
        """Family, protocol keywords and the closing rule are IPv6; an IPv4 segment's are unchanged."""
        v6 = _segment(V6_PREFIX)
        v6["security_policy"]["rules"] = [
            {"index": 10, "name": "ssh", "action": "permit", "protocol": "tcp", "port_start": 22},
            {"index": 20, "name": "any", "action": "permit", "protocol": "any"},
        ]
        v4 = _segment("10.1.0.0/24", id="seg-b")
        v4["security_policy"]["rules"] = [{"index": 10, "name": "any", "action": "permit", "protocol": "any"}]
        acl6, acl4 = get_acls([_activation(v6, 100), _activation(v4, 200)])
        assert acl6["family"] == "ipv6" and acl4["family"] == "ipv4"
        assert [r["protocol"] for r in acl6["rules"]] == ["tcp", "ipv6", "ipv6"]
        assert acl6["rules"][-1]["name"] == "implicit-deny-all"
        assert [r["protocol"] for r in acl4["rules"]] == ["ip", "ip"]

    def test_the_internet_band_denies_the_ipv6_non_internet_space(self) -> None:
        """First-match ACLs have no 'not': every IPv6 non-internet prefix is denied before `permit ... any`."""
        segment = _segment(V6_PREFIX)
        segment["security_policy"]["rules"] = [
            {
                "index": 10,
                "name": "web",
                "action": "permit",
                "protocol": "tcp",
                "port_start": 443,
                "destination_zone": {"name": "INTERNET"},
            }
        ]
        (acl,) = get_acls([_activation(segment)])
        rules = acl["rules"]
        permit_at = next(i for i, r in enumerate(rules) if r["action"] == "permit")
        assert [r["dst"] for r in rules[:permit_at]] == list(NON_INTERNET_PREFIXES_IPV6)
        assert all(r["action"] == "deny" and r["protocol"] == "ipv6" for r in rules[:permit_at])
        assert rules[permit_at]["dst"] == "any"

    def test_a_rule_naming_an_endpoint_of_the_other_family_is_refused(self) -> None:
        """No line of an IPv6 ACL can match an IPv4 prefix: refused, never dropped."""
        segment = _segment(V6_PREFIX)
        segment["security_policy"]["rules"] = [
            {
                "index": 10,
                "name": "to-v4",
                "action": "permit",
                "protocol": "tcp",
                "port_start": 80,
                "destination_zone": {"name": "PROD"},
                "destination_prefixes": [{"prefix": "10.9.0.0/16"}],
            }
        ]
        with pytest.raises(AddressFamilyMismatchError, match="'to-v4' matches 10.9.0.0/16"):
            get_acls([_activation(segment)])

    def test_the_ios_xe_form_of_an_ipv6_acl(self) -> None:
        """Prefix notation and `host`, the family on the ACL, an IPv4 endpoint refused."""
        acl = {
            "name": "ACL-VLAN100-IN",
            "vlan_id": 100,
            "family": "ipv6",
            "rules": [
                {
                    "seq": 10,
                    "action": "permit",
                    "protocol": "tcp",
                    "src": V6_PREFIX,
                    "dst": "fd5a::5/128",
                    "dst_port": "eq 22",
                },
                {"seq": 9990, "action": "deny", "protocol": "ipv6", "src": "any", "dst": "any"},
            ],
        }
        (converted,) = get_cisco_xe_acls([acl], "leaf")
        assert converted["family"] == "ipv6"
        assert (converted["lines"][0]["src"], converted["lines"][0]["dst"]) == (V6_PREFIX, "host fd5a::5")
        acl["rules"][0]["dst"] = "10.0.0.0/8"
        with pytest.raises(ValueError, match="is IPv4, in an IPv6 ACL"):
            get_cisco_xe_acls([acl], "leaf")


class TestOverlayFamilyAwareness:
    """The anycast-gateway flag and the VRF's IPv6 family are decided by the gateway's family."""

    @staticmethod
    def _node(segment: dict):
        segment["security_zone"] = {"name": "PROD", "l3_vni": 3901}
        return build_overlay_node([], "leaf", [_activation(segment)], [], frozenset(), "00:1c:73:00:dc:01")

    def test_an_ipv6_only_device_has_the_anycast_gateway(self) -> None:
        """Before, `any(gateway_ip)` left an IPv6-only device without the anycast MAC and flag."""
        node = self._node(_segment(V6_PREFIX, "fd5a:1760:0:100::1/64"))
        assert node is not None and node.gateway.enabled is True
        assert node.l2[0].gateway_ipv6 and not node.l2[0].gateway_ip

    def test_no_gateway_no_anycast(self) -> None:
        """An L2-only segment has none."""
        node = self._node(_segment(None))
        assert node is not None and node.gateway.enabled is False


def _golden(name: str) -> str:
    return (CONFIGS / name / "output.txt").read_text()


class TestRenderedGoldens:
    """The new goldens show the IPv6 form on every leaf platform; the dual-stack one both families in one VRF."""

    @pytest.mark.parametrize(
        ("platform", "gateway", "acl_line"),
        [
            ("cisco_nxos", "ipv6 address fd5a:1760:0:100::1/64", "ipv6 access-list ACL-VLAN100-IN"),
            ("arista_eos", "ipv6 address virtual fd5a:1760:0:100::1/64", "ipv6 access-list ACL-VLAN100-IN"),
            ("cisco_xe", "ipv6 address fd5a:1760:0:100::1/64", "ipv6 access-list ACL-VLAN100-IN"),
            ("sonic", '"Vlan100|fd5a:1760:0:100::1/64"', '"type": "L3V6"'),
            ("dell_sonic", '"Vlan100|fd5a:1760:0:100::1/64"', '"type": "L3V6"'),
        ],
    )
    def test_every_platform_renders_the_svi_and_the_ipv6_acl(self, platform: str, gateway: str, acl_line: str) -> None:
        """The SVI address, and an IPv6 ACL bound on it."""
        text = _golden(f"leaf_{platform}_ipv6_segments")
        assert gateway in text and acl_line in text
        assert not re.search(r"^ip access-list", text, re.M)

    def test_the_dual_stack_vrf_has_both_families_and_both_origination_matches(self) -> None:
        """NX-OS: ipv6 address family in `vrf context` and under BGP, the route-map matches each family."""
        text = _golden("leaf_cisco_nxos_dual_stack")
        assert text.count("address-family ipv6 unicast") >= 2
        assert "match ipv6 address prefix-list PL-ORIG-seg-100" in text
        assert "match ip address prefix-list PL-ORIG-seg-200" in text
        assert "ipv6 traffic-filter ACL-VLAN100-IN in" in text and "ip access-group ACL-VLAN200-IN in" in text


class TestRouteProtectionCarriesTheIpv6Prefix:
    """The BGP route protection derives from the model: an IPv6 segment's prefix reaches the IPv6 lists."""

    def test_the_environment_prefix_of_an_ipv6_segment_becomes_an_ipv6_type5_entry(self) -> None:
        """transforms/helpers/bgp_facts.py reads the zone's segments' gateway prefixes whatever the family; the EVPN
        expectation turns them into exact entries of family ipv6 (the templates write `ipv6 prefix-list` for those)."""
        from transforms.helpers.bgp_facts import environment_prefixes
        from utils.bgp_expectation import Fabric, _type5

        roots = {
            "environment_zones": [
                {
                    "name": "PROD",
                    "vrf_type": "prod",
                    "network_segments": [{"gateway": {"ip_prefix": {"prefix": V6_PREFIX}}}],
                    "vrfs": [],
                }
            ]
        }
        by_namespace, ranges = environment_prefixes(roots)
        assert by_namespace == {"PROD": frozenset({V6_PREFIX})}
        fabric = Fabric(namespace_prefixes=by_namespace, range_namespaces=ranges)
        (entry,) = _type5(frozenset({"PROD"}), fabric)
        assert (entry.prefix, entry.family) == (V6_PREFIX, "ipv6")


class TestFamilyAwareGuards:
    """The IPv4-by-design guards stay IPv4 and say so; the family-aware ones handle both."""

    def test_the_snat_address_check_is_ipv4_by_design(self) -> None:
        """is_public_address judges a snat_address, which is IPv4: an IPv6 address is not 'public' there."""
        from utils.exchange_transit import is_public_address

        assert is_public_address("198.51.100.7/32") and not is_public_address("fd5a::1/128")

    def test_the_pool_utilization_count_handles_both_families(self) -> None:
        """Assignable addresses: an IPv4 /24 loses 2, an IPv6 /64 loses 1."""
        from ipaddress import ip_network

        from checks.pool_utilization import _assignable

        assert _assignable(ip_network("10.0.0.0/24")) == 254
        assert _assignable(ip_network("fd5a::/64")) == 2**64 - 1

    def test_a_vrf_static_of_the_exchange_is_ipv4_by_design(self) -> None:
        """The exchange statics run through the IPv4 legs of a context: an IPv6 static is refused, not dropped."""
        from transforms.helpers.static_routes import get_static_routes

        cap = {
            "typename": "ManagedStaticRoute",
            "name": "r",
            "prefix": "fd5a::/64",
            "next_hop": "fd5a::1",
            "vrf": {"security_zone": {"name": "PROD", "l3_vni": 1}},
        }
        with pytest.raises(ValueError, match="is not IPv4"):
            get_static_routes([cap])

    def test_the_internet_band_of_an_ipv6_permit_denies_the_ula_and_documentation_space_first(self) -> None:
        """Before `permit ... any`: the tenant pools (fd5a::/32 inside fc00::/7), link-local, multicast, documentation."""
        from ipaddress import IPv6Network

        bands = [IPv6Network(n) for n in NON_INTERNET_PREFIXES_IPV6]
        for prefix in ("fd5a:1760::/32", "fd5a:1840::/32", "fe80::/64", "ff02::/16", "2001:db8::/48", "::1/128"):
            net = IPv6Network(prefix)
            assert any(net.subnet_of(band) for band in bands), prefix


class TestRouteProtectionTakesTheIpv6SegmentPrefix:
    """utils/bgp_expectation.py takes an IPv6 segment's prefix into the IPv6 lists (nothing rewritten)."""

    def test_the_type5_entry_and_the_ipv6_list_entries_of_the_session_policy(self) -> None:
        """An internal VRF session of a device in the namespace: the ipv6 family allows the IPv6 segment prefix."""
        from utils.bgp_expectation import Device, Fabric, expect_session

        fabric = Fabric(namespace_prefixes={"PROD": frozenset({V6_PREFIX, "10.1.0.0/24"})})
        device = Device("leaf-1", "leaf", namespaces=frozenset({"PROD"}))
        session = {"vrf": "PROD", "remote_device": "bl", "address_families": ["ipv4", "ipv6"], "session_type": "IBGP"}
        result = expect_session(session, device, fabric)
        families = {family: policy for family, policy in result.families.items()}
        v6 = families["ipv6"].inbound
        assert v6 is not None and any(e.prefix == V6_PREFIX and e.family == "ipv6" for e in v6)
        v4 = families["ipv4"].inbound
        assert v4 is not None and all(e.family == "ipv4" for e in v4)
