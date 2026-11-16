"""The checks see the registrations the edge accepts behind a cloud peering."""

from __future__ import annotations

from typing import Any

from tests.unit.cloud_leg_helpers import attachment, peering, prefix
from tests.unit.generators.test_segment_destination_prefixes import _dest_rule
from tests.unit.generators.test_segment_destination_prefixes import _run as _run_security
from tests.unit.generators.test_segment_destination_prefixes import _unreachable
from tests.unit.routing_invariants_helpers import _rule_eight
from tests.unit.routing_invariants_helpers import _run as _run_invariants

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
        assert any("registered behind more than one session" in e and "cloud gateway 'dxgw+tgw'" in e for e in errors)

    def test_the_primary_and_secondary_peering_of_one_gateway_are_one_point(self) -> None:
        """A dual connection landing on one gateway registers its block once."""
        roots = _aws()
        roots["CloudPeeringInterface"].append(
            peering("vif-sec", "dx-nordix", ends=(("eg-fr202", "Ethernet1/13.1000"),), on="dxgw", account_owner="C001")
        )
        assert _rule_eight(_run_invariants(**roots)) == []


def _hub(*networks: tuple[str, str, str | None]) -> dict:
    """A leg on our network account's transport (a provider owns it) to a hub with (network, block, owner) behind it."""
    return {
        "CloudPeeringInterface": [
            peering(
                "vif-hub",
                "dx-hub",
                ends=(_EDGE_PORT,),
                account_owner="P028",
                account_owner_type="OrganizationProvider",
            )
        ],
        "CloudGatewayAttachment": [
            attachment("tgwa-dxgw", "tgw", peer="dxgw", table="shared"),
            *(
                attachment(
                    f"tgw-{name}",
                    "tgw",
                    network=name,
                    blocks=(prefix(block),),
                    propagates=("shared",),
                    network_owner=owner_key,
                )
                for name, block, owner_key in networks
            ),
        ],
    }


class TestSharedHubOwnership:
    """A hub leg registers each prefix for the owner of its attachment's network (rules 7 and 8)."""

    def test_two_customers_behind_one_hub_are_no_conflict(self) -> None:
        check = _run_invariants(**_hub(("vaultex", "10.30.0.0/16", "C003"), ("novantis", "10.150.0.0/16", "C015")))
        assert _rule_eight(check) == []
        assert _invariants_errors(check, "no customer owner") == []

    def test_the_same_prefix_of_two_owners_behind_one_hub_is_a_conflict(self) -> None:
        check = _run_invariants(**_hub(("vaultex", "10.30.0.0/16", "C003"), ("novantis", "10.30.0.0/16", "C015")))
        assert any("registered by more than one owner (C003, C015" in e for e in _rule_eight(check))

    def test_an_owner_behind_the_hub_and_its_own_leg_is_two_points(self) -> None:
        roots = _hub(("vaultex", "10.30.0.0/16", "C003"))
        roots["CloudPeeringInterface"].append(
            peering("vif-own", "dx-own", ends=(("eg-fr202", "Ethernet1/13.1000"),), on="dxgw2", account_owner="C003")
        )
        roots["CloudGatewayAttachment"].append(
            attachment("own-vpc", "dxgw2", network="v2", blocks=(prefix("10.30.0.0/16"),))
        )
        assert any("registered behind more than one session" in e for e in _rule_eight(_run_invariants(**roots)))

    def test_a_network_of_no_customer_behind_a_provider_transport_is_rule_seven(self) -> None:
        errors = _invariants_errors(_run_invariants(**_hub(("orphan", "10.30.0.0/16", None))), "no customer owner")
        assert len(errors) == 1 and "cloud peering 'vif-hub'" in errors[0]
