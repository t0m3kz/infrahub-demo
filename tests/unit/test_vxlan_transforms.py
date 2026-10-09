"""Unit tests for VXLAN/ACL transform functions in transforms/common.py.

Covers:
  - get_vlans()                  — activation-based VLAN extraction
  - _vlans_from_activations()    — VLAN list from SegmentDeployment records
  - _l2_from_activations()       — L2 VNI mappings from activations
  - _l3_from_activations()       — L3 VNI (VRF) mappings from activations
  - _platform_vxlan_config()     — Arista keys; anycast_gateway inherited from the base config
  - get_acls()                   — zero-trust ACL list from each segment's security_policy
  - isolation_mode propagation   — _vlans_from_activations and arista_eos.j2 rendering
"""

import logging
from pathlib import Path

import jinja2
import pytest

from transforms.common import _fabric_anycast_mac, _fabric_rt_asn
from transforms.helpers.acl import _build_acl_rule, get_acls
from transforms.helpers.segments import _vlans_from_activations, get_vlans
from transforms.helpers.vxlan import (
    _DEFAULT_ANYCAST_GATEWAY_MAC,
    _L3VNI_SVI_VLAN_BASE,
    _L3VNI_SVI_VLAN_MAX,
    _collect_l3_vni_from_namespaces,
    _l2_from_activations,
    _l3_from_activations,
    _overlay_is_ebgp,
    _platform_vxlan_config,
    _select_evpn_bgp_process,
    _warn_unencodable_vnis,
    get_interfaces,
    get_vxlan_config,
)

# ---------------------------------------------------------------------------
# Helpers: activation structures (as returned by clean_data())
# ---------------------------------------------------------------------------


def _make_activation(
    *,
    vlan_id: int = 100,
    vni: int | None = 10100,
    gateway_ip: str | None = None,
    arp_suppression: bool = True,
    customer_name: str = "seg-100",
    ns_name: str = "default",
    l3_vni: int | None = None,
    owner_name: str | None = None,
) -> dict:
    """Build a cleaned SegmentDeployment dict as returned by clean_data()."""
    ns: dict = {"name": ns_name}
    if l3_vni is not None:
        ns["l3_vni"] = l3_vni
    if owner_name is not None:
        ns["owner"] = {"name": owner_name}

    gateway: dict = {"ip_prefix": {"ip_namespace": ns}}
    if gateway_ip is not None:
        gateway["address"] = gateway_ip

    segment: dict = {
        "name": f"Owner - production - {customer_name}",
        "customer_name": customer_name,
        "arp_suppression": arp_suppression,
        "gateway": gateway,
    }

    return {
        "vlan_id": vlan_id,
        "vni": vni,
        "status": "active",
        "segment": segment,
    }


# ===========================================================================
# get_vlans()
# ===========================================================================


class TestGetVlans:
    def test_empty_returns_empty(self) -> None:
        assert get_vlans() == []

    def test_none_returns_empty(self) -> None:
        assert get_vlans(activations=None) == []

    def test_with_activations(self) -> None:
        acts = [_make_activation(vlan_id=60, customer_name="prod")]
        result = get_vlans(activations=acts)
        assert len(result) == 1
        assert result[0]["vlan_id"] == 60


# ===========================================================================
# _vlans_from_activations()
# ===========================================================================


class TestVlansFromActivations:
    def test_empty_returns_empty(self) -> None:
        assert _vlans_from_activations([]) == []

    def test_basic_activation(self) -> None:
        acts = [_make_activation(vlan_id=10, customer_name="web")]
        result = _vlans_from_activations(acts)
        assert len(result) == 1
        assert result[0]["vlan_id"] == 10
        assert result[0]["name"] == "web"

    def test_gateway_ip_from_segment_prefix(self) -> None:
        acts = [_make_activation(vlan_id=20, gateway_ip="10.0.20.1/24")]
        result = _vlans_from_activations(acts)
        assert result[0]["gateway_ip"] == "10.0.20.1/24"

    def test_gateway_ip_none_when_no_gateway(self) -> None:
        acts = [_make_activation(vlan_id=20)]
        result = _vlans_from_activations(acts)
        assert result[0]["gateway_ip"] is None

    def test_arp_suppression_from_segment(self) -> None:
        acts = [_make_activation(vlan_id=25, arp_suppression=False)]
        result = _vlans_from_activations(acts)
        assert result[0]["arp_suppression"] is False

    def test_arp_suppression_default_true(self) -> None:
        acts = [_make_activation(vlan_id=26)]
        result = _vlans_from_activations(acts)
        assert result[0]["arp_suppression"] is True

    def test_vrf_from_non_default_namespace(self) -> None:
        acts = [_make_activation(vlan_id=30, ns_name="tenant_a", l3_vni=50001)]
        result = _vlans_from_activations(acts)
        assert result[0]["vrf"] == "tenant_a"

    def test_vrf_none_for_default_namespace(self) -> None:
        acts = [_make_activation(vlan_id=40)]
        result = _vlans_from_activations(acts)
        assert result[0]["vrf"] is None

    def test_deduplication_by_vlan_id(self) -> None:
        acts = [
            _make_activation(vlan_id=50, customer_name="first"),
            _make_activation(vlan_id=50, customer_name="second"),
        ]
        result = _vlans_from_activations(acts)
        assert len(result) == 1
        assert result[0]["name"] == "first"


# ===========================================================================
# _l2_from_activations()
# ===========================================================================


class TestL2FromActivations:
    def test_empty_returns_empty(self) -> None:
        assert _l2_from_activations([]) == []

    def test_basic_l2_mapping(self) -> None:
        acts = [_make_activation(vlan_id=100, vni=10100)]
        result = _l2_from_activations(acts)
        assert len(result) == 1
        assert result[0]["vlan_id"] == 100
        assert result[0]["vni"] == 10100

    def test_no_vni_skipped(self) -> None:
        """Traditional VLAN (vni=None) should NOT appear in L2 VNI mappings."""
        acts = [_make_activation(vlan_id=200, vni=None)]
        result = _l2_from_activations(acts)
        assert result == []

    def test_gateway_ip_and_vrf(self) -> None:
        acts = [_make_activation(vlan_id=300, vni=10300, gateway_ip="10.0.3.1/24", ns_name="ns_x", l3_vni=50001)]
        result = _l2_from_activations(acts)
        assert result[0]["gateway_ip"] == "10.0.3.1/24"
        assert result[0]["vrf"] == "ns_x"
        assert result[0]["l3_vni"] == 50001

    def test_arp_suppression_from_segment(self) -> None:
        acts = [_make_activation(vlan_id=350, vni=10350, arp_suppression=False)]
        result = _l2_from_activations(acts)
        assert result[0]["arp_suppression"] is False

    def test_deduplication(self) -> None:
        acts = [
            _make_activation(vlan_id=400, vni=10400),
            _make_activation(vlan_id=400, vni=99999),
        ]
        result = _l2_from_activations(acts)
        assert len(result) == 1
        assert result[0]["vni"] == 10400


# ===========================================================================
# _l3_from_activations()
# ===========================================================================


class TestL3FromActivations:
    def test_empty_returns_empty(self) -> None:
        assert _l3_from_activations([]) == []

    def test_extracts_vrf_from_namespace(self) -> None:
        acts = [_make_activation(ns_name="customer_a", l3_vni=50001)]
        result = _l3_from_activations(acts)
        assert len(result) == 1
        assert result[0]["vrf_name"] == "customer_a"
        assert result[0]["l3_vni"] == 50001

    def test_excludes_default_namespace(self) -> None:
        acts = [_make_activation(ns_name="default")]
        result = _l3_from_activations(acts)
        assert result == []

    def test_excludes_namespace_without_l3_vni(self) -> None:
        acts = [_make_activation(ns_name="no_vni_ns", l3_vni=None)]
        result = _l3_from_activations(acts)
        assert result == []

    def test_deduplicates_by_namespace(self) -> None:
        acts = [
            _make_activation(vlan_id=10, ns_name="tenant_x", l3_vni=50002),
            _make_activation(vlan_id=20, ns_name="tenant_x", l3_vni=50002),
        ]
        result = _l3_from_activations(acts)
        assert len(result) == 1

    def test_multiple_namespaces(self) -> None:
        acts = [
            _make_activation(vlan_id=10, ns_name="ns_a", l3_vni=50001),
            _make_activation(vlan_id=20, ns_name="ns_b", l3_vni=50002),
        ]
        result = _l3_from_activations(acts)
        assert len(result) == 2
        names = {e["vrf_name"] for e in result}
        assert names == {"ns_a", "ns_b"}


# ===========================================================================
# _platform_vxlan_config() for arista_eos
# ===========================================================================


class TestTransformVxlanArista:
    def _base_config(self, l2_mappings: list | None = None) -> dict:
        return {
            "enabled": True,
            "role": "leaf",
            "vtep": {"source_interface": "Loopback0", "ipv4": "10.0.0.1", "udp_port": 4789},
            "l2_vni_mappings": l2_mappings or [],
            "l3_vni_mappings": [],
            "flooding": "evpn",
            "evpn": {"enabled": True, "rd_format": "10.0.0.1:{vni}", "rt_format": "65001:{vni}"},
            "microsegmentation": {"enabled": False, "vrf_count": 0},
            "anycast_gateway": {"enabled": False, "mac": "00:1c:73:00:dc:01"},
        }

    def test_interface_set_to_vxlan1(self) -> None:
        result = _platform_vxlan_config(self._base_config(), "arista_eos")
        assert result["interface"] == "Vxlan1"

    def test_original_config_not_mutated(self) -> None:
        """_platform_vxlan_config copies — original dict is untouched."""
        mappings = [{"vlan_id": 10, "vni": 10010, "gateway_ip": "10.0.10.1/24"}]
        base = self._base_config(mappings)
        _platform_vxlan_config(base, "arista_eos")
        assert "interface" not in base

    def test_anycast_gateway_inherited_from_base_not_recomputed(self) -> None:
        """anycast_gateway is computed once, platform-agnostically, in
        get_vxlan_config's base_config — _platform_vxlan_config must inherit
        it, not recompute it (that logic moved out; see
        TestGetVxlanConfigAnycastGateway for the real coverage)."""
        base = self._base_config()
        base["anycast_gateway"] = {"enabled": True, "mac": "00:1c:73:00:dc:01"}
        result = _platform_vxlan_config(base, "arista_eos")
        assert result["anycast_gateway"] == {"enabled": True, "mac": "00:1c:73:00:dc:01"}


