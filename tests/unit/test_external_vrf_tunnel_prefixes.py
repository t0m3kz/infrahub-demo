"""A tunnel's ``registered_prefixes`` are registered behind the SD-WAN gateway at one of its ends, like an office LAN."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from tests.integration.demo_data import relationship_names
from tests.unit.external_vrf_peers_helpers import _bgp_facing, _subif
from tests.unit.test_circuit_query_shapes import _QUERIES, _selected_paths
from transforms.helpers.bgp_external import (
    get_external_peer_prefixes,
    get_external_session_policies,
    registered_by_gateway,
)
from utils.data_cleaning import clean_data

_ROOT = Path(__file__).parents[2]
_GW1 = "EQX-FR2-SDWAN-GW1"
_ACME = "198.18.0.0/24"
_HANDOFF = "Ethernet1/13.1900"


def _raw_owner(org_id: str, typename: str = "OrganizationCustomer") -> dict[str, Any]:
    """An ``owner { node { __typename org_id display_label } }`` selection."""
    return {"node": {"__typename": typename, "org_id": {"value": org_id}, "display_label": f"{org_id} label"}}


def _raw_prefix(prefix: str, namespace: str = "PROD") -> dict[str, Any]:
    """A registered IpamPrefix node as the edge query selects it."""
    return {"node": {"prefix": {"value": prefix}, "ip_namespace": {"node": {"name": {"value": namespace}}}}}


def _tunnels(*raw: dict[str, Any]) -> list[dict[str, Any]]:
    """The cleaned TopologySessionCircuit root of raw edge-query nodes."""
    return clean_data({"TopologySessionCircuit": {"edges": [{"node": n} for n in raw]}})["TopologySessionCircuit"]


def _tunnel(
    owner: str | None,
    *prefixes: tuple[str, str],
    ends: tuple[str, ...] = (_GW1, "EXT-ACME-EDGE1"),
    owner_type: str = "OrganizationCustomer",
) -> dict[str, Any]:
    """A raw tunnel node: owner, the device of each end port, registered (prefix, namespace) pairs."""
    return {
        "owner": _raw_owner(owner, owner_type) if owner else {"node": None},
        "registered_prefixes": {"edges": [_raw_prefix(prefix, namespace) for prefix, namespace in prefixes]},
        "capabilities": {"edges": [{"node": {"device": {"node": {"name": {"value": device}}}}} for device in ends]},
    }


def _offices(*offices: tuple[str | None, str, tuple[str, ...]]) -> list[dict[str, Any]]:
    """The cleaned office root: (gateway, owner org_id, PROD LAN prefixes) each."""
    raw = [
        {
            "owner": _raw_owner(owner),
            "sdwan_gateway": {"node": {"name": {"value": gateway}}} if gateway else {"node": None},
            "lan_prefixes": {"edges": [_raw_prefix(lan) for lan in lans]},
            "segment_deployments": {"edges": []},
        }
        for gateway, owner, lans in offices
    ]
    return clean_data({"TopologyCustomerOffice": {"edges": [{"node": n} for n in raw]}})["TopologyCustomerOffice"]


def _peers(offices: list[dict[str, Any]], tunnels: list[dict[str, Any]] | None) -> dict[str, dict[str, list[str]]]:
    """The accepted prefixes of a PROD handoff facing the SD-WAN gateway."""
    return get_external_peer_prefixes(
        [_subif(_HANDOFF, "PROD")],
        None,
        "eg-fr201",
        device_capabilities=_bgp_facing((_HANDOFF, _GW1)),
        offices=offices,
        tunnels=tunnels,
    )


class TestRegisteredByGateway:
    """One registration source behind the gateway: the offices' prefixes and the tunnels'."""

    def test_the_tunnel_registers_behind_the_gateway_at_one_end(self) -> None:
        """The partner range joins the office LAN under the gateway, owned by the tunnel's owner."""
        found = registered_by_gateway(
            _offices((_GW1, "C002", ("10.210.2.0/24",))), _tunnels(_tunnel("C001", (_ACME, "PROD")))
        )
        assert set(found) == {_GW1}
        assert [(owner, prefix["prefix"]) for owner, prefix in found[_GW1]] == [
            ("C002", "10.210.2.0/24"),
            ("C001", _ACME),
        ]

    def test_the_partner_end_is_no_gateway(self) -> None:
        """Only the end named by an office's sdwan_gateway is a gateway: the partner router gets nothing."""
        found = registered_by_gateway(_offices((_GW1, "C002", ())), _tunnels(_tunnel("C001", (_ACME, "PROD"))))
        assert "EXT-ACME-EDGE1" not in found

    def test_a_gateway_no_office_names_is_absent(self) -> None:
        """A tunnel ending on a gateway with no office behind it registers nothing anywhere."""
        assert registered_by_gateway(_offices((None, "C002", ())), _tunnels(_tunnel("C001", (_ACME, "PROD")))) == {}

    def test_a_tunnel_without_registered_prefixes_adds_nothing(self) -> None:
        """The office's own SD-WAN tunnel is an inventory record: the gateway keeps just the office's LANs."""
        found = registered_by_gateway(_offices((_GW1, "C002", ())), _tunnels(_tunnel("C002")))
        assert found == {_GW1: []}

    def test_no_tunnels_is_the_offices_alone(self) -> None:
        """tunnels=None reads like an empty root."""
        assert registered_by_gateway(_offices((_GW1, "C002", ("10.210.2.0/24",))), None).keys() == {_GW1}


