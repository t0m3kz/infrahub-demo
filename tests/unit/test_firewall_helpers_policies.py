"""Unit tests for get_zone_policies() in transforms/helpers/firewall.py."""

from tests.unit.firewall_helpers_shared import (
    _make_segment_with_prefix,
)
from transforms.helpers.firewall import (
    get_zone_policies,
)

# ---------------------------------------------------------------------------
# Helpers — policy / rule data
# ---------------------------------------------------------------------------


def _make_policy(
    *,
    name: str = "east-west",
    default_action: str = "deny",
    enabled: bool = True,
    rules: list | None = None,
) -> dict:
    return {
        "name": name,
        "default_action": default_action,
        "enabled": enabled,
        "rules": rules or [],
    }


def _make_rule(
    *,
    index: int = 10,
    name: str | None = None,
    action: str = "permit",
    protocol: str = "tcp",
    port_start: int | None = 443,
    port_end: int | None = None,
    src_zone: str | None = None,
    dst_zone: str | None = None,
    src_segment: dict | None = None,
    dst_segment: dict | None = None,
    src_prefixes: list[dict] | None = None,
    dst_prefixes: list[dict] | None = None,
    src_ip_addresses: list[dict] | None = None,
    dst_ip_addresses: list[dict] | None = None,
    log: bool = False,
    disabled: bool = False,
    security_profile: dict | None = None,
    description: str | None = None,
) -> dict:
    rule: dict = {
        "index": index,
        "name": name or f"rule-{index}",
        "action": action,
        "protocol": protocol,
        "port_start": port_start,
        "port_end": port_end,
        "source_segment": src_segment,
        "destination_segment": dst_segment,
        "source_prefixes": src_prefixes,
        "destination_prefixes": dst_prefixes,
        "source_ip_addresses": src_ip_addresses,
        "destination_ip_addresses": dst_ip_addresses,
        "log": log,
        "disabled": disabled,
        "description": description or "",
    }
    # A rule's zone is its segment's security_zone.
    if src_zone is not None:
        rule["source_segment"] = {**(rule["source_segment"] or {}), "security_zone": {"name": src_zone}}
    if dst_zone is not None:
        rule["destination_segment"] = {**(rule["destination_segment"] or {}), "security_zone": {"name": dst_zone}}
    if security_profile is not None:
        rule["security_profile"] = security_profile
    return rule


# ===========================================================================
# get_zone_policies()
# ===========================================================================


class TestGetZonePoliciesDefaultAction:
    def test_default_permit_adds_a_catch_all_for_the_policys_segment(self) -> None:
        """Unmatched traffic from the policy's segment passes; the table still ends in deny."""
        policy = {
            **_make_policy(name="seg-web-egress", default_action="permit", rules=[_make_rule()]),
            "segment": {**_make_segment_with_prefix("10.1.0.0/24"), "security_zone": {"name": "web"}},
        }
        rules = get_zone_policies([policy])[0]["rules"]
        catch_all = rules[-2]
        assert (catch_all["name"], catch_all["action"], catch_all["src"], catch_all["src_zone"], catch_all["dst"]) == (
            "seg-web-egress-default-permit",
            "permit",
            "10.1.0.0/24",
            "web",
            None,
        )
        assert rules[-1]["name"] == "implicit-deny-all"

    def test_default_deny_adds_nothing_before_the_implicit_deny(self) -> None:
        """Deny is what the implicit deny-all already does."""
        rules = get_zone_policies([_make_policy(rules=[_make_rule()])])[0]["rules"]
        assert [rule["name"] for rule in rules] == ["rule-10", "implicit-deny-all"]


class TestGetZonePoliciesExternalSegment:
    def test_permit_to_a_prefix_less_segment_renders_zone_with_destination_any(self) -> None:
        rule = _make_rule(
            index=10,
            name="to-internet",
            src_zone="PROD-ZONE",
            dst_segment={"name": "internet"},
            dst_zone="INTERNET-ZONE",
            src_segment={"gateway": {"ip_prefix": {"prefix": "10.1.0.0/24"}}},
        )
        result = get_zone_policies([_make_policy(rules=[rule])])
        out = result[0]["rules"][0]
        assert (out["src_zone"], out["dst_zone"], out["dst"], out["src"]) == (
            "PROD-ZONE",
            "INTERNET-ZONE",
            None,
            "10.1.0.0/24",
        )
        assert (out["action"], out["raw_protocol"], out["port_start"]) == ("permit", "tcp", 443)


