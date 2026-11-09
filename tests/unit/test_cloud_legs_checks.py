"""The checks see the registrations the edge accepts behind a cloud peering: segment security reachability and the
routing invariants rules 7 and 8 (transforms/helpers/bgp_cloud.py, shared with the edge transform)."""

from __future__ import annotations

from typing import Any

import pytest
from graphql import FragmentDefinitionNode, OperationDefinitionNode, parse

from tests.unit.cloud_leg_helpers import attachment, peering, prefix
from tests.unit.query_shape_helpers import _selection_tree
from tests.unit.routing_invariants_helpers import _rule_eight
from tests.unit.routing_invariants_helpers import _run as _run_invariants
from tests.unit.test_circuit_query_shapes import _QUERIES, _selected_paths
from tests.unit.test_segment_destination_prefixes import _dest_rule
from tests.unit.test_segment_destination_prefixes import _run as _run_security
from tests.unit.test_segment_destination_prefixes import _unreachable
from utils.graphql_queries import load_query

_EDGE_PORT = ("eg-fr201", "Ethernet1/13.1000")


def _aws(owner: str | None = "C001", block: str = "10.10.0.0/16", ns: tuple[str, str] = ("PROD", "prod")) -> dict:
    """The roots of one AWS leg: the VIF on ``dxgw``, the hub hop and the VPC attachment holding ``block``."""
    return {
        "CloudPeeringInterface": [
            peering("vif-nordix", "dx-nordix", ends=(_EDGE_PORT,), account_owner=owner, zone=ns[0])
        ],
        "CloudGatewayAttachment": [
            attachment("tgwa-dxgw", "tgw", peer="dxgw", table="shared"),
            attachment(
                "tgwa-vpc", "tgw", network="nordix-vpc-prod", blocks=(prefix(block, *ns),), propagates=("shared",)
            ),
        ],
    }


class TestSegmentSecurityReachability:
    """Rule 10 of 30_all: ``nordix-prod-to-aws`` aims at the VPC block behind the AWS session."""

    def test_the_vpc_block_behind_the_cloud_session_is_reachable(self) -> None:
        """No 'not reachable: no session accepts it' for the destination the cloud model registers."""
        check = _run_security([_dest_rule("nordix-prod-to-aws", "10.10.0.0/16")], src_owner="C001", **_aws())
        assert _unreachable(check) == []

    def test_a_subnet_of_the_block_is_reachable(self) -> None:
        """Inside the registered prefix is reachable."""
        check = _run_security([_dest_rule("r", "10.10.4.0/24")], src_owner="C001", **_aws())
        assert _unreachable(check) == []

    def test_the_azure_vnet_nothing_registers_is_still_unreachable(self) -> None:
        """10.40.0.0/16 has no peering behind it: reported as before."""
        check = _run_security([_dest_rule("r", "10.40.0.0/16")], src_owner="C001", **_aws())
        assert [m.split()[1] for m in _unreachable(check)] == ["10.40.0.0/16"]

    def test_a_block_registered_in_another_environment_is_unreachable(self) -> None:
        """A PROD rule never reaches a NONPROD registration."""
        roots = _aws(block="10.50.0.0/16", ns=("NONPROD", "non_prod"))
        check = _run_security([_dest_rule("r", "10.50.0.0/16")], src_owner="C001", **roots)
        assert len(_unreachable(check)) == 1

    def test_another_owners_block_has_no_route_back(self) -> None:
        """The edge announces C001's space to C001's sessions only: the destination of C002's VPC is refused."""
        check = _run_security([_dest_rule("r", "10.10.0.0/16")], src_owner="C001", **_aws(owner="C002"))
        assert any("not by the owner of its source segment (C001)" in m for m in check._captured_errors)

    def test_a_provider_owned_account_registers_nothing_reachable(self) -> None:
        """Only a customer owner counts."""
        check = _run_security([_dest_rule("r", "10.10.0.0/16")], src_owner="C001", **_aws(owner=None))
        assert len(_unreachable(check)) == 1


def _invariants_errors(check: Any, text: str) -> list[str]:
    return [m for m in check._captured_errors if text in m]


