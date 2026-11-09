"""Unit tests for CheckRoutingInvariants, fed canned routing_invariants query responses."""

from __future__ import annotations

from typing import Any

import pytest

from tests.integration.demo_data import relationship_names
from tests.unit.cloud_leg_helpers import attachment, peering
from tests.unit.external_vrf_peers_helpers import _raw_gateway, _raw_vrf
from tests.unit.routing_invariants_helpers import (
    _CUSTOMER,
    _PROVIDER,
    _block,
    _gateway_office,
    _ns,
    _office,
    _onprem,
    _run,
    _subnet,
    _v,
    _vpc,
)
from tests.unit.test_circuit_query_shapes import _QUERIES, _selected_paths
from utils.data_cleaning import clean_data
from utils.routing_domain import RoutingDomainError


def _interface(device_role: str, role: str | None, ns_type: str = "internet") -> dict[str, Any]:
    return {
        "vrf": {"node": {"id": "vrf-1", "security_zone": {"node": _ns("INTERNET", ns_type)}}},
        "__typename": "DcimPhysicalInterface",
        "id": "if-1",
        "name": _v("Ethernet1/14"),
        "role": _v(role),
        "device": {"node": {"id": "d1", "name": _v("dev-1"), "role": _v(device_role)}},
        "ip_address": {
            "node": {"address": _v("203.0.113.1/31"), "ip_namespace": {"node": {"name": {"value": "default"}}}}
        },
    }


def _attachment(
    name: str,
    att_type: str,
    env: str | None,
    associated: str,
    propagates: list[str],
) -> dict[str, Any]:
    account = {"node": {"id": f"acc-{name}", "name": _v(f"acc-{name}"), "environment": _v(env)}}
    return {
        "id": f"att-{name}",
        "name": _v(name),
        "owner_account": account,
        "virtual_network": {"node": {"id": f"vn-{name}", "name": _v(f"vn-{name}"), "account": account}}
        if att_type == "vpc"
        else {"node": None},
        "peer_gateway": {"node": {"id": f"gw-{name}"}} if att_type != "vpc" else {"node": None},
        "associated_route_table": {"node": {"id": f"rt-{associated}", "name": _v(associated)}},
        "propagates_to_route_tables": {"edges": [{"node": {"id": f"rt-{rt}", "name": _v(rt)}} for rt in propagates]},
    }


def _lan_office(name: str, lan: dict[str, Any]) -> dict[str, Any]:
    """An office behind an SD-WAN gateway registering one PROD LAN (an office with no gateway registers nothing)."""
    return _gateway_office(name, "GW1", _CUSTOMER, lan)


def _routed_vpc(name: str, env: str | None, *blocks: dict[str, Any], zone: str = "PROD") -> dict[str, list[Any]]:
    """A VPC whose blocks are registered behind a cloud session of the VRF of ``zone``: the zone of a block is its
    registrant's, so the check only sees a block through the leg that registers it."""
    gateway = f"gw-{name}"
    return {
        "CloudVirtualNetwork": [_vpc(name, env, *blocks)],
        "CloudPeeringInterface": [peering(f"vif-{name}", f"dx-{name}", on=gateway, zone=zone)],
        "CloudGatewayAttachment": [attachment(f"att-{name}", gateway, network=name, blocks=blocks)],
    }


def _sections(*parts: dict[str, list[Any]], **extra: list[Any]) -> dict[str, list[Any]]:
    """Merge query sections by key (lists concatenated)."""
    merged: dict[str, list[Any]] = {}
    for part in (*parts, extra):
        for key, nodes in part.items():
            merged.setdefault(key, []).extend(nodes)
    return merged


class TestL3VniSharing:
    """Namespaces of different types never share an l3_vni."""

    def test_shared_l3_vni_across_types_fails(self) -> None:
        """Namespaces of different types sharing an l3_vni share a route-target."""
        check = _run(SecurityZone=[_ns("PROD", "prod", 5), _ns("NONPROD", "non_prod", 5)])
        assert len(check._captured_errors) == 1
        assert "L3 VNI 5" in check._captured_errors[0]

    def test_distinct_vnis_pass(self) -> None:
        """Distinct l3_vni values raise nothing."""
        check = _run(SecurityZone=[_ns("PROD", "prod", 5), _ns("NONPROD", "non_prod", 6)])
        assert check._captured_errors == []


