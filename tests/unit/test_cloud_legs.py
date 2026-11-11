"""Cloud legs (transforms/helpers/bgp_cloud.py): the neighbor, the owner and the registered prefixes of a session
whose far end is a CloudPeeringInterface, and what the edge policy makes of them (utils/ownership.py)."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from tests.unit.cloud_leg_helpers import (
    attachment,
    aws_chain,
    cleaned,
    interface_vrf,
    peering,
    prefix,
)
from transforms.helpers.bgp import _build_session_from_peering, _far_end_is_ours
from transforms.helpers.bgp_cloud import (
    CloudLeg,
    cloud_legs,
    graft_cloud_far_ends,
    legs_by_interface,
    reachable_prefixes,
)
from transforms.helpers.bgp_external import (
    get_external_peer_prefixes,
    get_external_session_policies,
    registered_by_attachment,
)
from utils.routing_domain import RoutingDomainError

_LOCAL = ("eg-fr201", "Ethernet1/13.1000")


def _legs(*peerings: dict[str, Any], attachments: tuple[dict[str, Any], ...] = ()) -> list[CloudLeg]:
    return cloud_legs(cleaned(*peerings), cleaned(*attachments))


def _blocks(found: list[dict[str, Any]]) -> list[str]:
    return [p["prefix"] for p in found]


class TestLegDerivation:
    """One leg per (peering, handoff or vpn session circuit, end of ours)."""

    def test_the_leg_carries_the_neighbor_the_key_and_our_end(self) -> None:
        """peer_ip, peer_asn, auth come from the peering; the local end is the other end of the circuit."""
        (leg,) = _legs(peering("vif-nordix", "dx-nordix"))
        assert leg.local == _LOCAL
        assert leg.peering == "vif-nordix"
        assert leg.transport == "dx-nordix"
        assert leg.transport_kind == "CloudDirectConnect"
        assert leg.peer_ip == {"address": "10.255.5.6/30"}
        assert leg.peer_asn == 64512
        assert leg.password == "s3cret"
        assert leg.point == "cloud gateway 'dxgw'"

    def test_the_owner_is_the_transports_account_owner(self) -> None:
        """Never the peering's: the account of the transport owns what it registers."""
        (leg,) = _legs(peering("vif", "dx", account_owner="C007"))
        assert leg.owner == "C007"

    def test_a_saas_transport_without_account_uses_its_own_owner(self) -> None:
        """No account on a SaaS holder: the transport's own owner."""
        (leg,) = _legs(peering("webex", "webex-fra", account_owner=None, saas_owner="C002", scope="saas", on=None))
        assert leg.owner == "C002"
        assert leg.group == "webex"

    def test_only_a_customer_owner_counts(self) -> None:
        """A provider-owned account owns nothing that crosses the border."""
        (leg,) = _legs(peering("vif", "dx", account_owner="P004", account_owner_type="OrganizationProvider"))
        assert leg.owner is None

    @pytest.mark.parametrize(
        ("overrides"),
        [{"routing": "static"}, {"circuit_type": "tunnel"}],
        ids=["static-routing", "tunnel-circuit"],
    )
    def test_no_bgp_session_no_leg(self, overrides: dict[str, Any]) -> None:
        """A static peering and a tunnel (inventory) circuit carry no session."""
        assert _legs(peering("vif", "dx", **overrides)) == []

    def test_a_peering_on_no_session_circuit_is_no_leg(self) -> None:
        """Without a circuit there is no end of ours."""
        raw = peering("vif", "dx")
        raw["capabilities"] = {"edges": []}
        assert _legs(raw) == []

    def test_a_vpn_tunnel_lands_through_the_transports_gateway(self) -> None:
        """The tunnel has no gateway of its own: the CloudVPNConnection's one is the attachment point."""
        (leg,) = _legs(
            peering("t1", "vpn-1", on=None, kind="CloudVPNConnection", transport_gateway="tgw", circuit_type="vpn")
        )
        assert leg.group == "tgw"
        assert leg.transport_kind == "CloudVPNConnection"

    def test_one_leg_per_end_of_ours(self) -> None:
        """A peering whose circuit lists two of our ports (a primary and a secondary link) is two legs."""
        legs = _legs(peering("vif", "dx", ends=(_LOCAL, ("eg-fr202", "Ethernet1/13.1000"))))
        assert sorted(leg.local for leg in legs) == [_LOCAL, ("eg-fr202", "Ethernet1/13.1000")]

    def test_an_interface_of_two_peerings_is_dropped(self, caplog: pytest.LogCaptureFixture) -> None:
        """Ambiguous: it accepts nothing, with a warning."""
        legs = _legs(peering("vif-1", "dx"), peering("vif-2", "dx"))
        with caplog.at_level(logging.WARNING):
            assert legs_by_interface(legs, "eg-fr201") == {}
        assert "several cloud peerings" in caplog.text

    def test_only_the_devices_own_interfaces(self) -> None:
        """legs_by_interface keys the legs of one device by interface name."""
        legs = _legs(peering("vif", "dx"))
        assert set(legs_by_interface(legs, "eg-fr201")) == {_LOCAL[1]}
        assert legs_by_interface(legs, "other") == {}

    def test_a_missing_session_and_local_interface_vrf_is_refused(self) -> None:
        """Neutral prefixes cannot supply a routing context when both explicit owners are absent."""
        raw = peering("vif", "dx")
        circuit = raw["capabilities"]["edges"][1]["node"]
        circuit["vrf"] = {"node": None}
        circuit["capabilities"]["edges"][0]["node"]["zone_capabilities"] = {"edges": []}
        with pytest.raises(RoutingDomainError, match="missing VRF instance"):
            _legs(raw)

    def test_same_zone_different_instance_on_the_local_end_is_refused(self) -> None:
        """An end's export VRF must agree by scoped ID, not merely by its zone name."""
        raw = peering("vif", "dx")
        circuit = raw["capabilities"]["edges"][1]["node"]
        circuit["capabilities"]["edges"][0]["node"]["zone_capabilities"] = interface_vrf("PROD", "AM")
        with pytest.raises(RoutingDomainError, match="conflicting VRF instances"):
            _legs(raw)

    def test_native_cloud_endpoint_has_no_managed_vrf(self) -> None:
        """Only our interface and the authored session circuit own the export context."""
        raw = peering("vif", "dx")
        circuit = raw["capabilities"]["edges"][1]["node"]
        assert "vrf" not in raw
        assert "vrf" not in circuit["capabilities"]["edges"][-1]["node"]
        assert "zone_capabilities" not in circuit["capabilities"]["edges"][-1]["node"]


