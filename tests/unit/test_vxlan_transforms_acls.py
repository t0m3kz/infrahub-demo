"""Unit tests for get_acls(): zero-trust ACL extraction, return legs, zones and isolation_mode."""

from transforms.helpers.acl import _build_acl_rule, get_acls
from utils.internet_space import NON_INTERNET_PREFIXES

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


def _internet_ref() -> dict:
    """A rule end naming a prefix-less external segment (the Internet segment)."""
    return {"id": "inet", "name": "internet", "security_zone": {"name": "INTERNET-ZONE"}}


class TestGetAclsInternetBand:
    """A permit to a segment with no prefix must not render as `permit <src> any`."""

    SRC = _seg_ref("10.1.0.0/24")

    def _rules(self, own: list[dict], **kwargs) -> list[dict]:
        act = _make_acl_activation(
            vlan_id=100, seg_id="seg-a", prefix="10.1.0.0/24", security_policies=[_policy(rules=own, **kwargs)]
        )
        return get_acls(activations=[act])[0]["rules"]

    def test_denies_every_internal_range_before_the_permit_any(self) -> None:
        rules = self._rules([_rule(src=self.SRC, dst=_internet_ref())])
        permit_at = next(i for i, r in enumerate(rules) if r["action"] == "permit")
        assert [r["dst"] for r in rules[:permit_at]] == list(NON_INTERNET_PREFIXES)
        assert all(
            r["action"] == "deny" and r["protocol"] == "ip" and r["src"] == "10.1.0.0/24" for r in rules[:permit_at]
        )
        denied = {r["dst"] for r in rules[:permit_at]}
        assert {"198.18.0.0/15", "100.64.0.0/10"} <= denied
        permit = rules[permit_at]
        assert (permit["protocol"], permit["src"], permit["dst"], permit["dst_port"]) == (
            "tcp",
            "10.1.0.0/24",
            "any",
            "eq 443",
        )
        assert permit["dst_zone"] == "INTERNET-ZONE"
        assert rules[-1]["name"] == "implicit-deny-all"

    def test_no_unconditional_permit_any_at_the_rules_own_index(self) -> None:
        rules = self._rules([_rule(index=10, src=self.SRC, dst=_internet_ref())])
        assert 10 not in [r["seq"] for r in rules]
        seqs = [r["seq"] for r in rules]
        assert seqs == sorted(seqs)

    def test_intra_zone_permits_are_not_shadowed(self) -> None:
        """Own permits to internal segments keep their own, earlier index."""
        rules = self._rules(
            [
                _rule(index=10, src=self.SRC, dst=_seg_ref("10.2.0.0/24")),
                _rule(index=20, src=self.SRC, dst=_internet_ref()),
            ]
        )
        internal = next(r for r in rules if r["dst"] == "10.2.0.0/24")
        first_deny = next(r for r in rules if r["action"] == "deny" and r["dst"] == "10.0.0.0/8")
        assert internal["seq"] == 10 < first_deny["seq"]

    def test_band_follows_the_return_legs(self) -> None:
        """A return leg is src=own prefix, dst=an internal peer: the denies must not shadow it."""
        inbound = {
            **_rule(port_start=5432),
            "source_segment": {"id": "seg-x", "name": "x", "gateway": {"ip_prefix": {"prefix": "10.9.0.0/24"}}},
            "destination_segment": {"id": "seg-a"},
            "policy": {"enabled": True},
        }
        act = _make_acl_activation(
            seg_id="seg-a",
            prefix="10.1.0.0/24",
            security_policies=[_policy(rules=[_rule(src=self.SRC, dst=_internet_ref())])],
            inbound_rules=[inbound],
        )
        rules = get_acls(activations=[act])[0]["rules"]
        ret = next(r for r in rules if r["name"].startswith("return-to-"))
        first_deny = next(r for r in rules if r["action"] == "deny")
        assert ret["seq"] == 5000 < first_deny["seq"]

    def test_denies_are_emitted_once_per_source(self) -> None:
        rules = self._rules(
            [
                _rule(index=10, src=self.SRC, dst=_internet_ref()),
                _rule(index=20, src=self.SRC, dst=_internet_ref(), port_start=80),
            ]
        )
        assert len([r for r in rules if r["action"] == "deny" and r["dst"] == "10.0.0.0/8"]) == 1
        assert [r["dst_port"] for r in rules if r["action"] == "permit"] == ["eq 443", "eq 80"]

    def test_without_a_segment_or_selector_the_rule_stays_any(self) -> None:
        rules = self._rules([_rule(index=10, src=self.SRC)])
        assert [(r["seq"], r["action"], r["dst"]) for r in rules[:1]] == [(10, "permit", "any")]
        assert len(rules) == 2

    def test_selector_on_the_external_segment_keeps_the_rule_in_place(self) -> None:
        rule = {
            **_rule(index=10, src=self.SRC, dst=_internet_ref()),
            "destination_prefixes": [{"prefix": "8.8.8.0/24"}],
        }
        rules = self._rules([rule])
        assert [(r["seq"], r["dst"]) for r in rules[:1]] == [(10, "8.8.8.0/24")]
        assert len(rules) == 2

    def test_deny_rule_to_the_external_segment_stays_in_place(self) -> None:
        rules = self._rules([_rule(index=10, action="deny", src=self.SRC, dst=_internet_ref())])
        assert [(r["seq"], r["action"], r["dst"]) for r in rules[:1]] == [(10, "deny", "any")]

    def test_default_permit_policy_needs_no_band(self) -> None:
        rules = self._rules([_rule(src=self.SRC, dst=_internet_ref())], default_action="permit")
        assert [r["name"] for r in rules] == ["default-permit-all"]

    def test_inbound_permit_from_the_external_segment_gets_no_return_leg(self) -> None:
        inbound = {
            **_rule(port_start=443),
            "source_segment": _internet_ref(),
            "destination_segment": {"id": "seg-a"},
            "policy": {"enabled": True},
        }
        act = _make_acl_activation(seg_id="seg-a", prefix="10.1.0.0/24", security_policies=[], inbound_rules=[inbound])
        assert [r["name"] for r in get_acls(activations=[act])[0]["rules"]] == ["implicit-deny-all"]


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
    else:
        src["security_zone"] = {"name": "INTERNET-ZONE"}  # a prefix-less source is the outside
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

    def test_source_without_prefix_gets_no_return_leg(self) -> None:
        """A prefix-less source is the outside (DNAT, out of scope): no `permit ... any` reply."""
        acl = get_acls(activations=[_segment_b([_inbound(src_prefix=None)])])[0]
        assert _returns(acl) == []

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
        rule = _rule(
            index=10, src=_seg_ref("10.1.0.0/24"), dst=_seg_ref("10.2.0.0/24"), src_zone="dmz", dst_zone="internal"
        )
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