# ===========================================================================
# get_vxlan_config() — anycast_gateway (platform-agnostic, symmetric IRB)
# ===========================================================================


class TestGetVxlanConfigAnycastGateway:
    """anycast_gateway is computed once in get_vxlan_config's base_config and
    inherited unchanged by _platform_vxlan_config — same standard anycast
    MAC on every leaf/border-leaf in the fabric, matching
    .dev/scenariusze.txt's "fabric forwarding anycast-gateway-mac" /
    "ip virtual-router mac-address" / "ip anycast-mac-address" (identical
    value across Cisco/Arista/SONiC)."""

    def _data(self) -> dict:
        return {"interfaces": [{"name": "Loopback0", "ip_addresses": [{"address": "10.0.0.3/32"}]}], "capabilities": []}

    @pytest.mark.parametrize("platform", ["arista_eos", "cisco_nxos", "dell_sonic"])
    def test_disabled_when_no_gateway_ip(self, platform: str) -> None:
        acts = [_make_activation(vlan_id=10, customer_name="a", gateway_ip=None)]
        result = get_vxlan_config(self._data(), platform, device_role="leaf", activations=acts)
        assert result is not None
        assert result["anycast_gateway"]["enabled"] is False

    @pytest.mark.parametrize("platform", ["arista_eos", "cisco_nxos", "dell_sonic"])
    def test_enabled_when_any_activation_has_gateway_ip(self, platform: str) -> None:
        acts = [
            _make_activation(vlan_id=10, customer_name="a", gateway_ip=None),
            _make_activation(vlan_id=20, customer_name="b", gateway_ip="10.0.20.1/24"),
        ]
        result = get_vxlan_config(self._data(), platform, device_role="leaf", activations=acts)
        assert result is not None
        assert result["anycast_gateway"]["enabled"] is True

    @pytest.mark.parametrize("platform", ["arista_eos", "cisco_nxos", "dell_sonic"])
    def test_mac_identical_across_platforms(self, platform: str) -> None:
        acts = [_make_activation(vlan_id=10, customer_name="a", gateway_ip="10.0.10.1/24")]
        result = get_vxlan_config(self._data(), platform, device_role="leaf", activations=acts)
        assert result is not None
        assert result["anycast_gateway"]["mac"] == "00:1c:73:00:dc:01"


# ===========================================================================
# get_vxlan_config() — VTEP-role gate
# ===========================================================================


class TestGetVxlanConfigVtepGate:
    """Spine/super-spine/hyper-spine are underlay+EVPN-route-reflector only —
    never VTEPs — so get_vxlan_config() must return None for those roles
    regardless of what activations/data it's given."""

    def _data(self) -> dict:
        return {"interfaces": [{"name": "Loopback0", "ip_addresses": [{"address": "10.0.0.1/32"}]}], "capabilities": []}

    def test_spine_role_returns_none_even_with_activations(self) -> None:
        acts = [_make_activation(vlan_id=100, customer_name="web")]
        assert get_vxlan_config(self._data(), "arista_eos", device_role="spine", activations=acts) is None

    def test_super_spine_role_returns_none_even_with_activations(self) -> None:
        acts = [_make_activation(vlan_id=100, customer_name="web")]
        assert get_vxlan_config(self._data(), "arista_eos", device_role="super_spine", activations=acts) is None

    def test_super_spine_hyphenated_role_returns_none(self) -> None:
        acts = [_make_activation(vlan_id=100, customer_name="web")]
        assert get_vxlan_config(self._data(), "arista_eos", device_role="super-spine", activations=acts) is None

    def test_hyper_spine_role_returns_none(self) -> None:
        acts = [_make_activation(vlan_id=100, customer_name="web")]
        assert get_vxlan_config(self._data(), "arista_eos", device_role="hyper-spine", activations=acts) is None

    def test_leaf_role_still_builds_config(self) -> None:
        acts = [_make_activation(vlan_id=100, customer_name="web")]
        result = get_vxlan_config(self._data(), "arista_eos", device_role="leaf", activations=acts)
        assert result is not None

    def test_border_spine_role_still_builds_config(self) -> None:
        """border-spine collapses spine+border-leaf — it IS a VTEP."""
        acts = [_make_activation(vlan_id=100, customer_name="web")]
        result = get_vxlan_config(self._data(), "arista_eos", device_role="border-spine", activations=acts)
        assert result is not None


# ===========================================================================
# get_acls()
# ===========================================================================

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_acl_activation(
    *,
    vlan_id: int = 100,
    customer_name: str = "seg-100",
    security_policies: list | None = None,
    seg_id: str | None = None,
    prefix: str | None = None,
    inbound_rules: list | None = None,
) -> dict:
    """Build a cleaned SegmentDeployment dict. security_policies is the
    segment's own policy as a 0/1-item list; None leaves the field unqueried."""
    ip_prefix: dict = {"ip_namespace": {"name": "tenant-a", "l3_vni": 50001}}
    if prefix is not None:
        ip_prefix["prefix"] = prefix
    seg: dict = {
        "name": f"Owner - production - {customer_name}",
        "customer_name": customer_name,
        "arp_suppression": True,
        "gateway": {"ip_prefix": ip_prefix},
    }
    if seg_id is not None:
        seg["id"] = seg_id
    if security_policies is not None:
        seg["security_policy"] = security_policies[0] if security_policies else None
    if inbound_rules is not None:
        seg["inbound_rules"] = inbound_rules
    return {"vlan_id": vlan_id, "vni": 10000 + vlan_id, "status": "active", "segment": seg}


def _policy(rules: list | None = None, enabled: bool = True, default_action: str = "deny") -> dict:
    return {"name": "p", "default_action": default_action, "enabled": enabled, "rules": rules or []}


def _rule(
    index: int = 10,
    action: str = "permit",
    protocol: str = "tcp",
    port_start: int | None = 443,
    port_end: int | None = None,
    src: dict | None = None,
    dst: dict | None = None,
    log: bool = False,
    disabled: bool = False,
    src_zone: str | None = None,
    dst_zone: str | None = None,
) -> dict:
    rule: dict = {
        "index": index,
        "name": f"rule-{index}",
        "action": action,
        "protocol": protocol,
        "port_start": port_start,
        "port_end": port_end,
        "source_segment": src,
        "destination_segment": dst,
        "log": log,
        "disabled": disabled,
    }
    # A rule's zone is its segment's security_zone.
    if src_zone is not None:
        rule["source_segment"] = {**(rule["source_segment"] or {}), "security_zone": {"name": src_zone}}
    if dst_zone is not None:
        rule["destination_segment"] = {**(rule["destination_segment"] or {}), "security_zone": {"name": dst_zone}}
    return rule


def _seg_ref(prefix: str, *, customer_name: str | None = None, environment: str | None = None) -> dict:
    ref: dict = {"id": "x", "name": "other", "gateway": {"ip_prefix": {"prefix": prefix}}}
    if customer_name is not None:
        ref["customer_name"] = customer_name
    if environment is not None:
        ref["environment"] = environment
    return ref


class TestGetAclsEmpty:
    def test_none_returns_empty(self) -> None:
        assert get_acls(activations=None) == []

    def test_empty_list_returns_empty(self) -> None:
        assert get_acls(activations=[]) == []

    def test_missing_security_policies_key_skips_acl(self) -> None:
        """Segments without security_policy in query data produce no ACL (backwards compat)."""
        act = _make_acl_activation(vlan_id=100, security_policies=None)
        assert get_acls(activations=[act]) == []

    def test_no_vlan_id_skipped(self) -> None:
        act = _make_acl_activation(vlan_id=0)
        act["vlan_id"] = None
        assert get_acls(activations=[act]) == []


class TestGetAclsDenyAll:
    def test_empty_policies_list_yields_deny_all(self) -> None:
        acts = [_make_acl_activation(vlan_id=100, security_policies=[])]
        result = get_acls(activations=acts)
        assert len(result) == 1
        deny = result[0]["rules"][-1]
        assert deny["action"] == "deny"
        assert deny["protocol"] == "ip"
        assert deny["src"] == "any"
        assert deny["dst"] == "any"
        assert deny["log"] is True
        assert deny["name"] == "implicit-deny-all"

    def test_disabled_policy_yields_deny_all(self) -> None:
        acts = [_make_acl_activation(vlan_id=101, security_policies=[_policy(enabled=False, rules=[_rule()])])]
        assert len(get_acls(activations=acts)[0]["rules"]) == 1


class TestGetAclsRuleExtraction:
    def test_permit_tcp_with_port(self) -> None:
        acts = [_make_acl_activation(vlan_id=100, security_policies=[_policy(rules=[_rule()])])]
        r = get_acls(activations=acts)[0]["rules"][0]
        assert r["seq"] == 10
        assert r["action"] == "permit"
        assert r["protocol"] == "tcp"
        assert r["dst_port"] == "eq 443"

    def test_protocol_any_maps_to_ip(self) -> None:
        acts = [
            _make_acl_activation(
                vlan_id=200, security_policies=[_policy(rules=[_rule(protocol="any", port_start=None)])]
            )
        ]
        assert get_acls(activations=acts)[0]["rules"][0]["protocol"] == "ip"

    def test_port_range(self) -> None:
        acts = [
            _make_acl_activation(
                vlan_id=201, security_policies=[_policy(rules=[_rule(protocol="tcp", port_start=8080, port_end=8090)])]
            )
        ]
        assert get_acls(activations=acts)[0]["rules"][0]["dst_port"] == "range 8080 8090"

    def test_port_ignored_for_non_tcp_udp(self) -> None:
        acts = [
            _make_acl_activation(vlan_id=202, security_policies=[_policy(rules=[_rule(protocol="icmp", port_start=8)])])
        ]
        assert get_acls(activations=acts)[0]["rules"][0]["dst_port"] is None

    def test_disabled_rule_skipped(self) -> None:
        rules = [_rule(index=10), _rule(index=20, disabled=True)]
        acts = [_make_acl_activation(vlan_id=300, security_policies=[_policy(rules=rules)])]
        assert len(get_acls(activations=acts)[0]["rules"]) == 2  # 1 permit + implicit deny

    def test_rules_sorted_by_index(self) -> None:
        rules = [_rule(index=30), _rule(index=10), _rule(index=20)]
        acts = [_make_acl_activation(vlan_id=400, security_policies=[_policy(rules=rules)])]
        seqs = [r["seq"] for r in get_acls(activations=acts)[0]["rules"][:-1]]
        assert seqs == [10, 20, 30]

    def test_dst_segment_prefix_used(self) -> None:
        r = _rule(dst=_seg_ref("192.168.10.0/24"))
        acts = [_make_acl_activation(vlan_id=500, security_policies=[_policy(rules=[r])])]
        assert get_acls(activations=acts)[0]["rules"][0]["dst"] == "192.168.10.0/24"

    def test_src_segment_prefix_used(self) -> None:
        r = _rule(src=_seg_ref("10.1.0.0/16"))
        acts = [_make_acl_activation(vlan_id=501, security_policies=[_policy(rules=[r])])]
        assert get_acls(activations=acts)[0]["rules"][0]["src"] == "10.1.0.0/16"