class TestRegisteredPrefixes:
    """Cloud to us: the prefixes behind the peering's gateway."""

    def test_rule_10_chain_registers_the_vpc_block(self) -> None:
        """dxgw -> tgw (hub hop) -> the VPC attachment: its CIDR block, owned by the transport's owner."""
        peerings, attachments = aws_chain()
        (leg,) = cloud_legs(peerings, attachments)
        assert [(registration.customer, registration.prefix["prefix"]) for registration in leg.registered] == [
            ("C001", "10.10.0.0/16")
        ]

    def test_allowed_prefixes_replace_the_network_blocks(self) -> None:
        """An attachment with allowed prefixes lets exactly those through."""
        attachments = cleaned(
            attachment(
                "a",
                "vgw",
                network="vpc",
                blocks=(prefix("10.10.0.0/16"), prefix("10.11.0.0/16")),
                allowed=(prefix("10.10.4.0/24"),),
            )
        )
        assert _blocks(reachable_prefixes(gateway_ref("vgw"), attachments)) == ["10.10.4.0/24"]

    def test_a_hop_attachments_allowed_prefixes_are_registered_too(self) -> None:
        """The association of a gateway with a hub lists what it lets through (the DX gateway allowed prefixes)."""
        attachments = cleaned(
            attachment("hop", "tgw", peer="dxgw", allowed=(prefix("10.200.0.0/16"),)),
            attachment("vpc", "tgw", network="v", blocks=(prefix("10.10.0.0/16"),)),
        )
        assert _blocks(reachable_prefixes(gateway_ref("dxgw"), attachments)) == ["10.10.0.0/16", "10.200.0.0/16"]

    def test_only_vpcs_propagating_into_the_entry_table_are_reachable(self) -> None:
        """The hub receives the hybrid hop in rt-a: a VPC propagating into rt-b only is not reachable."""
        attachments = cleaned(
            attachment("hop", "tgw", peer="dxgw", table="rt-a"),
            attachment("in", "tgw", network="in", blocks=(prefix("10.10.0.0/16"),), propagates=("rt-a",)),
            attachment("out", "tgw", network="out", blocks=(prefix("10.20.0.0/16"),), propagates=("rt-b",)),
        )
        assert _blocks(reachable_prefixes(gateway_ref("dxgw"), attachments)) == ["10.10.0.0/16"]

    def test_a_hop_without_a_table_filters_nothing(self) -> None:
        """No associated route table on the hop: every VPC of the hub is reachable."""
        attachments = cleaned(
            attachment("hop", "tgw", peer="dxgw"),
            attachment("a", "tgw", network="a", blocks=(prefix("10.10.0.0/16"),), propagates=("rt-x",)),
            attachment("b", "tgw", network="b", blocks=(prefix("10.20.0.0/16"),)),
        )
        assert _blocks(reachable_prefixes(gateway_ref("dxgw"), attachments)) == ["10.10.0.0/16", "10.20.0.0/16"]

    def test_hub_to_hub_is_one_more_hop_and_a_loop_ends(self) -> None:
        """dxgw -> tgw-1 <-> tgw-2 -> VPC, with the hubs peered both ways: found once, no endless walk."""
        attachments = cleaned(
            attachment("h1", "tgw-1", peer="dxgw"),
            attachment("h2", "tgw-2", peer="tgw-1"),
            attachment("h3", "tgw-1", peer="tgw-2"),
            attachment("vpc", "tgw-2", network="v", blocks=(prefix("10.30.0.0/16"),)),
        )
        assert _blocks(reachable_prefixes(gateway_ref("dxgw"), attachments)) == ["10.30.0.0/16"]

    def test_each_session_binds_neutral_prefixes_to_its_export_vrf(self) -> None:
        """The same native cloud space may be registered into distinct scoped session contexts."""
        attachments = (attachment("a", "vgw", network="v", blocks=(prefix("10.10.0.0/16"),)),)
        legs = _legs(
            peering("prod", "dx-p", on="vgw"),
            peering("nonprod", "dx-n", on="vgw", zone="NONPROD"),
            attachments=attachments,
        )
        assert {leg.peering: leg.registered[0].zone["name"] for leg in legs} == {
            "prod": "PROD",
            "nonprod": "NONPROD",
        }
        assert len({leg.registered[0].vrf["id"] for leg in legs}) == 2
        assert all("security_zone" not in leg.registered[0].prefix for leg in legs)
        assert {leg.registered[0].prefix["ip_namespace"]["name"] for leg in legs} == {"default"}

    def test_same_zone_in_two_deployments_retains_distinct_vrf_ids(self) -> None:
        """A shared policy zone must not collapse deployment-scoped export owners."""
        legs = _legs(
            peering("fr", "dx-fr", on="vgw", deployment="FR"),
            peering("am", "dx-am", on="vgw", deployment="AM"),
            attachments=(attachment("a", "vgw", network="v", blocks=(prefix("10.10.0.0/16"),)),),
        )
        assert {leg.registered[0].zone["name"] for leg in legs} == {"PROD"}
        assert len({leg.registered[0].vrf["id"] for leg in legs}) == 2

    def test_a_public_peering_registers_nothing(self) -> None:
        """A public virtual interface learns the provider's public ranges, which nobody registers."""
        peerings, attachments = aws_chain()
        peerings[0]["scope"] = "public"
        (leg,) = cloud_legs(peerings, attachments)
        assert leg.registered == ()

    def test_no_gateway_registers_nothing(self) -> None:
        """A SaaS peering that lands on no gateway has no attachment to read."""
        (leg,) = _legs(peering("webex", "webex-fra", account_owner=None, saas_owner="C002", scope="saas", on=None))
        assert leg.registered == ()


