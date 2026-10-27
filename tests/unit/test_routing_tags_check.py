"""Unit tests for CheckRoutingTags, fed canned routing_tags query responses in the raw GraphQL shape."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from checks.routing_tags import CheckRoutingTags


def _v(value: Any) -> dict[str, Any]:
    return {"value": value}


def _customer(cid: str, org_id: str, tag: int | None) -> dict[str, Any]:
    """One OrganizationCustomer edge."""
    return {"node": {"id": cid, "org_id": _v(org_id), "routing_tag": _v(tag)}}


def _segment(
    name: str,
    tag: int | None,
    *,
    owner: str = "c-1",
    kind: str = "ManagedVxlanSegment",
    namespace_type: str | None = "prod",
    zone: str = "PROD-ZONE",
) -> dict[str, Any]:
    """One ManagedNetworkSegment edge (a routed PROD tenant segment by default)."""
    gateway: dict[str, Any] = {"node": None}
    if namespace_type:
        namespace = {"node": {"name": _v("NS"), "namespace_type": _v(namespace_type)}}
        gateway = {"node": {"ip_prefix": {"node": {"ip_namespace": namespace}}}}
    return {
        "node": {
            "__typename": kind,
            "id": f"seg-{name}",
            "name": _v(name),
            "routing_tag": _v(tag),
            "owner": {"node": {"id": owner}},
            "security_zone": {"node": {"name": _v(zone)}},
            "gateway": gateway,
        }
    }


def _run(customers: list[dict[str, Any]], segments: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    """Run the check against canned data; return (errors, infos)."""
    check = cast(Any, CheckRoutingTags.__new__(CheckRoutingTags))
    errors: list[str] = []
    infos: list[str] = []
    check.log_error = lambda message: errors.append(message)
    check.log_info = lambda message: infos.append(message)
    check.validate(
        {"OrganizationCustomer": {"edges": customers}, "ManagedNetworkSegment": {"edges": segments}},
    )
    return errors, infos


class TestComplete:
    """What the check accepts, and the community it reports."""

    def test_tagged_customer_and_segment_pass_and_report_their_communities(self) -> None:
        errors, infos = _run([_customer("c-1", "C005", 12)], [_segment("c005-web-p", 5)])

        assert errors == []
        assert infos == [
            "ManagedVxlanSegment 'c005-web-p' renders 64497:5",
            "OrganizationCustomer 'C005' renders 64496:12",
        ]

    def test_customer_without_segments_needs_no_tag(self) -> None:
        errors, _ = _run([_customer("c-1", "C005", None)], [])

        assert errors == []

    @pytest.mark.parametrize(
        ("kind", "namespace_type", "zone"),
        [
            pytest.param("CloudNetworkSegment", "prod", "PROD-ZONE", id="cloud"),
            pytest.param("ManagedVxlanSegment", "management", "PROD-ZONE", id="management-namespace"),
            pytest.param("ManagedVxlanSegment", "prod", "MANAGEMENT-ZONE", id="exchange-zone"),
            pytest.param("ManagedVxlanSegment", None, "PROD-ZONE", id="no-gateway"),
        ],
    )
    def test_segment_that_is_not_a_routed_tenant_segment_needs_no_tag_and_neither_does_its_owner(
        self, kind: str, namespace_type: str | None, zone: str
    ) -> None:
        segment = _segment("s", None, kind=kind, namespace_type=namespace_type, zone=zone)

        errors, _ = _run([_customer("c-1", "C005", None)], [segment])

        assert errors == []


class TestViolations:
    """Each rule fails with an error naming the kind, the object and the rule."""

    def test_routed_segment_without_tag(self) -> None:
        errors, _ = _run([_customer("c-1", "C005", 12)], [_segment("c005-web-p", None)])

        assert errors == [
            "ManagedVxlanSegment 'c005-web-p': routed tenant segment has no routing_tag "
            "(rule: every routed segment is tagged)"
        ]

    def test_customer_owning_a_routed_segment_without_tag(self) -> None:
        errors, _ = _run([_customer("c-1", "C005", None)], [_segment("c005-web-p", 5), _segment("c005-db-p", 6)])

        assert errors == [
            "OrganizationCustomer 'C005': owns routed segment(s) c005-db-p, c005-web-p but has no routing_tag "
            "(rule: every customer owning a routed segment is tagged)"
        ]

    def test_only_the_owner_of_the_untagged_segment_is_reported(self) -> None:
        customers = [_customer("c-1", "C005", None), _customer("c-2", "C006", None)]

        errors, _ = _run(customers, [_segment("c006-web-p", 5, owner="c-2")])

        assert len(errors) == 1
        assert errors[0].startswith("OrganizationCustomer 'C006'")

    def test_duplicate_customer_tag(self) -> None:
        errors, _ = _run([_customer("c-1", "C005", 12), _customer("c-2", "C006", 12)], [])

        assert errors == [
            "routing_tag 12 is on 2 customers: OrganizationCustomer 'C005', OrganizationCustomer 'C006' "
            "(rule: a customer routing tag is unique)"
        ]

    def test_duplicate_segment_tag(self) -> None:
        segments = [_segment("a", 5), _segment("b", 5)]

        errors, _ = _run([], segments)

        assert len(errors) == 1
        assert "routing_tag 5 is on 2 segments" in errors[0]
        assert "(rule: a segment routing tag is unique)" in errors[0]

    def test_same_number_on_a_customer_and_a_segment_is_fine(self) -> None:
        """The two kinds have their own pools and their own community ASN."""
        errors, _ = _run([_customer("c-1", "C005", 5)], [_segment("a", 5)])

        assert errors == []


def test_query_selects_what_the_check_reads(root_dir: Path) -> None:
    """The routed-segment test (kind, namespace type, zone), owner and both tags come from the query."""
    query = (root_dir / "queries/validation/routing_tags.gql").read_text()
    for needed in (
        "OrganizationCustomer",
        "ManagedNetworkSegment",
        "routing_tag { value }",
        "owner { node { id } }",
        "namespace_type { value }",
        "security_zone",
        "__typename",
    ):
        assert needed in query