class TestInternetVrf:
    """The INTERNET VRF carries only transit and firewall-leg interfaces."""

    def test_transit_edge_and_provider_pass(self) -> None:
        """Edge/provider interfaces and a firewall leg may hold INTERNET addresses."""
        check = _run(
            InternetPhysicalInterfaces=[
                _interface("edge", "uplink"),
                _interface("provider", "customer"),
                _interface("internal-firewall", None),
                _interface("external-firewall", None),
            ]
        )
        assert check._captured_errors == []

    def test_server_interface_fails(self) -> None:
        """A server holding an INTERNET address is host-facing."""
        check = _run(InternetPhysicalInterfaces=[_interface("endpoint", None)])
        assert len(check._captured_errors) == 1
        assert "transit-only" in check._captured_errors[0] or "only transit" in check._captured_errors[0]

    def test_customer_port_on_edge_passes(self) -> None:
        """An ISP PE is an `edge`-role device whose customer-facing port legitimately holds an internet address."""
        check = _run(InternetPhysicalInterfaces=[_interface("edge", "customer")])
        assert check._captured_errors == []

    def test_non_internet_interface_ignored(self) -> None:
        """An address outside the INTERNET namespace is not this check's concern."""
        check = _run(InternetVirtualInterfaces=[_interface("endpoint", "customer", ns_type="prod")])
        assert check._captured_errors == []

    def test_segment_gateway_in_internet_fails(self) -> None:
        """A segment with its gateway in the INTERNET namespace fails; one in prod passes."""

        def segment(ns_type: str) -> dict[str, Any]:
            return {
                "security_zone": {"node": _ns("N", ns_type)},
                "__typename": "ManagedVxlanSegment",
                "id": "s",
                "name": _v("seg"),
                "gateway": {
                    "node": {
                        "ip_prefix": {
                            "node": {
                                "security_zone": {"node": _ns("N", ns_type)},
                                "prefix": _v("10.0.0.0/24"),
                                "ip_namespace": {"node": {"name": {"value": "default"}}},
                            }
                        }
                    }
                },
            }

        assert len(_run(ManagedNetworkSegment=[segment("internet")])._captured_errors) == 1
        assert _run(ManagedNetworkSegment=[segment("prod")])._captured_errors == []

    def test_segment_without_gateway_passes(self) -> None:
        """A segment with no gateway (L2-only) passes."""
        check = _run(
            ManagedNetworkSegment=[
                {
                    "__typename": "ManagedVxlanSegment",
                    "id": "s",
                    "name": _v("i"),
                    "security_zone": {"node": _ns("PROD", "prod", 60000)},
                    "gateway": {"node": None},
                }
            ]
        )
        assert check._captured_errors == []


class TestTgwRouteTables:
    """Every attachment of one TGW route table resolves to one namespace type."""

    def test_pure_table_passes(self) -> None:
        """Prod VPCs and a prod hub attachment sharing tables are accepted."""
        check = _run(
            CloudGatewayAttachment=[
                _attachment("hub", "gateway", "p", "shared", ["shared", "isolated"]),
                _attachment("a", "vpc", "p", "shared", ["shared"]),
                _attachment("b", "vpc", "p", "isolated", ["isolated"]),
            ]
        )
        assert check._captured_errors == []

    def test_mixed_association_fails(self) -> None:
        """A prod and a staging VPC associated with one table is an error."""
        check = _run(
            CloudGatewayAttachment=[
                _attachment("a", "vpc", "p", "shared", []),
                _attachment("b", "vpc", "s", "shared", []),
            ]
        )
        assert len(check._captured_errors) == 1
        assert "shared" in check._captured_errors[0]

    def test_mixed_propagation_fails(self) -> None:
        """Propagating a prod and a nonprod attachment into one table is an error."""
        check = _run(
            CloudGatewayAttachment=[
                _attachment("a", "vpc", "p", "ra", ["shared"]),
                _attachment("b", "vpc", "d", "rb", ["shared"]),
            ]
        )
        assert any("'shared'" in e for e in check._captured_errors)

    def test_non_prod_environments_are_one_type(self) -> None:
        """Staging and development both resolve to non_prod, so they may share a table."""
        check = _run(
            CloudGatewayAttachment=[
                _attachment("a", "vpc", "s", "shared", []),
                _attachment("b", "vpc", "d", "shared", []),
            ]
        )
        assert check._captured_errors == []

    def test_gateway_attachment_resolves_via_its_owner_account(self) -> None:
        """An attachment whose target is a gateway takes the environment of its owner account."""
        check = _run(
            CloudGatewayAttachment=[
                _attachment("hub", "gateway", "d", "shared", []),
                _attachment("a", "vpc", "p", "shared", []),
            ]
        )
        assert len(check._captured_errors) == 1

    def test_unknown_environment_warns(self) -> None:
        """An account without an environment is a warning, not an error."""
        check = _run(CloudGatewayAttachment=[_attachment("a", "vpc", None, "shared", [])])
        assert check._captured_errors == []
        assert check._captured_infos and check._captured_infos[0].startswith("WARNING")