def gateway_ref(name: str) -> dict[str, Any]:
    """A cleaned gateway reference."""
    return {"id": f"gw-{name}", "name": name}


class TestAttachmentPoint:
    """The sessions that land on gateways of one hub chain are ONE attachment point (rule 8 of routing_invariants)."""

    @staticmethod
    def _aws() -> tuple[dict[str, Any], ...]:
        """00_full: a Direct Connect gateway associated with the transit gateway, a VPC on the transit gateway."""
        return (
            attachment("assoc", "tgw", peer="dxgw"),
            attachment("vpc", "tgw", network="v", blocks=(prefix("10.200.0.0/16"),)),
        )

    def test_a_transit_vif_and_a_vpn_to_the_associated_hub_are_one_point(self) -> None:
        """dxgw -> tgw and the VPN on tgw reach the same network: the prefix behind both is not a conflict."""
        legs = _legs(
            peering("vif", "dx", on="dxgw"),
            peering("t1", "vpn", on=None, kind="CloudVPNConnection", transport_gateway="tgw", circuit_type="vpn"),
            attachments=self._aws(),
        )
        assert {leg.group for leg in legs} == {"dxgw+tgw"}
        assert {leg.point for leg in legs} == {"cloud gateway 'dxgw+tgw'"}
        points = registered_by_attachment([], [], legs)
        assert list(points) == ["cloud gateway 'dxgw+tgw'"]

    def test_a_gateway_with_no_gateway_neighbour_is_its_own_point(self) -> None:
        """The virtual private gateway of a VPC joins no chain: a prefix behind it AND the hub stays a conflict."""
        legs = _legs(
            peering("vif-t", "dx", on="dxgw"),
            peering("vif-v", "dx", on="vgw"),
            attachments=(*self._aws(), attachment("vgwa", "vgw", network="v", blocks=(prefix("10.200.0.0/16"),))),
        )
        assert {leg.peering: leg.group for leg in legs} == {"vif-t": "dxgw+tgw", "vif-v": "vgw"}

    def test_the_chain_follows_the_association_either_way_and_through_two_hubs(self) -> None:
        """A chain of three gateways is one point from any of its ends."""
        attachments = (attachment("a1", "tgw-1", peer="dxgw"), attachment("a2", "tgw-2", peer="tgw-1"))
        legs = _legs(peering("x", "dx", on="tgw-2"), peering("y", "dx", on="dxgw"), attachments=attachments)
        assert {leg.group for leg in legs} == {"dxgw+tgw-1+tgw-2"}


