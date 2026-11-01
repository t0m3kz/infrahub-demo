"""Rule 8 of CheckRoutingInvariants: one owner and one attachment point per registered prefix."""

from __future__ import annotations

from typing import Any

from tests.integration.demo_data import relationship_names
from tests.unit.routing_invariants_helpers import (
    _C002,
    _C005,
    _C015,
    _CUSTOMER,
    _PROVIDER,
    _block,
    _connection,
    _gateway_office,
    _private_connection,
    _rule_eight,
    _run,
    _segment,
    _transit_connection,
    _vpc,
)
from tests.unit.test_circuit_query_shapes import _QUERIES, _selected_paths


class TestOwnershipConflicts:
    """Rule 8: no prefix of two owners, no owner's prefix behind two attachment points."""

    def _demo(self, **extra: Any) -> dict[str, Any]:
        """The 30_all shape: C002 and C003 offices behind GW1, C003 and C015 VPCs on the hub transit connection."""
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
            "TopologyProviderConnection": [
                _transit_connection(
                    "hub",
                    "rt-shared",
                    [
                        ("rt-shared", _vpc("c003", "p", _block("10.30.0.0/16"))),
                        ("rt-shared", _vpc("c015", "p", _block("10.32.0.0/16"), owner=_C015)),
                    ],
                )
            ],
        }
        sections.update(extra)
        return sections

    def test_the_demo_shape_passes(self) -> None:
        """Every prefix has one owner and one attachment point; an office-hosted segment is its owner's own."""
        assert _rule_eight(_run(**self._demo())) == []

    def test_a_prefix_of_two_owners_fails(self) -> None:
        """C003's office LAN equals C015's VPC block: it would be announced to the other owner's session."""
        check = _run(
            **self._demo(
                TopologyCustomerOffice=[_gateway_office("C003-OFFICE", "GW1", _CUSTOMER, _block("10.32.0.0/16"))]
            )
        )
        errors = _rule_eight(check)
        assert len(errors) == 1
        assert "Namespace 'PROD'" in errors[0]
        assert "10.32.0.0/16" in errors[0]
        assert "C003, C015" in errors[0]
        assert "SD-WAN gateway 'GW1'" in errors[0]
        assert "provider connection 'hub'" in errors[0]
        assert "another owner's session" in errors[0]

    def test_a_segment_prefix_of_another_owner_fails(self) -> None:
        """An on-prem segment of C003 on the same prefix as C015's VPC block."""
        check = _run(**self._demo(ManagedNetworkSegment=[_segment("c003-web", "10.32.0.0/16", _CUSTOMER)]))
        errors = _rule_eight(check)
        assert len(errors) == 1
        assert "registered by more than one owner" in errors[0]

    def test_an_owners_prefix_behind_two_connections_fails(self) -> None:
        """C003's VPC block reaches the hub and a private connection of its own: two attachment points."""
        vpc = _vpc("c003", "p", _block("10.30.0.0/16"))
        connections = [
            _transit_connection("hub", "rt-shared", [("rt-shared", vpc)]),
            _private_connection("direct", vpc, sessions=("HO-DIRECT",)),
        ]
        errors = _rule_eight(_run(TopologyProviderConnection=connections))
        assert len(errors) == 1
        assert "10.30.0.0/16 of C003" in errors[0]
        assert "provider connection 'direct', provider connection 'hub'" in errors[0]
        assert "exactly one session" in errors[0]

    def test_the_two_circuits_of_one_dual_connection_are_one_attachment_point(self) -> None:
        """A dual connection has two session circuits and one attachment point: no multiple-sessions report."""
        vpc = _vpc("c003", "p", _block("10.30.0.0/16"))
        dual = _private_connection("dual", vpc, sessions=("HO-PRI", "HO-SEC"))
        assert _rule_eight(_run(TopologyProviderConnection=[dual])) == []

    def test_an_owners_lan_behind_two_gateways_fails(self) -> None:
        """The same LAN registered by two offices of C003 behind GW1 and GW2."""
        offices = [
            _gateway_office("C003-A", "GW1", _CUSTOMER, _block("10.210.3.0/24")),
            _gateway_office("C003-B", "GW2", _CUSTOMER, _block("10.210.3.0/24")),
        ]
        errors = _rule_eight(_run(TopologyCustomerOffice=offices))
        assert len(errors) == 1
        assert "SD-WAN gateway 'GW1', SD-WAN gateway 'GW2'" in errors[0]

    def test_two_offices_behind_one_gateway_are_one_attachment_point(self) -> None:
        """One owner's two offices behind GW1 each with its own LAN: no conflict."""
        offices = [
            _gateway_office("C003-A", "GW1", _CUSTOMER, _block("10.210.3.0/24")),
            _gateway_office("C003-B", "GW1", _CUSTOMER, _block("10.210.4.0/24")),
        ]
        assert _rule_eight(_run(TopologyCustomerOffice=offices)) == []

    def test_the_same_prefix_in_two_namespaces_does_not_conflict(self) -> None:
        """PROD and NON-PROD are separate routing domains: the rule runs per namespace."""
        check = _run(
            ManagedNetworkSegment=[_segment("dev", "10.32.0.0/16", _CUSTOMER, ns_name="NON-PROD", ns_type="non_prod")],
            TopologyProviderConnection=[
                _private_connection("direct", _vpc("c015", "p", _block("10.32.0.0/16"), owner=_C015))
            ],
        )
        assert _rule_eight(check) == []

    def test_a_vpc_that_propagates_elsewhere_is_not_behind_the_connection(self) -> None:
        """Only the VPCs propagating into the hybrid attachment's table are reachable: the isolated one is not."""
        connection = _transit_connection(
            "hub",
            "rt-shared",
            [
                ("rt-shared", _vpc("c015", "p", _block("10.32.0.0/16"), owner=_C015)),
                ("rt-isolated", _vpc("x", "p", _block("10.30.0.0/16"))),
            ],
        )
        check = _run(
            TopologyProviderConnection=[connection],
            TopologyCustomerOffice=[_gateway_office("C003-OFFICE", "GW1", _CUSTOMER, _block("10.30.0.0/16"))],
        )
        assert _rule_eight(check) == []

    def test_a_connection_without_a_session_circuit_has_no_session(self) -> None:
        """No session circuit, no session: its blocks are not an attachment point."""
        vpc = _vpc("c003", "p", _block("10.30.0.0/16"))
        check = _run(
            TopologyProviderConnection=[
                _private_connection("direct", vpc, sessions=()),
                _private_connection("hub", vpc),
            ]
        )
        assert _rule_eight(check) == []

    def test_a_non_customer_registration_is_not_judged_here(self) -> None:
        """A provider-owned office registers nothing at the border (rule 7 reports it); rule 8 stays silent."""
        check = _run(
            TopologyCustomerOffice=[_gateway_office("P-OFFICE", "GW1", _PROVIDER, _block("10.30.0.0/16"))],
            TopologyProviderConnection=[_private_connection("direct", _vpc("c003", "p", _block("10.30.0.0/16")))],
        )
        assert _rule_eight(check) == []

    def test_an_office_without_a_gateway_is_not_an_attachment_point(self) -> None:
        """No sdwan_gateway: nothing is announced through it, so it cannot conflict."""
        check = _run(
            TopologyCustomerOffice=[_gateway_office("C003-OFFICE", None, _CUSTOMER, _block("10.32.0.0/16"))],
            TopologyProviderConnection=[
                _private_connection("direct", _vpc("c015", "p", _block("10.32.0.0/16"), owner=_C015))
            ],
        )
        assert _rule_eight(check) == []

    def test_an_empty_payload_passes(self) -> None:
        """Nothing registered: nothing to report."""
        assert _rule_eight(_run()) == []