class TestAclRuleEndpointResolver:
    """The leaf ACL resolves a side like the firewall rule table does
    (transforms/helpers/policy.py rule_endpoint): segment prefix, else the
    first prefix/IP selector, else any."""

    def test_destination_prefix_selector_without_segment(self) -> None:
        rule = {**_rule(src=_seg_ref("10.1.0.0/24")), "destination_prefixes": [{"prefix": "10.40.0.0/16"}]}
        acl_rule = _build_acl_rule(rule)
        assert (acl_rule["src"], acl_rule["dst"]) == ("10.1.0.0/24", "10.40.0.0/16")

    def test_destination_ip_selector_without_segment(self) -> None:
        rule = {**_rule(src=_seg_ref("10.1.0.0/24")), "destination_ip_addresses": [{"address": "198.51.100.7/32"}]}
        assert _build_acl_rule(rule)["dst"] == "198.51.100.7/32"

    def test_segment_prefix_wins_over_selectors(self) -> None:
        rule = {**_rule(dst=_seg_ref("10.2.0.0/24")), "destination_prefixes": [{"prefix": "10.40.0.0/16"}]}
        assert _build_acl_rule(rule)["dst"] == "10.2.0.0/24"

    def test_no_segment_and_no_selector_is_any(self) -> None:
        assert _build_acl_rule(_rule())["dst"] == "any"

    def test_return_leg_of_an_inbound_rule_uses_the_same_resolver(self) -> None:
        """The reply goes back to what the forward rule matched as its source."""
        inbound = {
            **_rule(port_start=5432),
            "source_segment": {"id": "seg-a", "name": "a"},
            "source_prefixes": [{"prefix": "10.9.0.0/24"}],
            "destination_segment": {"id": "seg-b"},
            "policy": {"enabled": True},
        }
        act = _make_acl_activation(seg_id="seg-b", prefix="10.2.0.0/24", security_policies=[], inbound_rules=[inbound])
        returns = [r for r in get_acls(activations=[act])[0]["rules"] if r["name"].startswith("return-to-")]
        assert [(r["src"], r["dst"], r["src_port"]) for r in returns] == [("10.2.0.0/24", "10.9.0.0/24", "eq 5432")]


class TestGetAclsDefaultAction:
    def test_default_permit_policy_closes_the_acl_with_a_permit(self) -> None:
        """The segment policy's default action is the ACL's last word."""
        acts = [
            _make_acl_activation(vlan_id=100, security_policies=[_policy(rules=[_rule()], default_action="permit")])
        ]
        last = get_acls(activations=acts)[0]["rules"][-1]
        assert (last["name"], last["action"], last["log"]) == ("default-permit-all", "permit", False)

    def test_disabled_default_permit_policy_still_ends_in_deny(self) -> None:
        """A disabled policy's default action does not apply."""
        acts = [_make_acl_activation(vlan_id=100, security_policies=[_policy(enabled=False, default_action="permit")])]
        last = get_acls(activations=acts)[0]["rules"][-1]
        assert (last["name"], last["action"]) == ("implicit-deny-all", "deny")


class TestGetAclsImplicitDeny:
    def test_implicit_deny_always_last(self) -> None:
        rules = [_rule(index=10), _rule(index=20)]
        acts = [_make_acl_activation(vlan_id=700, security_policies=[_policy(rules=rules)])]
        assert get_acls(activations=acts)[0]["rules"][-1]["name"] == "implicit-deny-all"

    def test_implicit_deny_seq_min_9990_when_no_rules(self) -> None:
        acts = [_make_acl_activation(vlan_id=702, security_policies=[])]
        assert get_acls(activations=acts)[0]["rules"][-1]["seq"] == 9990

    def test_implicit_deny_seq_above_last_rule(self) -> None:
        acts = [_make_acl_activation(vlan_id=703, security_policies=[_policy(rules=[_rule(index=100)])])]
        last_seq = get_acls(activations=acts)[0]["rules"][-1]["seq"]
        assert last_seq >= 110


class TestGetAclsMultipleActivations:
    def test_sorted_by_vlan_id(self) -> None:
        acts = [
            _make_acl_activation(vlan_id=300, security_policies=[]),
            _make_acl_activation(vlan_id=100, security_policies=[]),
            _make_acl_activation(vlan_id=200, security_policies=[]),
        ]
        assert [a["vlan_id"] for a in get_acls(activations=acts)] == [100, 200, 300]

    def test_deduplication_by_vlan_id(self) -> None:
        acts = [
            _make_acl_activation(vlan_id=100, customer_name="first", security_policies=[]),
            _make_acl_activation(vlan_id=100, customer_name="second", security_policies=[]),
        ]
        assert len(get_acls(activations=acts)) == 1

    def test_acl_name_format(self) -> None:
        acts = [_make_acl_activation(vlan_id=42, security_policies=[])]
        assert get_acls(activations=acts)[0]["name"] == "ACL-VLAN42-IN"


# ===========================================================================
# East-West return legs (inbound_rules)
# ===========================================================================


def _inbound(
    *,
    src_id: str = "seg-a-id",
    src_prefix: str | None = "10.0.1.0/24",
    src_name: str = "seg-a",
    index: int = 10,
    protocol: str = "tcp",
    port_start: int | None = 443,
    port_end: int | None = None,
    action: str = "permit",
    disabled: bool = False,
    policy_enabled: bool = True,
) -> dict:
    """An inbound_rules entry as clean_data() produces it: a rule whose
    destination is the segment carrying it."""
    src: dict = {"id": src_id, "name": src_name, "customer_name": src_name, "environment": "p"}
    if src_prefix is not None:
        src["gateway"] = {"ip_prefix": {"prefix": src_prefix}}
    return {
        "index": index,
        "name": f"rule-{index}",
        "action": action,
        "protocol": protocol,
        "port_start": port_start,
        "port_end": port_end,
        "disabled": disabled,
        "policy": {"enabled": policy_enabled},
        "source_segment": src,
    }


def _segment_b(inbound_rules: list, security_policies: list | None = None) -> dict:
    return _make_acl_activation(
        vlan_id=200,
        customer_name="seg-b",
        seg_id="seg-b-id",
        prefix="10.0.2.0/24",
        security_policies=security_policies or [],
        inbound_rules=inbound_rules,
    )


def _returns(acl: dict) -> list[dict]:
    return [r for r in acl["rules"] if r["name"].startswith("return-to-")]


class TestGetAclsReturnRules:
    """A permit A -> B:port adds B's reply leg (B:port -> A) to B's own
    ingress ACL, read from B's inbound_rules, so A need not be on this leaf."""

    def test_tcp_permit_adds_established_return_leg(self) -> None:
        """src=own prefix, src_port=the rule's port, dst=the rule's source, established."""
        acl = get_acls(activations=[_segment_b([_inbound()])])[0]
        (ret,) = _returns(acl)
        assert ret["action"] == "permit"
        assert ret["protocol"] == "tcp"
        assert ret["src"] == "10.0.2.0/24"
        assert ret["src_port"] == "eq 443"
        assert ret["dst"] == "10.0.1.0/24"
        assert ret["dst_port"] is None
        assert ret["established"] is True
        assert ret["name"] == "return-to-seg-a-rule-10"

    def test_return_leg_does_not_need_the_source_on_this_leaf(self) -> None:
        """B alone on the leaf still gets the return leg (the old mirror needed A here too)."""
        acls = get_acls(activations=[_segment_b([_inbound()])])
        assert len(acls) == 1
        assert _returns(acls[0])

    def test_udp_port_range_returns_from_the_range_without_established(self) -> None:
        acl = get_acls(activations=[_segment_b([_inbound(protocol="udp", port_start=30000, port_end=30010)])])[0]
        (ret,) = _returns(acl)
        assert ret["protocol"] == "udp"
        assert ret["src_port"] == "range 30000 30010"
        assert ret["established"] is False

    def test_any_protocol_returns_as_plain_reverse_match(self) -> None:
        acl = get_acls(activations=[_segment_b([_inbound(protocol="any", port_start=None)])])[0]
        (ret,) = _returns(acl)
        assert ret["protocol"] == "ip"
        assert ret["src_port"] is None
        assert ret["established"] is False

    def test_deny_disabled_and_disabled_policy_rules_add_nothing(self) -> None:
        inbound = [
            _inbound(index=10, action="deny"),
            _inbound(index=20, disabled=True),
            _inbound(index=30, policy_enabled=False),
        ]
        acl = get_acls(activations=[_segment_b(inbound)])[0]
        assert _returns(acl) == []

    def test_intra_segment_rule_adds_nothing(self) -> None:
        acl = get_acls(activations=[_segment_b([_inbound(src_id="seg-b-id", src_prefix="10.0.2.0/24")])])[0]
        assert _returns(acl) == []

    def test_identical_reply_legs_are_rendered_once(self) -> None:
        """Two rules from the same source on the same port share one return line."""
        acl = get_acls(activations=[_segment_b([_inbound(index=10), _inbound(index=20)])])[0]
        assert len(_returns(acl)) == 1

    def test_return_legs_follow_own_rules_and_precede_implicit_deny(self) -> None:
        own = [_policy(rules=[_rule(index=20, dst=_seg_ref("10.0.9.0/24"))])]
        acl = get_acls(
            activations=[
                _segment_b(
                    [_inbound(index=10), _inbound(index=20, src_id="c", src_name="seg-c", src_prefix="10.0.3.0/24")],
                    own,
                )
            ]
        )[0]
        seqs = [r["seq"] for r in _returns(acl)]
        assert seqs == [5000, 5010]
        assert acl["rules"][0]["seq"] == 20
        assert acl["rules"][-1]["name"] == "implicit-deny-all"
        assert acl["rules"][-1]["seq"] == 9990

    def test_source_without_prefix_returns_to_any(self) -> None:
        acl = get_acls(activations=[_segment_b([_inbound(src_prefix=None)])])[0]
        (ret,) = _returns(acl)
        assert ret["dst"] == "any"

    def test_return_leg_attribution_is_own_segment_to_rule_source(self) -> None:
        acl = get_acls(activations=[_segment_b([_inbound()])])[0]
        (ret,) = _returns(acl)
        assert ret["src_customer"] == "seg-b"
        assert ret["dst_customer"] == "seg-a"
        assert ret["dst_environment"] == "p"

    def test_own_rules_are_not_mirrored_onto_a_local_destination(self) -> None:
        """A's rule to B no longer adds src=A,dst=any to B's ACL: that line
        matched none of B's own packets."""
        a = _make_acl_activation(
            vlan_id=100,
            seg_id="seg-a-id",
            security_policies=[_policy(rules=[_rule(index=10, dst={"id": "seg-b-id", "name": "b"})])],
        )
        b = _make_acl_activation(vlan_id=200, seg_id="seg-b-id", security_policies=[])
        acl_b = next(acl for acl in get_acls(activations=[a, b]) if acl["vlan_id"] == 200)
        assert [r["name"] for r in acl_b["rules"]] == ["implicit-deny-all"]


