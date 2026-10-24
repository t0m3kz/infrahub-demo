"""Unit tests for external eBGP peers in the tenant VRFs: session mode, peer prefixes and prefix ownership."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from tests.unit.external_vrf_peers_helpers import (
    _C2,
    _C3,
    _C5,
    _C15,
    _GW1,
    _NON_PROD,
    _PROD,
    _PROVIDER,
    _bgp_facing,
    _peering,
    _subif,
)
from transforms.helpers.bgp import _build_session_from_peering
from transforms.helpers.bgp_external import get_external_peer_prefixes
from utils.ownership import customer_owner


def _blocks(*pairs: tuple[str, str]) -> list[dict[str, Any]]:
    """Cleaned cidr_blocks: (prefix, namespace name)."""
    return [{"prefix": prefix, "ip_namespace": {"name": ns}} for prefix, ns in pairs]


def _transit_vif(*, vlan: int = 1000, device: str = "eg-fr201", attachments: list[dict[str, Any]]) -> dict[str, Any]:
    """A cleaned transit VIF on ``device`` whose hybrid attachment is associated with rt-shared."""
    return {
        "name": f"vif-transit-{vlan}-{device}",
        "vlan_id": vlan,
        "direct_connect": {"physical_devices": [{"name": device}]},
        "virtual_network": None,
        "transit_gateway_attachment": {
            "associated_route_table": {"id": "rt-shared"},
            "transit_gateway": {"attachments": attachments},
        },
    }


def _vpc_attachment(tables: list[str], *blocks: tuple[str, str], owner: dict[str, Any] | None = _C3) -> dict[str, Any]:
    """A VPC attachment propagating to ``tables``; its account is owned by ``owner``."""
    return {
        "attachment_type": "vpc",
        "propagates_to_route_tables": [{"id": table} for table in tables],
        "virtual_network": {"cidr_blocks": _blocks(*blocks), "account": {"owner": owner}},
    }


_NO_SDWAN: dict[str, Any] = {"device_capabilities": None, "offices": None, "tunnels": None}


def _office(
    gateway: str | None,
    *lans: tuple[str, str],
    hosted: tuple[tuple[str, str], ...] = (),
    owner: dict[str, Any] | None = _C2,
    segment_owner: dict[str, Any] | None = _C2,
    segment_status: str = "active",
) -> dict[str, Any]:
    """A cleaned TopologyCustomerOffice root entry: its SD-WAN gateway, (prefix, namespace) LANs and hosted segments.

    ``hosted`` are the (gateway prefix, namespace) of the segments the office hosts itself (segment_deployments),
    all owned by ``segment_owner`` with ``segment_status``; the LANs are owned by the office's ``owner``.
    """
    return {
        "owner": owner,
        "sdwan_gateway": {"name": gateway} if gateway else None,
        "lan_prefixes": _blocks(*lans),
        "segment_deployments": [
            {
                "segment": {
                    "status": segment_status,
                    "owner": segment_owner,
                    "gateway": {"ip_prefix": {"prefix": prefix, "ip_namespace": {"name": namespace}}},
                }
            }
            for prefix, namespace in hosted
        ],
    }


# ---------------------------------------------------------------------------
# Session mode
# ---------------------------------------------------------------------------


class TestExternalFlag:
    @pytest.mark.parametrize("namespace", [_PROD, _NON_PROD])
    def test_regular_peer_in_a_tenant_vrf_is_external(self, namespace: dict[str, Any]) -> None:
        """A regular (partner/provider) eBGP peer in PROD or NON-PROD gets the filtered mode."""
        session = _build_session_from_peering(_peering(namespace), "eg-fr201", {"asn": 4200000100}, [])
        assert session is not None
        assert session["vrf"] == namespace["name"]
        assert session["external"] is True
        assert "transit" not in session

    def test_internet_peer_is_transit_not_external(self) -> None:
        """The INTERNET VRF keeps the transit mode (default in, public out)."""
        namespace = {"name": "INTERNET", "namespace_type": "internet"}
        session = _build_session_from_peering(_peering(namespace), "eg-fr201", {"asn": 4200000100}, [])
        assert session is not None
        assert session["transit"] is True
        assert "external" not in session

    @pytest.mark.parametrize(
        ("namespace", "peering_role"),
        [
            (_PROD, None),
            (_PROD, "dci"),
            ({"name": "MANAGEMENT", "namespace_type": "management"}, "regular"),
            ({"name": "PROD"}, "regular"),
        ],
    )
    def test_other_vrf_sessions_are_not_external(self, namespace: dict[str, Any], peering_role: str | None) -> None:
        """No regular role, a non-tenant VRF or an unknown namespace type: rendered as before, unfiltered."""
        session = _build_session_from_peering(
            _peering(namespace, peering_role=peering_role), "eg-fr201", {"asn": 4200000100}, []
        )
        assert session is not None
        assert "external" not in session

    def test_default_namespace_is_not_a_vrf_session(self) -> None:
        """A global-table session is never external, whatever its role."""
        session = _build_session_from_peering(
            _peering({"name": "default"}), "eg-fr201", {"asn": 4200000100}, [_subif("Ethernet1/13.1000", "default")]
        )
        assert session is not None
        assert "vrf" not in session
        assert "external" not in session


# ---------------------------------------------------------------------------
# Inbound: the far end's registered prefixes
# ---------------------------------------------------------------------------


class TestExternalPeerPrefixes:
    def test_transit_vif_accepts_only_vpcs_on_its_route_table_in_its_namespace(self) -> None:
        """rt-shared VPCs in PROD only: an isolated-table VPC and a NON-PROD block are not accepted."""
        vif = _transit_vif(
            attachments=[
                _vpc_attachment(["rt-shared"], ("10.30.0.0/16", "PROD")),
                _vpc_attachment(["rt-isolated"], ("10.150.0.0/16", "PROD")),
                _vpc_attachment(["rt-shared"], ("10.60.0.0/16", "NON-PROD")),
                {"attachment_type": "hybrid", "propagates_to_route_tables": [{"id": "rt-shared"}]},
            ]
        )
        found = get_external_peer_prefixes([_subif("Ethernet1/13.1000", "PROD")], [vif], "eg-fr201", **_NO_SDWAN)
        assert found == {"Ethernet1/13.1000": {"C003": ["10.30.0.0/16"]}}

    def test_private_vif_accepts_its_own_vpc_blocks_in_its_namespace(self) -> None:
        """A NON-PROD sub-interface never accepts the PROD block of the same VPC."""
        vif = {
            "name": "vif-private",
            "vlan_id": 1001,
            "direct_connect": {"physical_devices": [{"name": "eg-fr201"}]},
            "virtual_network": {
                "cidr_blocks": _blocks(("10.60.0.0/16", "NON-PROD"), ("10.31.0.0/16", "PROD")),
                "account": {"owner": _C3},
            },
        }
        found = get_external_peer_prefixes([_subif("Ethernet1/13.1001", "NON-PROD")], [vif], "eg-fr201", **_NO_SDWAN)
        assert found == {"Ethernet1/13.1001": {"C003": ["10.60.0.0/16"]}}

    def test_default_route_and_ipv6_are_never_accepted(self) -> None:
        """A registered 0/0 (or any IPv6 block) is dropped from the inbound list."""
        vif = _transit_vif(
            attachments=[
                _vpc_attachment(
                    ["rt-shared"], ("0.0.0.0/0", "PROD"), ("2001:db8::/56", "PROD"), ("10.30.0.0/16", "PROD")
                )
            ]
        )
        found = get_external_peer_prefixes([_subif("Ethernet1/13.1000", "PROD")], [vif], "eg-fr201", **_NO_SDWAN)
        assert found == {"Ethernet1/13.1000": {"C003": ["10.30.0.0/16"]}}

    def test_vif_on_another_device_or_vlan_is_ignored(self) -> None:
        """Only this device's Direct Connects, matched on the sub-interface's dot1q tag."""
        vifs = [
            _transit_vif(device="eg-fr202", attachments=[_vpc_attachment(["rt-shared"], ("10.30.0.0/16", "PROD"))]),
            _transit_vif(vlan=1002, attachments=[_vpc_attachment(["rt-shared"], ("10.40.0.0/16", "PROD"))]),
        ]
        interfaces = [_subif("Ethernet1/13.1000", "PROD"), _subif("Ethernet1/13", "PROD"), _subif("Loopback0", None)]
        assert get_external_peer_prefixes(interfaces, vifs, "eg-fr201", **_NO_SDWAN) == {}

    def test_two_vifs_on_one_vlan_accept_nothing(self, caplog: pytest.LogCaptureFixture) -> None:
        """Ambiguous VLAN on one device: fail closed and say so."""
        vifs = [
            _transit_vif(attachments=[_vpc_attachment(["rt-shared"], ("10.30.0.0/16", "PROD"))]),
            {**_transit_vif(attachments=[]), "name": "vif-other"},
        ]
        with caplog.at_level(logging.WARNING):
            found = get_external_peer_prefixes([_subif("Ethernet1/13.1000", "PROD")], vifs, "eg-fr201", **_NO_SDWAN)
        assert found == {}
        assert "accepts nothing" in caplog.text

    def test_hybrid_attachment_without_route_table_accepts_nothing(self) -> None:
        """No associated route table: nothing is reachable over the VIF."""
        vif = _transit_vif(attachments=[_vpc_attachment(["rt-shared"], ("10.30.0.0/16", "PROD"))])
        vif["transit_gateway_attachment"]["associated_route_table"] = None
        found = get_external_peer_prefixes([_subif("Ethernet1/13.1000", "PROD")], [vif], "eg-fr201", **_NO_SDWAN)
        assert found == {"Ethernet1/13.1000": {}}

    def test_no_virtual_interfaces_is_empty(self) -> None:
        """A sub-interface with neither a VIF nor an SD-WAN gateway behind it is absent."""
        assert get_external_peer_prefixes([_subif("Ethernet1/13.1900", "PROD")], None, "eg-fr201", **_NO_SDWAN) == {}


class TestSdwanGatewayPrefixes:
    """The SD-WAN handoff accepts the LANs of the offices behind the gateway it faces."""

    def test_offices_behind_the_gateway_are_accepted_sorted(self) -> None:
        """The union of every GW1 office's PROD LAN, in address order, deduplicated."""
        offices = [
            _office(_GW1, ("10.210.5.0/24", "PROD")),
            _office(_GW1, ("10.210.2.0/24", "PROD"), ("10.210.5.0/24", "PROD")),
        ]
        found = get_external_peer_prefixes(
            [_subif("Ethernet1/13.1900", "PROD")],
            None,
            "eg-fr201",
            device_capabilities=_bgp_facing(("Ethernet1/13.1900", _GW1)),
            offices=offices,
            tunnels=None,
        )
        assert found == {"Ethernet1/13.1900": {"C002": ["10.210.2.0/24", "10.210.5.0/24"]}}

    def test_offices_of_another_gateway_or_none_are_excluded(self) -> None:
        """Only offices whose sdwan_gateway is the far end of this handoff count."""
        offices = [
            _office(_GW1, ("10.210.5.0/24", "PROD")),
            _office("EQX-AM5-SDWAN-GW1", ("10.210.9.0/24", "PROD")),
            _office(None, ("10.210.15.0/24", "PROD")),
        ]
        found = get_external_peer_prefixes(
            [_subif("Ethernet1/13.1900", "PROD")],
            None,
            "eg-fr201",
            device_capabilities=_bgp_facing(("Ethernet1/13.1900", _GW1)),
            offices=offices,
            tunnels=None,
        )
        assert found == {"Ethernet1/13.1900": {"C002": ["10.210.5.0/24"]}}

    def test_wrong_namespace_default_and_ipv6_lans_are_excluded(self) -> None:
        """A LAN registered in another namespace, a 0/0 or an IPv6 LAN never reaches the PROD filter."""
        offices = [
            _office(
                _GW1,
                ("10.210.5.0/24", "PROD"),
                ("10.211.5.0/24", "NON-PROD"),
                ("0.0.0.0/0", "PROD"),
                ("2001:db8:210::/64", "PROD"),
            )
        ]
        found = get_external_peer_prefixes(
            [_subif("Ethernet1/13.1900", "PROD")],
            None,
            "eg-fr201",
            device_capabilities=_bgp_facing(("Ethernet1/13.1900", _GW1)),
            offices=offices,
            tunnels=None,
        )
        assert found == {"Ethernet1/13.1900": {"C002": ["10.210.5.0/24"]}}

    def test_non_prod_session_never_gets_prod_lans(self) -> None:
        """A NON-PROD handoff to the same gateway accepts only NON-PROD LANs (here: none)."""
        found = get_external_peer_prefixes(
            [_subif("Ethernet1/13.1901", "NON-PROD")],
            None,
            "eg-fr201",
            device_capabilities=_bgp_facing(("Ethernet1/13.1901", _GW1)),
            offices=[_office(_GW1, ("10.210.5.0/24", "PROD"), ("10.210.2.0/24", "PROD"))],
            tunnels=None,
        )
        assert found == {"Ethernet1/13.1901": {}}

    def test_gateway_whose_offices_register_no_lan_accepts_nothing(self) -> None:
        """A known gateway with no registered LAN maps to [] (deny), not to a VIF guess."""
        found = get_external_peer_prefixes(
            [_subif("Ethernet1/13.1900", "PROD")],
            None,
            "eg-fr201",
            device_capabilities=_bgp_facing(("Ethernet1/13.1900", _GW1)),
            offices=[_office(_GW1)],
            tunnels=None,
        )
        assert found == {"Ethernet1/13.1900": {}}

    def test_segments_hosted_by_an_office_behind_the_gateway_are_accepted(self) -> None:
        """An office that hosts its LAN as a segment registers the segment's prefix, not a typed lan_prefixes entry."""
        offices = [
            _office(_GW1, hosted=(("10.176.5.0/24", "PROD"),)),
            _office(_GW1, ("10.210.2.0/24", "PROD")),
        ]
        found = get_external_peer_prefixes(
            [_subif("Ethernet1/13.1900", "PROD")],
            None,
            "eg-fr201",
            device_capabilities=_bgp_facing(("Ethernet1/13.1900", _GW1)),
            offices=offices,
            tunnels=None,
        )
        assert found == {"Ethernet1/13.1900": {"C002": ["10.176.5.0/24", "10.210.2.0/24"]}}

    def test_hosted_segments_and_lans_of_one_office_are_one_deduplicated_set(self) -> None:
        offices = [
            _office(_GW1, ("10.176.5.0/24", "PROD"), hosted=(("10.176.5.0/24", "PROD"), ("10.176.6.0/24", "PROD")))
        ]
        found = get_external_peer_prefixes(
            [_subif("Ethernet1/13.1900", "PROD")],
            None,
            "eg-fr201",
            device_capabilities=_bgp_facing(("Ethernet1/13.1900", _GW1)),
            offices=offices,
            tunnels=None,
        )
        assert found == {"Ethernet1/13.1900": {"C002": ["10.176.5.0/24", "10.176.6.0/24"]}}

    def test_hosted_segments_of_another_gateway_or_none_are_excluded(self) -> None:
        offices = [
            _office(_GW1, hosted=(("10.176.5.0/24", "PROD"),)),
            _office("EQX-AM5-SDWAN-GW1", hosted=(("10.176.9.0/24", "PROD"),)),
            _office(None, hosted=(("10.176.15.0/24", "PROD"),)),
        ]
        found = get_external_peer_prefixes(
            [_subif("Ethernet1/13.1900", "PROD")],
            None,
            "eg-fr201",
            device_capabilities=_bgp_facing(("Ethernet1/13.1900", _GW1)),
            offices=offices,
            tunnels=None,
        )
        assert found == {"Ethernet1/13.1900": {"C002": ["10.176.5.0/24"]}}

    def test_hosted_segments_in_another_namespace_default_or_ipv6_never_reach_the_filter(self) -> None:
        """A hosted NON-PROD segment, a segment without a gateway prefix and an IPv6 one are not accepted on PROD."""
        offices = [
            _office(
                _GW1,
                hosted=(
                    ("10.176.5.0/24", "PROD"),
                    ("10.184.5.0/24", "NON-PROD"),
                    ("2001:db8:176::/64", "PROD"),
                ),
            )
        ]
        offices[0]["segment_deployments"].append({"segment": {"gateway": None}})
        found = get_external_peer_prefixes(
            [_subif("Ethernet1/13.1900", "PROD"), _subif("Ethernet1/13.1901", "NON-PROD")],
            None,
            "eg-fr201",
            device_capabilities=_bgp_facing(("Ethernet1/13.1900", _GW1), ("Ethernet1/13.1901", _GW1)),
            offices=offices,
            tunnels=None,
        )
        assert found == {
            "Ethernet1/13.1900": {"C002": ["10.176.5.0/24"]},
            "Ethernet1/13.1901": {"C002": ["10.184.5.0/24"]},
        }

    def test_an_office_without_hosted_segments_keeps_its_lans(self) -> None:
        """A footprint with no segment_deployments key at all (an older query) registers its lan_prefixes alone."""
        office = {"owner": _C2, "sdwan_gateway": {"name": _GW1}, "lan_prefixes": _blocks(("10.210.2.0/24", "PROD"))}
        found = get_external_peer_prefixes(
            [_subif("Ethernet1/13.1900", "PROD")],
            None,
            "eg-fr201",
            device_capabilities=_bgp_facing(("Ethernet1/13.1900", _GW1)),
            offices=[office],
            tunnels=None,
        )
        assert found == {"Ethernet1/13.1900": {"C002": ["10.210.2.0/24"]}}

    def test_no_offices_leaves_the_handoff_absent(self) -> None:
        """No office footprint at all: the gateway is not known, so the handoff accepts nothing."""
        found = get_external_peer_prefixes(
            [_subif("Ethernet1/13.1900", "PROD")],
            None,
            "eg-fr201",
            device_capabilities=_bgp_facing(("Ethernet1/13.1900", _GW1)),
            offices=[],
            tunnels=None,
        )
        assert found == {}

    def test_handoff_facing_a_non_gateway_device_gets_no_office_lans(self) -> None:
        """Office LANs go only to the session whose far end is their gateway, not to any PROD handoff."""
        found = get_external_peer_prefixes(
            [_subif("Ethernet1/13.1900", "PROD"), _subif("Ethernet1/14.1900", "PROD")],
            None,
            "eg-fr201",
            device_capabilities=_bgp_facing(("Ethernet1/13.1900", _GW1), ("Ethernet1/14.1900", "EXT-PARTNER-01")),
            offices=[_office(_GW1, ("10.210.5.0/24", "PROD"))],
            tunnels=None,
        )
        assert found == {"Ethernet1/13.1900": {"C002": ["10.210.5.0/24"]}}

    def test_handoff_with_several_far_ends_is_not_resolved_to_a_gateway(self) -> None:
        """Ambiguous far end on one sub-interface: fail closed rather than trust one of them."""
        found = get_external_peer_prefixes(
            [_subif("Ethernet1/13.1900", "PROD")],
            None,
            "eg-fr201",
            device_capabilities=_bgp_facing(("Ethernet1/13.1900", _GW1), ("Ethernet1/13.1900", "EXT-PARTNER-01")),
            offices=[_office(_GW1, ("10.210.5.0/24", "PROD"))],
            tunnels=None,
        )
        assert found == {}

    def test_malformed_peering_raises(self) -> None:
        """A peering without exactly two ends is refused, as when the session itself is built."""
        capabilities = _bgp_facing(("Ethernet1/13.1900", _GW1))
        capabilities[1]["peerings"][0]["capabilities"].pop()
        with pytest.raises(ValueError, match="expected 2 capabilities"):
            get_external_peer_prefixes(
                [_subif("Ethernet1/13.1900", "PROD")],
                None,
                "eg-fr201",
                device_capabilities=capabilities,
                offices=[_office(_GW1, ("10.210.5.0/24", "PROD"))],
                tunnels=None,
            )


