"""CheckRoutingInvariants rule 9: the routed prefixes of two on-prem segments never overlap in one namespace."""

from __future__ import annotations

from typing import Any

from tests.unit.routing_invariants_helpers import _onprem, _run


def _errors(check: Any) -> list[str]:
    """The captured errors of the segment overlap rule."""
    return [message for message in check._captured_errors if "overlaps segment" in message]


def test_two_segments_with_disjoint_prefixes_pass() -> None:
    check = _run(ManagedNetworkSegment=[_onprem("a", "10.1.1.0/24", pid="p1"), _onprem("b", "10.1.2.0/24", pid="p2")])
    assert _errors(check) == []


def test_a_segment_inside_the_prefix_of_another_is_an_error() -> None:
    """Containment is an overlap: both cannot be routed in one VRF without translation."""
    check = _run(ManagedNetworkSegment=[_onprem("a", "10.1.0.0/16", pid="p1"), _onprem("b", "10.1.2.0/24", pid="p2")])
    errors = _errors(check)
    assert len(errors) == 1 and "'a'" in errors[0] and "'b'" in errors[0]


def test_the_same_prefix_in_two_namespaces_passes() -> None:
    """PROD and NON-PROD never meet: the same range may exist in each."""
    check = _run(
        ManagedNetworkSegment=[
            _onprem("a", "10.1.1.0/24", pid="p1"),
            _onprem("b", "10.1.1.0/24", ns_name="NON-PROD", ns_type="non_prod", pid="p2"),
        ]
    )
    assert _errors(check) == []


def test_a_container_prefix_may_contain_a_segment() -> None:
    """The pool a segment's /24 is allocated from is a container (member_type prefix), not a routed subnet."""
    check = _run(
        ManagedNetworkSegment=[
            _onprem("pool", "10.1.0.0/16", member_type="prefix", pid="p1"),
            _onprem("b", "10.1.2.0/24", pid="p2"),
        ]
    )
    assert _errors(check) == []


def test_a_cloud_segment_is_not_an_on_prem_prefix() -> None:
    check = _run(ManagedNetworkSegment=[_cloud("c", "10.1.1.0/24"), _onprem("b", "10.1.1.0/24", pid="p2")])
    assert _errors(check) == []


def _cloud(name: str, prefix: str) -> dict[str, Any]:
    """A cloud segment: its subnets are checked against their VPC, not as on-prem space."""
    segment = _onprem(name, prefix, pid="pc")
    segment["__typename"] = "CloudNetworkSegment"
    return segment
