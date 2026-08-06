"""Unit tests for CheckSegmentPlacement."""

from __future__ import annotations

from typing import Any, cast

from checks.segment_placement import CheckSegmentPlacement


def _check() -> Any:
    check = cast(Any, CheckSegmentPlacement.__new__(CheckSegmentPlacement))
    errors: list[str] = []
    check._captured_errors = errors
    check.log_error = lambda message: errors.append(message)
    return check


class TestSegmentPlacementCheck:
    def test_vxlan_rejects_cloud_deployment(self) -> None:
        """VXLAN segment attached to cloud deployment should fail compatibility check."""
        check = _check()
        payload = {
            "ManagedVxlanSegment": [
                {
                    "name": "seg-vx-1",
                    "stretch_scope": "local",
                    "customer_deployments": [
                        {
                            "typename": "TopologyCustomerCloud",
                            "name": "C001-P-AWS",
                            "parent": {"typename": "TopologyCloudRegion", "name": "eu-central-1"},
                        }
                    ],
                    "segment_deployments": [],
                }
            ]
        }

        check.validate(payload)

        assert len(check._captured_errors) >= 1
        assert "unsupported deployment type" in check._captured_errors[0]

    def test_vxlan_local_requires_exactly_one_deployment(self) -> None:
        """stretch_scope=local must have exactly one customer deployment."""
        check = _check()
        payload = {
            "ManagedVxlanSegment": [
                {
                    "name": "seg-vx-2",
                    "stretch_scope": "local",
                    "customer_deployments": [
                        {
                            "typename": "TopologyCustomerDC",
                            "name": "C001-P-DC11",
                            "parent": {"typename": "TopologyDataCenter", "name": "DC11"},
                        },
                        {
                            "typename": "TopologyCustomerDC",
                            "name": "C001-P-DC12",
                            "parent": {"typename": "TopologyDataCenter", "name": "DC12"},
                        },
                    ],
                    "segment_deployments": [],
                }
            ]
        }

        check.validate(payload)

        assert any("stretch_scope=local" in msg for msg in check._captured_errors)

    def test_cloud_segment_must_attach_to_customer_cloud(self) -> None:
        """CloudNetworkSegment cannot attach to non-cloud customer deployments."""
        check = _check()
        payload = {
            "CloudNetworkSegment": [
                {
                    "name": "seg-cloud-1",
                    "customer_deployment": {"typename": "TopologyCustomerDC", "name": "C001-P-DC11"},
                }
            ]
        }

        check.validate(payload)

        assert len(check._captured_errors) == 1
        assert "must attach only to TopologyCustomerCloud" in check._captured_errors[0]

    def test_stretched_vxlan_without_global_intent_is_allowed(self) -> None:
        """Stretched scope can rely on SegmentDeployment VNI convergence without global hints."""
        check = _check()
        payload = {
            "ManagedVxlanSegment": [
                {
                    "name": "seg-vx-4",
                    "stretch_scope": "global",
                    "customer_deployments": [
                        {
                            "typename": "TopologyCustomerDC",
                            "name": "C001-P-DC11",
                            "parent": {"typename": "TopologyDataCenter", "name": "DC11"},
                        },
                        {
                            "typename": "TopologyCustomerDC",
                            "name": "C001-P-DC12",
                            "parent": {"typename": "TopologyDataCenter", "name": "DC12"},
                        },
                    ],
                    "segment_deployments": [],
                }
            ]
        }

        check.validate(payload)

        assert check._captured_errors == []
