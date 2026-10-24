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
            *(
                {"segment": {"node": {"status": _v("active"), "owner": _owner(owner), "gateway": _gw(p)}}}
                for p in hosted
            )
        ),
    }


def _gw(prefix: dict[str, Any]) -> dict[str, Any]:
    """A segment gateway selection around an ip_prefix node."""
    return {"node": {"ip_prefix": {"node": prefix}}}


def _tunnel(
    owner: str | None, *prefixes: dict[str, Any], ends: tuple[str, ...] = (_GW1, "EXT-ACME-EDGE1")
) -> dict[str, Any]:
    """A TopologyTunnelCircuit as segment_security.gql selects it."""
    return {
        "owner": _owner(owner),
        "registered_prefixes": _edges(*prefixes),
        "capabilities": _edges(*({"device": {"node": {"name": _v(device)}}} for device in ends)),
    }


def _vif(owner: str | None, *blocks: dict[str, Any], circuit: str | None = "HANDOFF-AWS") -> dict[str, Any]:
    """A private CloudVirtualInterface onto one VPC with its blocks, on a handoff circuit (None: no session)."""
    return {
        "name": _v("vif"),
        "virtual_circuit": {"node": {"name": _v(circuit)} if circuit else None},
        "virtual_network": {"node": {"cidr_blocks": _edges(*blocks), "account": {"node": {"owner": _owner(owner)}}}},
        "transit_gateway_attachment": {"node": None},
    }


def _run(rules: list[dict[str, Any]], segments: list[dict[str, Any]] | None = None, **roots: list[Any]) -> Any:
    """Validate a payload: the source segment ``src`` (PROD) plus ``segments``, the rules and the registration roots."""
    check = _check()
    all_segments = [_segment("src", name="src"), *(segments or [])]
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
        """A destination_segment rule (the internet segment, a tenant segment) has no prefix to reach."""
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

    def test_a_retired_segment_is_no_longer_served(self) -> None:
        """A retired segment's space is not on-prem any more."""
        segment = _segment("old", prefix="10.9.0.0/24", status=_v("retired"))
        assert len(_unreachable(_run([_dest_rule("r", "10.9.0.0/24")], [segment]))) == 1

    def test_a_segment_of_another_environment_is_not_reachable(self) -> None:
        """A PROD rule never reaches a NON-PROD segment's range."""
        segment = _segment("dev", env="d", zone="NONPROD-ZONE", prefix="10.9.0.0/24")
        assert len(_unreachable(_run([_dest_rule("r", "10.9.0.0/24")], [segment]))) == 1


class TestRegisteredDestinations:
    """Registered behind an external session: a VPC block, an office LAN or hosted segment, a tunnel's range."""

    def test_a_vpc_block_behind_a_handoff_is_reachable(self) -> None:
        """The VPC of a private VIF that has a handoff circuit."""
        check = _run([_dest_rule("r", "10.10.0.0/16")], CloudVirtualInterface=[_vif("C001", _prefix("10.10.0.0/16"))])
        assert _unreachable(check) == []

    def test_a_vpc_block_of_a_vif_without_a_handoff_is_unreachable(self) -> None:
        """No handoff circuit, no session: the block is registered but nothing carries it."""
        vif = _vif("C001", _prefix("10.10.0.0/16"), circuit=None)
        assert len(_unreachable(_run([_dest_rule("r", "10.10.0.0/16")], CloudVirtualInterface=[vif]))) == 1

    def test_a_vpc_block_of_a_provider_account_is_unreachable(self) -> None:
        """Only a customer-owned prefix is accepted at the border."""
        vif = _vif("P027", _prefix("10.10.0.0/16"))
        vif["virtual_network"]["node"]["account"]["node"]["owner"] = _owner("P027", "OrganizationProvider")
        assert len(_unreachable(_run([_dest_rule("r", "10.10.0.0/16")], CloudVirtualInterface=[vif]))) == 1

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
            TopologyTunnelCircuit=[_tunnel("C001", _prefix(_ACME))],
        )
        assert _unreachable(check) == []

    def test_a_tunnel_range_at_a_gateway_without_an_office_is_unreachable(self) -> None:
        """No office behind the gateway: no session faces it, the range is accepted nowhere."""
        check = _run([_dest_rule("r", _ACME)], TopologyTunnelCircuit=[_tunnel("C001", _prefix(_ACME))])
        assert len(_unreachable(check)) == 1

    def test_a_tunnel_range_of_a_provider_owned_tunnel_is_unreachable(self) -> None:
        """A tunnel without a customer owner registers nothing acceptable."""
        check = _run(
            [_dest_rule("r", _ACME)],
            TopologyCustomerOffice=[_office(_GW1, "C001")],
            TopologyTunnelCircuit=[_tunnel(None, _prefix(_ACME))],
        )
        assert len(_unreachable(check)) == 1

    def test_the_namespace_of_the_rules_own_prefix_object_does_not_matter(self) -> None:
        """The rule names 198.18.0.0/24 as a `default` namespace prefix; the registration is the PROD one."""
        check = _run(
            [_dest_rule("r", _ACME)],
            TopologyCustomerOffice=[_office(_GW1, "C001")],
            TopologyTunnelCircuit=[_tunnel("C001", _prefix(_ACME))],
        )
        assert _unreachable(check) == []


