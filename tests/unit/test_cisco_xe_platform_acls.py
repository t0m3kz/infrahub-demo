"""Unit tests for the Catalyst 9000 / IOS-XE segment ACLs and inter-VRF statics (``cisco_xe``).

The ACL translation (transforms/helpers/cisco_xe.py) and the ``cisco_xe_acl`` / ``cisco_xe_vrf_static`` templates:
what IOS-XE cannot express in them is refused. Byte-exact rendering is covered by tests/smoke/test_configs_smoke.py;
the platform wiring, the dialect and the refusal guard are in tests/unit/test_cisco_xe_platform.py.
"""

from __future__ import annotations

import re
from typing import Any
from unittest.mock import patch

import pytest

from tests.smoke.fixture_builders.common import run_transform
from tests.unit.cisco_xe_platform_helpers import _golden_input, _make_transform
from transforms.common import BaseDeviceTransform
from transforms.config.access_leaf import AccessLeaf
from transforms.config.access_switch import AccessSwitch
from transforms.config.border_leaf import BorderLeaf
from transforms.config.leaf import Leaf
from transforms.helpers.cisco_xe import get_cisco_xe_acls, get_cisco_xe_vrf_routes, refuse_unfiltered_ipv6

# ---------------------------------------------------------------------------
# 6. Segment ACLs and inter-VRF statics: transforms/helpers/cisco_xe.py + the cisco_xe_acl / cisco_xe_vrf_static templates
# ---------------------------------------------------------------------------

ACL_EVPN = "leaf_cisco_xe_with_acl"
ACL_LISP = "access_switch_cisco_xe_lisp_with_acl"
EXCHANGE_ACL = "leaf_cisco_xe_exchange_acl"


def _rule(**overrides: Any) -> dict[str, Any]:
    """One rule of the neutral ACL model (transforms/helpers/acl.py), permit tcp any any by default."""
    return {
        "seq": 10,
        "action": "permit",
        "protocol": "tcp",
        "src": "any",
        "src_port": None,
        "dst": "any",
        "dst_port": None,
        "established": False,
        "log": False,
        "name": "",
        "src_zone": None,
        "dst_zone": None,
        "src_customer": None,
        "src_environment": None,
        "dst_customer": None,
        "dst_environment": None,
        **overrides,
    }


def _acl(*rules: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"name": "ACL-VLAN100-IN", "vlan_id": 100, "segment_name": "seg", "rules": list(rules)}]


def _entries(*rules: dict[str, Any]) -> list[dict[str, Any]]:
    """The non-remark lines of the IOS-XE ACL built from `rules`."""
    return [line for line in get_cisco_xe_acls(_acl(*rules), "leaf")[0]["lines"] if "remark" not in line]


def _first_policy_rule(data: dict[str, Any]) -> dict[str, Any]:
    """The raw first rule of the policy of the first segment carried by Ethernet10 of a golden input."""
    return _host_segment(data)["security_policy"]["node"]["rules"]["edges"][0]["node"]


def _host_segment(data: dict[str, Any]) -> dict[str, Any]:
    """The raw first segment node carried by Ethernet10 of a golden input."""
    interfaces = data["DcimDevice"]["edges"][0]["node"]["interfaces"]["edges"]
    port = next(e["node"] for e in interfaces if e["node"]["name"]["value"] == "Ethernet10")
    return port["capabilities"]["edges"][0]["node"]


