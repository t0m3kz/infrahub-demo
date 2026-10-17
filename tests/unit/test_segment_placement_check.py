"""Unit tests for CheckSegmentPlacement."""

from __future__ import annotations

from pathlib import Path
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


class TestOfficeSegments:
    """An office hosts its own VXLAN segments (it is TopologySegmentHosting), locally only."""

    @staticmethod
    def _segment(stretch_scope: str, deployments: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "ManagedVxlanSegment": [
                {
                    "name": "seg-office",
                    "stretch_scope": stretch_scope,
                    "customer_deployments": deployments,
                    "segment_deployments": [],
                }
            ]
        }

    # The vxlan_segment query selects no parent on an office (the OfficeCustomer hosts nothing), so the
    # parent kind check, which would call TopologyOfficeCustomer "not TopologySegmentHosting", is skipped.
    _OFFICE = {"typename": "TopologyCustomerOffice", "name": "C005-P"}

    def test_local_vxlan_segment_on_an_office_passes(self) -> None:
        check = _check()

        check.validate(self._segment("local", [self._OFFICE]))

        assert check._captured_errors == []

    def test_office_parent_is_not_checked_for_segment_hosting(self) -> None:
        """Even if a parent came back, the office is the hosting node: its parent is not judged."""
        check = _check()
        office = {**self._OFFICE, "parent": {"typename": "TopologyOfficeCustomer", "name": "C005"}}

        check.validate(self._segment("local", [office]))

        assert check._captured_errors == []

    def test_stretching_a_segment_onto_an_office_is_an_error(self) -> None:
        """An office has no border gateway to stretch a segment through."""
        check = _check()
        dc = {
            "typename": "TopologyCustomerDC",
            "name": "C005-P-DC10",
            "parent": {"typename": "TopologyDataCenter", "name": "DC10"},
        }

        check.validate(self._segment("global", [dc, self._OFFICE]))

        assert len(check._captured_errors) == 1
        assert "must be local" in check._captured_errors[0]
        assert "C005-P" in check._captured_errors[0]

    def test_other_footprints_still_need_a_segment_hosting_parent(self) -> None:
        """The exemption is the office's own: a DC footprint under a non-hosting parent is still an error."""
        check = _check()
        dc = {
            "typename": "TopologyCustomerDC",
            "name": "C005-P-DC10",
            "parent": {"typename": "TopologyOfficeCustomer", "name": "C005"},
        }

        check.validate(self._segment("local", [dc]))

        assert any("not TopologySegmentHosting" in msg for msg in check._captured_errors)


class TestInlineTermination:
    """terminate_inline needs an on-prem HA pair: the fabric drops the segment's gateway."""

    @staticmethod
    def _vlan_segment(inline_service: dict[str, Any] | None, terminate_inline: bool = True) -> dict[str, Any]:
        return {
            "ManagedVlanSegment": [
                {
                    "name": "seg-web",
                    "customer_deployment": {"typename": "TopologyCustomerColocation", "name": "C001-P-FR"},
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

        check.validate({"ManagedVlanSegment": [{"name": "seg-orphan", "customer_deployment": None}]})

        assert len(check._captured_errors) == 1
        assert "has no customer_deployment" in check._captured_errors[0]

    def test_cloud_segment_without_customer_deployment_is_an_error(self) -> None:
        """A cloud segment attached to no deployment is an error, not silently skipped."""
        check = _check()

        check.validate({"CloudNetworkSegment": [{"name": "c003-subnet-p", "customer_deployment": None}]})

        assert len(check._captured_errors) == 1
        assert "has no customer_deployment" in check._captured_errors[0]

    def test_cloud_segment_on_cloud_deployment_passes(self) -> None:
        """A cloud segment on TopologyCustomerCloud raises nothing."""
        check = _check()
        deployment = {"typename": "TopologyCustomerCloud", "name": "C003-P"}

        check.validate({"CloudNetworkSegment": [{"name": "c003-subnet-p", "customer_deployment": deployment}]})

        assert check._captured_errors == []


class TestTenantVlanSegmentInDataCenter:
    """A tenant segment inside a DC is VXLAN; only an exchange-zone service segment may be a VLAN there."""

    @staticmethod
    def _vlan_segment(deployment_kind: str, zone: str | None) -> dict[str, Any]:
        """One VLAN segment on a footprint of ``deployment_kind``, in ``zone`` (None: unzoned)."""
        return {
            "ManagedVlanSegment": [
                {
                    "name": "c006-nordix-prod-p",
                    "customer_deployment": {"typename": deployment_kind, "name": "C006-P-DC11"},
                    "security_zone": {"name": zone} if zone else None,
                }
            ]
        }

    def test_tenant_vlan_segment_on_a_dc_footprint_is_an_error(self) -> None:
        """A PROD-ZONE VLAN segment on a TopologyCustomerDC is a tenant segment of the wrong kind."""
        check = _check()

        check.validate(self._vlan_segment("TopologyCustomerDC", "PROD-ZONE"))

        assert len(check._captured_errors) == 1
        assert "is a ManagedVxlanSegment" in check._captured_errors[0]
        assert "C006-P-DC11" in check._captured_errors[0]

    def test_unzoned_vlan_segment_on_a_dc_footprint_is_an_error(self) -> None:
        """No zone is no exemption: the rule fails closed."""
        check = _check()

        check.validate(self._vlan_segment("TopologyCustomerDC", None))

        assert len(check._captured_errors) == 1

    def test_exchange_zone_vlan_segment_on_a_dc_footprint_passes(self) -> None:
        """The DC-local MANAGEMENT services segment is authored infra data in an exchange zone."""
        check = _check()

        check.validate(self._vlan_segment("TopologyCustomerDC", "MANAGEMENT-ZONE"))

        assert check._captured_errors == []

    def test_tenant_vlan_segment_on_a_colocation_footprint_passes(self) -> None:
        """Cages and colocation metros keep their tenant VLAN segments."""
        check = _check()

        check.validate(self._vlan_segment("TopologyCustomerColocation", "PROD-ZONE"))

        assert check._captured_errors == []

    def test_query_selects_the_vlan_segment_zone(self) -> None:
        """The exemption reads security_zone.name, so the check's query must select it on VLAN segments."""
        query = (Path(__file__).parents[2] / "queries" / "validation" / "segment_placement.gql").read_text()
        vlan_block = query[query.index("ManagedVlanSegment") : query.index("ManagedVxlanSegment")]

        assert "security_zone { node { name { value } } }" in vlan_block
