"""CheckSegmentSecurity: a rule's ``destination_prefixes`` must be reachable (on-prem or behind an external session)."""

from __future__ import annotations

from typing import Any

import pytest

from tests.integration.demo_data import attribute_names, relationship_names
from tests.unit.test_circuit_query_shapes import _QUERIES, _selected_paths
from tests.unit.test_segment_security_check import _check, _rule, _segment, _v

_GW1 = "EQX-FR2-SDWAN-GW1"
_ACME = "198.18.0.0/24"


def _owner(org_id: str | None, typename: str = "OrganizationCustomer") -> dict[str, Any]:
    """An ``owner { node { __typename org_id display_label } }`` selection (None: no owner)."""
    if org_id is None:
        return {"node": None}
    return {"node": {"__typename": typename, "org_id": _v(org_id), "display_label": _v(f"{org_id} label")}}


def _prefix(prefix: str, ns_type: str = "prod") -> dict[str, Any]:
    """A registered IpamPrefix node with its namespace and namespace_type."""
    name = "PROD" if ns_type == "prod" else "NON-PROD"
    return {"prefix": _v(prefix), "ip_namespace": {"node": {"name": _v(name), "namespace_type": _v(ns_type)}}}


def _edges(*nodes: dict[str, Any]) -> dict[str, Any]:
    """A many-relationship selection of the given nodes."""
    return {"edges": [{"node": node} for node in nodes]}


def _dest_rule(rule_id: str, *cidrs: str, src: str | None = "src", **extra: Any) -> dict[str, Any]:
    """A rule from segment ``src`` with the given destination prefixes (id and prefix, as the query selects)."""
    nodes = [{"id": f"pfx-{cidr}", "prefix": _v(cidr)} for cidr in cidrs]
    return _rule(rule_id, src, None, destination_prefixes=_edges(*nodes), **extra)


def _office(
    gateway: str | None, owner: str | None = "C001", *lans: dict[str, Any], hosted: tuple[dict[str, Any], ...] = ()
) -> dict[str, Any]:
    """A TopologyCustomerOffice as segment_security.gql selects it: gateway, LANs, hosted segment prefixes."""
    return {
        "owner": _owner(owner),
        "sdwan_gateway": {"node": {"name": _v(gateway)} if gateway else None},
        "lan_prefixes": _edges(*lans),
        "segment_deployments": _edges(
            *({"segment": {"node": {"owner": _owner(owner), "gateway": _gw(p)}}} for p in hosted)
        ),
    }


def _gw(prefix: dict[str, Any]) -> dict[str, Any]:
    """A segment gateway selection around an ip_prefix node."""
    return {"node": {"ip_prefix": {"node": prefix}}}


def _tunnel(
    owner: str | None, *prefixes: dict[str, Any], ends: tuple[str, ...] = (_GW1, "EXT-ACME-EDGE1")
) -> dict[str, Any]:
    """A TopologySessionCircuit as segment_security.gql selects it: no stored owner, the office of ``owner`` in its
    locations (and a second location that is no office)."""
    return {
        "locations": _edges({"owner": _owner(owner)}, {}),
        "registered_prefixes": _edges(*prefixes),
        "endpoints": _edges(*({"device": {"node": {"name": _v(device)}}} for device in ends)),
    }


def _run(
    rules: list[dict[str, Any]],
    segments: list[dict[str, Any]] | None = None,
    src_owner: str | None = None,
    **roots: list[Any],
) -> Any:
    """Validate a payload: the source segment ``src`` (PROD, owned by ``src_owner``) plus ``segments``, the rules and
    the registration roots."""
    check = _check()
    source = _segment("src", name="src", owner=_owner(src_owner))
    all_segments = [source, *(segments or [])]
    check.validate(
        {
            "ManagedNetworkSegment": {"edges": all_segments},
            "SecurityPolicyRule": {"edges": rules},
            **{root: _edges(*nodes) for root, nodes in roots.items()},
        }
    )
    return check