class TestCiscoXeAclTranslation:
    """get_cisco_xe_acls: the neutral ACL model in IOS-XE `ip access-list extended` fields."""

    def test_action_protocol_and_any(self) -> None:
        """permit/deny, ip/tcp/udp/icmp and `any` pass straight through."""
        entries = _entries(
            _rule(action="deny", protocol="ip"), _rule(protocol="udp"), _rule(protocol="icmp", action="deny")
        )

        assert [(e["action"], e["protocol"], e["src"], e["dst"]) for e in entries] == [
            ("deny", "ip", "any", "any"),
            ("permit", "udp", "any", "any"),
            ("deny", "icmp", "any", "any"),
        ]

    @pytest.mark.parametrize(
        ("cidr", "expected"),
        [
            ("10.100.0.0/24", "10.100.0.0 0.0.0.255"),
            ("10.0.0.0/8", "10.0.0.0 0.255.255.255"),
            ("192.168.1.128/25", "192.168.1.128 0.0.0.127"),
            ("10.9.9.9/32", "host 10.9.9.9"),
            ("10.9.9.9", "host 10.9.9.9"),
            ("0.0.0.0/0", "any"),
        ],
    )
    def test_prefixes_become_wildcard_masks_host_or_any(self, cidr: str, expected: str) -> None:
        """A CIDR is `network wildcard`, a /32 is `host`, a /0 is `any`; both ends are translated."""
        entry = _entries(_rule(src=cidr, dst=cidr))[0]

        assert entry["src"] == expected
        assert entry["dst"] == expected

    def test_ports_and_ranges_keep_their_side(self) -> None:
        """`eq N` / `range N M` stay on the side the model put them on, `established` and `log` are carried."""
        entries = _entries(
            _rule(dst_port="eq 443"),
            _rule(protocol="udp", dst_port="range 5000 5010"),
            _rule(src_port="eq 5432", established=True, log=True),
        )

        assert [(e["src_port"], e["dst_port"], e["established"], e["log"]) for e in entries] == [
            (None, "eq 443", False, False),
            (None, "range 5000 5010", False, False),
            ("eq 5432", None, True, True),
        ]

    def test_lines_are_numbered_in_rule_order_and_remarks_get_their_own_number(self) -> None:
        """10, 20, ... in list order; the neutral seqs (consecutive ones here) cannot collide with a remark."""
        lines = get_cisco_xe_acls(
            _acl(
                _rule(seq=1, name="a", src_zone="prod", dst_zone="web"),
                _rule(seq=2, name="b"),
                _rule(seq=9990, name="implicit-deny-all", action="deny", protocol="ip"),
            ),
            "leaf",
        )[0]["lines"]

        assert [line["seq"] for line in lines] == [10, 20, 30, 40, 50, 60, 70]
        assert [line.get("remark") for line in lines] == [
            "Zone: prod -> web",
            "Rule: a",
            None,
            "Rule: b",
            None,
            "Rule: implicit-deny-all",
            None,
        ]

    def test_remarks_carry_zone_customer_and_rule_name_in_that_order(self) -> None:
        """Zone, customer/environment and the rule name each get a remark before their entry."""
        lines = get_cisco_xe_acls(
            _acl(
                _rule(
                    name="r1",
                    src_zone="prod",
                    dst_zone=None,
                    src_customer="Acme",
                    src_environment="prod",
                    dst_customer="Beta",
                )
            ),
            "leaf",
        )[0]["lines"]

        assert [line.get("remark") for line in lines] == [
            "Zone: prod -> any",
            "Customer: Acme/prod -> Beta/any",
            "Rule: r1",
            None,
        ]

    def test_a_remark_is_one_line_of_at_most_100_characters(self) -> None:
        """A rule name with a newline or an overlong name cannot start a second config line or exceed IOS's limit."""
        lines = get_cisco_xe_acls(_acl(_rule(name="evil\nip access-list extended X " + "y" * 200)), "leaf")[0]["lines"]

        remark = lines[0]["remark"]
        assert "\n" not in remark
        assert len(remark) == 100
        assert remark.startswith("Rule: evil ip access-list extended X ")

    def test_empty_input_gives_no_acls(self) -> None:
        """No ACLs in, none out."""
        assert get_cisco_xe_acls(None, "leaf") == []
        assert get_cisco_xe_acls([], "leaf") == []

    @pytest.mark.parametrize(
        ("rule", "message"),
        [
            (_rule(src="2001:db8::/32"), "'2001:db8::/32' is IPv6"),
            (_rule(dst="2001:db8::1"), "'2001:db8::1' is IPv6"),
            (_rule(dst="10.1.1.10/24"), "'10.1.1.10/24' is not an IPv4 network address"),
            (_rule(dst="web-servers"), "'web-servers' is not an IPv4 network address"),
            (_rule(protocol="gre"), "protocol 'gre'"),
            (_rule(action="reject"), "action 'reject'"),
            (_rule(dst_port="gt 1024"), "port match 'gt 1024'"),
            (_rule(dst_port="range 100 10"), "port match 'range 100 10' is out of range"),
            (_rule(dst_port="eq 70000"), "port match 'eq 70000' is out of range"),
            (_rule(protocol="icmp", dst_port="eq 80"), "a port match on protocol 'icmp'"),
            (_rule(protocol="udp", established=True), "`established` on protocol 'udp'"),
        ],
    )
    def test_a_rule_that_cannot_be_expressed_is_refused_not_dropped(self, rule: dict[str, Any], message: str) -> None:
        """Every case names the ACL and the rule and ends the way the other cisco_xe refusals do."""
        with pytest.raises(ValueError) as excinfo:
            get_cisco_xe_acls(_acl(_rule(name="ok"), {**rule, "name": "bad-rule"}), "leaf")

        text = str(excinfo.value)
        assert text.startswith("cisco_xe leaf cannot render ACL ACL-VLAN100-IN rule 'bad-rule': ")
        assert message in text
        assert text.endswith("; refusing incomplete config")