# ===========================================================================
# Zone support
# ===========================================================================


class TestGetAclsZoneSupport:
    def test_zone_fields_passed_through(self) -> None:
        """src_zone / dst_zone come from the rule segments' security_zone."""
        rule = _rule(index=10, src_zone="dmz", dst_zone="internal")
        acts = [_make_acl_activation(vlan_id=100, security_policies=[_policy(rules=[rule])])]
        r = get_acls(activations=acts)[0]["rules"][0]
        assert r["src_zone"] == "dmz"
        assert r["dst_zone"] == "internal"

    def test_zone_fields_none_when_absent(self) -> None:
        """Rules whose segments carry no zone have src_zone=None, dst_zone=None."""
        acts = [_make_acl_activation(vlan_id=100, security_policies=[_policy(rules=[_rule()])])]
        r = get_acls(activations=acts)[0]["rules"][0]
        assert r["src_zone"] is None
        assert r["dst_zone"] is None

    def test_implicit_deny_has_null_zones(self) -> None:
        acts = [_make_acl_activation(vlan_id=100, security_policies=[])]
        deny = get_acls(activations=acts)[0]["rules"][-1]
        assert deny["src_zone"] is None
        assert deny["dst_zone"] is None

    def test_partial_zone_src_only(self) -> None:
        rule = _rule(index=10, src_zone="external")
        acts = [_make_acl_activation(vlan_id=100, security_policies=[_policy(rules=[rule])])]
        r = get_acls(activations=acts)[0]["rules"][0]
        assert r["src_zone"] == "external"
        assert r["dst_zone"] is None


class TestGetAclsCustomerAttribution:
    """src_customer/src_environment/dst_customer/dst_environment let a policy
    mixing rules from different customers' segments be attributed per-rule,
    not just at the whole-ACL segment_name level."""

    def test_customer_environment_fields_passed_through(self) -> None:
        rule = _rule(
            index=10,
            src=_seg_ref("10.1.0.0/16", customer_name="acme", environment="p"),
            dst=_seg_ref("192.168.10.0/24", customer_name="globex", environment="s"),
        )
        acts = [_make_acl_activation(vlan_id=100, security_policies=[_policy(rules=[rule])])]
        r = get_acls(activations=acts)[0]["rules"][0]
        assert r["src_customer"] == "acme"
        assert r["src_environment"] == "p"
        assert r["dst_customer"] == "globex"
        assert r["dst_environment"] == "s"

    def test_customer_fields_none_when_absent(self) -> None:
        acts = [_make_acl_activation(vlan_id=100, security_policies=[_policy(rules=[_rule()])])]
        r = get_acls(activations=acts)[0]["rules"][0]
        assert r["src_customer"] is None
        assert r["src_environment"] is None
        assert r["dst_customer"] is None
        assert r["dst_environment"] is None

    def test_implicit_deny_has_null_customer_fields(self) -> None:
        acts = [_make_acl_activation(vlan_id=100, security_policies=[])]
        deny = get_acls(activations=acts)[0]["rules"][-1]
        assert deny["src_customer"] is None
        assert deny["dst_customer"] is None


# ===========================================================================
# isolation_mode and firewall-skip logic
# ===========================================================================


def _make_isolation_activation(
    vlan_id: int,
    seg_id: str,
    policies: list | None = None,
    firewall_id: str | None = None,
    isolation_mode: str = "normal",
) -> dict:
    """Return a single activation dict for isolation_mode / firewall-skip tests.

    The segment always has ``security_policy`` present so that the skip vs.
    render decision is exercised rather than the "field not queried" early-exit.
    """
    seg: dict = {
        "id": seg_id,
        "name": f"owner - prod - vlan{vlan_id}",
        "customer_name": f"vlan{vlan_id}",
        "arp_suppression": True,
        "prefix": {"ip_namespace": {"name": "tenant-a", "l3_vni": 50001}},
        "security_policy": policies[0] if policies else None,
        "isolation_mode": isolation_mode,
    }
    if firewall_id is not None:
        seg["inline_service"] = {"id": firewall_id}
    return {"vlan_id": vlan_id, "vni": 10000 + vlan_id, "status": "active", "segment": seg}


class TestGetAclsIsolationMode:
    """Firewall-skip logic gated by isolation_mode on the segment."""

    def test_normal_segment_with_firewall_skips_acl(self) -> None:
        """A segment with a firewall and isolation_mode='normal' produces no ACL."""
        act = _make_isolation_activation(
            vlan_id=100, seg_id="seg-fw-normal", firewall_id="fw-1", isolation_mode="normal"
        )
        result = get_acls(activations=[act])
        assert result == []

    def test_isolated_segment_with_firewall_skips_acl(self) -> None:
        """'isolated' is not 'microsegmented' — firewall skip still applies."""
        act = _make_isolation_activation(
            vlan_id=101, seg_id="seg-fw-isolated", firewall_id="fw-1", isolation_mode="isolated"
        )
        result = get_acls(activations=[act])
        assert result == []

    def test_microsegmented_segment_with_firewall_renders_acl(self) -> None:
        """isolation_mode='microsegmented' bypasses the firewall skip — ACL must be generated."""
        policies = [_policy(rules=[_rule(index=10)])]
        act = _make_isolation_activation(
            vlan_id=102,
            seg_id="seg-fw-micro",
            firewall_id="fw-1",
            isolation_mode="microsegmented",
            policies=policies,
        )
        result = get_acls(activations=[act])
        assert len(result) == 1
        assert result[0]["isolation_mode"] == "microsegmented"

    def test_microsegmented_without_firewall_renders_acl(self) -> None:
        """Microsegmented segment with no firewall still renders its ACL normally."""
        policies = [_policy(rules=[_rule(index=10)])]
        act = _make_isolation_activation(
            vlan_id=103, seg_id="seg-micro-nofw", isolation_mode="microsegmented", policies=policies
        )
        result = get_acls(activations=[act])
        assert len(result) == 1

    def test_normal_without_firewall_renders_acl(self) -> None:
        """Baseline: no firewall, isolation_mode='normal' → ACL rendered as always."""
        policies = [_policy(rules=[_rule(index=10)])]
        act = _make_isolation_activation(
            vlan_id=104, seg_id="seg-normal-nofw", isolation_mode="normal", policies=policies
        )
        result = get_acls(activations=[act])
        assert len(result) == 1

    def test_isolation_mode_propagated_to_acl_dict(self) -> None:
        """isolation_mode value from the segment must appear in the output ACL dict."""
        policies = [_policy(rules=[_rule(index=10)])]
        act = _make_isolation_activation(
            vlan_id=105, seg_id="seg-isolated-nofw", isolation_mode="isolated", policies=policies
        )
        result = get_acls(activations=[act])
        assert len(result) == 1
        assert result[0]["isolation_mode"] == "isolated"

    def test_missing_isolation_mode_defaults_to_normal(self) -> None:
        """When 'isolation_mode' key is absent from the segment dict, it defaults to 'normal'."""
        act = _make_isolation_activation(
            vlan_id=106, seg_id="seg-no-iso-key", policies=[_policy(rules=[_rule(index=10)])]
        )
        # Remove the key entirely — _make_isolation_activation always sets it, so pop it
        del act["segment"]["isolation_mode"]
        result = get_acls(activations=[act])
        assert len(result) == 1
        assert result[0]["isolation_mode"] == "normal"

    def test_apply_on_switch_rule_renders_despite_the_firewall(self) -> None:
        """An apply_on_switch rule holds on the leaf even when the segment has an
        inline_service: its whole ACL renders (an SVI ACL denies what it does not permit)."""
        forced = {**_rule(index=10), "apply_on_switch": True}
        policies = [_policy(rules=[forced, _rule(index=20, port_start=22)])]
        act = _make_isolation_activation(vlan_id=107, seg_id="seg-fw-forced", firewall_id="fw-1", policies=policies)
        result = get_acls(activations=[act])
        assert [rule["name"] for rule in result[0]["rules"]] == ["rule-10", "rule-20", "implicit-deny-all"]

    def test_disabled_apply_on_switch_rule_does_not_force_the_acl(self) -> None:
        forced = {**_rule(index=10, disabled=True), "apply_on_switch": True}
        act = _make_isolation_activation(
            vlan_id=108, seg_id="seg-fw-off", firewall_id="fw-1", policies=[_policy(rules=[forced])]
        )
        assert get_acls(activations=[act]) == []

    def test_apply_on_switch_field_present_in_rule(self) -> None:
        """_build_acl_rule must not crash when 'apply_on_switch' appears in the rule dict."""
        rule_dict = {
            "index": 10,
            "name": "r",
            "action": "permit",
            "protocol": "tcp",
            "apply_on_switch": True,
        }
        result = _build_acl_rule(rule_dict)
        assert result["action"] == "permit"


# ===========================================================================
# isolation_mode propagation in _vlans_from_activations()
# ===========================================================================