class TestVpcNamespaces:
    """A VPC block lives in the namespace type of its account's environment."""

    def test_prod_account_in_prod_passes(self) -> None:
        """Environment p with a PROD block passes."""
        assert _run(**_routed_vpc("a", "p", _block("10.30.0.0/16")))._captured_errors == []

    def test_non_prod_account_in_non_prod_passes(self) -> None:
        """Any non-p environment maps to the NONPROD namespace."""
        block = _block("10.31.0.0/16", "NONPROD", "non_prod")
        assert _run(**_routed_vpc("a", "d", block, zone="NONPROD"))._captured_errors == []

    def test_block_in_wrong_namespace_type_fails(self) -> None:
        """A prod account whose block is registered behind a NONPROD session is an error."""
        check = _run(**_routed_vpc("a", "p", _block("10.30.0.0/16", "NONPROD", "non_prod"), zone="NONPROD"))
        assert len(check._captured_errors) == 1
        assert "prod network" in check._captured_errors[0]

    def test_account_without_environment_is_not_judged(self) -> None:
        """No environment, nothing to compare against."""
        assert _run(**_routed_vpc("a", None, _block("10.30.0.0/16")))._captured_errors == []


class TestSubnetsInVpc:
    """Every subnet cidr_block lies inside one of its VPC's blocks."""

    def test_subnet_inside_block_passes(self) -> None:
        """A /24 inside the VPC /16 passes."""
        check = _run(
            CloudVirtualNetwork=[_vpc("a", "p", _block("10.30.0.0/16"))],
            CloudNetworkSegment=[_subnet("s", "10.30.1.0/24", "a")],
        )
        assert check._captured_errors == []

    def test_subnet_outside_block_fails(self) -> None:
        """A subnet outside every block is an error naming the VPC."""
        check = _run(
            CloudVirtualNetwork=[_vpc("a", "p", _block("10.30.0.0/16"))],
            CloudNetworkSegment=[_subnet("s", "10.99.1.0/24", "a")],
        )
        assert len(check._captured_errors) == 1
        assert "outside every cidr_blocks" in check._captured_errors[0]

    def test_vpc_without_blocks_fails(self) -> None:
        """A VPC with no aggregate cannot contain its subnet."""
        check = _run(CloudVirtualNetwork=[_vpc("a", "p")], CloudNetworkSegment=[_subnet("s", "10.30.1.0/24", "a")])
        assert len(check._captured_errors) == 1
        assert "none registered" in check._captured_errors[0]

    def test_second_block_may_contain_subnet(self) -> None:
        """Containment in any one block suffices."""
        check = _run(
            CloudVirtualNetwork=[_vpc("a", "p", _block("10.30.0.0/16"), _block("10.31.0.0/16"))],
            CloudNetworkSegment=[_subnet("s", "10.31.4.0/24", "a")],
        )
        assert check._captured_errors == []

    def test_invalid_cidr_fails(self) -> None:
        """An unparsable cidr_block is reported, not raised."""
        check = _run(
            CloudVirtualNetwork=[_vpc("a", "p", _block("10.30.0.0/16"))],
            CloudNetworkSegment=[_subnet("s", "not-a-cidr", "a")],
        )
        assert len(check._captured_errors) == 1


