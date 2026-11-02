"""CheckCloudConnection: rules R1 to R9 on canned query responses, and its registration."""

from __future__ import annotations

from typing import Any, cast

import yaml

from checks.cloud_connection import CheckCloudConnection
from tests.integration.demo_data import ROOT


def _v(value: Any) -> dict[str, Any]:
    return {"value": value}


def _node(node_id: str | None, **fields: Any) -> dict[str, Any]:
    """A relationship to one object, ``{"node": None}`` for no object."""
    if node_id is None:
        return {"node": None}
    return {"node": {"id": node_id, "name": _v(node_id), **fields}}


def _edges(*nodes: dict[str, Any]) -> dict[str, Any]:
    return {"edges": [{"node": node} for node in nodes]}


def _gateway(gateway_id: str, gateway_type: str = "hub") -> dict[str, Any]:
    return _node(gateway_id, gateway_type=_v(gateway_type))


def _table(table_id: str, gateway: str = "tgw", gateway_type: str = "hub") -> dict[str, Any]:
    return {"id": table_id, "name": _v(table_id), "gateway": _gateway(gateway, gateway_type)}


def _attachment(name: str = "att", **overrides: Any) -> dict[str, Any]:
    attachment: dict[str, Any] = {
        "id": name,
        "name": _v(name),
        "gateway": _gateway("tgw"),
        "virtual_network": _node("vnet"),
        "peer_gateway": _node(None),
        "associated_route_table": _node(None),
        "propagates_to_route_tables": _edges(),
    }
    attachment.update(overrides)
    return attachment


def _transport(name: str, connection_type: str = "physical", rides_on: str | None = None, **overrides: Any) -> dict:
    """A well-placed Direct Connect (account and region), the fields the rules read."""
    transport = {
        "__typename": "CloudDirectConnect",
        "id": name,
        "name": _v(name),
        "connection_type": _v(connection_type),
        "partner_account": _node(None),
        "rides_on": _node(rides_on, connection_type=_v("physical")) if rides_on else _node(None),
        "account": _node("acct"),
        "owner": _node(None),
        "region": _node("region"),
        "saas_region": _node(None),
    }
    transport.update(overrides)
    return transport


def _vpn(name: str = "vpn", **overrides: Any) -> dict[str, Any]:
    """A well-placed VPN connection."""
    vpn = {
        "__typename": "CloudVPNConnection",
        "id": name,
        "name": _v(name),
        "account": _node("acct"),
        "owner": _node(None),
        "region": _node("region"),
        "saas_region": _node(None),
    }
    vpn.update(overrides)
    return vpn


def _saas_transport(name: str = "saas", **overrides: Any) -> dict[str, Any]:
    """A SaaS Direct Connect: a SaaS region and an owner, no account."""
    fields: dict[str, Any] = {
        "account": _node(None),
        "region": _node(None),
        "saas_region": _node("webex"),
        "owner": _node("c900"),
    }
    fields.update(overrides)
    transport = _transport(name)
    transport.update(fields)
    return transport


def _port(name: str, kind: str = "DcimPhysicalInterface", device: str | None = "dx") -> dict[str, Any]:
    """The parent_interface of a peering: an interface of ``device``."""
    return _node(name, __typename=kind, device=_node(device))


def _peering(
    name: str = "p1", device: str = "dx", kind: str = "CloudDirectConnect", **overrides: Any
) -> dict[str, Any]:
    peering = {
        "id": name,
        "name": _v(name),
        "scope": _v("private"),
        "vlan_id": _v(None),
        "tunnel_index": _v(None),
        "endpoint_ip": _v(None),
        "tunnel_mode": _v(None),
        "crypto_profile": _node(None),
        "tunnel_key": _node(None),
        "device": _node(device, __typename=kind),
        "parent_interface": _node(None),
    }
    peering.update(overrides)
    return peering


def _run(**kinds: list[dict[str, Any]]) -> list[str]:
    """Validate canned responses (kind -> nodes) and return the error messages."""
    check = cast(Any, CheckCloudConnection.__new__(CheckCloudConnection))
    errors: list[dict[str, Any]] = []
    check.log_error = lambda **kwargs: errors.append(kwargs)
    check.validate({kind: _edges(*nodes) for kind, nodes in kinds.items()})
    for error in errors:
        assert error["object_id"] and error["object_type"]
    return [error["message"] for error in errors]