def _unreachable(check: Any) -> list[str]:
    """The errors this rule raised."""
    return [message for message in check._captured_errors if "is not reachable" in message]


class TestUnreachableDestinations:
    """A destination nothing registers behind a session is an error."""

    def test_nothing_registers_the_destination(self) -> None:
        """The Azure VNet range of the policy: no segment, VPC, office or tunnel registers it."""
        check = _run([_dest_rule("nordix-prod-to-aws", "10.40.0.0/16")])
        assert _unreachable(check) == [
            "destination 10.40.0.0/16 of rule nordix-prod-to-aws is not reachable: "
            "no session accepts it (not on-prem space, not registered behind an external session)"
        ]

    def test_every_unreachable_entry_is_reported(self) -> None:
        """One error per entry, the reachable one stays quiet."""
        check = _run(
            [_dest_rule("r", "10.40.0.0/16", _ACME, "203.0.113.0/24")],
            TopologyCustomerOffice=[_office(_GW1, "C001", _prefix(_ACME))],
        )
        assert [m.split()[1] for m in _unreachable(check)] == ["10.40.0.0/16", "203.0.113.0/24"]

    def test_a_destination_wider_than_the_registered_prefix_is_unreachable(self) -> None:
        """Only space inside a registered prefix is reachable: a supernet reaches beyond it."""
        check = _run([_dest_rule("r", "198.18.0.0/16")], TopologyCustomerOffice=[_office(_GW1, "C001", _prefix(_ACME))])
        assert len(_unreachable(check)) == 1

    def test_a_destination_inside_the_registered_prefix_is_reachable(self) -> None:
        """A host route or a subnet of a registered prefix passes."""
        check = _run(
            [_dest_rule("r", "198.18.0.8/29", "198.18.0.1/32")],
            TopologyCustomerOffice=[_office(_GW1, "C001", _prefix(_ACME))],
        )
        assert _unreachable(check) == []

    def test_an_invalid_prefix_is_not_this_checks_business(self) -> None:
        """A malformed value is the schema's to refuse; the check does not crash on it."""
        assert _unreachable(_run([_dest_rule("r", "not-a-prefix")])) == []

    def test_a_rule_without_destination_prefixes_is_not_checked(self) -> None:
        """A destination_segment or destination_zone rule (a tenant segment, the internet) has no prefix to reach."""
        rule = _rule("r", "src", "other")
        assert _unreachable(_run([rule], [_segment("other", prefix="10.9.0.0/24")])) == []

    @pytest.mark.parametrize(
        "extra",
        [
            {"disabled": _v(True)},
            {"policy": {"node": {"id": "pol", "name": _v("pol"), "enabled": _v(False)}}},
        ],
        ids=["rule-disabled", "policy-disabled"],
    )
    def test_a_disabled_rule_is_not_checked(self, extra: dict[str, Any]) -> None:
        """Nothing is rendered for a disabled rule or a disabled policy, so nothing has to be reachable."""
        assert _unreachable(_run([_dest_rule("r", "10.40.0.0/16", **extra)])) == []


class TestOnPremDestinations:
    """On-prem space: inside the gateway prefix of a segment."""

    def test_a_destination_in_a_segment_prefix_is_reachable(self) -> None:
        """Another tenant segment's range."""
        check = _run([_dest_rule("r", "10.9.0.0/25")], [_segment("other", prefix="10.9.0.0/24")])
        assert _unreachable(check) == []

    def test_a_segment_of_another_environment_is_not_reachable(self) -> None:
        """A PROD rule never reaches a NON-PROD segment's range."""
        segment = _segment("dev", env="d", zone="NONPROD-ZONE", prefix="10.9.0.0/24")
        assert len(_unreachable(_run([_dest_rule("r", "10.9.0.0/24")], [segment]))) == 1