def _make_iso_activation(vlan_id: int, isolation_mode: str | None = None) -> dict:
    """Build a minimal activation dict, optionally with isolation_mode on the segment."""
    seg: dict = {
        "name": f"seg-{vlan_id}",
        "customer_name": f"seg-{vlan_id}",
        "arp_suppression": True,
        "prefix": {"ip_namespace": {"name": "default"}},
    }
    if isolation_mode is not None:
        seg["isolation_mode"] = isolation_mode
    return {"vlan_id": vlan_id, "vni": 10000 + vlan_id, "status": "active", "segment": seg}


class TestVlansFromActivationsIsolationMode:
    """isolation_mode is read from segment dict and stored verbatim in the VLAN dict."""

    def test_isolation_mode_normal_propagated(self) -> None:
        acts = [_make_iso_activation(vlan_id=100, isolation_mode="normal")]
        result = _vlans_from_activations(acts)
        assert result[0]["isolation_mode"] == "normal"

    def test_isolation_mode_isolated_propagated(self) -> None:
        acts = [_make_iso_activation(vlan_id=101, isolation_mode="isolated")]
        result = _vlans_from_activations(acts)
        assert result[0]["isolation_mode"] == "isolated"

    def test_isolation_mode_microsegmented_propagated(self) -> None:
        acts = [_make_iso_activation(vlan_id=102, isolation_mode="microsegmented")]
        result = _vlans_from_activations(acts)
        assert result[0]["isolation_mode"] == "microsegmented"

    def test_isolation_mode_missing_defaults_to_normal(self) -> None:
        """When segment dict has no isolation_mode key the VLAN dict gets 'normal'."""
        acts = [_make_iso_activation(vlan_id=103, isolation_mode=None)]
        result = _vlans_from_activations(acts)
        assert result[0]["isolation_mode"] == "normal"


# ===========================================================================
# Arista EOS leaf template — isolation_mode rendering smoke tests
# ===========================================================================

# The template uses includes like 'common/arista_eos_mlag.j2' so the loader
# root must be templates/configs/ (one level above both 'leafs/' and 'common/').
_TEMPLATES_CONFIGS_DIR = Path(__file__).parent.parent.parent / "templates" / "configs"
_LEAF_TEMPLATE_NAME = "leafs/arista_eos.j2"


@pytest.fixture
def arista_env() -> jinja2.Environment:
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(_TEMPLATES_CONFIGS_DIR)),
        undefined=jinja2.Undefined,  # silent undefined — avoids errors from optional ctx keys
    )


def _minimal_ctx(**overrides) -> dict:
    """Minimal rendering context that avoids template errors for optional sections."""
    ctx: dict = {
        "hostname": "test-leaf",
        "vlans": [],
        "acls": [],
        "interfaces": [],
        "loopback_name": "Loopback0",
        "ospf": None,
        "bgp": None,
        "vxlan": {"enabled": False},
        "vrf_gateways": {},
        # Management-section variables expected by arista_eos_management.j2
        "ntp": None,
        "syslog": None,
        "snmp": None,
        "aaa": None,
        # MLAG section
        "mlag": None,
    }
    ctx.update(overrides)
    return ctx


def _vxlan_with_anycast(mac: str = "00:1c:73:00:dc:01") -> dict:
    """Return a minimal vxlan dict with anycast gateway enabled (SVI block condition)."""
    return {
        "enabled": True,
        "interface": "Vxlan1",
        "vtep": {"source_interface": "Loopback0", "ipv4": "10.0.0.1", "udp_port": 4789},
        "l2_vni_mappings": [],
        "l3_vni_mappings": [],
        "flooding": "evpn",
        "evpn": {"enabled": False},
        "microsegmentation": {"enabled": False, "vrf_count": 0},
        "anycast_gateway": {"enabled": True, "mac": mac},
    }


class TestAristaLeafTemplateIsolationMode:
    """Smoke tests: isolation_mode values produce the expected comment lines in rendered output."""

    def test_isolated_vlan_renders_pvlan_comment(self, arista_env: jinja2.Environment) -> None:
        """VLAN block for isolation_mode='isolated' must contain the private-vlan remark."""
        vlans = [{"vlan_id": 100, "name": "test-isolated", "isolation_mode": "isolated"}]
        ctx = _minimal_ctx(vlans=vlans)
        rendered = arista_env.get_template(_LEAF_TEMPLATE_NAME).render(**ctx)
        assert "private-vlan type: isolated" in rendered

    def test_normal_vlan_no_pvlan_comment(self, arista_env: jinja2.Environment) -> None:
        """VLAN block for isolation_mode='normal' must NOT contain the private-vlan remark."""
        vlans = [{"vlan_id": 200, "name": "test-normal", "isolation_mode": "normal"}]
        ctx = _minimal_ctx(vlans=vlans)
        rendered = arista_env.get_template(_LEAF_TEMPLATE_NAME).render(**ctx)
        assert "private-vlan" not in rendered

    def test_microsegmented_svi_renders_mode_comment(self, arista_env: jinja2.Environment) -> None:
        """SVI block for isolation_mode='microsegmented' must contain the MICROSEGMENTED comment."""
        vlans = [
            {
                "vlan_id": 300,
                "name": "test-micro",
                "isolation_mode": "microsegmented",
                "gateway_ip": "10.0.3.1/24",
                "gateway_ipv6": None,
                "vrf": None,
            }
        ]
        ctx = _minimal_ctx(vlans=vlans, vxlan=_vxlan_with_anycast())
        rendered = arista_env.get_template(_LEAF_TEMPLATE_NAME).render(**ctx)
        assert "MICROSEGMENTED" in rendered

    def test_isolated_svi_renders_mode_comment(self, arista_env: jinja2.Environment) -> None:
        """SVI block for isolation_mode='isolated' with a gateway must contain the ISOLATED comment."""
        vlans = [
            {
                "vlan_id": 400,
                "name": "test-iso-svi",
                "isolation_mode": "isolated",
                "gateway_ip": "10.0.4.1/24",
                "gateway_ipv6": None,
                "vrf": None,
            }
        ]
        ctx = _minimal_ctx(vlans=vlans, vxlan=_vxlan_with_anycast())
        rendered = arista_env.get_template(_LEAF_TEMPLATE_NAME).render(**ctx)
        assert "ISOLATED" in rendered


# ---------------------------------------------------------------------------
# get_interfaces() — OSPF interface authentication (password relationship)
# ---------------------------------------------------------------------------


def _return_leg_acl() -> dict:
    """One ACL holding a tcp return leg, as get_acls() emits it."""
    ret = {
        "seq": 5000,
        "action": "permit",
        "protocol": "tcp",
        "src": "10.0.2.0/24",
        "src_port": "range 8000 8080",
        "dst": "10.0.1.0/24",
        "dst_port": None,
        "established": True,
        "log": False,
        "name": "return-to-seg-a-rule-10",
    }
    return {"name": "ACL-VLAN200-IN", "vlan_id": 200, "segment_name": "seg-b", "rules": [ret]}


class TestLeafTemplatesRenderReturnLegs:
    """src_port and established reach the rendered ACL line."""

    def test_arista_line_has_source_port_and_established(self, arista_env: jinja2.Environment) -> None:
        rendered = arista_env.get_template(_LEAF_TEMPLATE_NAME).render(**_minimal_ctx(acls=[_return_leg_acl()]))
        assert "5000 permit tcp 10.0.2.0/24 range 8000 8080 10.0.1.0/24 established" in rendered

    def test_nxos_line_has_source_port_and_established(self, arista_env: jinja2.Environment) -> None:
        rendered = arista_env.get_template("leafs/cisco_nxos.j2").render(
            **_minimal_ctx(acls=[_return_leg_acl()], ospf=[], bgp=[], loopback_name="loopback0")
        )
        assert "5000 permit tcp 10.0.2.0/24 range 8000 8080 10.0.1.0/24 established" in rendered

    def test_forward_rule_line_is_unchanged(self, arista_env: jinja2.Environment) -> None:
        acl = _return_leg_acl()
        acl["rules"][0].update(src_port=None, established=False, dst_port="eq 443")
        rendered = arista_env.get_template(_LEAF_TEMPLATE_NAME).render(**_minimal_ctx(acls=[acl]))
        assert "5000 permit tcp 10.0.2.0/24 10.0.1.0/24 eq 443 ! return-to-seg-a-rule-10" in rendered

    def test_sonic_rule_gets_source_port_range_and_ack_flag(self, arista_env: jinja2.Environment) -> None:
        acl_table: dict = {}
        acl_rule: dict = {}
        arista_env.get_template("common/sonic_acl.j2").render(
            acls=[_return_leg_acl()], acl_table=acl_table, acl_rule=acl_rule
        )
        entry = acl_rule["ACL-VLAN200-IN|SEQ_5000"]
        assert entry["L4_SRC_PORT_RANGE"] == "8000-8080"
        assert entry["TCP_FLAGS"] == "0x10/0x10"
        assert "L4_DST_PORT" not in entry


def _make_ospf_interface(
    *,
    area: int = 0,
    mode: str | None = None,
    metric: int | None = None,
    process_id: str | None = "1",
    authentication_mode: str | None = None,
    password: str | None = None,
) -> dict:
    """Build a cleaned RoutingOSPFInterface dict as it appears in interface_capabilities."""
    entry: dict = {
        "typename": "RoutingOSPFInterface",
        "peering": {
            "ospf_area": {"area": area},
            "ospf_process": [{"process_id": process_id, "capabilities": [{"name": "dev1"}]}],
        },
    }
    if mode is not None:
        entry["mode"] = mode
    if metric is not None:
        entry["metric"] = metric
    if authentication_mode is not None:
        entry["authentication_mode"] = authentication_mode
    if password is not None:
        entry["password"] = {"password": password}
    return entry


class TestGetInterfacesOspfAuthentication:
    """get_interfaces() must thread OSPF authentication_mode/password through
    the RoutingPassword relationship into interface.ospf, mirroring how BGP
    resolves its own password relationship in _build_session_from_peering()."""

    def test_password_and_mode_pass_through(self):
        iface = {
            "name": "Ethernet1",
            "interface_capabilities": [
                _make_ospf_interface(area=0, authentication_mode="md5", password="s3cr3t"),
            ],
        }
        result = get_interfaces([iface])
        assert result[0]["ospf"]["authentication_mode"] == "md5"
        assert result[0]["ospf"]["password"] == "s3cr3t"

    def test_no_password_defaults_to_none(self):
        iface = {
            "name": "Ethernet1",
            "interface_capabilities": [_make_ospf_interface(area=0)],
        }
        result = get_interfaces([iface])
        assert result[0]["ospf"]["authentication_mode"] is None
        assert result[0]["ospf"]["password"] is None