class TestR1AttachmentTarget:
    """An attachment has exactly one target."""

    def test_a_network_target_passes(self) -> None:
        """A virtual network alone is a target."""
        assert _run(CloudGatewayAttachment=[_attachment()]) == []

    def test_a_gateway_target_passes(self) -> None:
        """A peer gateway alone is a target."""
        attachment = _attachment(virtual_network=_node(None), peer_gateway=_gateway("dxgw", "edge"))
        assert _run(CloudGatewayAttachment=[attachment]) == []

    def test_no_target_fails(self) -> None:
        """The server accepts an attachment with no target; the check does not."""
        errors = _run(CloudGatewayAttachment=[_attachment(virtual_network=_node(None))])
        assert len(errors) == 1 and "R1" in errors[0] and "none" in errors[0]

    def test_two_targets_fail(self) -> None:
        """A network and a gateway together are two targets."""
        errors = _run(CloudGatewayAttachment=[_attachment(peer_gateway=_gateway("dxgw", "edge"))])
        assert len(errors) == 1 and "virtual_network and peer_gateway" in errors[0]

    def test_a_gateway_attached_to_itself_fails(self) -> None:
        """The peer gateway is not the attachment's own gateway."""
        attachment = _attachment(virtual_network=_node(None), peer_gateway=_gateway("tgw"))
        errors = _run(CloudGatewayAttachment=[attachment])
        assert len(errors) == 1 and "own gateway" in errors[0]


class TestR2RouteTables:
    """Route tables belong to a hub, and an attachment uses its own gateway's."""

    def test_a_hub_table_used_by_its_gateway_passes(self) -> None:
        """The table is the hub's and the attachment lands on that hub."""
        table = _table("rt")
        attachment = _attachment(associated_route_table={"node": table}, propagates_to_route_tables=_edges(table))
        assert _run(CloudGatewayAttachment=[attachment], CloudGatewayRouteTable=[table]) == []

    def test_a_table_of_an_edge_gateway_fails(self) -> None:
        """Only a hub has route tables."""
        errors = _run(CloudGatewayRouteTable=[_table("rt", "dxgw", "edge")])
        assert len(errors) == 1 and "only a hub" in errors[0]

    def test_a_table_of_another_gateway_fails(self) -> None:
        """An attachment on one hub associated with another hub's table."""
        table = _table("rt", "other-tgw")
        errors = _run(CloudGatewayAttachment=[_attachment(associated_route_table={"node": table})])
        assert len(errors) == 1 and "another gateway" in errors[0]

    def test_a_propagation_into_another_gateways_table_fails(self) -> None:
        """Propagation is checked like association."""
        errors = _run(CloudGatewayAttachment=[_attachment(propagates_to_route_tables=_edges(_table("rt", "other")))])
        assert len(errors) == 1 and "another gateway" in errors[0]


class TestR3Transports:
    """rides_on: no loop, physical on physical, a logical circuit on a transport of ours has no partner."""

    def test_a_logical_circuit_riding_on_a_physical_transport_passes(self) -> None:
        """The circuit and the transport that holds its port."""
        assert _run(CloudDirectConnect=[_transport("port"), _transport("vc", "logical", rides_on="port")]) == []

    def test_riding_on_itself_fails(self) -> None:
        """The server accepts it (measured); the check does not."""
        errors = _run(CloudDirectConnect=[_transport("m1", rides_on="m1")])
        assert len(errors) == 1 and "rides on itself" in errors[0]

    def test_a_loop_of_two_fails(self) -> None:
        """a rides on b and b rides on a."""
        errors = _run(CloudDirectConnect=[_transport("a", rides_on="b"), _transport("b", rides_on="a")])
        assert len(errors) == 2 and all("loop" in error for error in errors)

    def test_a_physical_transport_on_a_logical_circuit_fails(self) -> None:
        """A port cannot ride on a circuit."""
        rider = _transport("m1", rides_on="circuit")
        rider["rides_on"] = _node("circuit", connection_type=_v("logical"))
        errors = _run(CloudDirectConnect=[rider, _transport("circuit", "logical")])
        assert len(errors) == 1 and "physical but rides on the logical" in errors[0]

    def test_a_logical_circuit_on_our_transport_with_a_partner_fails(self) -> None:
        """A partner's connection rides on the partner's port, not on ours."""
        circuit = _transport("vc", "logical", rides_on="port", partner_account=_node("partner"))
        errors = _run(CloudDirectConnect=[circuit, _transport("port")])
        assert len(errors) == 1 and "partner_account" in errors[0]

    def test_a_logical_circuit_with_a_partner_and_no_rides_on_passes(self) -> None:
        """The partner-hosted case: a partner account and nothing to ride on."""
        assert _run(CloudDirectConnect=[_transport("vc", "logical", partner_account=_node("partner"))]) == []