class TestCiscoXeVrfRoutes:
    """get_cisco_xe_vrf_routes: the exchange statics as `ip route vrf` arguments."""

    ROUTES = [
        {"vrf": "PROD", "l3_vni": 50001, "prefix": "0.0.0.0/0", "nexthop": "100.66.0.4"},
        {"vrf": "INTERNET", "l3_vni": 50003, "prefix": "192.0.2.5/32", "nexthop": "100.66.32.4", "local_only": True},
    ]

    def test_dotted_masks_and_exported_flag(self) -> None:
        """/0 and /32 become 0.0.0.0 and 255.255.255.255; `local_only` is not exported."""
        routes = get_cisco_xe_vrf_routes(self.ROUTES, ["PROD", "INTERNET"], "leaf", lisp=False)

        assert [(r["vrf"], r["network"], r["mask"], r["nexthop"], r["exported"]) for r in routes] == [
            ("PROD", "0.0.0.0", "0.0.0.0", "100.66.0.4", True),
            ("INTERNET", "192.0.2.5", "255.255.255.255", "100.66.32.4", False),
        ]

    def test_no_routes(self) -> None:
        """No exchange statics in, none out."""
        assert get_cisco_xe_vrf_routes(None, [], "leaf", lisp=False) == []

    def test_a_vrf_the_device_does_not_define_is_refused(self) -> None:
        """The `ip route vrf` line would be rejected by the device."""
        with pytest.raises(ValueError, match="the VRF is not defined on this device; refusing incomplete config"):
            get_cisco_xe_vrf_routes(self.ROUTES, ["PROD"], "leaf", lisp=False)

    @pytest.mark.parametrize(
        "route",
        [
            {"vrf": "PROD", "prefix": "::/0", "nexthop": "100.66.0.4"},
            {"vrf": "PROD", "prefix": "0.0.0.0/0", "nexthop": "2001:db8::1"},
        ],
    )
    def test_ipv6_is_refused(self, route: dict[str, Any]) -> None:
        """An IPv6 prefix or next hop has no `ip route` form."""
        with pytest.raises(ValueError, match="cisco_xe leaf cannot render the VRF PROD static .*: IPv6"):
            get_cisco_xe_vrf_routes([route], ["PROD"], "leaf", lisp=False)

    def test_an_exported_static_is_refused_on_a_lisp_fabric(self) -> None:
        """LISP has no redistribution into the VRF: an exported static would stay local, silently."""
        with pytest.raises(ValueError, match="a LISP fabric cannot redistribute an exchange static"):
            get_cisco_xe_vrf_routes(self.ROUTES, ["PROD", "INTERNET"], "leaf", lisp=True)

    def test_a_local_only_static_is_rendered_on_a_lisp_fabric(self) -> None:
        """A static that is not exported needs no redistribution: LISP renders it as a plain `ip route`."""
        routes = get_cisco_xe_vrf_routes(self.ROUTES[1:], ["INTERNET"], "leaf", lisp=True)

        assert [r["exported"] for r in routes] == [False]


