"""Unit tests for checks/sdwan.py."""

from __future__ import annotations

from typing import Any

from checks.sdwan import validate_managed_devices_active


def _device(name: str, status: str = "active") -> dict[str, Any]:
    return {"name": name, "status": status}


class TestValidateManagedDevicesActive:
    def test_no_managed_devices_is_an_error(self) -> None:
        errors = validate_managed_devices_active({"managed_devices": []})
        assert len(errors) == 1
        assert "no managed_devices" in errors[0]

    def test_missing_managed_devices_key_is_an_error(self) -> None:
        errors = validate_managed_devices_active({})
        assert len(errors) == 1

    def test_all_active_is_no_errors(self) -> None:
        errors = validate_managed_devices_active({"managed_devices": [_device("GW1"), _device("EDGE1")]})
        assert errors == []

    def test_inactive_device_is_an_error(self) -> None:
        errors = validate_managed_devices_active(
            {"managed_devices": [_device("GW1"), _device("EDGE1", status="provisioning")]}
        )
        assert len(errors) == 1
        assert "EDGE1" in errors[0]
        assert "provisioning" in errors[0]

    def test_multiple_inactive_devices_each_get_their_own_error(self) -> None:
        errors = validate_managed_devices_active(
            {
                "managed_devices": [
                    _device("GW1", status="decommissioned"),
                    _device("EDGE1", status="provisioning"),
                ]
            }
        )
        assert len(errors) == 2