class TestVpcOverlaps:
    """B2: no overlap inside one namespace between VPC blocks or with on-prem prefixes."""

    def test_disjoint_vpcs_and_onprem_pass(self) -> None:
        """Disjoint blocks and on-prem prefixes in one namespace pass."""
        check = _run(
            **_sections(
                _routed_vpc("a", "p", _block("10.30.0.0/16")),
                _routed_vpc("b", "p", _block("10.40.0.0/16")),
                ManagedNetworkSegment=[_onprem("web", "10.0.5.0/24")],
            )
        )
        assert check._captured_errors == []

    def test_vpc_vs_vpc_overlap_fails(self) -> None:
        """A /24 inside another VPC's /16 in the same namespace is an error."""
        check = _run(
            **_sections(
                _routed_vpc("a", "p", _block("10.30.0.0/16")),
                _routed_vpc("b", "p", _block("10.30.4.0/24")),
            )
        )
        assert len(check._captured_errors) == 1
        assert "overlaps VPC" in check._captured_errors[0]

    def test_vpc_overlap_in_different_namespaces_passes(self) -> None:
        """The same CIDR in PROD and NONPROD is two VRFs, not an overlap."""
        check = _run(
            **_sections(
                _routed_vpc("a", "p", _block("10.30.0.0/16")),
                _routed_vpc("b", "d", _block("10.30.0.0/16", "NONPROD", "non_prod"), zone="NONPROD"),
            )
        )
        assert check._captured_errors == []

    def test_vpc_vs_onprem_overlap_fails(self) -> None:
        """A VPC block covering an on-prem segment prefix in the same namespace is an error."""
        check = _run(
            **_sections(
                _routed_vpc("a", "p", _block("10.0.0.0/16")), ManagedNetworkSegment=[_onprem("web", "10.0.5.0/24")]
            )
        )
        assert len(check._captured_errors) == 1
        assert "on-prem prefix" in check._captured_errors[0]

    def test_vpc_vs_onprem_in_other_namespace_passes(self) -> None:
        """An on-prem prefix in another VRF does not overlap."""
        check = _run(
            **_sections(
                _routed_vpc("a", "p", _block("10.0.0.0/16")),
                ManagedNetworkSegment=[_onprem("web", "10.0.5.0/24", "NONPROD", "non_prod")],
            )
        )
        assert check._captured_errors == []

    def test_container_prefix_is_ignored(self) -> None:
        """A container prefix behind a gateway is a supernet, not a routed subnet."""
        check = _run(
            **_sections(
                _routed_vpc("a", "p", _block("10.30.0.0/16")),
                ManagedNetworkSegment=[_onprem("web", "10.0.0.0/8", member_type="prefix")],
            )
        )
        assert check._captured_errors == []

    def test_shared_prefix_object_fails(self) -> None:
        """One prefix object referenced by a VPC and an on-prem segment is flagged once."""
        check = _run(
            **_sections(
                _routed_vpc("a", "p", _block("10.30.0.0/16", block_id="shared")),
                ManagedNetworkSegment=[_onprem("web", "10.30.0.0/16", pid="shared")],
            )
        )
        assert len(check._captured_errors) == 1
        assert "adopted an on-prem prefix" in check._captured_errors[0]

    def test_cloud_segment_is_not_onprem(self) -> None:
        """CloudNetworkSegment inherits the managed segment query but is not an on-prem prefix."""
        cloud = _onprem("sub", "10.30.1.0/24")
        cloud["__typename"] = "CloudNetworkSegment"
        check = _run(**_sections(_routed_vpc("a", "p", _block("10.30.0.0/16")), ManagedNetworkSegment=[cloud]))
        assert check._captured_errors == []


