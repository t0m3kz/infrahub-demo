"""Rule 8 of CheckRoutingInvariants: one owner and one attachment point per registered prefix."""

from __future__ import annotations

from typing import Any

from tests.unit.routing_invariants_helpers import (
    _C002,
    _C005,
    _C015,
    _CUSTOMER,
    _PROVIDER,
    _block,
    _gateway_office,
    _rule_eight,
    _run,
    _segment,
)


class TestOwnershipConflicts:
    """Rule 8: no prefix of two owners, no owner's prefix behind two attachment points."""

    def _demo(self, **extra: Any) -> dict[str, Any]:
        """The 30_all shape: the C002, C003 and C005 offices behind GW1 and the tenants' on-prem segments."""
        sections: dict[str, Any] = {
            "ManagedNetworkSegment": [
                _segment("c003-web", "10.5.10.0/24", _CUSTOMER),
                _segment("c015-web", "10.7.10.0/24", _C015),
                _segment("c005-lan", "10.176.5.0/24", _C005),
            ],
            "TopologyCustomerOffice": [
                _gateway_office("C002-OFFICE", "GW1", _C002, _block("10.210.2.0/24")),
                _gateway_office("C003-OFFICE", "GW1", _CUSTOMER, _block("10.210.3.0/24")),
                _gateway_office("C005-OFFICE", "GW1", _C005, hosted=(("10.176.5.0/24", _C005),)),
            ],
        }
        sections.update(extra)
        return sections

    def test_the_demo_shape_passes(self) -> None:
        """Every prefix has one owner and one attachment point; an office-hosted segment is its owner's own."""
        assert _rule_eight(_run(**self._demo())) == []

    def test_a_prefix_of_two_owners_fails(self) -> None:
        """C003's office LAN equals C015's segment prefix: it would be announced to the other owner's session."""
        check = _run(
            **self._demo(
                TopologyCustomerOffice=[_gateway_office("C003-OFFICE", "GW1", _CUSTOMER, _block("10.7.10.0/24"))]
            )
        )
        errors = _rule_eight(check)
        assert len(errors) == 1
        assert "Namespace 'PROD'" in errors[0]
        assert "10.7.10.0/24" in errors[0]
        assert "C003, C015" in errors[0]
        assert "behind GW1" in errors[0]
        assert "another owner's session" in errors[0]

    def test_a_segment_prefix_of_another_owner_fails(self) -> None:
        """An on-prem segment of C003 on the same prefix as C002's office LAN."""
        check = _run(**self._demo(ManagedNetworkSegment=[_segment("c003-web", "10.210.2.0/24", _CUSTOMER)]))
        errors = _rule_eight(check)
        assert len(errors) == 1
        assert "registered by more than one owner" in errors[0]

    def test_an_owners_lan_behind_two_gateways_fails(self) -> None:
        """The same LAN registered by two offices of C003 behind GW1 and GW2."""
        offices = [
            _gateway_office("C003-A", "GW1", _CUSTOMER, _block("10.210.3.0/24")),
            _gateway_office("C003-B", "GW2", _CUSTOMER, _block("10.210.3.0/24")),
        ]
        errors = _rule_eight(_run(TopologyCustomerOffice=offices))
        assert len(errors) == 1
        assert "GW1, GW2" in errors[0]
        assert "exactly one session" in errors[0]

    def test_an_offices_lan_behind_its_own_two_gateways_is_no_conflict(self) -> None:
        """Redundant gateways of ONE office are one attachment point: its LAN sits behind each of them."""
        offices = [_gateway_office("C003-A", ("GW1", "GW2"), _CUSTOMER, _block("10.210.3.0/24"))]
        assert _rule_eight(_run(TopologyCustomerOffice=offices)) == []

    def test_two_owners_on_one_prefix_behind_redundant_gateways_still_fail(self) -> None:
        """Two offices of different owners register one LAN, each behind its own two gateways: a shared prefix."""
        offices = [
            _gateway_office("C003-A", ("GW1", "GW2"), _CUSTOMER, _block("10.210.3.0/24")),
            _gateway_office("C002-A", ("GW1", "GW2"), _C002, _block("10.210.3.0/24")),
        ]
        errors = _rule_eight(_run(TopologyCustomerOffice=offices))
        assert len(errors) == 1
        assert "registered by more than one owner" in errors[0]

    def test_an_owners_lan_behind_two_different_gateway_sets_fails(self) -> None:
        """The same LAN registered behind (GW1, GW2) by one office and behind GW1 alone by another is two points."""
        offices = [
            _gateway_office("C003-A", ("GW1", "GW2"), _CUSTOMER, _block("10.210.3.0/24")),
            _gateway_office("C003-B", "GW1", _CUSTOMER, _block("10.210.3.0/24")),
        ]
        errors = _rule_eight(_run(TopologyCustomerOffice=offices))
        assert len(errors) == 1
        assert "exactly one session" in errors[0]

    def test_two_offices_behind_one_gateway_are_one_attachment_point(self) -> None:
        """One owner's two offices behind GW1 each with its own LAN: no conflict."""
        offices = [
            _gateway_office("C003-A", "GW1", _CUSTOMER, _block("10.210.3.0/24")),
            _gateway_office("C003-B", "GW1", _CUSTOMER, _block("10.210.4.0/24")),
        ]
        assert _rule_eight(_run(TopologyCustomerOffice=offices)) == []

    def test_the_same_prefix_in_two_namespaces_does_not_conflict(self) -> None:
        """PROD and NONPROD are separate routing domains: the rule runs per namespace."""
        check = _run(
            ManagedNetworkSegment=[_segment("dev", "10.32.0.0/16", _CUSTOMER, ns_name="NONPROD", ns_type="non_prod")],
            TopologyCustomerOffice=[_gateway_office("C015-OFFICE", "GW1", _C015, _block("10.32.0.0/16"))],
        )
        assert _rule_eight(check) == []

    def test_a_non_customer_registration_is_not_judged_here(self) -> None:
        """A provider-owned office registers nothing at the border (rule 7 reports it); rule 8 stays silent."""
        check = _run(
            TopologyCustomerOffice=[_gateway_office("P-OFFICE", "GW1", _PROVIDER, _block("10.30.0.0/16"))],
            ManagedNetworkSegment=[_segment("c003-web", "10.30.0.0/16", _CUSTOMER)],
        )
        assert _rule_eight(check) == []

    def test_an_office_without_a_gateway_is_not_an_attachment_point(self) -> None:
        """No sdwan_gateways: nothing is announced through it, so it cannot conflict."""
        check = _run(
            TopologyCustomerOffice=[_gateway_office("C003-OFFICE", None, _CUSTOMER, _block("10.32.0.0/16"))],
            ManagedNetworkSegment=[_segment("c015-web", "10.32.0.0/16", _C015)],
        )
        assert _rule_eight(check) == []

    def test_an_empty_payload_passes(self) -> None:
        """Nothing registered: nothing to report."""
        assert _rule_eight(_run()) == []
