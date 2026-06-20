"""Unit tests for VXLAN/ACL transform functions in transforms/common.py.

Covers:
  - get_vlans()                  — activation-based VLAN extraction
  - _vlans_from_activations()    — VLAN list from SegmentDeployment records
  - _l2_from_activations()       — L2 VNI mappings from activations
  - _l3_from_activations()       — L3 VNI (VRF) mappings from activations
  - _transform_vxlan_arista()    — anycast_gateway enabled from gateway_ip presence
  - get_acls()                   — zero-trust ACL list from security_policies on segments
"""

from transforms.common import (
    _l2_from_activations,
    _l3_from_activations,
    _transform_vxlan_arista,
    _vlans_from_activations,
    get_acls,
    get_vlans,
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

    prefix: dict = {"ip_namespace": ns}
    if gateway_ip is not None:
        prefix["gateway_ip"] = gateway_ip

    return {
        "vlan_id": vlan_id,
        "vni": vni,
        "status": "active",
        "segment": {
            "name": f"Owner - production - {customer_name}",
            "customer_name": customer_name,
            "arp_suppression": arp_suppression,
            "prefix": prefix,
        },
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
# _transform_vxlan_arista()
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
        }

    def test_interface_set_to_vxlan1(self) -> None:
        result = _transform_vxlan_arista(self._base_config(), local_as=None)
        assert result["interface"] == "Vxlan1"

    def test_anycast_gateway_disabled_when_no_gateway_ip(self) -> None:
        mappings = [
            {"vlan_id": 10, "vni": 10010, "name": "seg-10", "gateway_ip": None},
            {"vlan_id": 20, "vni": 10020, "name": "seg-20", "gateway_ip": None},
        ]
        result = _transform_vxlan_arista(self._base_config(mappings), local_as=None)
        assert result["anycast_gateway"]["enabled"] is False

    def test_anycast_gateway_enabled_when_any_mapping_has_gateway_ip(self) -> None:
        mappings = [
            {"vlan_id": 10, "vni": 10010, "name": "seg-10", "gateway_ip": None},
            {"vlan_id": 20, "vni": 10020, "name": "seg-20", "gateway_ip": "10.100.20.1/24"},
        ]
        result = _transform_vxlan_arista(self._base_config(mappings), local_as=None)
        assert result["anycast_gateway"]["enabled"] is True

    def test_anycast_gateway_enabled_when_all_mappings_have_gateway_ip(self) -> None:
        mappings = [
            {"vlan_id": 10, "vni": 10010, "name": "seg-10", "gateway_ip": "10.0.10.1/24"},
            {"vlan_id": 20, "vni": 10020, "name": "seg-20", "gateway_ip": "10.0.20.1/24"},
        ]
        result = _transform_vxlan_arista(self._base_config(mappings), local_as=None)
        assert result["anycast_gateway"]["enabled"] is True

    def test_anycast_gateway_disabled_when_no_mappings(self) -> None:
        result = _transform_vxlan_arista(self._base_config([]), local_as=None)
        assert result["anycast_gateway"]["enabled"] is False

    def test_anycast_mac_correct(self) -> None:
        result = _transform_vxlan_arista(self._base_config(), local_as=None)
        assert result["anycast_gateway"]["mac"] == "00:1c:73:00:dc:01"

    def test_original_config_not_mutated(self) -> None:
        """_transform_vxlan_arista uses .copy() — original dict is untouched."""
        mappings = [{"vlan_id": 10, "vni": 10010, "gateway_ip": "10.0.10.1/24"}]
        base = self._base_config(mappings)
        _transform_vxlan_arista(base, local_as=None)
        assert "anycast_gateway" not in base
        assert "interface" not in base


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
) -> dict:
    """Build a cleaned SegmentDeployment dict with optional security_policies."""
    seg: dict = {
        "name": f"Owner - production - {customer_name}",
        "customer_name": customer_name,
        "arp_suppression": True,
        "prefix": {"ip_namespace": {"name": "tenant-a", "l3_vni": 50001}},
    }
    if seg_id is not None:
        seg["id"] = seg_id
    if security_policies is not None:
        seg["security_policies"] = security_policies
    return {"vlan_id": vlan_id, "vni": 10000 + vlan_id, "status": "active", "segment": seg}