class TestBorderLeafTemplateFirewallContextDot1q:
    """FirewallContext sub-interface (role='service') must render its own
    802.1Q encapsulation on border-leaf — the IP address alone isn't enough
    to scope it to the right VLAN on real hardware."""

    def _ctx_with_context_subinterface(self) -> dict:
        return _minimal_ctx(
            interfaces=[
                {
                    "name": "Ethernet1/49.150",
                    "role": "service",
                    "status": "active",
                    "description": None,
                    "ip_addresses": [{"address": "10.99.99.1/31", "ip_namespace": {"name": "default"}}],
                    "dot1q_vlan": 150,
                    "vlans": [],
                }
            ],
        )

    def test_cisco_nxos_renders_encapsulation_dot1q(self) -> None:
        env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(str(_TEMPLATES_CONFIGS_DIR)),
            undefined=jinja2.Undefined,
        )
        ctx = self._ctx_with_context_subinterface()
        ctx["name"] = "test-border-leaf"
        ctx["ospf"] = []
        ctx["bgp"] = []
        rendered = env.get_template("border_leafs/cisco_nxos.j2").render(**{**ctx, "loopback_name": "loopback0"})
        assert "interface Ethernet1/49.150" in rendered
        assert "encapsulation dot1q 150" in rendered

    def test_arista_eos_renders_encapsulation_dot1q(self, arista_env: jinja2.Environment) -> None:
        ctx = self._ctx_with_context_subinterface()
        rendered = arista_env.get_template("border_leafs/arista_eos.j2").render(**ctx)
        assert "interface Ethernet1/49.150" in rendered
        assert "encapsulation dot1q vlan 150" in rendered

    def test_no_dot1q_vlan_omits_encapsulation_line(self, arista_env: jinja2.Environment) -> None:
        ctx = _minimal_ctx(
            interfaces=[
                {
                    "name": "Ethernet1",
                    "role": "uplink",
                    "status": "active",
                    "description": None,
                    "ip_addresses": [],
                    "dot1q_vlan": None,
                    "vlans": [],
                }
            ],
        )
        rendered = arista_env.get_template("border_leafs/arista_eos.j2").render(**ctx)
        assert "encapsulation" not in rendered


class TestGetInterfacesFirewallContextDot1q:
    """get_interfaces() must expose the FirewallContext sub-interface's own
    vlan_id as a scalar `dot1q_vlan` (border-leaf/firewall PBR leg) — distinct
    from `vlans` (customer segment trunk/access VLANs), since a context
    sub-interface's VLAN comes from the FW-context VLAN pool, not any
    customer segment."""

    def test_firewall_context_capability_sets_dot1q_vlan(self):
        iface = {
            "name": "Ethernet1/49.150",
            "parent_interface": {"name": "Ethernet1/49"},
            "interface_capabilities": [
                {"typename": "ManagedFirewallContext", "name": "dc10-shared", "vlan_id": 150},
            ],
        }
        result = get_interfaces([iface])
        assert result[0]["dot1q_vlan"] == 150
        assert result[0]["parent_interface"] == {"name": "Ethernet1/49"}

    def test_no_firewall_context_capability_leaves_dot1q_vlan_none(self):
        iface = {"name": "Ethernet1", "interface_capabilities": []}
        result = get_interfaces([iface])
        assert result[0]["dot1q_vlan"] is None
        assert result[0]["parent_interface"] is None


# ===========================================================================
# _select_evpn_bgp_process() — the EVPN discriminator
# ===========================================================================


class TestSelectEvpnBgpProcess:
    """The BGP process that carries EVPN is selected by `typename` then
    `process_role`.

    The original filter was `service_type == "bgp"`. ManagedBGP has no
    `service_type` attribute, so it matched nothing — which pinned
    `evpn.enabled` to False and left `rt_format` unset on every VTEP in the
    project while ruff, ty and the whole test suite stayed green.
    """

    def test_typename_is_the_discriminator_not_service_type(self) -> None:
        """A ManagedBGP with no `service_type` key must still be found."""
        caps = [{"typename": "ManagedBGP", "name": "bgp-overlay", "local_as": {"asn": 65001}}]
        assert _select_evpn_bgp_process(caps) is not None

    def test_service_type_bgp_alone_is_not_matched(self) -> None:
        """Guards the regression: `service_type` is not a real ManagedBGP field,
        so a capability carrying only that must not be mistaken for BGP."""
        caps = [{"service_type": "bgp", "name": "not-really-bgp"}]
        assert _select_evpn_bgp_process(caps) is None

    def test_non_bgp_capabilities_are_ignored(self) -> None:
        caps = [
            {"typename": "ManagedNTP", "name": "ntp"},
            {"typename": "ManagedOSPF", "name": "ospf"},
        ]
        assert _select_evpn_bgp_process(caps) is None

    def test_empty_capabilities_returns_none(self) -> None:
        assert _select_evpn_bgp_process([]) is None

    def test_overlay_process_wins_over_underlay(self) -> None:
        """EVPN lives in the overlay process — picking the underlay one would
        derive the RD from the wrong router-id."""
        caps = [
            {"typename": "ManagedBGP", "name": "a-underlay", "process_role": "underlay"},
            {"typename": "ManagedBGP", "name": "z-overlay", "process_role": "overlay"},
        ]
        result = _select_evpn_bgp_process(caps)
        assert result is not None
        assert result["name"] == "z-overlay"

    def test_overlay_wins_regardless_of_capability_order(self) -> None:
        caps = [
            {"typename": "ManagedBGP", "name": "z-overlay", "process_role": "overlay"},
            {"typename": "ManagedBGP", "name": "a-underlay", "process_role": "underlay"},
        ]
        result = _select_evpn_bgp_process(caps)
        assert result is not None
        assert result["name"] == "z-overlay"

    def test_selection_is_deterministic_across_query_order(self) -> None:
        """Unsorted `[0]` made a device's route-distinguisher depend on the order
        the GraphQL backend happened to return capabilities in."""
        a = {"typename": "ManagedBGP", "name": "aaa", "local_as": {"asn": 1}}
        b = {"typename": "ManagedBGP", "name": "bbb", "local_as": {"asn": 2}}
        forward = _select_evpn_bgp_process([a, b])
        reverse = _select_evpn_bgp_process([b, a])
        assert forward == reverse
        assert forward is not None
        assert forward["name"] == "aaa"

    def test_falls_back_to_first_process_when_no_role_tagged(self) -> None:
        """ebgp-ebgp collapses underlay+overlay onto one ASN, and older data may
        predate process_role — a real VTEP must not silently lose EVPN."""
        caps = [{"typename": "ManagedBGP", "name": "bgp", "local_as": {"asn": 65001}}]
        result = _select_evpn_bgp_process(caps)
        assert result is not None
        assert result["name"] == "bgp"


# ===========================================================================
# get_vxlan_config() — EVPN enablement and RD/RT derivation
# ===========================================================================


def _bgp_cap(
    *,
    name: str = "bgp-overlay",
    asn: int = 4245880500,
    router_id: str = "10.0.0.7/32",
    process_role: str | None = "overlay",
) -> dict:
    cap: dict = {
        "typename": "ManagedBGP",
        "name": name,
        "local_as": {"asn": asn},
        "router_id": {"address": router_id},
    }
    if process_role is not None:
        cap["process_role"] = process_role
    return cap


class TestGetVxlanConfigEvpn:
    def _data(self, capabilities: list | None = None) -> dict:
        return {
            "name": "dc1-leaf-1",
            "interfaces": [{"name": "Loopback0", "ip_addresses": [{"address": "10.0.0.1/32"}]}],
            "capabilities": capabilities if capabilities is not None else [],
        }

    def test_evpn_disabled_without_a_bgp_process(self) -> None:
        """No BGP process means no fabric ASN, so there is no RT to derive.

        `rt_format` must be None, NOT the literal string "auto": templates skip
        the line when the format is unset, and `rd auto` / `route-target ... auto`
        — while valid NX-OS/EOS — is a syntax error in FRR, which is what the
        Dell SONiC templates emit.

        `rd_format` still resolves, because the RD is per-VTEP and the VTEP's own
        loopback identifies it uniquely even with no BGP process to read a
        router-id from. It is unused in practice since `enabled` is False and the
        templates gate the whole EVPN block on that.
        """
        acts = [_make_activation(vlan_id=100, vni=10100)]
        result = get_vxlan_config(self._data(), "arista_eos", device_role="leaf", activations=acts)
        assert result is not None
        assert result["evpn"]["enabled"] is False
        assert result["evpn"]["rt_format"] is None
        assert result["evpn"]["rd_format"] == "10.0.0.1:{vni}"

    def test_evpn_enabled_with_a_bgp_process(self) -> None:
        """The case that was unreachable before: a VTEP with BGP renders EVPN."""
        acts = [_make_activation(vlan_id=100, vni=10100)]
        result = get_vxlan_config(self._data([_bgp_cap()]), "arista_eos", device_role="leaf", activations=acts)
        assert result is not None
        assert result["evpn"]["enabled"] is True

    def test_rd_uses_overlay_router_id_not_vtep_ip(self) -> None:
        """RD is per-VTEP by design — it exists to distinguish the same
        segment's routes per advertising VTEP."""
        acts = [_make_activation(vlan_id=100, vni=10100)]
        result = get_vxlan_config(
            self._data([_bgp_cap(router_id="10.0.0.7/32")]),
            "arista_eos",
            device_role="leaf",
            activations=acts,
        )
        assert result is not None
        assert result["evpn"]["rd_format"] == "10.0.0.7:{vni}"

    def test_rt_uses_fabric_asn_not_device_local_asn(self) -> None:
        """The fabric-breaking case. Under ebgp-ebgp each VTEP has its own ASN,
        so an RT derived from local_as makes two leaves advertise the same
        segment under different route-targets and neither imports the other's
        routes — the segment silently fails to forward between racks.
        """
        acts = [_make_activation(vlan_id=100, vni=10100)]
        result = get_vxlan_config(
            self._data([_bgp_cap(asn=4245880501)]),
            "arista_eos",
            device_role="leaf",
            activations=acts,
            fabric_rt_asn=4245880999,
        )
        assert result is not None
        assert result["evpn"]["rt_format"] == "4245880999:{vni}"

    def test_two_vteps_with_different_local_asns_derive_the_same_rt(self) -> None:
        """The property that actually matters: same fabric RT ASN in, same RT out."""
        acts = [_make_activation(vlan_id=100, vni=10100)]
        leaf1 = get_vxlan_config(
            self._data([_bgp_cap(asn=4245880501, router_id="10.0.0.1/32")]),
            "arista_eos",
            device_role="leaf",
            activations=acts,
            fabric_rt_asn=4245880999,
        )
        leaf2 = get_vxlan_config(
            self._data([_bgp_cap(asn=4245880502, router_id="10.0.0.2/32")]),
            "arista_eos",
            device_role="leaf",
            activations=acts,
            fabric_rt_asn=4245880999,
        )
        assert leaf1 is not None and leaf2 is not None
        assert leaf1["evpn"]["rt_format"] == leaf2["evpn"]["rt_format"]
        # ...while the RD stays per-VTEP, which is its whole purpose.
        assert leaf1["evpn"]["rd_format"] != leaf2["evpn"]["rd_format"]

    def test_rt_falls_back_to_overlay_asn_when_fabric_asn_absent(self) -> None:
        """ebgp-ibgp/ospf-ibgp share one overlay ASN fabric-wide, so they stay
        correct even before evpn_rt_as is populated."""
        acts = [_make_activation(vlan_id=100, vni=10100)]
        result = get_vxlan_config(
            self._data([_bgp_cap(asn=4245880999)]),
            "arista_eos",
            device_role="leaf",
            activations=acts,
            fabric_rt_asn=None,
        )
        assert result is not None
        assert result["evpn"]["rt_format"] == "4245880999:{vni}"

    def test_rd_and_rt_derived_from_the_overlay_process(self) -> None:
        caps = [
            _bgp_cap(name="a-underlay", asn=4245880501, router_id="10.255.0.1/32", process_role="underlay"),
            _bgp_cap(name="z-overlay", asn=4245880999, router_id="10.0.0.7/32", process_role="overlay"),
        ]
        acts = [_make_activation(vlan_id=100, vni=10100)]
        result = get_vxlan_config(self._data(caps), "arista_eos", device_role="leaf", activations=acts)
        assert result is not None
        assert result["evpn"]["rd_format"] == "10.0.0.7:{vni}"
        assert result["evpn"]["rt_format"] == "4245880999:{vni}"