class TestCiscoXeIpv6SviIsRefused:
    """An IPv6 gateway SVI cannot be protected by the IPv4 segment ACL."""

    def test_an_ipv6_gateway_svi_with_an_acl_is_refused(self) -> None:
        """An `ip access-group` ACL never filters an IPv6 SVI: refuse rather than ship it open."""
        with pytest.raises(ValueError, match="the segment ACL of VLAN 300: its gateway SVI is IPv6"):
            refuse_unfiltered_ipv6(
                [
                    {"vlan_id": 100, "acl_name": "ACL-VLAN100-IN", "gateway_ipv6": None},
                    {"vlan_id": 300, "acl_name": "ACL-VLAN300-IN", "gateway_ipv6": "2001:db8::1/64"},
                ],
                "leaf",
            )

    def test_an_ipv6_svi_without_an_acl_and_ipv4_svis_pass(self) -> None:
        """No ACL on the IPv6 SVI (no security policy in the query): nothing to protect, nothing to refuse."""
        refuse_unfiltered_ipv6(
            [
                {"vlan_id": 100, "acl_name": "ACL-VLAN100-IN", "gateway_ipv6": None},
                {"vlan_id": 300, "acl_name": None, "gateway_ipv6": "2001:db8::1/64"},
            ],
            "leaf",
        )

    def test_through_transform(self) -> None:
        """An IPv6 gateway on the ACL-protected segment makes transform() raise."""
        data = _golden_input(ACL_EVPN)
        gateway = _host_segment(data)["gateway"]["node"]
        gateway["address"]["value"] = "2001:db8:100::1/64"
        gateway["ip_prefix"]["node"]["prefix"]["value"] = "2001:db8:100::/64"

        with pytest.raises(
            ValueError, match="cisco_xe leaf cannot render the segment ACL of VLAN 100: its gateway SVI"
        ):
            run_transform(Leaf, data)


# (golden, transform, device role): the EVPN leaf and the office's LISP access-switch both render the segment ACLs.
ACL_GOLDENS = [(ACL_EVPN, Leaf, "leaf"), (ACL_LISP, AccessSwitch, "access-switch")]