class TestProviderSpace:
    """A range registered through a connection is provider space: owners may each hold it behind their own session."""

    @staticmethod
    def _saas(conn_id: str, owner: tuple[str, str, str], prefix: str = "198.51.100.0/24") -> dict[str, Any]:
        """A public connection of ``owner`` registering ``prefix`` (a SaaS range)."""
        return _connection(conn_id, owner=owner, registered=(_block(prefix),))

    def test_two_owners_registering_one_saas_range_behind_their_own_connections_pass(self) -> None:
        """C003 and C015 each subscribe to the same range: not a leak, each is announced only its own."""
        check = _run(TopologyProviderConnection=[self._saas("zs-c003", _CUSTOMER), self._saas("zs-c015", _C015)])
        assert _rule_eight(check) == []

    def test_provider_space_held_as_an_onprem_segment_still_conflicts(self) -> None:
        """An owner holding the same prefix as an on-prem segment is not behind a session: still shared."""
        check = _run(
            TopologyProviderConnection=[self._saas("zs-c003", _CUSTOMER)],
            ManagedNetworkSegment=[_segment("c015-web", "198.51.100.0/24", _C015)],
        )
        errors = _rule_eight(check)
        assert len(errors) == 1
        assert "registered by more than one owner" in errors[0]

    def test_a_vpc_block_is_not_provider_space(self) -> None:
        """Only registered_prefixes of a connection are provider space: two owners on one VPC block still conflict."""
        connections = [
            _private_connection("a", _vpc("c003", "p", _block("10.30.0.0/16"))),
            _private_connection("b", _vpc("c015", "p", _block("10.30.0.0/16"), owner=_C015)),
        ]
        errors = _rule_eight(_run(TopologyProviderConnection=connections))
        assert len(errors) == 1
        assert "registered by more than one owner" in errors[0]

    def test_one_owner_registering_a_saas_range_behind_two_connections_fails(self) -> None:
        """The exemption is for distinct owners: one owner's range behind two connections is two attachment points."""
        check = _run(TopologyProviderConnection=[self._saas("zs-a", _CUSTOMER), self._saas("zs-b", _CUSTOMER)])
        errors = _rule_eight(check)
        assert len(errors) == 1
        assert "exactly one session" in errors[0]