class TestOfficeLanPrefixes:
    """An office is always PROD: its LAN lives in the PROD namespace type and overlaps nothing routed there."""

    def test_prod_office_in_prod_disjoint_passes(self) -> None:
        """A prod office LAN in PROD next to disjoint VPC and on-prem prefixes passes."""
        check = _run(
            TopologyCustomerOffice=[_lan_office("C005-OFFICE", _block("10.210.5.0/24"))],
            **_routed_vpc("a", "p", _block("10.150.0.0/16")),
            ManagedNetworkSegment=[_onprem("web", "10.5.10.0/24")],
        )
        assert check._captured_errors == []

    def test_office_lan_in_wrong_namespace_type_fails(self) -> None:
        """A prod office LAN registered in NONPROD is an error naming the office."""
        office = _gateway_office("C005-OFFICE", "GW1", _CUSTOMER, _block("10.210.5.0/24", "NONPROD", "non_prod"))
        office["vrfs"] = {"edges": [{"node": _raw_vrf("NONPROD", "C005-OFFICE")}]}
        office["sdwan_gateway"] = {"node": _raw_gateway("GW1", "NONPROD")}
        with pytest.raises(RoutingDomainError, match="missing PROD gateway handoff VRF"):
            _run(TopologyCustomerOffice=[office])

    def test_office_lan_overlapping_onprem_fails(self) -> None:
        """An office LAN covering an on-prem segment prefix in one namespace is an error."""
        check = _run(
            TopologyCustomerOffice=[_lan_office("C005-OFFICE", _block("10.5.0.0/16"))],
            ManagedNetworkSegment=[_onprem("web", "10.5.10.0/24")],
        )
        assert len(check._captured_errors) == 1
        assert "office 'C005-OFFICE' LAN prefix 10.5.0.0/16 overlaps on-prem prefix" in check._captured_errors[0]

    def test_office_lan_overlapping_vpc_fails(self) -> None:
        """An office LAN inside a VPC block in one namespace is an error."""
        check = _run(
            TopologyCustomerOffice=[_lan_office("C005-OFFICE", _block("10.150.5.0/24"))],
            **_routed_vpc("a", "p", _block("10.150.0.0/16")),
        )
        assert len(check._captured_errors) == 1
        assert "overlaps office 'C005-OFFICE' LAN prefix 10.150.5.0/24" in check._captured_errors[0]

    def test_office_lans_overlapping_each_other_fail(self) -> None:
        """Two offices announcing overlapping LANs into one namespace is an error."""
        check = _run(
            TopologyCustomerOffice=[
                _lan_office("C002-OFFICE", _block("10.210.0.0/16")),
                _lan_office("C005-OFFICE", _block("10.210.5.0/24")),
            ]
        )
        assert len(check._captured_errors) == 1
        assert "office 'C002-OFFICE'" in check._captured_errors[0]

    def test_office_lan_overlap_in_other_namespace_passes(self) -> None:
        """The same CIDR on a prod office and a nonprod segment sits in two VRFs."""
        check = _run(
            TopologyCustomerOffice=[_lan_office("C005-OFFICE", _block("10.5.10.0/24"))],
            ManagedNetworkSegment=[_onprem("web", "10.5.10.0/24", "NONPROD", "non_prod")],
        )
        assert check._captured_errors == []

    def test_office_lan_adopting_onprem_prefix_object_fails(self) -> None:
        """One prefix object referenced as an office LAN and by an on-prem segment is flagged once."""
        check = _run(
            TopologyCustomerOffice=[_lan_office("C005-OFFICE", _block("10.5.10.0/24", block_id="shared"))],
            ManagedNetworkSegment=[_onprem("web", "10.5.10.0/24", pid="shared")],
        )
        assert len(check._captured_errors) == 1
        assert "the LAN prefix adopted an on-prem prefix" in check._captured_errors[0]


