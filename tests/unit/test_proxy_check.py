"""Unit tests for CheckProxy."""

from __future__ import annotations

from typing import Any, cast

from checks.proxy import CheckProxy


def _check() -> Any:
    check = cast(Any, CheckProxy.__new__(CheckProxy))
    errors: list[str] = []
    check._captured_errors = errors
    check.log_error = lambda message: errors.append(message)
    return check


def _device(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": "proxy-1",
        "status": "active",
        "platform": {"netmiko_device_type": "cisco_xe"},
        "interfaces": [{"role": "uplink", "status": "active"}],
        "capabilities": [{"typename": "ManagedProxyHA", "name": "ha-1", "capabilities": [{}, {}]}],
    }
    base.update(overrides)
    return base


class TestProxyCheck:
    def test_missing_device_emits_error(self) -> None:
        check = _check()
        check.validate({"DcimPhysicalDevice": []})
        assert check._captured_errors == ["Proxy device not found"]

    def test_missing_platform_status_uplink_and_ha_emit_errors(self) -> None:
        check = _check()
        bad = _device(
            platform={},
            status="planned",
            interfaces=[{"role": "uplink", "status": "planned"}],
            capabilities=[],
        )

        check.validate({"DcimPhysicalDevice": [bad]})

        assert any("no platform with netmiko_device_type" in error for error in check._captured_errors)
        assert any("status is 'planned'" in error for error in check._captured_errors)
        assert any("none are active" in error for error in check._captured_errors)
        assert any("has no HA domain" in error for error in check._captured_errors)

    def test_ha_domain_with_single_member_emits_error(self) -> None:
        check = _check()
        bad = _device(capabilities=[{"typename": "ManagedProxyHA", "name": "ha-1", "capabilities": [{}]}])

        check.validate({"DcimPhysicalDevice": [bad]})

        assert any("fewer than 2 members" in error for error in check._captured_errors)

    def test_fully_valid_device_has_no_errors(self) -> None:
        check = _check()
        good = _device()

        check.validate({"DcimPhysicalDevice": [good]})

        assert check._captured_errors == []