def _policy(rules: list | None = None, enabled: bool = True) -> dict:
    return {"name": "p", "default_action": "deny", "enabled": enabled, "rules": rules or []}


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
    if src_zone is not None:
        rule["source_zone"] = {"name": src_zone}
    if dst_zone is not None:
        rule["destination_zone"] = {"name": dst_zone}
    return rule


def _seg_ref(prefix: str) -> dict:
    return {"id": "x", "name": "other", "prefix": {"prefix": prefix}}


class TestGetAclsEmpty:
    def test_none_returns_empty(self) -> None:
        assert get_acls(activations=None) == []

    def test_empty_list_returns_empty(self) -> None:
        assert get_acls(activations=[]) == []

    def test_missing_security_policies_key_skips_acl(self) -> None:
        """Segments without security_policies in query data produce no ACL (backwards compat)."""
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
# Symmetric East-West ACL mirroring
# ===========================================================================


def _seg_id_ref(seg_id: str, prefix: str) -> dict:
    """Build a destination_segment dict as clean_data() would produce (with id)."""
    return {"id": seg_id, "name": "some-segment", "prefix": {"prefix": prefix}}


class TestGetAclsEastWestMirroring:
    """Rules that target another local segment are mirrored onto the destination's ACL."""

    def test_no_mirroring_when_no_segment_ids(self) -> None:
        """Segments without 'id' in their dict produce no mirroring (no id index)."""
        dst_ref = _seg_ref("10.0.2.0/24")  # _seg_ref uses id="x" — does not match either activation
        rule_a = _rule(index=10, dst=dst_ref)
        acts = [
            _make_acl_activation(vlan_id=100, security_policies=[_policy(rules=[rule_a])]),
            _make_acl_activation(vlan_id=200, security_policies=[]),
        ]
        # VLAN 200 has no seg_id so the dst id "x" won't resolve → no mirror
        result = get_acls(activations=acts)
        vlan200_rules = next(a["rules"] for a in result if a["vlan_id"] == 200)
        # Only the implicit deny, no mirrored rule
        assert len(vlan200_rules) == 1
        assert vlan200_rules[0]["name"] == "implicit-deny-all"

    def test_cross_segment_rule_mirrored_onto_destination(self) -> None:
        """When A has a rule to B, B's ACL gains a mirrored rule from A."""
        acts = [
            _make_acl_activation(
                vlan_id=100,
                customer_name="seg-a",
                seg_id="seg-a-id",
                security_policies=[_policy(rules=[_rule(index=10, dst=_seg_id_ref("seg-b-id", "10.0.2.0/24"))])],
            ),
            _make_acl_activation(
                vlan_id=200,
                customer_name="seg-b",
                seg_id="seg-b-id",
                security_policies=[],
            ),
        ]
        result = get_acls(activations=acts)
        vlan200 = next(a for a in result if a["vlan_id"] == 200)
        non_deny = [r for r in vlan200["rules"] if r["name"] != "implicit-deny-all"]
        assert len(non_deny) == 1
        mirrored = non_deny[0]
        # Mirrored rule should mirror dst → "any" (traffic already arriving on B's SVI)
        assert mirrored["dst"] == "any"
        assert mirrored["action"] == "permit"
        assert mirrored["protocol"] == "tcp"
        assert mirrored["dst_port"] == "eq 443"
        assert "mirror-from-seg-a" in mirrored["name"]

    def test_mirror_preserves_source_cidr(self) -> None:
        """Source segment CIDR from original rule is preserved in the mirror."""
        acts = [
            _make_acl_activation(
                vlan_id=100,
                customer_name="seg-a",
                seg_id="seg-a-id",
                security_policies=[
                    _policy(
                        rules=[
                            _rule(
                                index=10,
                                src=_seg_id_ref("seg-c-id", "10.0.1.0/24"),
                                dst=_seg_id_ref("seg-b-id", "10.0.2.0/24"),
                            )
                        ]
                    )
                ],
            ),
            _make_acl_activation(vlan_id=200, customer_name="seg-b", seg_id="seg-b-id", security_policies=[]),
        ]
        result = get_acls(activations=acts)
        vlan200 = next(a for a in result if a["vlan_id"] == 200)
        mirrored = [r for r in vlan200["rules"] if r["name"] != "implicit-deny-all"][0]
        assert mirrored["src"] == "10.0.1.0/24"
        assert mirrored["dst"] == "any"

    def test_mirrored_rule_seq_above_own_rules(self) -> None:
        """Mirrored rules get sequence numbers above the segment's own rules (≥5000)."""
        acts = [
            _make_acl_activation(
                vlan_id=100,
                seg_id="seg-a-id",
                security_policies=[_policy(rules=[_rule(index=10, dst=_seg_id_ref("seg-b-id", "10.0.2.0/24"))])],
            ),
            _make_acl_activation(
                vlan_id=200,
                seg_id="seg-b-id",
                security_policies=[_policy(rules=[_rule(index=20)])],
            ),
        ]
        result = get_acls(activations=acts)
        vlan200 = next(a for a in result if a["vlan_id"] == 200)
        own_seqs = [
            r["seq"] for r in vlan200["rules"] if r["name"] not in ("implicit-deny-all",) and "mirror" not in r["name"]
        ]
        mirror_seqs = [r["seq"] for r in vlan200["rules"] if "mirror" in r["name"]]
        assert mirror_seqs
        assert all(ms >= 5000 for ms in mirror_seqs)
        assert all(ms > max(own_seqs) for ms in mirror_seqs)

    def test_no_self_mirroring(self) -> None:
        """Rules where destination is the same segment do not create mirrored duplicates."""
        acts = [
            _make_acl_activation(
                vlan_id=100,
                seg_id="seg-a-id",
                security_policies=[_policy(rules=[_rule(index=10, dst=_seg_id_ref("seg-a-id", "10.0.1.0/24"))])],
            ),
        ]
        result = get_acls(activations=acts)
        # Only own rule + implicit deny; no mirrored rule
        assert len(result[0]["rules"]) == 2

    def test_mirror_implicit_deny_always_last(self) -> None:
        """Implicit deny is still the very last rule even when mirrored rules are added."""
        acts = [
            _make_acl_activation(
                vlan_id=100,
                seg_id="seg-a-id",
                security_policies=[_policy(rules=[_rule(index=10, dst=_seg_id_ref("seg-b-id", "10.0.2.0/24"))])],
            ),
            _make_acl_activation(vlan_id=200, seg_id="seg-b-id", security_policies=[]),
        ]
        result = get_acls(activations=acts)
        vlan200 = next(a for a in result if a["vlan_id"] == 200)
        assert vlan200["rules"][-1]["name"] == "implicit-deny-all"


# ===========================================================================
# Zone support
# ===========================================================================


class TestGetAclsZoneSupport:
    def test_zone_fields_passed_through(self) -> None:
        """src_zone / dst_zone appear in each rule dict when set on the schema rule."""
        rule = _rule(index=10, src_zone="dmz", dst_zone="internal")
        acts = [_make_acl_activation(vlan_id=100, security_policies=[_policy(rules=[rule])])]
        r = get_acls(activations=acts)[0]["rules"][0]
        assert r["src_zone"] == "dmz"
        assert r["dst_zone"] == "internal"

    def test_zone_fields_none_when_absent(self) -> None:
        """Rules without zone references have src_zone=None, dst_zone=None."""
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