class TestRegisteredPrefixOwners:
    """Rule 7: a VPC with blocks and an office with LANs or hosted segments are owned by a customer."""

    def test_customer_owned_vpc_and_office_pass(self) -> None:
        """A VPC whose account and an office whose owner are customers raise nothing."""
        check = _run(
            CloudVirtualNetwork=[_vpc("a", "p", _block("10.30.0.0/16"))],
            TopologyCustomerOffice=[_office("C003-OFFICE", _block("10.210.3.0/24"))],
        )
        assert check._captured_errors == []

    def test_provider_owned_account_fails(self) -> None:
        """A provider-owned account of a VPC with blocks is an error naming the VPC, account, owner and effect."""
        check = _run(CloudVirtualNetwork=[_vpc("hub", "p", _block("10.30.0.0/16"), owner=_PROVIDER)])
        assert len(check._captured_errors) == 1
        message = check._captured_errors[0]
        assert "VPC 'hub'" in message
        assert "account 'acc'" in message
        assert "owner P028 (Internal (P028), OrganizationProvider)" in message
        assert "never accepted or announced at the border" in message

    def test_account_without_owner_fails(self) -> None:
        """An account with no owner is an error that says so."""
        check = _run(CloudVirtualNetwork=[_vpc("a", "p", _block("10.30.0.0/16"), owner=None)])
        assert len(check._captured_errors) == 1
        assert "has no owner" in check._captured_errors[0]

    def test_vpc_without_blocks_is_ignored(self) -> None:
        """A VPC that registers no block announces nothing, whoever owns its account."""
        assert _run(CloudVirtualNetwork=[_vpc("a", "p", owner=_PROVIDER)])._captured_errors == []
        assert _run(CloudVirtualNetwork=[_vpc("a", "p", owner=None)])._captured_errors == []

    def test_provider_owned_office_with_lans_fails(self) -> None:
        """A provider-owned office with lan_prefixes is an error naming the office and owner."""
        check = _run(TopologyCustomerOffice=[_office("P028-OFFICE", _block("10.210.9.0/24"), owner=_PROVIDER)])
        assert len(check._captured_errors) == 1
        message = check._captured_errors[0]
        assert "office 'P028-OFFICE'" in message
        assert "owner P028 (Internal (P028), OrganizationProvider)" in message
        assert "never accepted or announced at the border" in message

    def test_provider_owned_office_hosting_segments_fails(self) -> None:
        """An office with no LAN but a hosted segment is held to the same rule."""
        check = _run(TopologyCustomerOffice=[_office("P028-OFFICE", owner=_PROVIDER, segments=1)])
        assert len(check._captured_errors) == 1

    def test_provider_owned_office_without_lans_or_segments_is_ignored(self) -> None:
        """An office that registers and hosts nothing announces nothing."""
        assert _run(TopologyCustomerOffice=[_office("P028-OFFICE", owner=_PROVIDER)])._captured_errors == []

    def test_customer_owned_office_hosting_segments_passes(self) -> None:
        """A customer-owned office hosting segments raises nothing."""
        assert _run(TopologyCustomerOffice=[_office("C003-OFFICE", segments=2)])._captured_errors == []

    def test_cleaned_owner_shape(self) -> None:
        """The cleaned query selection reads as `typename`, `org_id` and `display_label` on the owner."""
        cleaned = clean_data({"TopologyCustomerOffice": {"edges": [{"node": _office("C003-OFFICE", segments=1)}]}})
        office = cleaned["TopologyCustomerOffice"][0]
        assert office["owner"] == {
            "typename": "OrganizationCustomer",
            "org_id": "C003",
            "display_label": "Vaultex Inc. (C003)",
        }
        assert office["network_segments"] == [{"id": "seg-C003-OFFICE-0"}]


def test_the_query_selects_what_rule_seven_reads() -> None:
    """The owner of an account and of an office, and the segments an office hosts, are selected, on real fields."""
    for root, reads in {
        "CloudVirtualNetwork": [
            "account.name",
            "account.owner.__typename",
            "account.owner.org_id",
            "account.owner.display_label",
            "cidr_blocks",
        ],
        "TopologyCustomerOffice": [
            "owner.__typename",
            "owner.org_id",
            "owner.display_label",
            "network_segments",
            "lan_prefixes",
        ],
        "TopologySessionCircuit": ["locations.owner.__typename", "locations.owner.org_id", "registered_prefixes"],
    }.items():
        selected = _selected_paths(_QUERIES / "validation" / "routing_invariants.gql", root)
        assert not [path for path in reads if path not in selected]
    assert {"owner", "network_segments", "lan_prefixes"} <= relationship_names("TopologyCustomerOffice")
    assert {"account", "cidr_blocks"} <= relationship_names("CloudVirtualNetwork")
    assert "owner" in relationship_names("CloudAccount")
    assert "owner" not in relationship_names("TopologySessionCircuit")


# ---------------------------------------------------------------------------
# 8. One owner and one attachment point per registered prefix
# ---------------------------------------------------------------------------