def test_the_query_selects_what_rule_eight_reads() -> None:
    """Segment owner, the office gateway and hosted segments, and the connection chain, on real fields."""
    path = _QUERIES / "validation" / "routing_invariants.gql"
    reads = {
        "ManagedNetworkSegment": [
            "owner.org_id",
            "gateway.ip_prefix.prefix",
            "gateway.ip_prefix.ip_namespace.name",
        ],
        "TopologyCustomerOffice": [
            "sdwan_gateway.name",
            "segment_deployments.segment.owner.org_id",
            "segment_deployments.segment.gateway.ip_prefix.prefix",
            "segment_deployments.segment.gateway.ip_prefix.ip_namespace.name",
            "lan_prefixes.ip_namespace.name",
        ],
        "TopologyProviderConnection": [
            "name",
            "session_circuits.name",
            "registered_prefixes.prefix",
            "registered_prefixes.ip_namespace.name",
            "attached_network.cidr_blocks.prefix",
            "attached_network.cidr_blocks.ip_namespace.name",
            "attached_network.account.owner.org_id",
            "transit_attachment.associated_route_table.id",
            "attached_gateway.attachments.attachment_type",
            "attached_gateway.attachments.propagates_to_route_tables.id",
            "attached_gateway.attachments.virtual_network.cidr_blocks.prefix",
            "attached_gateway.attachments.virtual_network.account.owner.org_id",
        ],
    }
    for root, paths in reads.items():
        selected = _selected_paths(path, root)
        assert not [item for item in paths if item not in selected], root
    assert "owner" in relationship_names("ManagedNetworkSegment")
    assert {"sdwan_gateway", "segment_deployments", "lan_prefixes"} <= relationship_names("TopologyCustomerOffice")
    assert {
        "transit_attachment",
        "attached_gateway",
        "attached_network",
        "session_circuits",
        "registered_prefixes",
    } <= relationship_names("TopologyProviderConnection")
    assert {"transit_gateway", "associated_route_table"} <= relationship_names("CloudTransitGatewayAttachment")
    assert "attachments" in relationship_names("CloudTransitGateway")
    assert "segment" in relationship_names("ManagedSegmentDeployment")