class TestGatewayHandoffAcceptsTunnelPrefixes:
    """The session facing the gateway accepts the tunnel's range from the tunnel's owner."""

    def test_the_office_lan_and_the_tunnel_range_are_accepted_per_owner(self) -> None:
        """Two owners behind one gateway: C002's LAN and C001's partner range."""
        found = _peers(_offices((_GW1, "C002", ("10.210.2.0/24",))), _tunnels(_tunnel("C001", (_ACME, "PROD"))))
        assert found == {_HANDOFF: {"C001": [_ACME], "C002": ["10.210.2.0/24"]}}

    def test_the_tunnel_owner_is_the_owner_not_the_office(self) -> None:
        """The range is owned by the tunnel's owner even when the only office behind the gateway is another's."""
        found = _peers(_offices((_GW1, "C002", ())), _tunnels(_tunnel("C001", (_ACME, "PROD"))))
        assert found == {_HANDOFF: {"C001": [_ACME]}}

    def test_a_range_in_another_namespace_is_not_accepted_by_a_prod_session(self) -> None:
        """A PROD handoff never accepts a NON-PROD (or default-namespace) registration."""
        tunnels = _tunnels(_tunnel("C001", (_ACME, "NON-PROD"), ("198.19.0.0/24", "default")))
        assert _peers(_offices((_GW1, "C002", ())), tunnels) == {_HANDOFF: {}}

    def test_a_tunnel_without_a_customer_owner_registers_nothing_acceptable(self) -> None:
        """A provider-owned or ownerless tunnel's range is never accepted."""
        provider = _tunnels(_tunnel("P035", (_ACME, "PROD"), owner_type="OrganizationProvider"))
        assert _peers(_offices((_GW1, "C002", ())), provider) == {_HANDOFF: {}}
        assert _peers(_offices((_GW1, "C002", ())), _tunnels(_tunnel(None, (_ACME, "PROD")))) == {_HANDOFF: {}}

    def test_a_tunnel_of_another_gateway_is_not_accepted(self) -> None:
        """A tunnel whose ends are not this gateway adds nothing to this session."""
        tunnels = _tunnels(_tunnel("C001", (_ACME, "PROD"), ends=("EQX-AM5-SDWAN-GW1", "EXT-ACME-EDGE1")))
        offices = _offices((_GW1, "C002", ("10.210.2.0/24",)), ("EQX-AM5-SDWAN-GW1", "C002", ()))
        assert _peers(offices, tunnels) == {_HANDOFF: {"C002": ["10.210.2.0/24"]}}

    def test_the_default_route_and_ipv6_are_dropped(self) -> None:
        """Like an office LAN, a tunnel's 0/0 or IPv6 range never reaches the filter."""
        tunnels = _tunnels(_tunnel("C001", ("0.0.0.0/0", "PROD"), ("2001:db8::/32", "PROD"), (_ACME, "PROD")))
        assert _peers(_offices((_GW1, "C002", ())), tunnels) == {_HANDOFF: {"C001": [_ACME]}}