class TestRegisteredDestinations:
    """Registered behind an external session: an office LAN or hosted segment, a tunnel's range."""

    def test_an_office_lan_behind_a_gateway_is_reachable(self) -> None:
        """An office's lan_prefixes entry."""
        check = _run(
            [_dest_rule("r", "10.210.2.0/24")], TopologyCustomerOffice=[_office(_GW1, "C002", _prefix("10.210.2.0/24"))]
        )
        assert _unreachable(check) == []

    def test_an_office_lan_without_a_gateway_is_unreachable(self) -> None:
        """No sdwan_gateway: no SD-WAN session carries the LAN."""
        office = _office(None, "C002", _prefix("10.210.2.0/24"))
        assert len(_unreachable(_run([_dest_rule("r", "10.210.2.0/24")], TopologyCustomerOffice=[office]))) == 1

    def test_a_segment_hosted_by_an_office_is_reachable(self) -> None:
        """An office with a fabric registers the prefix of the segment it hosts."""
        office = _office(_GW1, "C005", hosted=(_prefix("10.176.5.0/24"),))
        assert _unreachable(_run([_dest_rule("r", "10.176.5.0/24")], TopologyCustomerOffice=[office])) == []

    def test_a_tunnel_range_behind_a_gateway_is_reachable(self) -> None:
        """The partner range C001-PARTNER-ACME-FR2 registers, the gateway having an office behind it."""
        check = _run(
            [_dest_rule("nordix-prod-to-partner-acme", _ACME)],
            TopologyCustomerOffice=[_office(_GW1, "C001")],
            TopologySessionCircuit=[_tunnel("C001", _prefix(_ACME))],
        )
        assert _unreachable(check) == []

    def test_a_tunnel_range_at_a_gateway_without_an_office_is_unreachable(self) -> None:
        """No office behind the gateway: no session faces it, the range is accepted nowhere."""
        check = _run([_dest_rule("r", _ACME)], TopologySessionCircuit=[_tunnel("C001", _prefix(_ACME))])
        assert len(_unreachable(check)) == 1

    def test_a_tunnel_range_of_a_provider_owned_tunnel_is_unreachable(self) -> None:
        """A tunnel without a customer owner registers nothing acceptable."""
        check = _run(
            [_dest_rule("r", _ACME)],
            TopologyCustomerOffice=[_office(_GW1, "C001")],
            TopologySessionCircuit=[_tunnel(None, _prefix(_ACME))],
        )
        assert len(_unreachable(check)) == 1

    def test_the_namespace_of_the_rules_own_prefix_object_does_not_matter(self) -> None:
        """The rule names 198.18.0.0/24 as a `default` namespace prefix; the registration is the PROD one."""
        check = _run(
            [_dest_rule("r", _ACME)],
            TopologyCustomerOffice=[_office(_GW1, "C001")],
            TopologySessionCircuit=[_tunnel("C001", _prefix(_ACME))],
        )
        assert _unreachable(check) == []


class TestOwnerOfTheDestination:
    """A registered destination belongs to the owner of the rule's source segment."""

    @staticmethod
    def _owned(check: Any) -> list[str]:
        """The errors that say the destination is registered by another owner."""
        return [m for m in check._captured_errors if "not by the owner of its source segment" in m]

    def test_a_destination_of_the_same_owner_passes(self) -> None:
        """C001's segment to C001's partner range."""
        check = _run(
            [_dest_rule("r", _ACME)],
            src_owner="C001",
            TopologyCustomerOffice=[_office(_GW1, "C001")],
            TopologySessionCircuit=[_tunnel("C001", _prefix(_ACME))],
        )
        assert check._captured_errors == []

    def test_a_destination_of_another_owner_through_a_tunnel_fails(self) -> None:
        """A partner range registered by C001's tunnel is not reachable from C015's segment."""
        check = _run(
            [_dest_rule("r", _ACME)],
            src_owner="C015",
            TopologyCustomerOffice=[_office(_GW1, "C001")],
            TopologySessionCircuit=[_tunnel("C001", _prefix(_ACME))],
        )
        assert len(self._owned(check)) == 1

    def test_on_prem_space_is_not_compared(self) -> None:
        """Another owner's on-prem segment range is reachable through the fabric, whoever the source is."""
        other = _segment("other", prefix="10.9.0.0/24", owner=_owner("C015"))
        check = _run([_dest_rule("r", "10.9.0.0/25")], [other], src_owner="C001")
        assert check._captured_errors == []

    def test_a_source_without_an_owner_is_not_compared(self) -> None:
        """No customer owner on the source segment: nothing to compare with."""
        check = _run(
            [_dest_rule("r", _ACME)],
            TopologyCustomerOffice=[_office(_GW1, "C001")],
            TopologySessionCircuit=[_tunnel("C001", _prefix(_ACME))],
        )
        assert check._captured_errors == []