class TestCiscoXeAclRenderingThroughTransform:
    """The golden inputs with a security policy render ACLs on the EVPN and the LISP path."""

    @pytest.mark.parametrize(("golden", "transform_cls", "role"), ACL_GOLDENS)
    def test_acl_is_defined_before_the_svi_that_binds_it(
        self, golden: str, transform_cls: type[BaseDeviceTransform], role: str
    ) -> None:
        """`ip access-list extended` precedes `interface Vlan<N>` and `ip access-group ... in` names the same ACL."""
        output = run_transform(transform_cls, _golden_input(golden))

        for vlan in (100, 200):
            name = f"ACL-VLAN{vlan}-IN"
            assert output.index(f"ip access-list extended {name}\n") < output.index(f"interface Vlan{vlan}\n")
            assert re.search(rf"^interface Vlan{vlan}\n(?: .*\n)*? ip access-group {name} in\n", output, re.M)

    @pytest.mark.parametrize(("golden", "transform_cls", "role"), ACL_GOLDENS)
    def test_entries_use_wildcards_ports_remarks_and_the_closing_deny(
        self, golden: str, transform_cls: type[BaseDeviceTransform], role: str
    ) -> None:
        """The shapes IOS-XE writes differently from NX-OS/EOS: wildcard masks, host, ranges, established, remarks."""
        output = run_transform(transform_cls, _golden_input(golden))

        for expected in (
            " 20 permit tcp any any eq 443\n",
            " 70 permit udp any 10.200.0.0 0.0.0.255 range 5000 5010\n",
            " 90 permit icmp any any\n",
            " 110 permit tcp any host 10.9.9.9 eq 5432 log\n",
            " 130 deny ip any any log\n",
            " 50 remark Zone: any -> web\n",
            " 50 permit tcp 10.200.0.0 0.0.0.255 eq 8443 10.100.0.0 0.0.0.255 established\n",
        ):
            assert expected in output, expected
        assert not re.search(r"^ \d+ (permit|deny) .*\d+\.\d+\.\d+\.\d+/\d+", output, re.M)

    @pytest.mark.parametrize(("golden", "transform_cls", "role"), ACL_GOLDENS)
    def test_the_acl_blocks_are_marked_unverified_no_lab(
        self, golden: str, transform_cls: type[BaseDeviceTransform], role: str
    ) -> None:
        """No lab has run an IOS-XE ACL: the ACL section and the SVI binding say so."""
        output = run_transform(transform_cls, _golden_input(golden))

        section = output.split("! Zero-Trust ACLs", 1)[1].split("ip access-list extended", 1)[0]
        assert "! unverified-no-lab:" in section
        assert "! unverified-no-lab: `ip access-group <name> in`" in output

    @pytest.mark.parametrize(
        ("golden", "transform_cls", "plain"),
        [
            (ACL_EVPN, Leaf, "leaf_cisco_xe_ebgp_ibgp"),
            (ACL_LISP, AccessSwitch, "access_switch_cisco_xe_lisp_ospf"),
        ],
    )
    def test_the_only_difference_from_the_policy_free_golden_is_the_acl(
        self, golden: str, transform_cls: type[BaseDeviceTransform], plain: str
    ) -> None:
        """Same hosted segments, with and without a security policy: dropping the ACL lines gives the plain config."""
        with_acl = run_transform(transform_cls, _golden_input(golden))
        stripped = re.sub(r"! Zero-Trust ACLs.*?(?=! Interfaces)", "", with_acl, flags=re.S)
        stripped = re.sub(
            r"^ ip access-group .*\n|^! unverified-no-lab: `ip access-group.*\n", "", stripped, flags=re.M
        )

        assert stripped == run_transform(transform_cls, _golden_input(plain))

    def test_the_lisp_path_still_renders_the_instances_of_the_hosted_segment(self) -> None:
        """Hosts behind an ACL are still registered: L3 instance-id for the VRF, L2 for the VNI, mobility on the SVI."""
        output = run_transform(AccessSwitch, _golden_input(ACL_LISP))

        assert re.search(r"^router lisp$", output, re.M)
        assert " instance-id 50001\n" in output
        assert " instance-id 10100\n" in output
        assert re.search(r"^ lisp mobility VRF_A-IPV4$", output, re.M)
        for absent in ("l2vpn evpn", "nve1", "router bgp"):
            assert absent not in output, absent

    @pytest.mark.parametrize(("golden", "transform_cls", "role"), ACL_GOLDENS)
    def test_an_ipv6_rule_endpoint_is_refused_through_transform(
        self, golden: str, transform_cls: type[BaseDeviceTransform], role: str
    ) -> None:
        """A rule whose destination is an IPv6 prefix cannot go into an `ip access-list extended`."""
        data = _golden_input(golden)
        _first_policy_rule(data)["destination_ip_addresses"] = {
            "edges": [{"node": {"address": {"value": "2001:db8::/32"}}}]
        }

        with pytest.raises(ValueError) as excinfo:
            run_transform(transform_cls, data)

        assert str(excinfo.value).startswith(f"cisco_xe {role} cannot render ACL ACL-VLAN100-IN rule 'allow-https': ")
        assert "IPv6" in str(excinfo.value)

    def test_access_leaf_renders_the_acl_too(self) -> None:
        """The access-leaf shares the leaf template: the same input renders the same ACL block."""
        data = _golden_input(ACL_EVPN)
        data["DcimDevice"]["edges"][0]["node"]["role"]["value"] = "access-leaf"

        output = run_transform(AccessLeaf, data)

        assert "ip access-list extended ACL-VLAN100-IN\n" in output
        assert " ip access-group ACL-VLAN100-IN in\n" in output

    def test_border_leaf_hosting_a_tenant_segment_is_still_refused_by_the_platform_independent_guard(self) -> None:
        """Every platform's border leaf refuses a routed tenant SVI with an ACL (_refuse_unprotected_tenant_svis)."""
        data = _golden_input(ACL_EVPN)
        data["DcimDevice"]["edges"][0]["node"]["role"]["value"] = "border-leaf"

        with pytest.raises(ValueError, match="cisco_xe border-leaf cannot render the SVI ACL for VLANs"):
            run_transform(BorderLeaf, data)

    def test_pbr_is_still_refused_with_acls_present(self) -> None:
        """Lifting the ACL refusal does not lift PBR: a PBR rule on a device with ACLs still raises."""
        real = BaseDeviceTransform._template_context

        def with_pbr(self: BaseDeviceTransform, *args: Any, **kwargs: Any) -> dict[str, Any]:
            return {**real(self, *args, **kwargs), "leaf_pbr_rules": [{"vlan_id": 100}]}

        with patch.object(BaseDeviceTransform, "_template_context", with_pbr):
            with pytest.raises(ValueError, match="cisco_xe leaf cannot render: leaf_pbr_rules"):
                run_transform(Leaf, _golden_input(ACL_EVPN))

    def test_other_platforms_and_roles_get_no_cisco_xe_context(self) -> None:
        """The cisco_xe keys exist only on a cisco_xe fabric device; an IPv6 rule is not this guard's business elsewhere."""
        config = {"acls": _acl(_rule(src="2001:db8::/32"))}

        for platform in ("arista_eos", "cisco_nxos"):
            assert _make_transform("leaf")._cisco_xe_context(platform, config) == {}
        assert _make_transform("spine")._cisco_xe_context("cisco_xe", config) == {}