class TestR4ServiceEndpointsAndKeys:
    """A service endpoint's segments belong to its network; a tunnel key needs IPsec."""

    @staticmethod
    def _endpoint(segment_network: str) -> dict[str, Any]:
        segment = {"id": "s1", "name": _v("s1"), "virtual_network": {"node": {"id": segment_network}}}
        return {"id": "ep", "name": _v("ep"), "virtual_network": _node("vnet"), "segments": _edges(segment)}

    def test_a_segment_of_the_network_passes(self) -> None:
        """The endpoint and its subnet are in one network."""
        assert _run(CloudServiceEndpoint=[self._endpoint("vnet")]) == []

    def test_a_segment_of_another_network_fails(self) -> None:
        """The endpoint lists a subnet of a different network."""
        errors = _run(CloudServiceEndpoint=[self._endpoint("other")])
        assert len(errors) == 1 and "R4" in errors[0]

    def test_a_tunnel_key_on_a_gre_tunnel_fails(self) -> None:
        """GRE has no pre-shared key."""
        peering = _peering(tunnel_mode=_v("gre"), tunnel_key=_node("key"), endpoint_ip=_v("203.0.113.1/32"))
        errors = _run(CloudDirectConnect=[_transport("dx")], CloudPeeringInterface=[peering])
        assert len(errors) == 1 and "tunnel_key" in errors[0]


class TestR5CryptoProfile:
    """A crypto profile belongs to an IPsec tunnel."""

    def test_a_profile_on_an_ipsec_tunnel_passes(self) -> None:
        """IPsec with a profile and a key."""
        peering = _peering(
            tunnel_mode=_v("ipsec"),
            crypto_profile=_node("prof"),
            tunnel_key=_node("key"),
            endpoint_ip=_v("203.0.113.1/32"),
        )
        assert _run(CloudDirectConnect=[_transport("dx")], CloudPeeringInterface=[peering]) == []

    def test_a_profile_without_ipsec_fails(self) -> None:
        """A profile on a GRE tunnel, or on a peering with no tunnel mode."""
        for mode in ("gre", None):
            errors = _run(
                CloudDirectConnect=[_transport("dx")],
                CloudPeeringInterface=[_peering(tunnel_mode=_v(mode), crypto_profile=_node("prof"))],
            )
            assert len(errors) == 1 and "R5" in errors[0]

    def test_an_ipsec_tunnel_without_a_profile_fails(self) -> None:
        """IPsec needs the IKE/ESP profile, with or without a key."""
        errors = _run(
            CloudDirectConnect=[_transport("dx")],
            CloudPeeringInterface=[_peering(tunnel_mode=_v("ipsec"), tunnel_key=_node("key"))],
        )
        assert len(errors) == 1 and "R5" in errors[0] and "no crypto_profile" in errors[0]

    def test_a_gre_tunnel_without_a_profile_passes(self) -> None:
        """GRE has no crypto profile; no mode and no profile is legal too (a SaaS tunnel of unknown mode)."""
        for mode in ("gre", None):
            assert (
                _run(CloudDirectConnect=[_transport("dx")], CloudPeeringInterface=[_peering(tunnel_mode=_v(mode))])
                == []
            )

    def test_a_tunnel_key_without_ipsec_still_fails_once(self) -> None:
        """R4 owns the key rule: a GRE tunnel with a key reports R4 only, R5 stays silent."""
        errors = _run(
            CloudDirectConnect=[_transport("dx")],
            CloudPeeringInterface=[_peering(tunnel_mode=_v("gre"), tunnel_key=_node("key"))],
        )
        assert len(errors) == 1 and "R4" in errors[0]