class TestFarEnd:
    """The session builder sees the cloud peering interface as the remote end."""

    def _peering_node(self, name: str = "BGP-1") -> dict[str, Any]:
        """The one-ended peering as the edge query returns it: only our sub-interface."""
        return {
            "name": name,
            "session_type": "EBGP",
            "peering_role": "regular",
            "ttl": 1,
            "capabilities": [
                {
                    "zone_capabilities": cleaned(*(e["node"] for e in interface_vrf()["edges"])),
                    "name": _LOCAL[1],
                    "device": {"name": _LOCAL[0]},
                    "ip_address": {
                        "address": "10.255.5.5/30",
                        "ip_namespace": {"node": {"name": {"value": "default"}}},
                    },
                }
            ],
            "bgp_processes": [{"local_as": {"asn": 4200000100}, "capabilities": [{"name": _LOCAL[0]}]}],
        }

    def _grafted(self, **kwargs: Any) -> dict[str, Any]:
        legs = legs_by_interface(_legs(peering("vif", "dx", **kwargs)), _LOCAL[0])
        device = {"name": _LOCAL[0], "capabilities": [{"typename": "ManagedBGP", "peerings": [self._peering_node()]}]}
        graft_cloud_far_ends(device, legs)
        return device["capabilities"][0]["peerings"][0]

    def test_the_session_is_external_with_the_neighbor_asn_and_key(self) -> None:
        """remote ip and AS from peer_ip and peer_asn, the BGP key from auth, never 'ours'."""
        session = _build_session_from_peering(self._grafted(), _LOCAL[0], {"asn": 4200000100}, [])
        assert session is not None
        assert session["external"] is True
        assert session["remote_ip"] == {"address": "10.255.5.6/30"}
        assert session["remote_as"] == {"asn": 64512}
        assert session["password"] == "s3cret"
        assert session["remote_device"] == "dx"
        assert session["vrf"] == "PROD"

    def test_a_cloud_far_end_is_never_ours(self) -> None:
        """The far end has no BGP process of its own with a role: external, however its device is named."""
        assert _far_end_is_ours(self._grafted(), "dx", frozenset()) is False

    def test_a_peering_password_wins_over_the_peering_interface_key(self) -> None:
        """A key written on the peering itself is not replaced."""
        node = self._peering_node()
        node["password"] = {"password": "own"}
        legs = legs_by_interface(_legs(peering("vif", "dx")), _LOCAL[0])
        graft_cloud_far_ends(
            {"name": _LOCAL[0], "capabilities": [{"typename": "ManagedBGP", "peerings": [node]}]}, legs
        )
        session = _build_session_from_peering(node, _LOCAL[0], {"asn": 4200000100}, [])
        assert session is not None
        assert session["password"] == "own"

    def test_no_peer_ip_skips_the_session_with_a_warning(self) -> None:
        """The usual 'no address on the far side' skip, not a crash."""
        warnings: list[str] = []
        session = _build_session_from_peering(
            self._grafted(peer_ip=None), _LOCAL[0], {"asn": 4200000100}, [], warnings=warnings
        )
        assert session is None
        assert "has no address" in warnings[0]

    def test_no_peer_asn_skips_an_ebgp_session(self) -> None:
        """eBGP needs the remote AS: the session is skipped."""
        assert _build_session_from_peering(self._grafted(peer_asn=None), _LOCAL[0], {"asn": 1}, []) is None

    def test_an_interface_that_is_no_leg_is_left_alone_and_refused(self) -> None:
        """No leg, no far end: the session builder keeps refusing a one-ended peering."""
        node = self._peering_node()
        graft_cloud_far_ends({"name": _LOCAL[0], "capabilities": [{"typename": "ManagedBGP", "peerings": [node]}]}, {})
        assert len(node["capabilities"]) == 1
        with pytest.raises(ValueError, match="expected 2 capabilities"):
            _build_session_from_peering(node, _LOCAL[0], {"asn": 1}, [])