class TestGetZonePolicies:
    def test_none_returns_empty(self) -> None:
        assert get_zone_policies(None) == []

    def test_empty_list_returns_empty(self) -> None:
        assert get_zone_policies([]) == []

    def test_disabled_policy_is_skipped(self) -> None:
        policy = _make_policy(name="skipped", enabled=False, rules=[_make_rule()])
        assert get_zone_policies([policy]) == []

    def test_single_policy_basic_fields(self) -> None:
        policy = _make_policy(name="east-west", default_action="deny")
        result = get_zone_policies([policy])
        assert len(result) == 1
        assert result[0]["name"] == "east-west"
        assert result[0]["default_action"] == "deny"

    def test_implicit_deny_always_appended(self) -> None:
        policy = _make_policy(rules=[])
        result = get_zone_policies([policy])
        last = result[0]["rules"][-1]
        assert last["name"] == "implicit-deny-all"
        assert last["action"] == "deny"
        assert last["protocol"] == "ip"
        assert last["log"] is True

    def test_implicit_deny_seq_min_9990_when_no_rules(self) -> None:
        policy = _make_policy(rules=[])
        result = get_zone_policies([policy])
        assert result[0]["rules"][-1]["seq"] == 9990

    def test_implicit_deny_seq_above_last_rule(self) -> None:
        policy = _make_policy(rules=[_make_rule(index=100)])
        result = get_zone_policies([policy])
        last_seq = result[0]["rules"][-1]["seq"]
        assert last_seq >= 110

    def test_disabled_rule_is_skipped(self) -> None:
        rules = [_make_rule(index=10), _make_rule(index=20, disabled=True)]
        policy = _make_policy(rules=rules)
        result = get_zone_policies([policy])
        # One active rule + implicit deny
        non_deny = [r for r in result[0]["rules"] if r["name"] != "implicit-deny-all"]
        assert len(non_deny) == 1
        assert non_deny[0]["seq"] == 10

    def test_rules_sorted_by_index(self) -> None:
        rules = [_make_rule(index=30), _make_rule(index=10), _make_rule(index=20)]
        policy = _make_policy(rules=rules)
        result = get_zone_policies([policy])
        seqs = [r["seq"] for r in result[0]["rules"] if r["name"] != "implicit-deny-all"]
        assert seqs == [10, 20, 30]

    def test_rules_numbered_across_the_whole_table(self) -> None:
        """Two policies whose rules share indexes get distinct, ordered seqs."""
        first = _make_policy(name="a", rules=[_make_rule(index=100, name="a1"), _make_rule(index=200, name="a2")])
        second = _make_policy(name="b", rules=[_make_rule(index=100, name="b1")])
        result = get_zone_policies([first, second])
        seqs = [(r["name"], r["seq"]) for p in result for r in p["rules"] if r["name"] != "implicit-deny-all"]
        assert seqs == [("a1", 10), ("a2", 20), ("b1", 30)]

    def test_one_implicit_deny_closes_the_table(self) -> None:
        """A deny-all per policy would shadow every later policy's rules."""
        first = _make_policy(name="a", rules=[_make_rule(name="a1")])
        second = _make_policy(name="b", rules=[_make_rule(name="b1")])
        result = get_zone_policies([first, second])
        names = [r["name"] for p in result for r in p["rules"]]
        assert names == ["a1", "b1", "implicit-deny-all"]

    def test_disabled_last_policy_does_not_drop_the_implicit_deny(self) -> None:
        first = _make_policy(name="a", rules=[_make_rule(name="a1")])
        disabled = _make_policy(name="b", enabled=False, rules=[_make_rule(name="b1")])
        result = get_zone_policies([first, disabled])
        assert [p["name"] for p in result] == ["a"]
        assert result[0]["rules"][-1]["name"] == "implicit-deny-all"

    def test_implicit_deny_seq_stays_above_a_long_table(self) -> None:
        rules = [_make_rule(index=i, name=f"r{i}") for i in range(1, 1001)]
        result = get_zone_policies([_make_policy(rules=rules)])
        assert result[0]["rules"][-1]["seq"] == 10010

    def test_rule_tcp_with_single_port(self) -> None:
        rule = _make_rule(protocol="tcp", port_start=443, port_end=None)
        result = get_zone_policies([_make_policy(rules=[rule])])
        r = result[0]["rules"][0]
        assert r["protocol"] == "tcp"
        assert r["dst_port"] == "eq 443"

    def test_rule_exposes_raw_protocol_and_raw_ports(self) -> None:
        """raw_protocol/port_start/port_end are the unmapped values, for
        templates (PAN-OS, Junos) that build their own service/application
        object instead of consuming the Cisco-ACL-style dst_port string."""
        rule = _make_rule(protocol="tcp", port_start=8080, port_end=8090)
        result = get_zone_policies([_make_policy(rules=[rule])])
        r = result[0]["rules"][0]
        assert r["raw_protocol"] == "tcp"
        assert r["port_start"] == 8080
        assert r["port_end"] == 8090

    def test_rule_raw_protocol_any_stays_any_unlike_mapped_protocol(self) -> None:
        rule = _make_rule(protocol="any", port_start=None)
        result = get_zone_policies([_make_policy(rules=[rule])])
        r = result[0]["rules"][0]
        assert r["protocol"] == "ip"
        assert r["raw_protocol"] == "any"

    def test_implicit_deny_rule_has_no_raw_protocol_or_ports(self) -> None:
        policy = _make_policy(rules=[])
        result = get_zone_policies([policy])
        last = result[0]["rules"][-1]
        assert last["raw_protocol"] == "any"
        assert last["port_start"] is None
        assert last["port_end"] is None

    def test_rule_tcp_with_port_range(self) -> None:
        rule = _make_rule(protocol="tcp", port_start=8080, port_end=8090)
        result = get_zone_policies([_make_policy(rules=[rule])])
        assert result[0]["rules"][0]["dst_port"] == "range 8080 8090"

    def test_rule_protocol_any_maps_to_ip(self) -> None:
        rule = _make_rule(protocol="any", port_start=None)
        result = get_zone_policies([_make_policy(rules=[rule])])
        assert result[0]["rules"][0]["protocol"] == "ip"

    def test_rule_icmp_port_not_set(self) -> None:
        """Port is ignored for non-TCP/UDP protocols."""
        rule = _make_rule(protocol="icmp", port_start=8)
        result = get_zone_policies([_make_policy(rules=[rule])])
        assert result[0]["rules"][0]["dst_port"] is None

    def test_rule_udp_single_port(self) -> None:
        rule = _make_rule(protocol="udp", port_start=53, port_end=None)
        result = get_zone_policies([_make_policy(rules=[rule])])
        assert result[0]["rules"][0]["dst_port"] == "eq 53"

    def test_rule_src_zone_and_dst_zone(self) -> None:
        rule = _make_rule(src_zone="dmz", dst_zone="internal")
        result = get_zone_policies([_make_policy(rules=[rule])])
        r = result[0]["rules"][0]
        assert r["src_zone"] == "dmz"
        assert r["dst_zone"] == "internal"

    def test_rule_zone_fields_none_when_absent(self) -> None:
        rule = _make_rule()
        result = get_zone_policies([_make_policy(rules=[rule])])
        r = result[0]["rules"][0]
        assert r["src_zone"] is None
        assert r["dst_zone"] is None

    def test_rule_src_and_dst_segment_prefix(self) -> None:
        src_seg = _make_segment_with_prefix("10.1.0.0/24")
        dst_seg = _make_segment_with_prefix("10.2.0.0/24")
        rule = _make_rule(src_segment=src_seg, dst_segment=dst_seg, port_start=None)
        result = get_zone_policies([_make_policy(rules=[rule])])
        r = result[0]["rules"][0]
        assert r["src"] == "10.1.0.0/24"
        assert r["dst"] == "10.2.0.0/24"

    def test_rule_src_dst_none_when_no_segments(self) -> None:
        rule = _make_rule(src_segment=None, dst_segment=None)
        result = get_zone_policies([_make_policy(rules=[rule])])
        r = result[0]["rules"][0]
        assert r["src"] is None
        assert r["dst"] is None

    def test_rule_dst_falls_back_to_destination_prefix(self) -> None:
        """No destination_segment (e.g. a cloud/partner/SaaS CIDR reached over
        an interconnect, not a customer segment) — destination_prefixes is
        the selector instead."""
        rule = _make_rule(dst_segment=None, dst_prefixes=[{"id": "p1", "prefix": "172.31.0.0/16"}])
        result = get_zone_policies([_make_policy(rules=[rule])])
        assert result[0]["rules"][0]["dst"] == "172.31.0.0/16"

    def test_rule_src_falls_back_to_source_prefix(self) -> None:
        rule = _make_rule(src_segment=None, src_prefixes=[{"id": "p1", "prefix": "10.50.0.0/16"}])
        result = get_zone_policies([_make_policy(rules=[rule])])
        assert result[0]["rules"][0]["src"] == "10.50.0.0/16"

    def test_rule_dst_falls_back_to_destination_ip_address_when_no_prefix(self) -> None:
        rule = _make_rule(
            dst_segment=None,
            dst_prefixes=[],
            dst_ip_addresses=[{"id": "ip1", "address": "203.0.113.5/32"}],
        )
        result = get_zone_policies([_make_policy(rules=[rule])])
        assert result[0]["rules"][0]["dst"] == "203.0.113.5/32"

    def test_rule_dst_prefix_takes_priority_over_ip_address(self) -> None:
        rule = _make_rule(
            dst_segment=None,
            dst_prefixes=[{"id": "p1", "prefix": "172.31.0.0/16"}],
            dst_ip_addresses=[{"id": "ip1", "address": "203.0.113.5/32"}],
        )
        result = get_zone_policies([_make_policy(rules=[rule])])
        assert result[0]["rules"][0]["dst"] == "172.31.0.0/16"

    def test_rule_dst_segment_takes_priority_over_destination_prefix(self) -> None:
        dst_seg = _make_segment_with_prefix("10.2.0.0/24")
        rule = _make_rule(dst_segment=dst_seg, dst_prefixes=[{"id": "p1", "prefix": "172.31.0.0/16"}])
        result = get_zone_policies([_make_policy(rules=[rule])])
        assert result[0]["rules"][0]["dst"] == "10.2.0.0/24"

    def test_rule_log_field(self) -> None:
        rule = _make_rule(log=True)
        result = get_zone_policies([_make_policy(rules=[rule])])
        assert result[0]["rules"][0]["log"] is True

    def test_rule_log_false_by_default(self) -> None:
        rule = _make_rule(log=False)
        result = get_zone_policies([_make_policy(rules=[rule])])
        assert result[0]["rules"][0]["log"] is False

    def test_rule_security_profile_extracted(self) -> None:
        rule = _make_rule(security_profile={"name": "strict-av"})
        result = get_zone_policies([_make_policy(rules=[rule])])
        assert result[0]["rules"][0]["security_profile"] == "strict-av"

    def test_rule_security_profile_none_when_absent(self) -> None:
        rule = _make_rule(security_profile=None)
        result = get_zone_policies([_make_policy(rules=[rule])])
        assert result[0]["rules"][0]["security_profile"] is None

    def test_multiple_policies_all_returned(self) -> None:
        policies = [
            _make_policy(name="policy-a", rules=[_make_rule(index=10)]),
            _make_policy(name="policy-b", rules=[_make_rule(index=20)]),
        ]
        result = get_zone_policies(policies)
        assert len(result) == 2
        names = {p["name"] for p in result}
        assert names == {"policy-a", "policy-b"}

    def test_mixed_enabled_disabled_policies(self) -> None:
        policies = [
            _make_policy(name="active", enabled=True, rules=[_make_rule()]),
            _make_policy(name="inactive", enabled=False, rules=[_make_rule()]),
        ]
        result = get_zone_policies(policies)
        assert len(result) == 1
        assert result[0]["name"] == "active"

    def test_implicit_deny_has_null_zones(self) -> None:
        policy = _make_policy(rules=[])
        result = get_zone_policies([policy])
        deny = result[0]["rules"][-1]
        assert deny["src_zone"] is None
        assert deny["dst_zone"] is None
        assert deny["src"] is None
        assert deny["dst"] is None

    def test_rule_port_range_same_start_end_uses_eq(self) -> None:
        """When port_end equals port_start the result is 'eq X', not 'range X X'."""
        rule = _make_rule(protocol="tcp", port_start=80, port_end=80)
        result = get_zone_policies([_make_policy(rules=[rule])])
        assert result[0]["rules"][0]["dst_port"] == "eq 80"

    def test_policy_enabled_key_missing_defaults_to_enabled(self) -> None:
        """A policy dict without an 'enabled' key is treated as enabled."""
        policy = {"name": "no-enabled-key", "default_action": "deny", "rules": []}
        result = get_zone_policies([policy])
        assert len(result) == 1
        assert result[0]["name"] == "no-enabled-key"