class TestTunnelPrefixesInThePolicies:
    """The owner's tunnel range is announced to its other sessions and its other prefixes to this one."""

    def test_the_range_is_accepted_here_and_announced_to_the_owners_other_session(self) -> None:
        """C001: partner range behind the gateway session, a VPC block behind a VIF session."""
        interfaces = [_subif(_HANDOFF, "PROD"), _subif("Ethernet1/13.1000", "PROD")]
        peers = {_HANDOFF: {"C001": [_ACME]}, "Ethernet1/13.1000": {"C001": ["10.10.0.0/16"]}}
        policies = get_external_session_policies(interfaces, peers, {})
        assert policies[_HANDOFF]["in"] == [_ACME]
        assert policies[_HANDOFF]["out"] == ["10.10.0.0/16"]
        assert policies["Ethernet1/13.1000"]["in"] == ["10.10.0.0/16"]
        assert policies["Ethernet1/13.1000"]["out"] == [_ACME]

    def test_the_range_is_never_announced_to_another_owners_session(self) -> None:
        """C002's session never learns C001's partner range."""
        interfaces = [_subif(_HANDOFF, "PROD"), _subif("Ethernet1/13.1000", "PROD")]
        peers = {_HANDOFF: {"C001": [_ACME]}, "Ethernet1/13.1000": {"C002": ["10.20.0.0/16"]}}
        policies = get_external_session_policies(interfaces, peers, {})
        assert policies["Ethernet1/13.1000"]["out"] == []
        assert policies[_HANDOFF]["out"] == []


class TestEdgeQueryAndData:
    """The edge query selects the tunnel fields in the shape the helper reads; the demo registers the partner range."""

    def test_the_edge_query_selects_the_tunnel_fields(self) -> None:
        """Owner, registered prefixes with their namespace and the device of each end port."""
        selected = _selected_paths(_QUERIES / "config" / "edge.gql", "TopologySessionCircuit")
        reads = [
            "owner.org_id",
            "owner.display_label",
            "registered_prefixes.prefix",
            "registered_prefixes.ip_namespace.name",
            "capabilities.device.name",
        ]
        assert not [item for item in reads if item not in selected]
        assert {"owner", "registered_prefixes", "capabilities"} <= relationship_names("TopologySessionCircuit")

    def test_a_cleaned_response_has_the_shape_the_helper_reads(self) -> None:
        """clean_data: owner dict with typename, registered_prefixes a list of dicts, capabilities device names."""
        (tunnel,) = _tunnels(_tunnel("C001", (_ACME, "PROD")))
        assert tunnel["owner"] == {"typename": "OrganizationCustomer", "org_id": "C001", "display_label": "C001 label"}
        assert tunnel["registered_prefixes"] == [{"prefix": _ACME, "ip_namespace": {"name": "PROD"}}]
        assert [port["device"]["name"] for port in tunnel["capabilities"]] == [_GW1, "EXT-ACME-EDGE1"]

    def test_the_partner_tunnel_registers_the_acme_range_in_prod(self) -> None:
        """01_partner_tunnel.yml creates the PROD prefix and lists it on the tunnel, owned by C001."""
        path = _ROOT / "data/demos/30_all/08_interconnects/07_zone_policies/01_partner_tunnel.yml"
        documents = [doc for doc in yaml.safe_load_all(path.read_text()) if doc]
        by_kind = {doc["spec"]["kind"]: doc["spec"]["data"] for doc in documents}
        assert {"prefix": _ACME, "ip_namespace": "PROD"} == {
            key: by_kind["IpamPrefix"][0][key] for key in ("prefix", "ip_namespace")
        }
        (tunnel,) = by_kind["TopologySessionCircuit"]
        assert tunnel["owner"] == "C001"
        assert tunnel["registered_prefixes"] == [[_ACME, "PROD"]]