# ===========================================================================
# _VTEP_ROLES — role spellings must match the schema
# ===========================================================================


class TestVtepRoleSpellings:
    def _data(self) -> dict:
        return {
            "name": "dev",
            "interfaces": [{"name": "Loopback0", "ip_addresses": [{"address": "10.0.0.1/32"}]}],
            "capabilities": [_bgp_cap()],
        }

    def test_hyphenated_border_leaf_is_a_vtep(self) -> None:
        acts = [_make_activation(vlan_id=100, vni=10100)]
        assert get_vxlan_config(self._data(), "arista_eos", device_role="border-leaf", activations=acts) is not None

    def test_underscored_border_leaf_is_not_accepted(self) -> None:
        """The schema role is `border-leaf`. Tolerating `border_leaf` hid the
        fact that transforms/config/border_leaf.py was passing the underscore
        form, which bgp.py's leaf-RR check could never match either.
        """
        acts = [_make_activation(vlan_id=100, vni=10100)]
        assert get_vxlan_config(self._data(), "arista_eos", device_role="border_leaf", activations=acts) is None

    def test_l2_leaf_is_not_a_vtep(self) -> None:
        """schemas/base/dcim.yml describes l2-leaf as explicitly having no
        VXLAN/overlay BGP — it trunks VLANs up to a leaf."""
        acts = [_make_activation(vlan_id=100, vni=10100)]
        assert get_vxlan_config(self._data(), "arista_eos", device_role="l2-leaf", activations=acts) is None

    def test_access_leaf_is_a_vtep(self) -> None:
        """...whereas access-leaf is described as a routed VTEP."""
        acts = [_make_activation(vlan_id=100, vni=10100)]
        assert get_vxlan_config(self._data(), "arista_eos", device_role="access-leaf", activations=acts) is not None


# ===========================================================================
# RD/RT encodability guard
# ===========================================================================


class TestUnencodableVniWarning:
    """A type-1 RD is `IPv4:2-byte` and a type-2 RT with a 4-byte ASN is
    `ASN:2-byte`. Every ASN in this project is a 4-byte private ASN, so a VNI
    above 65535 cannot be encoded into either and the device rejects the line.
    """

    def test_no_warning_for_encodable_vnis(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING):
            _warn_unencodable_vnis([{"vni": 65535}], [{"l3_vni": 50001}], device_name="leaf-1")
        assert caplog.text == ""

    def test_warns_for_oversized_l2_vni(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING):
            _warn_unencodable_vnis([{"vni": 100100}], [], device_name="leaf-1")
        assert "100100" in caplog.text
        assert "leaf-1" in caplog.text

    def test_warns_for_oversized_l3_vni(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING):
            _warn_unencodable_vnis([], [{"l3_vni": 16777000}], device_name="leaf-1")
        assert "16777000" in caplog.text

    def test_missing_vni_values_do_not_crash(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING):
            _warn_unencodable_vnis([{"vni": None}, {}], [{"l3_vni": None}], device_name="leaf-1")
        assert caplog.text == ""

    def test_warns_when_an_l2_vni_collides_with_an_l3_vni(self, caplog: pytest.LogCaptureFixture) -> None:
        """The VNI space is flat, not one namespace per VNI type.

        An L2 segment allocated on top of a VRF's L3 VNI makes the device reject
        the `member vni ... associate` line. This is the symptom of the L2 and
        L3 pool ranges overlapping, which is why the L2 pool is capped at 49999
        below the 50001-59999 L3 range.
        """
        with caplog.at_level(logging.WARNING):
            _warn_unencodable_vnis([{"vni": 50001}], [{"l3_vni": 50001}], device_name="leaf-1")
        assert "50001" in caplog.text
        assert "both an L2 VNI and an L3 VNI" in caplog.text

    def test_no_collision_warning_for_disjoint_ranges(self, caplog: pytest.LogCaptureFixture) -> None:
        """The capped L2 range (10001-49999) never meets the L3 range."""
        with caplog.at_level(logging.WARNING):
            _warn_unencodable_vnis(
                [{"vni": 10001}, {"vni": 49999}],
                [{"l3_vni": 50001}, {"l3_vni": 59999}],
                device_name="leaf-1",
            )
        assert caplog.text == ""

    def test_collision_and_oversize_are_reported_independently(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING):
            _warn_unencodable_vnis(
                [{"vni": 50001}, {"vni": 70000}],
                [{"l3_vni": 50001}],
                device_name="leaf-1",
            )
        assert "70000" in caplog.text
        assert "16-bit" in caplog.text
        assert "both an L2 VNI and an L3 VNI" in caplog.text


# ===========================================================================
# _fabric_rt_asn() — reaching the fabric AS from a device's deployment
# ===========================================================================


class TestFabricRtAsn:
    """Only TopologyDataCenter and TopologyColocationMetro inherit
    TopologySegmentHosting. A border-leaf's deployment IS the DC, but a leaf's
    deployment is the pod — one hop below it.
    """

    def test_reads_from_the_deployment_directly(self) -> None:
        """DC-level tiers: border-leaf, border-spine."""
        assert _fabric_rt_asn({"evpn_rt_as": {"asn": 4245880999}}) == 4245880999

    def test_reads_through_the_parent_hop(self) -> None:
        """Pod-level tiers: leaf, tor, access-leaf."""
        deployment = {"parent": {"evpn_rt_as": {"asn": 4245880999}}}
        assert _fabric_rt_asn(deployment) == 4245880999

    def test_direct_value_wins_over_parent(self) -> None:
        deployment = {"evpn_rt_as": {"asn": 1}, "parent": {"evpn_rt_as": {"asn": 2}}}
        assert _fabric_rt_asn(deployment) == 1

    def test_returns_none_when_unset(self) -> None:
        assert _fabric_rt_asn({"evpn_rt_as": None}) is None
        assert _fabric_rt_asn({}) is None

    def test_returns_none_for_missing_or_malformed_deployment(self) -> None:
        assert _fabric_rt_asn(None) is None
        assert _fabric_rt_asn("not-a-dict") is None

    def test_non_integer_asn_is_rejected(self) -> None:
        assert _fabric_rt_asn({"evpn_rt_as": {"asn": "4245880999"}}) is None


# ===========================================================================
# _fabric_anycast_mac() — the fabric's second must-agree-everywhere constant
# ===========================================================================


class TestFabricAnycastMac:
    """Same deployment/parent walk as _fabric_rt_asn, for the same reason: a
    leaf's deployment is the pod, a border-leaf's IS the DC.
    """

    def test_reads_from_the_deployment_directly(self) -> None:
        assert _fabric_anycast_mac({"evpn_anycast_gateway_mac": "00:00:5e:00:01:01"}) == "00:00:5e:00:01:01"

    def test_reads_through_the_parent_hop(self) -> None:
        deployment = {"parent": {"evpn_anycast_gateway_mac": "00:00:5e:00:01:01"}}
        assert _fabric_anycast_mac(deployment) == "00:00:5e:00:01:01"

    def test_direct_value_wins_over_parent(self) -> None:
        deployment = {
            "evpn_anycast_gateway_mac": "00:00:5e:00:01:01",
            "parent": {"evpn_anycast_gateway_mac": "00:00:5e:00:01:02"},
        }
        assert _fabric_anycast_mac(deployment) == "00:00:5e:00:01:01"

    def test_whitespace_only_is_treated_as_unset(self) -> None:
        """An empty string would otherwise render `anycast-gateway-mac` with no value."""
        assert _fabric_anycast_mac({"evpn_anycast_gateway_mac": "   "}) is None

    def test_value_is_stripped(self) -> None:
        assert _fabric_anycast_mac({"evpn_anycast_gateway_mac": " 00:00:5e:00:01:01 "}) == "00:00:5e:00:01:01"

    def test_returns_none_when_unset_or_malformed(self) -> None:
        assert _fabric_anycast_mac({"evpn_anycast_gateway_mac": None}) is None
        assert _fabric_anycast_mac({}) is None
        assert _fabric_anycast_mac(None) is None
        assert _fabric_anycast_mac("not-a-dict") is None