class TestEnvironmentOfTheSource:
    """A rule reaches the registrations of its source segment's environment, never the other one."""

    def test_a_prod_rule_does_not_reach_a_non_prod_registration(self) -> None:
        """The partner range registered only in NON-PROD is unreachable from a PROD segment."""
        check = _run(
            [_dest_rule("r", _ACME)],
            TopologyCustomerOffice=[_office(_GW1, "C001")],
            TopologyTunnelCircuit=[_tunnel("C001", _prefix(_ACME, "non_prod"))],
        )
        assert len(_unreachable(check)) == 1

    def test_a_non_prod_rule_does_not_reach_a_prod_registration(self) -> None:
        """And the other way round."""
        dev = _segment("dev", env="d", zone="NONPROD-ZONE", prefix="10.8.0.0/24")
        check = _run(
            [_dest_rule("r", _ACME, src="dev")],
            [dev],
            TopologyCustomerOffice=[_office(_GW1, "C001")],
            TopologyTunnelCircuit=[_tunnel("C001", _prefix(_ACME))],
        )
        assert len(_unreachable(check)) == 1

    def test_a_rule_without_a_source_segment_reaches_any_environment(self) -> None:
        """No source segment names no environment: any registered namespace type counts."""
        check = _run(
            [_dest_rule("r", _ACME, src=None)],
            TopologyCustomerOffice=[_office(_GW1, "C001")],
            TopologyTunnelCircuit=[_tunnel("C001", _prefix(_ACME, "non_prod"))],
        )
        assert _unreachable(check) == []


def test_the_query_selects_what_the_destination_rule_reads() -> None:
    """Rule prefixes, segment status and the registrations of the edge, on real schema fields."""
    path = _QUERIES / "validation" / "segment_security.gql"
    reads = {
        "SecurityPolicyRule": ["destination_prefixes.prefix", "source_segment.id", "disabled", "policy.enabled"],
        "TopologyCustomerOffice": [
            "owner.org_id",
            "sdwan_gateway.name",
            "lan_prefixes.prefix",
            "lan_prefixes.ip_namespace.namespace_type",
            "segment_deployments.segment.status",
            "segment_deployments.segment.owner.org_id",
            "segment_deployments.segment.gateway.ip_prefix.prefix",
            "segment_deployments.segment.gateway.ip_prefix.ip_namespace.namespace_type",
        ],
        "TopologyTunnelCircuit": [
            "owner.org_id",
            "registered_prefixes.prefix",
            "registered_prefixes.ip_namespace.namespace_type",
            "capabilities.device.name",
        ],
        "CloudVirtualInterface": [
            "virtual_circuit.name",
            "virtual_network.cidr_blocks.prefix",
            "virtual_network.cidr_blocks.ip_namespace.namespace_type",
            "virtual_network.account.owner.org_id",
            "transit_gateway_attachment.associated_route_table.id",
            "transit_gateway_attachment.transit_gateway.attachments.virtual_network.cidr_blocks.prefix",
        ],
    }
    # The segment root spreads a fragment from another file (not resolvable here): status is selected beside it.
    assert "        status { value }\n        environment { value }" in path.read_text()
    assert {"status", "environment"} <= attribute_names("ManagedNetworkSegment")
    for root, paths in reads.items():
        selected = _selected_paths(path, root)
        assert not [item for item in paths if item not in selected], root
    assert {"sdwan_gateway", "lan_prefixes", "segment_deployments"} <= relationship_names("TopologyCustomerOffice")
    assert {"registered_prefixes", "capabilities"} <= relationship_names("TopologyTunnelCircuit")