class TestEnvironmentOfTheSource:
    """A rule reaches the registrations of its source segment's environment, never the other one."""

    def test_a_prod_rule_does_not_reach_a_non_prod_registration(self) -> None:
        """The partner range registered only in NON-PROD is unreachable from a PROD segment."""
        check = _run(
            [_dest_rule("r", _ACME)],
            TopologyCustomerOffice=[_office(_GW1, "C001")],
            TopologySessionCircuit=[_tunnel("C001", _prefix(_ACME, "non_prod"))],
        )
        assert len(_unreachable(check)) == 1

    def test_a_non_prod_rule_does_not_reach_a_prod_registration(self) -> None:
        """And the other way round."""
        dev = _segment("dev", env="d", zone="NONPROD-ZONE", prefix="10.8.0.0/24")
        check = _run(
            [_dest_rule("r", _ACME, src="dev")],
            [dev],
            TopologyCustomerOffice=[_office(_GW1, "C001")],
            TopologySessionCircuit=[_tunnel("C001", _prefix(_ACME))],
        )
        assert len(_unreachable(check)) == 1

    def test_a_rule_without_a_source_segment_reaches_any_environment(self) -> None:
        """No source segment names no environment: any registered namespace type counts."""
        check = _run(
            [_dest_rule("r", _ACME, src=None)],
            TopologyCustomerOffice=[_office(_GW1, "C001")],
            TopologySessionCircuit=[_tunnel("C001", _prefix(_ACME, "non_prod"))],
        )
        assert _unreachable(check) == []


def test_the_query_selects_what_the_destination_rule_reads() -> None:
    """Rule prefixes, segment environment and the registrations of the edge, on real schema fields."""
    path = _QUERIES / "validation" / "segment_security.gql"
    reads = {
        "SecurityPolicyRule": ["destination_prefixes.prefix", "source_segment.id", "disabled", "policy.enabled"],
        "TopologyCustomerOffice": [
            "owner.org_id",
            "sdwan_gateway.name",
            "lan_prefixes.prefix",
            "lan_prefixes.ip_namespace.namespace_type",
            "segment_deployments.segment.owner.org_id",
            "segment_deployments.segment.gateway.ip_prefix.prefix",
            "segment_deployments.segment.gateway.ip_prefix.ip_namespace.namespace_type",
        ],
        "TopologySessionCircuit": [
            "locations.owner.org_id",
            "registered_prefixes.prefix",
            "registered_prefixes.ip_namespace.namespace_type",
            "endpoints.device.name",
        ],
    }
    # The segment root spreads a fragment from another file (not resolvable here): environment is selected beside it.
    assert "        ...SegmentGroupPolicyFields\n        environment { value }" in path.read_text()
    assert (
        "        owner { node { __typename org_id { value } display_label } }\n        security_zone"
        in path.read_text()
    )
    assert "environment" in attribute_names("ManagedNetworkSegment")
    assert "owner" in relationship_names("ManagedNetworkSegment")
    for root, paths in reads.items():
        selected = _selected_paths(path, root)
        assert not [item for item in paths if item not in selected], root
    assert {"sdwan_gateway", "lan_prefixes", "segment_deployments"} <= relationship_names("TopologyCustomerOffice")
    assert {"registered_prefixes", "endpoints", "locations"} <= relationship_names("TopologySessionCircuit")
