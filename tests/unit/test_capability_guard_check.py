"""Unit tests for CheckCapabilityGuard."""

from __future__ import annotations

from typing import Any

from checks.capability_guard import CheckCapabilityGuard


def _check() -> Any:
    check = CheckCapabilityGuard.__new__(CheckCapabilityGuard)
    errors: list[str] = []
    check._captured_errors = errors
    check.log_error = lambda message: errors.append(message)
    return check


def _payload(devices: list[dict[str, Any]]) -> dict[str, Any]:
    return {"DcimDevice": {"edges": [{"node": device} for device in devices]}}


class TestCapabilityGuard:
    def test_no_devices_no_errors(self) -> None:
        """Empty payload should not emit findings."""
        check = _check()

        check.validate({"DcimDevice": {"edges": []}})

        assert check._captured_errors == []

    def test_non_overlay_role_is_skipped(self) -> None:
        """Spine devices should not be validated by overlay guard."""
        check = _check()
        payload = _payload(
            [
                {
                    "name": {"value": "dc1-sp1"},
                    "role": {"value": "spine"},
                    "device_capabilities": {
                        "edges": [{"node": {"name": {"value": "BGP-underlay"}}}],
                    },
                }
            ]
        )

        check.validate(payload)

        assert check._captured_errors == []

    def test_underlay_without_overlay_emits_error(self) -> None:
        """Leaf with only underlay capability must trigger finding."""
        check = _check()
        payload = _payload(
            [
                {
                    "name": {"value": "dc1-leaf1"},
                    "role": {"value": "leaf"},
                    "device_capabilities": {
                        "edges": [{"node": {"name": {"value": "BGP-underlay"}}}],
                    },
                }
            ]
        )

        check.validate(payload)

        assert len(check._captured_errors) == 1
        assert "dc1-leaf1" in check._captured_errors[0]

    def test_underlay_and_overlay_is_valid(self) -> None:
        """Leaf with both underlay and overlay should pass."""
        check = _check()
        payload = _payload(
            [
                {
                    "name": {"value": "dc1-leaf2"},
                    "role": {"value": "leaf"},
                    "device_capabilities": {
                        "edges": [
                            {"node": {"name": {"value": "BGP-underlay"}}},
                            {"node": {"name": {"value": "BGP-overlay"}}},
                        ],
                    },
                }
            ]
        )

        check.validate(payload)

        assert check._captured_errors == []

    def test_overlay_without_underlay_is_accepted(self) -> None:
        """Check enforces missing overlay only when underlay exists."""
        check = _check()
        payload = _payload(
            [
                {
                    "name": {"value": "dc1-access1"},
                    "role": {"value": "access-leaf"},
                    "device_capabilities": {
                        "edges": [{"node": {"name": {"value": "BGP-overlay"}}}],
                    },
                }
            ]
        )

        check.validate(payload)

        assert check._captured_errors == []
