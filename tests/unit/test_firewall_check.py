"""Unit tests for CheckFirewall."""

from __future__ import annotations

from typing import Any

from checks.firewall import CheckFirewall


def _check() -> Any:
    check = CheckFirewall.__new__(CheckFirewall)
    errors: list[str] = []
    infos: list[str] = []
    check._captured_errors = errors
    check._captured_infos = infos
    check.log_error = lambda message: errors.append(message)
    check.log_info = lambda message: infos.append(message)
    return check


class TestFirewallCheck:
    def test_missing_zone_reference_raises_error(self) -> None:
        check = _check()
        payload = {
            "SecurityZone": [{"name": "zone-a", "network_segments": [{"prefix": {"prefix": "10.0.0.0/24"}}]}],
            "SecurityPolicy": [
                {
                    "name": "policy-1",
                    "enabled": True,
                    "rules": [
                        {
                            "name": "rule-1",
                            "disabled": False,
                            "source_zone": {"name": "zone-a"},
                            "destination_zone": {"name": "zone-missing"},
                        }
                    ],
                }
            ],
        }

        check.validate(payload)

        assert len(check._captured_errors) == 1
        assert "non-existent SecurityZone" in check._captured_errors[0]

    def test_empty_zone_members_emits_info(self) -> None:
        check = _check()
        payload = {
            "SecurityZone": [{"name": "zone-empty", "network_segments": []}],
            "SecurityPolicy": [
                {
                    "name": "policy-1",
                    "enabled": True,
                    "rules": [
                        {
                            "name": "rule-1",
                            "disabled": False,
                            "source_zone": {"name": "zone-empty"},
                            "destination_zone": {"name": "zone-empty"},
                        }
                    ],
                }
            ],
        }

        check.validate(payload)

        assert check._captured_errors == []
        assert len(check._captured_infos) == 2
        assert "has no member segments" in check._captured_infos[0]

    def test_disabled_policy_and_rule_are_skipped(self) -> None:
        check = _check()
        payload = {
            "SecurityZone": [{"name": "zone-a", "network_segments": [{"prefix": {"prefix": "10.0.0.0/24"}}]}],
            "SecurityPolicy": [
                {
                    "name": "policy-disabled",
                    "enabled": False,
                    "rules": [
                        {
                            "name": "rule-1",
                            "disabled": False,
                            "source_zone": {"name": "zone-missing"},
                        }
                    ],
                },
                {
                    "name": "policy-2",
                    "enabled": True,
                    "rules": [
                        {
                            "name": "rule-disabled",
                            "disabled": True,
                            "destination_zone": {"name": "zone-missing"},
                        }
                    ],
                },
            ],
        }

        check.validate(payload)

        assert check._captured_errors == []
        assert check._captured_infos == []
