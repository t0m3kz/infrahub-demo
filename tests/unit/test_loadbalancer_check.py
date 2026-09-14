"""Unit tests for CheckLoadBalancer."""

from __future__ import annotations

from typing import Any, cast

from checks.loadbalancer import CheckLoadBalancer


def _check() -> Any:
    check = cast(Any, CheckLoadBalancer.__new__(CheckLoadBalancer))
    errors: list[str] = []
    check._captured_errors = errors
    check.log_error = lambda message: errors.append(message)
    return check


def _device(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": "lb-1",
        "status": "active",
        "platform": {"netmiko_device_type": "f5_tmsh"},
        "interfaces": [
            {
                "role": "uplink",
                "status": "active",
                "interface_capabilities": [{"typename": "LoadbalancerVIP", "name": "vip-1"}],
            }
        ],
        "capabilities": [{"typename": "ManagedLoadbalancerHA", "name": "ha-1", "capabilities": [{}, {}]}],
    }
    base.update(overrides)
    return base


class TestLoadBalancerCheck:
    def test_missing_device_emits_error(self) -> None:
        check = _check()
        check.validate({"DcimPhysicalDevice": []})
        assert check._captured_errors == ["Load balancer device not found"]

    def test_missing_platform_status_uplink_ha_and_vip_emit_errors(self) -> None:
        check = _check()
        bad = _device(
            platform={},
            status="planned",
            interfaces=[{"role": "uplink", "status": "planned", "interface_capabilities": []}],
            capabilities=[],
        )

        check.validate({"DcimPhysicalDevice": [bad]})

        assert any("no platform with netmiko_device_type" in error for error in check._captured_errors)
        assert any("status is 'planned'" in error for error in check._captured_errors)
        assert any("none are active" in error for error in check._captured_errors)
        assert any("has no HA domain" in error for error in check._captured_errors)
        assert any("has no LoadbalancerVIP bound" in error for error in check._captured_errors)

    def test_ha_domain_with_single_member_emits_error(self) -> None:
        check = _check()
        bad = _device(capabilities=[{"typename": "ManagedLoadbalancerHA", "name": "ha-1", "capabilities": [{}]}])

        check.validate({"DcimPhysicalDevice": [bad]})

        assert any("fewer than 2 members" in error for error in check._captured_errors)

    def test_valid_device_has_no_errors(self) -> None:
        check = _check()

        check.validate({"DcimPhysicalDevice": [_device()]})

        assert check._captured_errors == []