class TestR6Tunnels:
    """A VPN tunnel has the provider's endpoint and is an interface of a VPN connection."""

    @staticmethod
    def _tunnel(**overrides: Any) -> dict[str, Any]:
        return _peering("Tunnel1", device="vpn", kind="CloudVPNConnection", **overrides)

    def test_a_tunnel_on_a_vpn_connection_passes(self) -> None:
        """Index, endpoint and a VPN connection."""
        tunnel = self._tunnel(tunnel_index=_v(1), endpoint_ip=_v("203.0.113.1/32"))
        assert _run(CloudVPNConnection=[_vpn()], CloudPeeringInterface=[tunnel]) == []

    def test_a_tunnel_without_an_endpoint_fails(self) -> None:
        """No provider address."""
        errors = _run(CloudVPNConnection=[_vpn()], CloudPeeringInterface=[self._tunnel(tunnel_index=_v(1))])
        assert len(errors) == 1 and "endpoint_ip" in errors[0]

    def test_a_peering_on_a_vpn_connection_without_an_index_needs_an_endpoint(self) -> None:
        """Being an interface of a VPN connection makes it a tunnel."""
        errors = _run(CloudVPNConnection=[_vpn()], CloudPeeringInterface=[self._tunnel()])
        assert len(errors) == 1 and "endpoint_ip" in errors[0]

    def test_a_tunnel_on_a_direct_connect_fails(self) -> None:
        """A tunnel index on an interface of a transport that is no VPN connection."""
        tunnel = _peering("t", tunnel_index=_v(1), endpoint_ip=_v("203.0.113.1/32"))
        errors = _run(CloudDirectConnect=[_transport("dx")], CloudPeeringInterface=[tunnel])
        assert len(errors) == 1 and "not of a VPN connection" in errors[0]


class TestParentsOfAPeering:
    """The parent of a peering: R7 is retired (interface_parents holds the same-device rule); R8 keeps port or LAG."""

    def test_a_peering_riding_on_a_port_of_its_transport_passes(self) -> None:
        """The parent is on the peering's own device."""
        peering = _peering(parent_interface=_port("dxcon-1a"), vlan_id=_v(1000))
        assert _run(CloudDirectConnect=[_transport("dx")], CloudPeeringInterface=[peering]) == []

    def test_the_other_device_rule_is_not_this_checks_business_any_more(self) -> None:
        """A peering on a port of another device passes here; checks/interface_parents.py reports it."""
        peering = _peering(parent_interface=_port("partner-port", device="partner"), vlan_id=_v(200))
        assert _run(CloudDirectConnect=[_transport("dx")], CloudPeeringInterface=[peering]) == []


class TestR8VlanPeerings:
    """A VLAN peering rides on a port or LAG; the VLANs of one parent are distinct."""

    def test_a_vlan_peering_on_a_lag_passes(self) -> None:
        """Two VIFs on one LAG with distinct VLANs."""
        peerings = [
            _peering("a", parent_interface=_port("lag1", "DcimLAGInterface"), vlan_id=_v(1000)),
            _peering("b", parent_interface=_port("lag1", "DcimLAGInterface"), vlan_id=_v(1100)),
        ]
        assert _run(CloudDirectConnect=[_transport("dx")], CloudPeeringInterface=peerings) == []

    def test_a_routed_port_peering_without_a_vlan_passes(self) -> None:
        """A parent port and no VLAN: a routed port."""
        peering = _peering(parent_interface=_port("ic-dedicated-port"))
        assert _run(CloudDirectConnect=[_transport("dx")], CloudPeeringInterface=[peering]) == []

    def test_a_vlan_without_a_parent_fails(self) -> None:
        """A VLAN has to ride on something."""
        errors = _run(CloudDirectConnect=[_transport("dx")], CloudPeeringInterface=[_peering(vlan_id=_v(300))])
        assert len(errors) == 1 and "R8" in errors[0] and "no parent_interface" in errors[0]

    def test_a_parent_that_is_no_port_or_lag_fails(self) -> None:
        """A peering on a peering."""
        peering = _peering(parent_interface=_port("other", "CloudPeeringInterface"), vlan_id=_v(7))
        errors = _run(CloudDirectConnect=[_transport("dx")], CloudPeeringInterface=[peering])
        assert len(errors) == 1 and "port or a LAG" in errors[0]

    def test_a_duplicate_vlan_on_one_parent_fails(self) -> None:
        """Two VIFs with VLAN 1000 on the same LAG."""
        peerings = [
            _peering("a", parent_interface=_port("lag1", "DcimLAGInterface"), vlan_id=_v(1000)),
            _peering("b", parent_interface=_port("lag1", "DcimLAGInterface"), vlan_id=_v(1000)),
        ]
        errors = _run(CloudDirectConnect=[_transport("dx")], CloudPeeringInterface=peerings)
        assert len(errors) == 1 and "like 'a'" in errors[0]

    def test_the_same_vlan_on_two_parents_passes(self) -> None:
        """ExpressRoute: VLAN 200 on the primary and on the secondary port."""
        peerings = [
            _peering("pri", parent_interface=_port("msee-pri"), vlan_id=_v(200)),
            _peering("sec", parent_interface=_port("msee-sec"), vlan_id=_v(200)),
        ]
        assert _run(CloudDirectConnect=[_transport("dx")], CloudPeeringInterface=peerings) == []


