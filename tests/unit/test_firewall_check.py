"""Unit tests for CheckFirewall: it validates the rules the firewall renders
(Firewall.collect_policies), fed the raw firewall_config query shape."""

from __future__ import annotations

from typing import Any, cast

from checks.firewall import CheckFirewall


def _v(value: Any) -> dict[str, Any]:
    return {"value": value}


def _node(inner: dict[str, Any] | None) -> dict[str, Any]:
    return {"node": inner}


def _edges(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    return {"edges": [{"node": node} for node in nodes]}


def _check() -> Any:
    check = cast(Any, CheckFirewall.__new__(CheckFirewall))
    errors: list[str] = []
    infos: list[str] = []
    check._captured_errors = errors
    check._captured_infos = infos
    check.log_error = lambda message: errors.append(message)
    check.log_info = lambda message: infos.append(message)
    return check


def _ref(seg_id: str, zone: str | None = "zone-a") -> dict[str, Any]:
    """A rule end (PolicyRuleSegmentFields)."""
    return {"id": seg_id, "name": _v(seg_id), "security_zone": _node({"name": _v(zone)} if zone else None)}


def _rule(name: str, src: str | None = "seg-a", dst: str | None = "seg-b", **extra: Any) -> dict[str, Any]:
    return {
        "id": f"rule-{name}",
        "index": _v(10),
        "name": _v(name),
        "action": _v("permit"),
        "disabled": _v(False),
        "source_segment": _node(_ref(src) if src else None),
        "destination_segment": _node(_ref(dst) if dst else None),
        "destination_prefixes": _edges([]),
        "destination_ip_addresses": _edges([]),
        **extra,
    }


def _policy(rules: list[dict[str, Any]], owner: str = "seg-a", enabled: bool = True) -> dict[str, Any]:
    return {
        "id": "pol-1",
        "name": _v("policy-1"),
        "enabled": _v(enabled),
        "segment": _node({"id": owner, "name": _v(owner)}),
        "rules": _edges(rules),
    }


def _carried(seg_id: str, policy: dict[str, Any] | None, inbound: list[dict[str, Any]] | None = None) -> dict:
    """A segment on a firewall interface (NetworkSegmentFields + SegmentRulesFields)."""
    return {
        "__typename": "ManagedVlanSegment",
        "id": seg_id,
        "name": _v(seg_id),
        "status": _v("active"),
        "vlan_id": _v(10),
        "security_policy": _node(policy),
        "inbound_rules": _edges(inbound or []),
    }


def _payload(*capabilities: dict[str, Any]) -> dict[str, Any]:
    interfaces = [
        {"name": _v(f"eth0.{i}"), "interface_capabilities": _edges([capability])}
        for i, capability in enumerate(capabilities)
    ]
    return {"DcimPhysicalDevice": _edges([{"name": _v("fw-1"), "interfaces": _edges(interfaces)}])}


def _validate(*capabilities: dict[str, Any]) -> Any:
    check = _check()
    check.validate(_payload(*capabilities))
    return check


class TestFirewallCheck:
    def test_rule_in_its_source_segments_policy_passes(self) -> None:
        """A rule between two zoned segments, held by its source's policy, raises nothing."""
        check = _validate(_carried("seg-a", _policy([_rule("rule-1")])))
        assert check._captured_errors == []
        assert check._captured_infos == []

    def test_rule_of_another_segment_in_the_policy_raises_error(self) -> None:
        check = _validate(_carried("seg-a", _policy([_rule("rule-1", src="seg-x")])))
        assert len(check._captured_errors) == 1
        assert "source segment 'seg-x' is not the policy's segment" in check._captured_errors[0]

    def test_policy_without_segment_raises_error(self) -> None:
        policy = {**_policy([_rule("rule-1")]), "segment": _node(None)}
        check = _validate(_carried("seg-a", policy))
        assert len(check._captured_errors) == 1
        assert "is not the policy's segment" in check._captured_errors[0]

    def test_missing_destination_selector_raises_error(self) -> None:
        check = _validate(_carried("seg-a", _policy([_rule("rule-1", dst=None)])))
        assert len(check._captured_errors) == 1
        assert "has no destination selector" in check._captured_errors[0]

    def test_prefix_destination_counts_as_selector(self) -> None:
        """A prefix-only destination is a complete selector and needs no zone."""
        rule = _rule(
            "rule-1", dst=None, destination_prefixes=_edges([{"id": "pfx-1", "prefix": _v("198.51.100.0/24")}])
        )
        check = _validate(_carried("seg-a", _policy([rule])))
        assert check._captured_errors == []
        assert check._captured_infos == []

    def test_segment_without_zone_emits_info(self) -> None:
        """A segment with no security_zone is reported: zone-based firewalls match any zone there."""
        rule = {**_rule("rule-1"), "source_segment": _node(_ref("seg-a", zone=None))}
        check = _validate(_carried("seg-a", _policy([rule])))
        assert check._captured_errors == []
        assert len(check._captured_infos) == 1
        assert "source_segment 'seg-a' has no security_zone" in check._captured_infos[0]

    def test_disabled_policy_and_rule_are_skipped(self) -> None:
        bad = _rule("bad", src="seg-x", dst=None)
        disabled_rule = {**bad, "id": "rule-off", "disabled": _v(True)}
        check = _validate(
            _carried("seg-a", _policy([disabled_rule])),
            _carried("seg-c", {**_policy([bad], owner="seg-c", enabled=False), "id": "pol-2", "name": _v("p-2")}),
        )
        assert check._captured_errors == []

    def test_inbound_rules_are_validated_against_their_own_policy(self) -> None:
        """The ingress leg of another segment's rule is checked with that rule's policy."""
        inbound = {**_rule("in-1", src="seg-x", dst="seg-a"), "policy": _node(_policy([], owner="seg-y"))}
        check = _validate(_carried("seg-a", None, inbound=[inbound]))
        assert len(check._captured_errors) == 1
        assert "'seg-x' is not the policy's segment" in check._captured_errors[0]

    def test_rule_reached_twice_is_reported_once(self) -> None:
        bad = _rule("rule-1", dst=None)
        inbound = {**bad, "policy": _node(_policy([]))}
        check = _validate(_carried("seg-a", _policy([bad]), inbound=[inbound]))
        assert len(check._captured_errors) == 1

    def test_rules_of_segments_the_firewall_does_not_serve_are_not_validated(self) -> None:
        """Only the served segments' rules are this firewall's: a context serving
        nothing and no carried segment leave nothing to check."""
        context = {
            "__typename": "ManagedFirewallContext",
            "id": "ctx-1",
            "name": _v("ctx-1"),
            "served_deployments": _edges([{"id": "dep-1", "network_segments": _edges([])}]),
        }
        check = _validate(context)
        assert check._captured_errors == []

    def test_context_served_segment_rules_are_validated(self) -> None:
        served = {
            "id": "seg-a",
            "security_policy": _node(_policy([_rule("rule-1", dst=None)])),
            "inbound_rules": _edges([]),
        }
        context = {
            "__typename": "ManagedFirewallContext",
            "id": "ctx-1",
            "name": _v("ctx-1"),
            "served_deployments": _edges([{"id": "dep-1", "network_segments": _edges([served])}]),
        }
        check = _validate(context)
        assert len(check._captured_errors) == 1
        assert "has no destination selector" in check._captured_errors[0]