class TestAnycastMacPlumbing:
    """The MAC has to reach the template from the fabric, not be invented per device."""

    def _data(self) -> dict:
        return {
            "name": "dc1-leaf-1",
            "interfaces": [{"name": "Loopback0", "ip_addresses": [{"address": "10.0.0.1/32"}]}],
            "capabilities": [_bgp_cap()],
        }

    def test_fabric_mac_overrides_the_default(self) -> None:
        acts = [_make_activation(vlan_id=100, vni=10100, gateway_ip="10.1.1.1/24")]
        result = get_vxlan_config(
            self._data(),
            "arista_eos",
            device_role="leaf",
            activations=acts,
            fabric_anycast_mac="00:00:5e:00:01:01",
        )
        assert result is not None
        assert result["anycast_gateway"]["mac"] == "00:00:5e:00:01:01"

    def test_falls_back_to_the_documented_default(self) -> None:
        acts = [_make_activation(vlan_id=100, vni=10100, gateway_ip="10.1.1.1/24")]
        result = get_vxlan_config(self._data(), "arista_eos", device_role="leaf", activations=acts)
        assert result is not None
        assert result["anycast_gateway"]["mac"] == _DEFAULT_ANYCAST_GATEWAY_MAC

    def test_two_leaves_in_one_fabric_get_the_same_mac(self) -> None:
        """The property that matters: a host ARPs for its gateway once and keeps
        that MAC as its flows land on a different leaf. Two different MACs
        blackhole traffic on the leaf that did not answer.
        """
        acts = [_make_activation(vlan_id=100, vni=10100, gateway_ip="10.1.1.1/24")]
        leaf1 = get_vxlan_config(
            {**self._data(), "capabilities": [_bgp_cap(asn=4245880501, router_id="10.0.0.1/32")]},
            "arista_eos",
            device_role="leaf",
            activations=acts,
            fabric_anycast_mac="00:00:5e:00:01:01",
        )
        leaf2 = get_vxlan_config(
            {**self._data(), "capabilities": [_bgp_cap(asn=4245880502, router_id="10.0.0.2/32")]},
            "cisco_nxos",
            device_role="leaf",
            activations=acts,
            fabric_anycast_mac="00:00:5e:00:01:01",
        )
        assert leaf1 is not None and leaf2 is not None
        assert leaf1["anycast_gateway"]["mac"] == leaf2["anycast_gateway"]["mac"]


# ===========================================================================
# _overlay_is_ebgp() and the RT fallback it guards
# ===========================================================================


def _bgp_cap_with_peerings(session_types: list[str], **kwargs) -> dict:
    cap = _bgp_cap(**kwargs)
    cap["peerings"] = [{"session_type": st} for st in session_types]
    return cap


class TestOverlayIsEbgp:
    """Decided by session type, never by TTL: an eBGP overlay peered over
    directly-connected links would defeat a TTL-based guess.
    """

    def test_all_ebgp_peerings_means_a_per_device_asn(self) -> None:
        assert _overlay_is_ebgp(_bgp_cap_with_peerings(["EBGP"])) is True

    def test_session_type_matching_is_case_insensitive(self) -> None:
        assert _overlay_is_ebgp(_bgp_cap_with_peerings(["ebgp"])) is True

    def test_ebgp_variants_are_recognised_by_prefix(self) -> None:
        """EBGP_MULTIHOP and friends are still eBGP."""
        assert _overlay_is_ebgp(_bgp_cap_with_peerings(["EBGP_MULTIHOP"])) is True

    def test_any_ibgp_peering_means_the_asn_is_shared(self) -> None:
        assert _overlay_is_ebgp(_bgp_cap_with_peerings(["IBGP"])) is False

    def test_mixed_sessions_count_as_shared(self) -> None:
        """One iBGP peering is enough to prove the ASN is not unique to this device."""
        assert _overlay_is_ebgp(_bgp_cap_with_peerings(["EBGP", "IBGP"])) is False

    def test_no_process_or_no_peerings_is_not_ebgp(self) -> None:
        assert _overlay_is_ebgp(None) is False
        assert _overlay_is_ebgp(_bgp_cap()) is False
        assert _overlay_is_ebgp(_bgp_cap_with_peerings([])) is False


class TestRtFallbackUnderEbgpOverlayIsLoudlyWrong:
    """The fallback `rt_asn = local_as` is correct under an iBGP overlay and
    fabric-breaking under an eBGP one. Nothing in the rendered config looks
    wrong — sessions come up, routes are advertised, imports never match — so
    the only way an operator finds out is if this is said out loud.
    """

    def _data(self, capabilities: list) -> dict:
        return {
            "name": "dc1-leaf-1",
            "interfaces": [{"name": "Loopback0", "ip_addresses": [{"address": "10.0.0.1/32"}]}],
            "capabilities": capabilities,
        }

    def test_errors_when_ebgp_overlay_has_no_fabric_asn(self, caplog: pytest.LogCaptureFixture) -> None:
        acts = [_make_activation(vlan_id=100, vni=10100)]
        with caplog.at_level(logging.ERROR):
            get_vxlan_config(
                self._data([_bgp_cap_with_peerings(["EBGP"], asn=4245880501)]),
                "arista_eos",
                device_role="leaf",
                activations=acts,
                fabric_rt_asn=None,
            )
        assert "evpn_rt_as" in caplog.text
        assert "dc1-leaf-1" in caplog.text
        assert "4245880501" in caplog.text

    def test_silent_when_the_fabric_asn_is_populated(self, caplog: pytest.LogCaptureFixture) -> None:
        acts = [_make_activation(vlan_id=100, vni=10100)]
        with caplog.at_level(logging.ERROR):
            get_vxlan_config(
                self._data([_bgp_cap_with_peerings(["EBGP"], asn=4245880501)]),
                "arista_eos",
                device_role="leaf",
                activations=acts,
                fabric_rt_asn=4245880999,
            )
        assert caplog.text == ""

    def test_silent_for_an_ibgp_overlay_without_a_fabric_asn(self, caplog: pytest.LogCaptureFixture) -> None:
        """ebgp-ibgp/ospf-ibgp share one overlay ASN fabric-wide, so the
        fallback lands on the same value on every VTEP."""
        acts = [_make_activation(vlan_id=100, vni=10100)]
        with caplog.at_level(logging.ERROR):
            get_vxlan_config(
                self._data([_bgp_cap_with_peerings(["IBGP"], asn=4245880999)]),
                "arista_eos",
                device_role="leaf",
                activations=acts,
                fabric_rt_asn=None,
            )
        assert caplog.text == ""


# ===========================================================================
# svi_vlan_id — the NX-OS-only local VLAN carrying each L3 VNI's transit SVI
# ===========================================================================


class TestL3VniSviVlanAssignment:
    """NX-OS cannot bind a VRF to an L3 VNI without an `ip forward` SVI, and
    that SVI needs a local VLAN. The number has local significance only — no two
    devices need to agree on it — so it is assigned by position rather than
    modelled or drawn from a pool.
    """

    @staticmethod
    def _ns(name: str, l3_vni: int) -> dict:
        return {"name": name, "l3_vni": l3_vni}

    def test_single_vrf_gets_the_base_vlan(self) -> None:
        mappings = _collect_l3_vni_from_namespaces([self._ns("production", 50001)])
        assert [m["svi_vlan_id"] for m in mappings] == [_L3VNI_SVI_VLAN_BASE]

    def test_vlans_are_assigned_by_sorted_vrf_name(self) -> None:
        """Assignment must be stable for a given set of VRFs regardless of the
        order GraphQL returned the namespaces in, or a re-render churns the
        config with no data change."""
        forward = _collect_l3_vni_from_namespaces(
            [self._ns("production", 50001), self._ns("development", 50002), self._ns("staging", 50003)]
        )
        reversed_order = _collect_l3_vni_from_namespaces(
            [self._ns("staging", 50003), self._ns("production", 50001), self._ns("development", 50002)]
        )
        assert [(m["vrf_name"], m["svi_vlan_id"]) for m in forward] == [
            ("development", _L3VNI_SVI_VLAN_BASE),
            ("production", _L3VNI_SVI_VLAN_BASE + 1),
            ("staging", _L3VNI_SVI_VLAN_BASE + 2),
        ]
        assert forward == reversed_order

    def test_band_is_above_the_customer_vlan_ceiling(self) -> None:
        """CUSTOMER_VLAN_ID_MAX is 3899 precisely so this band cannot collide
        with a customer segment's VLAN."""
        from generators.helpers.pools import CUSTOMER_VLAN_ID_MAX

        assert CUSTOMER_VLAN_ID_MAX < _L3VNI_SVI_VLAN_BASE

    def test_band_is_below_the_nxos_reserved_range(self) -> None:
        """NX-OS reserves 3968-4094 for its own internal use."""
        assert _L3VNI_SVI_VLAN_MAX < 3968

    def test_exhausted_band_yields_none_and_warns(self, caplog: pytest.LogCaptureFixture) -> None:
        """None, not an out-of-range VLAN: the template filters these out rather
        than emitting `interface VlanNone`, so the VRF simply gets no symmetric
        IRB on NX-OS instead of a config the device rejects.
        """
        count = _L3VNI_SVI_VLAN_MAX - _L3VNI_SVI_VLAN_BASE + 2
        namespaces = [self._ns(f"vrf-{index:04d}", 50001 + index) for index in range(count)]
        with caplog.at_level(logging.WARNING):
            mappings = _collect_l3_vni_from_namespaces(namespaces)

        assert len(mappings) == count
        assert mappings[-2]["svi_vlan_id"] == _L3VNI_SVI_VLAN_MAX
        assert mappings[-1]["svi_vlan_id"] is None
        assert mappings[-1]["vrf_name"] in caplog.text

    def test_default_namespace_is_excluded(self) -> None:
        """The default namespace is the underlay — it has no tenant L3 VNI."""
        mappings = _collect_l3_vni_from_namespaces([{"name": "default", "l3_vni": 50001}])
        assert mappings == []

    def test_namespace_without_an_l3_vni_is_excluded(self) -> None:
        mappings = _collect_l3_vni_from_namespaces([{"name": "production", "l3_vni": None}])
        assert mappings == []