# ---------------------------------------------------------------------------
# Ownership: who registered the prefix
# ---------------------------------------------------------------------------


class TestCustomerOwner:
    def test_only_a_customer_counts(self) -> None:
        """org_id first, display_label when it is empty; a provider, no owner or a malformed one is no customer."""
        assert customer_owner(_C3) == "C003"
        assert customer_owner({"typename": "OrganizationCustomer", "org_id": None, "display_label": "Acme"}) == "Acme"
        assert customer_owner({"typename": "OrganizationCustomer", "org_id": "", "display_label": ""}) is None
        assert customer_owner(_PROVIDER) is None
        assert customer_owner(None) is None
        assert customer_owner("C003") is None

    @pytest.mark.parametrize(
        ("owner", "expected"),
        [
            ({"typename": "OrganizationCustomer", "display_label": "Acme"}, "Acme"),
            ({"typename": "OrganizationCustomer", "org_id": "C009"}, "C009"),
            ({"typename": "OrganizationCustomer"}, None),
            ({"typename": "OrganizationCustomer", "org_id": None, "display_label": None}, None),
            ({"org_id": "C003", "display_label": "C003 label"}, None),
            ({"typename": "OrganizationProvider", "org_id": "P027"}, None),
            ({"typename": "OrganizationInternal", "org_id": "I001"}, None),
            ({}, None),
            (["OrganizationCustomer"], None),
            (3, None),
        ],
        ids=[
            "label-when-org_id-key-missing",
            "org_id-only",
            "customer-with-no-key-at-all",
            "customer-with-null-keys",
            "no-typename",
            "provider",
            "internal",
            "empty-dict",
            "list",
            "int",
        ],
    )
    def test_owner_key_for_malformed_or_foreign_owners(self, owner: Any, expected: str | None) -> None:
        """Missing keys fall back (org_id, display_label, None); anything that is not a customer dict is None."""
        assert customer_owner(owner) == expected