class TestRoutingInvariantsCloudLegs:
    """Rules 7 (the owner is a customer) and 8 (one owner, one attachment point) over the cloud registrations."""

    def test_a_clean_leg_raises_nothing(self) -> None:
        """One customer, one gateway, one block."""
        assert _rule_eight(_run_invariants(**_aws())) == []
        assert _invariants_errors(_run_invariants(**_aws()), "no customer owner") == []

    def test_a_leg_registering_prefixes_with_no_customer_owner_is_reported(self) -> None:
        """Rule 7: the VPC block would never be accepted or announced."""
        errors = _invariants_errors(_run_invariants(**_aws(owner=None)), "no customer owner")
        assert len(errors) == 1
        assert "cloud peering 'vif-nordix'" in errors[0]
        assert "transport 'dx-nordix'" in errors[0]

    def test_two_owners_of_one_block_are_reported(self) -> None:
        """Rule 8: two legs of different owners reaching one block through two gateways."""
        roots = _aws()
        roots["CloudPeeringInterface"].append(
            peering(
                "vif-other", "dx-other", ends=(("eg-fr202", "Ethernet1/13.1000"),), on="dxgw2", account_owner="C002"
            )
        )
        roots["CloudGatewayAttachment"].append(
            attachment("tgwa-vpc2", "dxgw2", network="v2", blocks=(prefix("10.10.0.0/16"),))
        )
        errors = _rule_eight(_run_invariants(**roots))
        assert any("registered by more than one owner (C001, C002" in e for e in errors)

    def test_an_owners_block_behind_two_gateways_is_reported(self) -> None:
        """Rule 8: one owner's prefix reachable behind two attachment points."""
        roots = _aws()
        roots["CloudPeeringInterface"].append(
            peering("vif-two", "dx-two", ends=(("eg-fr202", "Ethernet1/13.1000"),), on="dxgw2", account_owner="C001")
        )
        roots["CloudGatewayAttachment"].append(
            attachment("tgwa-vpc2", "dxgw2", network="v2", blocks=(prefix("10.10.0.0/16"),))
        )
        errors = _rule_eight(_run_invariants(**roots))
        assert any("registered behind more than one session" in e and "cloud gateway 'dxgw'" in e for e in errors)

    def test_the_primary_and_secondary_peering_of_one_gateway_are_one_point(self) -> None:
        """A dual connection landing on one gateway registers its block once."""
        roots = _aws()
        roots["CloudPeeringInterface"].append(
            peering("vif-sec", "dx-nordix", ends=(("eg-fr202", "Ethernet1/13.1000"),), on="dxgw", account_owner="C001")
        )
        assert _rule_eight(_run_invariants(**roots)) == []


_PEERING_READS = [
    "name",
    "scope",
    "routing",
    "peer_ip.address",
    "peer_asn.asn",
    "auth.password",
    "gateway.id",
    "gateway.name",
    "advertised_prefixes.prefix",
    "device.__typename",
    "device.name",
    "device.account.owner.org_id",
    "device.owner.org_id",
    "device.gateway.name",
    "capabilities.__typename",
    "capabilities.circuit_type",
    "capabilities.capabilities.name",
    "capabilities.capabilities.device.name",
    "capabilities.vrf.id",
    "capabilities.vrf.security_zone.name",
    # The VRF of a circuit end: the ManagedVRF among the interface's capabilities (alias zone_capabilities).
    "capabilities.capabilities.capabilities.id",
    "capabilities.capabilities.capabilities.security_zone.name",
]
_ATTACHMENT_READS = [
    "gateway.id",
    "gateway.name",
    "peer_gateway.id",
    "peer_gateway.name",
    "virtual_network.id",
    "virtual_network.cidr_blocks.prefix",
    "virtual_network.cidr_blocks.ip_namespace.name",
    "allowed_prefixes.prefix",
    "allowed_prefixes.ip_namespace.name",
    "associated_route_table.id",
    "propagates_to_route_tables.id",
]


@pytest.mark.parametrize(
    "query", ["config/edge.gql", "validation/segment_security.gql", "validation/routing_invariants.gql"]
)
def test_the_queries_select_what_the_cloud_legs_read(query: str) -> None:
    """The edge and both checks select the peering and attachment fields transforms/helpers/bgp_cloud.py reads."""
    peering_paths = _selected_paths(_QUERIES / query, "CloudPeeringInterface")
    attachment_paths = _selected_paths(_QUERIES / query, "CloudGatewayAttachment")
    assert not [item for item in _PEERING_READS if item not in peering_paths]
    assert not [item for item in _ATTACHMENT_READS if item not in attachment_paths]
    for paths in (peering_paths, attachment_paths):
        assert not {p for p in paths if p.endswith(("ip_namespace.vrf_type", "ip_namespace.l3_vni", "vrf_name"))}
    assert "peer_ip.security_zone" not in peering_paths


def test_the_firewall_selects_exchange_gateway_zone_owners_without_registry_fallbacks() -> None:
    """The exchange's two routing domains come from its VRF edges, not address namespaces: the firewall reads the
    exchanges of its contexts (``exchanges``), whose legs are the context's legs in those VRFs."""
    document = parse(load_query(_QUERIES / "config/firewall.gql"))
    fragments = {d.name.value: d for d in document.definitions if isinstance(d, FragmentDefinitionNode)}
    operation = next(d for d in document.definitions if isinstance(d, OperationDefinitionNode))
    tree = _selection_tree(operation.selection_set, fragments)
    context = tree["DcimDevice"]["edges"]["node"]["shared_capabilities"]["edges"]["node"]
    exchange = context["exchanges"]["edges"]["node"]
    for side in ("a", "z"):
        assert f"vrf_{side}" in exchange, f"ExchangeGateway.vrf_{side} is not selected"
        vrf = exchange[f"vrf_{side}"]["node"]
        assert "id" in vrf
        assert set(vrf["security_zone"]["node"]) == {"id", "name", "vrf_type", "l3_vni"}