class TestR9Placement:
    """A transport is in a region or a SaaS region, with an account or an owner."""

    def test_a_cloud_transport_with_account_and_region_passes(self) -> None:
        """The usual case, for both kinds."""
        assert _run(CloudDirectConnect=[_transport("dx")], CloudVPNConnection=[_vpn()]) == []

    def test_a_saas_transport_passes(self) -> None:
        """A SaaS region and an owner, no account."""
        assert _run(CloudDirectConnect=[_saas_transport()]) == []

    def test_no_place_fails(self) -> None:
        """Neither region nor SaaS region."""
        errors = _run(CloudDirectConnect=[_transport("dx", region=_node(None))])
        assert len(errors) == 1 and "exactly one of region, saas_region" in errors[0] and "none" in errors[0]

    def test_both_places_fail(self) -> None:
        """A region and a SaaS region together."""
        errors = _run(CloudDirectConnect=[_transport("dx", saas_region=_node("webex"))])
        assert len(errors) == 1 and "region and saas_region" in errors[0]

    def test_an_owner_next_to_an_account_fails(self) -> None:
        """The owner is for a SaaS where there is no account."""
        errors = _run(CloudVPNConnection=[_vpn(owner=_node("c900"))])
        assert len(errors) == 1 and "account and an owner" in errors[0]

    def test_no_account_and_no_owner_fails(self) -> None:
        """A SaaS transport without its owner."""
        errors = _run(CloudDirectConnect=[_saas_transport(owner=_node(None))])
        assert len(errors) == 1 and "saas_region and an owner" in errors[0]

    def test_no_account_in_a_cloud_region_fails(self) -> None:
        """An account-less transport needs a SaaS region, not a cloud region."""
        errors = _run(CloudDirectConnect=[_transport("dx", account=_node(None), owner=_node("c900"))])
        assert len(errors) == 1 and "saas_region" in errors[0]

    def test_a_saas_peering_on_a_saas_transport_passes(self) -> None:
        """Scope saas where the transport has a SaaS region."""
        peering = _peering(device="saas", scope=_v("saas"))
        assert _run(CloudDirectConnect=[_saas_transport()], CloudPeeringInterface=[peering]) == []

    def test_a_saas_peering_on_a_cloud_transport_fails(self) -> None:
        """Scope saas needs the SaaS region."""
        errors = _run(CloudDirectConnect=[_transport("dx")], CloudPeeringInterface=[_peering(scope=_v("saas"))])
        assert len(errors) == 1 and "no saas_region" in errors[0]

    def test_a_peering_on_a_device_that_is_no_transport_fails(self) -> None:
        """The schema lets a peering sit on any DcimDevice."""
        errors = _run(CloudPeeringInterface=[_peering(device="leaf-1")])
        assert len(errors) == 1 and "not of a transport" in errors[0]


def test_the_check_is_registered_with_its_query_and_a_smoke_entry() -> None:
    """.infrahub.yml names the class, the file and the query; the smoke file lists the query."""
    config = yaml.safe_load((ROOT / ".infrahub.yml").read_text())
    checks = {c["name"]: c for c in config["check_definitions"]}
    queries = {q["name"]: q["file_path"] for q in config["queries"]}
    smoke = yaml.safe_load((ROOT / "tests/smoke/test_graphql.yml").read_text())
    smoked = {item["resource_name"] for item in smoke["tests"]} if "tests" in smoke else set()

    assert checks["validate_cloud_connection"]["class_name"] == "CheckCloudConnection"
    assert checks["validate_cloud_connection"]["file_path"] == "checks/cloud_connection.py"
    assert CheckCloudConnection.query == "cloud_connection"
    assert (ROOT / queries["cloud_connection"]).is_file()
    assert "cloud_connection" in smoked or "cloud_connection" in (ROOT / "tests/smoke/test_graphql.yml").read_text()
