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
                    "owner": "cust-1",
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
                    "owner": "cust-1",
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
                    "owner": "cust-1",
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
                    "owner": "cust-1",
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


class TestInlineTermination:
    """terminate_inline needs an on-prem HA pair: the fabric drops the segment's gateway."""

    @staticmethod
    def _vlan_segment(inline_service: dict[str, Any] | None, terminate_inline: bool = True) -> dict[str, Any]:
        return {
            "ManagedVlanSegment": [
                {
                    "name": "seg-web",
                    "owner": "cust-1",
                    "customer_deployment": {"typename": "TopologyCustomerDC", "name": "C001-P-DC1"},
                    "terminate_inline": terminate_inline,
                    "inline_service": inline_service,
                }
            ]
        }

    def test_inline_without_service_is_an_error(self) -> None:
        """No inline_service means no gateway anywhere."""
        check = _check()

        check.validate(self._vlan_segment(None))

        assert len(check._captured_errors) == 1
        assert "no inline_service" in check._captured_errors[0]

    def test_inline_service_must_be_an_ha_pair(self) -> None:
        """A firewall context (or anything but an FW/LB/proxy HA pair) cannot be the gateway."""
        check = _check()

        check.validate(self._vlan_segment({"typename": "ManagedFirewallContext", "name": "ctx-1"}))

        assert len(check._captured_errors) == 1
        assert "cannot be its gateway" in check._captured_errors[0]

    def test_firewall_ha_pair_is_accepted(self) -> None:
        """A ManagedFirewallHA terminating the segment passes; so does a segment not terminated inline."""
        check = _check()

        check.validate(self._vlan_segment({"typename": "ManagedFirewallHA", "name": "fw-ha"}))
        check.validate(self._vlan_segment(None, terminate_inline=False))

        assert check._captured_errors == []


class TestVlanSegmentDeployment:
    def test_vlan_segment_without_deployment_is_an_error(self) -> None:
        """Every segment needs its deployment — without one it terminates nowhere."""
        check = _check()

        check.validate({"ManagedVlanSegment": [{"name": "seg-orphan", "owner": "cust-1", "customer_deployment": None}]})

        assert len(check._captured_errors) == 1
        assert "has no customer_deployment" in check._captured_errors[0]


class TestOwner:
    def test_customer_segment_without_owner_is_an_error(self) -> None:
        """The schema leaves owner optional (for ManagedExternalSegment), so the check holds customer segments to it."""
        check = _check()
        deployment = {"typename": "TopologyCustomerDC", "name": "C001-P-DC1"}

        check.validate({"ManagedVlanSegment": [{"name": "seg-web", "owner": None, "customer_deployment": deployment}]})

        assert len(check._captured_errors) == 1
        assert "has no owner" in check._captured_errors[0]
