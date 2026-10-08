"""Unit tests for CheckFirewall."""

from __future__ import annotations

from typing import Any, cast

from checks.firewall import CheckFirewall


def _check() -> Any:
    check = cast(Any, CheckFirewall.__new__(CheckFirewall))
    errors: list[str] = []
    infos: list[str] = []
    check._captured_errors = errors
    check._captured_infos = infos
    check.log_error = lambda message: errors.append(message)
    check.log_info = lambda message: infos.append(message)
    return check


def _segment(seg_id: str, zone: str | None = "zone-a", tag: str | None = None) -> dict[str, Any]:
    return {
        "id": seg_id,
        "name": seg_id,
        "security_zone": {"name": zone} if zone else None,
        "security_tag": {"id": tag, "name": tag} if tag else None,
    }


def _payload(rules: list[dict[str, Any]], tag_rules: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "SecurityPolicy": [{"name": "policy-1", "enabled": True, "rules": rules}],
        "SecurityTagRule": tag_rules or [],
    }


class TestFirewallCheck:
    def test_segment_rule_with_zones_passes(self) -> None:
        """A rule between two zoned segments raises nothing."""
        check = _check()
        rule = {"name": "rule-1", "source_segment": _segment("seg-a"), "destination_segment": _segment("seg-b")}

        check.validate(_payload([rule]))

        assert check._captured_errors == []
        assert check._captured_infos == []

    def test_missing_selector_raises_error(self) -> None:
        """A side with no segment, IP or prefix is an error."""
        check = _check()
        rule = {"name": "rule-1", "source_segment": _segment("seg-a")}

        check.validate(_payload([rule]))

        assert len(check._captured_errors) == 1
        assert "has no destination selector" in check._captured_errors[0]

    def test_prefix_selector_counts_as_selector(self) -> None:
        """A prefix-only destination is a complete selector and needs no zone."""
        check = _check()
        rule = {
            "name": "rule-1",
            "source_segment": _segment("seg-a"),
            "destination_prefixes": [{"id": "pfx-1", "prefix": "198.51.100.0/24"}],
        }

        check.validate(_payload([rule]))

        assert check._captured_errors == []
        assert check._captured_infos == []

    def test_segment_without_zone_emits_info(self) -> None:
        """A segment with no security_zone is reported: zone-based firewalls match any zone there."""
        check = _check()
        rule = {
            "name": "rule-1",
            "source_segment": _segment("seg-a", zone=None),
            "destination_segment": _segment("seg-b"),
        }

        check.validate(_payload([rule]))

        assert check._captured_errors == []
        assert len(check._captured_infos) == 1
        assert "source_segment 'seg-a' has no security_zone" in check._captured_infos[0]

    def test_segment_tags_without_contract_raise_error(self) -> None:
        """Tagged source and destination segments need a matching SecurityTagRule."""
        check = _check()
        rule = {
            "name": "rule-1",
            "source_segment": _segment("seg-a", tag="tag-a"),
            "destination_segment": _segment("seg-b", tag="tag-b"),
        }

        check.validate(_payload([rule]))
        assert len(check._captured_errors) == 1
        assert "without a matching SecurityTagRule contract" in check._captured_errors[0]

        check = _check()
        contract = {"source_tag": {"id": "tag-a", "name": "a"}, "destination_tag": {"id": "tag-b", "name": "b"}}
        check.validate(_payload([rule], tag_rules=[contract]))
        assert check._captured_errors == []

    def test_disabled_policy_and_rule_are_skipped(self) -> None:
        """Disabled policies and rules are not validated."""
        check = _check()
        payload = {
            "SecurityPolicy": [
                {"name": "policy-disabled", "enabled": False, "rules": [{"name": "rule-1", "disabled": False}]},
                {"name": "policy-2", "enabled": True, "rules": [{"name": "rule-disabled", "disabled": True}]},
            ],
        }

        check.validate(payload)

        assert check._captured_errors == []
        assert check._captured_infos == []