def _subif(name: str = _LOCAL[1], namespace: str = "PROD") -> dict[str, Any]:
    return {
        "zone_capabilities": cleaned(*(e["node"] for e in interface_vrf(namespace)["edges"])),
        "name": name,
        "ip_address": {"address": "10.255.5.5/30", "ip_namespace": {"node": {"name": {"value": "default"}}}},
    }


def _peer_prefixes(legs: list[CloudLeg], interfaces: list[dict[str, Any]]) -> dict[str, dict[str, list[str]]]:
    return get_external_peer_prefixes(
        interfaces,
        _LOCAL[0],
        device_capabilities=None,
        offices=None,
        tunnels=None,
        cloud_legs=legs_by_interface(legs, _LOCAL[0]),
    )


_ONPREM = {"PROD": {"C001": ["10.5.10.0/24"], "C002": ["10.6.10.0/24"]}}


class TestSessionPolicy:
    """The cloud leg through the one exchange rule."""

    def test_rule_10_the_session_accepts_the_vpc_block_and_is_announced_the_owners_space(self) -> None:
        """IN holds the VPC block of C001; OUT the on-prem space of C001, nothing of C002."""
        peerings, attachments = aws_chain()
        legs = cloud_legs(peerings, attachments)
        peers = _peer_prefixes(legs, [_subif()])
        assert peers == {_LOCAL[1]: {"C001": ["10.10.0.0/16"]}}
        policy = get_external_session_policies(
            [_subif()], peers, _ONPREM, cloud_legs=legs_by_interface(legs, _LOCAL[0])
        )[_LOCAL[1]]
        assert policy == {"owners": ["C001"], "in": ["10.10.0.0/16"], "out": ["10.5.10.0/24"]}

    def test_a_non_prod_session_does_not_accept_a_prod_block(self) -> None:
        """PROD and NONPROD never mix: a NONPROD sub-interface accepts only NONPROD registrations."""
        peerings, attachments = aws_chain()
        legs = cloud_legs(peerings, attachments)
        peers = _peer_prefixes(legs, [_subif(namespace="NONPROD")])
        assert peers == {_LOCAL[1]: {}}

    def test_a_saas_leg_has_the_transports_owner_and_accepts_what_its_gateway_reaches(self) -> None:
        """No account: the transport's own owner registers the SaaS range behind its hub."""
        legs = _legs(
            peering("webex", "webex-fra", account_owner=None, saas_owner="C002", scope="saas", on="hub"),
            attachments=(attachment("sa", "hub", peer=None, network="webex-net", blocks=(prefix("203.0.113.0/24"),)),),
        )
        peers = _peer_prefixes(legs, [_subif()])
        assert peers == {_LOCAL[1]: {"C002": ["203.0.113.0/24"]}}

    def test_a_public_peering_announces_its_advertised_prefixes_to_its_session_alone(self) -> None:
        """advertised_prefixes are ours going out: in OUT of that session, not learned, nothing accepted."""
        legs = _legs(
            peering(
                "pub", "dx", scope="public", on=None, advertised=(prefix("198.51.100.0/24", "INTERNET", "internet"),)
            )
        )
        by_interface = legs_by_interface(legs, _LOCAL[0])
        peers = _peer_prefixes(legs, [_subif()])
        assert peers == {_LOCAL[1]: {}}
        policy = get_external_session_policies([_subif()], peers, _ONPREM, cloud_legs=by_interface)[_LOCAL[1]]
        assert policy == {"owners": [], "in": [], "out": ["198.51.100.0/24"]}

    def test_advertised_prefixes_of_an_ownerless_transport_are_not_announced(self) -> None:
        """Only a customer's space crosses the border."""
        (leg,) = _legs(
            peering("pub", "dx", scope="public", on=None, account_owner=None, advertised=(prefix("198.51.100.0/24"),))
        )
        assert leg.advertised == ()

    def test_two_owners_of_one_block_are_a_conflict(self, caplog: pytest.LogCaptureFixture) -> None:
        """Two sessions of different owners reaching the same VPC block: logged, as an SD-WAN conflict is."""
        legs = _legs(
            peering("vif-1", "dx-1", ends=(("eg-fr201", "Ethernet1/13.1000"),), on="gw1", account_owner="C001"),
            peering("vif-2", "dx-2", ends=(("eg-fr201", "Ethernet1/13.1100"),), on="gw2", account_owner="C002"),
            attachments=(
                attachment("a1", "gw1", network="v", blocks=(prefix("10.10.0.0/16"),)),
                attachment("a2", "gw2", network="v", blocks=(prefix("10.10.0.0/16"),)),
            ),
        )
        by_interface = legs_by_interface(legs, _LOCAL[0])
        interfaces = [_subif("Ethernet1/13.1000"), _subif("Ethernet1/13.1100")]
        peers = _peer_prefixes(legs, interfaces)
        with caplog.at_level(logging.WARNING):
            get_external_session_policies(interfaces, peers, _ONPREM, cloud_legs=by_interface)
        assert "registered by more than one owner (C001, C002" in caplog.text

    def test_the_primary_and_secondary_peering_of_one_gateway_are_one_attachment_point(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Both ExpressRoute links land on one gateway: the same block behind both is no conflict."""
        legs = _legs(
            peering("pri", "er", ends=(("eg-fr201", "Ethernet1/13.1000"),), on="ergw"),
            peering("sec", "er", ends=(("eg-fr201", "Ethernet1/13.1100"),), on="ergw"),
            attachments=(attachment("a", "ergw", network="v", blocks=(prefix("10.10.0.0/16"),)),),
        )
        by_interface = legs_by_interface(legs, _LOCAL[0])
        interfaces = [_subif("Ethernet1/13.1000"), _subif("Ethernet1/13.1100")]
        peers = _peer_prefixes(legs, interfaces)
        with caplog.at_level(logging.WARNING):
            policies = get_external_session_policies(interfaces, peers, _ONPREM, cloud_legs=by_interface)
        assert "more than one session" not in caplog.text
        assert policies["Ethernet1/13.1000"]["in"] == policies["Ethernet1/13.1100"]["in"] == ["10.10.0.0/16"]

    def test_a_block_behind_two_gateways_is_a_conflict(self, caplog: pytest.LogCaptureFixture) -> None:
        """Two attachment points for one owner's prefix: reachable through exactly one session is the rule."""
        legs = _legs(
            peering("vif-1", "dx-1", ends=(("eg-fr201", "Ethernet1/13.1000"),), on="gw1"),
            peering("vif-2", "dx-2", ends=(("eg-fr201", "Ethernet1/13.1100"),), on="gw2"),
            attachments=(
                attachment("a1", "gw1", network="v", blocks=(prefix("10.10.0.0/16"),)),
                attachment("a2", "gw2", network="v", blocks=(prefix("10.10.0.0/16"),)),
            ),
        )
        by_interface = legs_by_interface(legs, _LOCAL[0])
        interfaces = [_subif("Ethernet1/13.1000"), _subif("Ethernet1/13.1100")]
        with caplog.at_level(logging.WARNING):
            get_external_session_policies(
                interfaces, _peer_prefixes(legs, interfaces), _ONPREM, cloud_legs=by_interface
            )
        assert "more than one session" in caplog.text

    def test_attachment_points_group_cloud_legs_by_gateway(self) -> None:
        """registered_by_attachment: an SD-WAN gateway by name and a cloud gateway once, however many legs."""
        legs = _legs(
            peering("pri", "er", ends=(("eg-fr201", "Ethernet1/13.1000"),), on="ergw"),
            peering("sec", "er", ends=(("eg-fr201", "Ethernet1/13.1100"),), on="ergw"),
            attachments=(attachment("a", "ergw", network="v", blocks=(prefix("10.10.0.0/16"),)),),
        )
        points = registered_by_attachment(None, None, legs)
        assert list(points) == ["cloud gateway 'ergw'"]
        assert {
            (registration.customer, registration.prefix["prefix"]) for registration in points["cloud gateway 'ergw'"]
        } == {("C001", "10.10.0.0/16")}
