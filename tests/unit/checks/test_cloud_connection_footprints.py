"""CheckCloudConnection R12-R16: each cloud footprint's ``connection`` reaches us (raw query shapes, cleaned)."""

from __future__ import annotations

from typing import Any, cast

from checks.connectivity.cloud_connection import CheckCloudConnection
from tests.unit.cloud_path_helpers import (
    footprint,
    handoff_path,
    hub,
    link_attachment,
    many,
    network_attachment,
    peering,
    session,
    v,
)

_FP = "C003-P-X"
_RT = f"rt-{_FP.lower()}"  # the generator's name; the check accepts any table of one owner


def _run_all(**roots: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    """Validate the raw roots (kind -> nodes): the error messages and the warnings."""
    check = cast(Any, CheckCloudConnection.__new__(CheckCloudConnection))
    errors: list[str] = []
    warnings: list[str] = []
    check.log_error = lambda **kwargs: errors.append(kwargs["message"])
    check.log_warning = warnings.append
    check.validate({kind: many(*nodes) for kind, nodes in roots.items()})
    return errors, warnings


def _run(**roots: list[dict[str, Any]]) -> list[str]:
    """The error messages of a validation."""
    return _run_all(**roots)[0]


def _rule(errors: list[str], rule: str) -> list[str]:
    return [error for error in errors if error.startswith(f"{rule}:")]


def _table(name: str, gateway: str = "tgw") -> dict[str, Any]:
    return {
        "id": name,
        "name": v(name),
        "gateway": {"node": {"id": gateway, "name": v(gateway), "gateway_type": v("hub")}},
    }


def _network(name: str, account: str = "acct-own") -> dict[str, Any]:
    return {"id": name, "name": v(name), "account": {"node": {"id": account}}, "region": {"node": {"id": "region-1"}}}


def _transit_world(**overrides: Any) -> dict[str, list[dict[str, Any]]]:
    """A boarded transit footprint on the hub tgw, whose link reaches our edge."""
    world: dict[str, list[dict[str, Any]]] = {
        "TopologyCustomerCloud": [footprint(_FP)],
        "CloudGateway": [hub("tgw")],
        "CloudGatewayRouteTable": [_table(_RT), _table("rt-onprem")],
        "CloudVirtualNetwork": [_network("vpc-a")],
        "CloudGatewayAttachment": [
            network_attachment("tgw-vpc-a", "tgw", "vpc-a", "C003", table=_RT, propagates=(_RT, "rt-onprem")),
            link_attachment("tgw-hybrid", "tgw", "dxgw"),
        ],
        "CloudPeeringInterface": [handoff_path("1", gateway="dxgw", account="acct-hub")],
    }
    world.update(overrides)
    return world


class TestR12Transit:
    def test_a_boarded_transit_footprint_raises_nothing(self) -> None:
        errors, warnings = _run_all(**_transit_world())
        assert [e for e in errors if e[:3] in {"R12", "R13", "R16"}] == [] and warnings == []

    def test_no_hub_in_the_region_and_environment(self) -> None:
        errors = _rule(_run(**_transit_world(CloudGateway=[hub("tgw", environment="n")])), "R12")
        assert len(errors) == 1 and "no hub gateway in region 'eu-central-1' and environment 'p'" in errors[0]
        assert "Author a CloudGateway with gateway_type hub" in errors[0]

    def test_two_hubs_are_refused(self) -> None:
        errors = _rule(_run(**_transit_world(CloudGateway=[hub("tgw"), hub("tgw2")])), "R12")
        assert len(errors) == 1 and "2 hub gateways" in errors[0]

    def test_a_network_not_attached_is_a_warning(self) -> None:
        world = _transit_world(CloudVirtualNetwork=[_network("vpc-a"), _network("vpc-b")])
        errors, warnings = _run_all(**world)
        assert _rule(errors, "R12") == []
        assert len(warnings) == 1 and "'vpc-b' has no attachment on hub 'tgw'" in warnings[0]

    def test_an_attachment_without_a_route_table_is_not_attached(self) -> None:
        attachments = [network_attachment("tgw-vpc-a", "tgw", "vpc-a", "C003"), link_attachment("l", "tgw", "dxgw")]
        _errors, warnings = _run_all(**_transit_world(CloudGatewayAttachment=attachments))
        assert len(warnings) == 1 and "'vpc-a'" in warnings[0]


class TestR13RouteTables:
    def test_two_owners_associated_with_one_table(self) -> None:
        attachments = [
            network_attachment("a", "tgw", "vpc-a", "C003", table="rt-shared"),
            network_attachment("b", "tgw", "vpc-b", "C015", table="rt-shared"),
        ]
        errors = _rule(_run(CloudGatewayAttachment=attachments), "R13")
        assert len(errors) == 1 and "associated with networks of C003, C015" in errors[0]

    def test_a_table_that_learns_another_owners_routes(self) -> None:
        attachments = [
            network_attachment("a", "tgw", "vpc-a", "C003", table="rt-a", propagates=("rt-a",)),
            network_attachment("b", "tgw", "vpc-b", "C015", table="rt-b", propagates=("rt-b", "rt-a")),
        ]
        errors = _rule(_run(CloudGatewayAttachment=attachments), "R13")
        assert (
            len(errors) == 1 and "'rt-a' is associated with networks of C003 and learns the routes of C015" in errors[0]
        )

    def test_the_link_table_may_learn_every_owner(self) -> None:
        attachments = [
            network_attachment("a", "tgw", "vpc-a", "C003", table="rt-a", propagates=("rt-a", "rt-onprem")),
            network_attachment("b", "tgw", "vpc-b", "C015", table="rt-b", propagates=("rt-b", "rt-onprem")),
            link_attachment("hybrid", "tgw", "dxgw"),
        ]
        assert _rule(_run(CloudGatewayAttachment=attachments), "R13") == []


class TestR14Dedicated:
    def test_its_own_peering_reaching_us_raises_nothing(self) -> None:
        errors = _run(
            TopologyCustomerCloud=[footprint(_FP, connection="dedicated")], CloudPeeringInterface=[handoff_path("1")]
        )
        assert _rule(errors, "R14") == []

    def test_no_peering_of_its_own(self) -> None:
        errors = _rule(
            _run(
                TopologyCustomerCloud=[footprint(_FP, connection="dedicated")],
                CloudPeeringInterface=[handoff_path("1", account="someone-else")],
            ),
            "R14",
        )
        assert len(errors) == 1 and "no peering of a transport in its account" in errors[0]

    def test_a_broken_path_names_the_hop(self) -> None:
        broken = peering("vif", "dx-1", sessions=(session("H", ("eg-01", "Eth1/1.100"), ("dx-1", "vif")),))
        errors = _rule(
            _run(TopologyCustomerCloud=[footprint(_FP, connection="dedicated")], CloudPeeringInterface=[broken]), "R14"
        )
        assert len(errors) == 1 and "stops at peering 'dx-1:vif'" in errors[0]


class TestR15Vpn:
    @staticmethod
    def _tunnel(*, internet: bool = True) -> dict[str, Any]:
        circuit = session(
            "VPN-1",
            ("eg-01", "Tunnel1"),
            ("vpn-1", "tun1"),
            circuit_type="vpn",
            internet=("eg-01",) if internet else (),
        )
        return peering("tun1", "vpn-1", transport_kind="CloudVPNConnection", sessions=(circuit,))

    def test_a_tunnel_on_an_edge_with_internet_raises_nothing(self) -> None:
        errors = _run(TopologyCustomerCloud=[footprint(_FP, connection="vpn")], CloudPeeringInterface=[self._tunnel()])
        assert _rule(errors, "R15") == []

    def test_no_tunnel(self) -> None:
        errors = _run(TopologyCustomerCloud=[footprint(_FP, connection="vpn")], CloudPeeringInterface=[])
        assert len(_rule(errors, "R15")) == 1 and "no CloudVPNConnection with a tunnel" in errors[0]

    def test_an_edge_without_internet(self) -> None:
        errors = _rule(
            _run(
                TopologyCustomerCloud=[footprint(_FP, connection="vpn")],
                CloudPeeringInterface=[self._tunnel(internet=False)],
            ),
            "R15",
        )
        assert len(errors) == 1 and "rides on eg-01, which has no live internet carrier" in errors[0]


class TestR16HubLink:
    def test_a_hub_whose_link_lands_no_peering(self) -> None:
        errors = _rule(_run(**_transit_world(CloudPeeringInterface=[])), "R16")
        assert len(errors) == 1 and "hub 'tgw' serves transit footprints" in errors[0]
        assert "no peering lands" in errors[0]

    def test_a_hub_serving_no_transit_footprint_is_not_judged(self) -> None:
        world = _transit_world(CloudPeeringInterface=[])
        world["TopologyCustomerCloud"] = [footprint(_FP, connection="dedicated")]
        assert _rule(_run(**world), "R16") == []