class TestPeerPrefixOwners:
    """get_external_peer_prefixes attributes each registered prefix to the object that registered it."""

    def test_vpc_blocks_take_the_owner_of_their_own_vpcs_account(self) -> None:
        """Two VPCs of two customers behind one transit VIF: each owner keeps its own blocks."""
        vif = _transit_vif(
            attachments=[
                _vpc_attachment(["rt-shared"], ("10.30.0.0/16", "PROD"), owner=_C3),
                _vpc_attachment(["rt-shared"], ("10.32.0.0/16", "PROD"), ("10.31.0.0/16", "PROD"), owner=_C15),
                _vpc_attachment(["rt-isolated"], ("10.150.0.0/16", "PROD"), owner=_C3),
            ]
        )
        found = get_external_peer_prefixes([_subif("Ethernet1/13.1000", "PROD")], [vif], "eg-fr201", **_NO_SDWAN)
        assert found == {"Ethernet1/13.1000": {"C003": ["10.30.0.0/16"], "C015": ["10.31.0.0/16", "10.32.0.0/16"]}}

    @pytest.mark.parametrize("owner", [_PROVIDER, None], ids=["provider", "no-owner"])
    def test_a_vpc_block_without_a_customer_owner_is_never_accepted(self, owner: dict[str, Any] | None) -> None:
        """A provider-owned or ownerless account registers a prefix nobody may announce to the far end."""
        vif = _transit_vif(
            attachments=[
                _vpc_attachment(["rt-shared"], ("10.33.0.0/16", "PROD"), owner=owner),
                _vpc_attachment(["rt-shared"], ("10.30.0.0/16", "PROD")),
            ]
        )
        found = get_external_peer_prefixes([_subif("Ethernet1/13.1000", "PROD")], [vif], "eg-fr201", **_NO_SDWAN)
        assert found == {"Ethernet1/13.1000": {"C003": ["10.30.0.0/16"]}}

    def test_a_vpc_without_an_account_is_never_accepted(self) -> None:
        """A virtual network whose account (or its owner) is null registers nothing acceptable."""
        vif = _transit_vif(attachments=[_vpc_attachment(["rt-shared"], ("10.30.0.0/16", "PROD"))])
        vif["transit_gateway_attachment"]["transit_gateway"]["attachments"][0]["virtual_network"]["account"] = None
        found = get_external_peer_prefixes([_subif("Ethernet1/13.1000", "PROD")], [vif], "eg-fr201", **_NO_SDWAN)
        assert found == {"Ethernet1/13.1000": {}}

    def test_private_vif_blocks_take_the_owner_of_its_vpc(self) -> None:
        """A private VIF reads vif.virtual_network.account.owner."""
        vif = {
            "name": "vif-private",
            "vlan_id": 1001,
            "direct_connect": {"physical_devices": [{"name": "eg-fr201"}]},
            "virtual_network": {"cidr_blocks": _blocks(("10.60.0.0/16", "NON-PROD")), "account": {"owner": _C15}},
        }
        found = get_external_peer_prefixes([_subif("Ethernet1/13.1001", "NON-PROD")], [vif], "eg-fr201", **_NO_SDWAN)
        assert found == {"Ethernet1/13.1001": {"C015": ["10.60.0.0/16"]}}

    def test_office_lans_take_the_office_owner_and_hosted_segments_their_own(self) -> None:
        """C002's LAN, C005's hosted office-lan segment: one sub-interface, two owners."""
        offices = [
            _office(_GW1, ("10.210.2.0/24", "PROD"), owner=_C2),
            _office(_GW1, hosted=(("10.176.5.0/24", "PROD"),), owner=_C5, segment_owner=_C5),
        ]
        found = get_external_peer_prefixes(
            [_subif("Ethernet1/13.1900", "PROD")],
            None,
            "eg-fr201",
            device_capabilities=_bgp_facing(("Ethernet1/13.1900", _GW1)),
            offices=offices,
            tunnels=None,
        )
        assert found == {"Ethernet1/13.1900": {"C002": ["10.210.2.0/24"], "C005": ["10.176.5.0/24"]}}

    def test_a_hosted_segment_is_owned_by_the_segment_not_by_the_office(self) -> None:
        """An office of C002 hosting a segment of C005: the prefix is C005's."""
        found = get_external_peer_prefixes(
            [_subif("Ethernet1/13.1900", "PROD")],
            None,
            "eg-fr201",
            device_capabilities=_bgp_facing(("Ethernet1/13.1900", _GW1)),
            offices=[_office(_GW1, hosted=(("10.176.5.0/24", "PROD"),), owner=_C2, segment_owner=_C5)],
            tunnels=None,
        )
        assert found == {"Ethernet1/13.1900": {"C005": ["10.176.5.0/24"]}}

    def test_prefixes_without_a_customer_owner_are_never_accepted(self) -> None:
        """A provider-owned office LAN, an ownerless office and a provider-owned hosted segment register nothing."""
        offices = [
            _office(_GW1, ("10.210.7.0/24", "PROD"), owner=_PROVIDER),
            _office(_GW1, ("10.210.8.0/24", "PROD"), owner=None),
            _office(_GW1, hosted=(("10.176.7.0/24", "PROD"),), owner=_C2, segment_owner=_PROVIDER),
            _office(_GW1, hosted=(("10.176.8.0/24", "PROD"),), owner=_C2, segment_owner=None),
        ]
        found = get_external_peer_prefixes(
            [_subif("Ethernet1/13.1900", "PROD")],
            None,
            "eg-fr201",
            device_capabilities=_bgp_facing(("Ethernet1/13.1900", _GW1)),
            offices=offices,
            tunnels=None,
        )
        assert found == {"Ethernet1/13.1900": {}}

    def test_a_retired_hosted_segment_is_skipped(self) -> None:
        """A retired segment's prefix is no longer served: not accepted, while the active sibling is."""
        offices = [
            _office(_GW1, hosted=(("10.176.6.0/24", "PROD"),), segment_status="retired"),
            _office(_GW1, hosted=(("10.176.5.0/24", "PROD"),)),
        ]
        found = get_external_peer_prefixes(
            [_subif("Ethernet1/13.1900", "PROD")],
            None,
            "eg-fr201",
            device_capabilities=_bgp_facing(("Ethernet1/13.1900", _GW1)),
            offices=offices,
            tunnels=None,
        )
        assert found == {"Ethernet1/13.1900": {"C002": ["10.176.5.0/24"]}}