class TestCiscoXeVrfStaticRendering:
    """The exchange statics: `ip route vrf` lines, and for exported ones the prefix-list, route-map and redistribution."""

    def test_statics_are_rendered_with_dotted_masks(self) -> None:
        """The exchange statics of the golden (PROD 0/0 to the shared context VIP, the SNAT /32s in INTERNET)."""
        output = run_transform(Leaf, _golden_input(EXCHANGE_ACL))

        for expected in (
            "ip route vrf INTERNET 192.0.2.5 255.255.255.255 100.66.32.4\n",
            "ip route vrf INTERNET 192.0.2.6 255.255.255.255 100.66.32.12\n",
            "ip route vrf PROD 0.0.0.0 0.0.0.0 100.66.0.4\n",
        ):
            assert expected in output, expected
        assert not re.search(r"^ip route vrf \S+ \S+/\d+", output, re.M)

    def test_exported_statics_are_redistributed_into_the_vrf_address_family_by_an_exact_match_route_map(self) -> None:
        """Exchange statics enter EVPN as type-5 routes, exact match only, only in the VRFs that have one."""
        output = run_transform(Leaf, _golden_input(EXCHANGE_ACL))

        assert "ip prefix-list PL-VRF-STATIC-PROD seq 10 permit 0.0.0.0/0\n" in output
        assert (
            "route-map RM-VRF-STATIC-2-EVPN-PROD permit 10\n match ip address prefix-list PL-VRF-STATIC-PROD\n"
            in output
        )
        assert re.search(
            r"^ address-family ipv4 vrf PROD\n  advertise l2vpn evpn\n  redistribute connected\n"
            r"  redistribute static route-map RM-VRF-STATIC-2-EVPN-PROD\n exit-address-family$",
            output,
            re.M,
        )
        assert "RM-VRF-STATIC-2-EVPN-MANAGEMENT" not in output

    def test_a_local_only_static_is_neither_prefix_listed_nor_redistributed(self) -> None:
        """The edge-style `local_only` default route stays a static inside its VRF."""
        real = BaseDeviceTransform._template_context

        def local_only(self: BaseDeviceTransform, *args: Any, **kwargs: Any) -> dict[str, Any]:
            config = real(self, *args, **kwargs)
            config["vrf_routes"] = [{**route, "local_only": True} for route in config["vrf_routes"]]
            return config

        with patch.object(BaseDeviceTransform, "_template_context", local_only):
            output = run_transform(Leaf, _golden_input(EXCHANGE_ACL))

        assert "ip route vrf PROD 0.0.0.0 0.0.0.0 100.66.0.4\n" in output
        assert "PL-VRF-STATIC" not in output
        assert "redistribute static" not in output

    def test_a_static_in_an_undefined_vrf_is_refused_through_transform(self) -> None:
        """A static whose VRF the device does not define would be rejected by it: refuse instead."""
        real = BaseDeviceTransform._template_context

        def stray_vrf(self: BaseDeviceTransform, *args: Any, **kwargs: Any) -> dict[str, Any]:
            config = real(self, *args, **kwargs)
            config["vrf_routes"] = [*config["vrf_routes"], {**config["vrf_routes"][0], "vrf": "GHOST"}]
            return config

        with patch.object(BaseDeviceTransform, "_template_context", stray_vrf):
            with pytest.raises(ValueError, match="the VRF GHOST static .*: the VRF is not defined on this device"):
                run_transform(Leaf, _golden_input(EXCHANGE_ACL))

    def test_border_leaf_renders_the_same_statics_without_an_acl(self) -> None:
        """The border-leaf golden has the statics and no security policy: no ACL, no access-group."""
        output = run_transform(BorderLeaf, _golden_input("border_leaf_cisco_xe_exchange_transit"))

        assert "ip route vrf PROD 0.0.0.0 0.0.0.0 100.66.0.4\n" in output
        assert "ip access-list extended" not in output
        assert "ip access-group" not in output
