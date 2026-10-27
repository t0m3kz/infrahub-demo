"""Unit tests for CheckLoadBalancer and its context validation."""

from __future__ import annotations

from typing import Any, cast

from checks.loadbalancer import CheckLoadBalancer, validate_loadbalancer_contexts


def _check() -> Any:
    check = cast(Any, CheckLoadBalancer.__new__(CheckLoadBalancer))
    errors: list[str] = []
    check._captured_errors = errors
    check.log_error = lambda message: errors.append(message)
    return check


def _vip(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "hostname": "web.c001.demo.local",
        "protocol": "tcp",
        "port": 443,
        "vip_ip": {"address": "100.66.96.1/32"},
        "pool_members": [
            {"typename": "DcimVirtualDevice", "name": "vm-1", "primary_address": {"address": "10.1.1.5/24"}}
        ],
    }
    base.update(overrides)
    return base


def _context(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": "dc1-lb-PROD",
        "vlan_id": 3100,
        "vip_prefix": {"prefix": "100.66.96.0/26"},
        "capabilities": [
            {
                "typename": "DcimVirtualInterface",
                "device": {"name": "lb-1"},
                "ip_address": {"address": "100.66.64.5/29"},
            },
            {"typename": "DcimPhysicalInterface", "device": {"name": "bl-1"}},
        ],
        "vips": [_vip()],
    }
    base.update(overrides)
    return base


def _device(contexts: list[dict[str, Any]] | None = None, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": "lb-1",
        "status": "active",
        "platform": {"name": "f5_tmos"},
        "interfaces": [{"role": "uplink", "status": "active", "capabilities": []}],
        "capabilities": [
            {
                "typename": "ManagedLoadbalancerHA",
                "name": "ha-1",
                "capabilities": [{}, {}],
                "contexts": [_context()] if contexts is None else contexts,
            }
        ],
    }
    base.update(overrides)
    return base


class TestLoadBalancerCheck:
    def test_missing_device_emits_error(self) -> None:
        check = _check()
        check.validate({"DcimPhysicalDevice": {"edges": []}, "DcimVirtualDevice": {"edges": []}})
        assert check._captured_errors == ["Load balancer device not found"]

    def test_virtual_device_skips_appliance_readiness_but_validates_contexts(self) -> None:
        device = _device(status="planned", interfaces=[{"role": "uplink", "status": "planned"}])
        device["capabilities"][0]["contexts"] = [_context(vlan_id=None)]
        check = _check()
        check.validate({"DcimPhysicalDevice": [], "DcimVirtualDevice": [device]})
        assert check._captured_errors == ["Context 'dc1-lb-PROD' on 'lb-1' has no leg VLAN"]

    def test_missing_platform_status_uplink_and_ha_emit_errors(self) -> None:
        bad = _device(
            platform={},
            status="planned",
            interfaces=[{"role": "uplink", "status": "planned", "capabilities": []}],
            capabilities=[],
        )
        check = _check()
        check.validate({"DcimPhysicalDevice": [bad]})
        assert any("has no platform" in error for error in check._captured_errors)
        assert any("status is 'planned'" in error for error in check._captured_errors)
        assert any("none are active" in error for error in check._captured_errors)
        assert any("has no HA domain" in error for error in check._captured_errors)

    def test_ha_domain_with_single_member_emits_error(self) -> None:
        bad = _device(capabilities=[{"typename": "ManagedLoadbalancerHA", "name": "ha-1", "capabilities": [{}]}])
        check = _check()
        check.validate({"DcimPhysicalDevice": [bad]})
        assert any("fewer than 2 members" in error for error in check._captured_errors)

    def test_valid_device_has_no_errors(self) -> None:
        check = _check()
        check.validate({"DcimPhysicalDevice": [_device()]})
        assert check._captured_errors == []

    def test_device_without_contexts_has_no_errors(self) -> None:
        check = _check()
        check.validate({"DcimPhysicalDevice": [_device(contexts=[])]})
        assert check._captured_errors == []


class TestContextValidation:
    def test_complete_context_is_clean(self) -> None:
        assert validate_loadbalancer_contexts(_device()) == []

    def test_context_without_vip_is_clean(self) -> None:
        assert validate_loadbalancer_contexts(_device([_context(vips=[])])) == []

    def test_missing_vlan_vip_range_and_leg_are_reported(self) -> None:
        errors = validate_loadbalancer_contexts(
            _device([_context(vlan_id=None, vip_prefix=None, capabilities=[], vips=[])])
        )
        assert any("no leg VLAN" in error for error in errors)
        assert any("no VIP range" in error for error in errors)
        assert any("no leg sub-interface" in error for error in errors)

    def test_leg_of_another_member_does_not_count(self) -> None:
        other = [
            {
                "typename": "DcimVirtualInterface",
                "device": {"name": "lb-2"},
                "ip_address": {"address": "100.66.64.6/29"},
            }
        ]
        errors = validate_loadbalancer_contexts(_device([_context(capabilities=other)]))
        assert any("no leg sub-interface" in error for error in errors)

    def test_vip_outside_range_without_address_or_members_is_reported(self) -> None:
        errors = validate_loadbalancer_contexts(
            _device(
                [
                    _context(
                        vips=[
                            _vip(vip_ip={"address": "10.9.9.9/32"}),
                            _vip(port=80, vip_ip=None),
                            _vip(port=8080, pool_members=[]),
                        ]
                    )
                ]
            )
        )
        assert any("outside the VIP range" in error for error in errors)
        assert any("has no address" in error for error in errors)
        assert any("no pool members" in error for error in errors)
