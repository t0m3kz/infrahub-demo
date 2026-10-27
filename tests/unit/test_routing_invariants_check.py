"""Unit tests for CheckRoutingInvariants, fed canned routing_invariants query responses."""

from __future__ import annotations

from typing import Any, cast

from checks.routing_invariants import CheckRoutingInvariants
from tests.integration.demo_data import relationship_names
from tests.unit.test_circuit_query_shapes import _QUERIES, _selected_paths
from utils.data_cleaning import clean_data


def _v(value: Any) -> dict[str, Any]:
    return {"value": value}


def _check() -> Any:
    """A check with log_error/log_info captured instead of sent to Infrahub."""
    check = cast(Any, CheckRoutingInvariants.__new__(CheckRoutingInvariants))
    errors: list[str] = []
    infos: list[str] = []
    check._captured_errors = errors
    check._captured_infos = infos
    check.log_error = lambda message: errors.append(message)
    check.log_info = lambda message: infos.append(message)
    return check


def _ns(name: str, ns_type: str | None, vni: int | None = None) -> dict[str, Any]:
    """An IpamNamespace in the raw query shape."""
    return {"id": f"ns-{name}", "name": _v(name), "l3_vni": _v(vni), "namespace_type": _v(ns_type)}


def _run(**sections: Any) -> Any:
    """Validate a payload made of the given query sections; return the check for assertions."""
    check = _check()
    check.validate({key: {"edges": [{"node": node} for node in nodes]} for key, nodes in sections.items()})
    return check


def _interface(device_role: str, role: str | None, ns_type: str = "internet") -> dict[str, Any]:
    return {
        "__typename": "DcimPhysicalInterface",
        "id": "if-1",
        "name": _v("Ethernet1/14"),
        "role": _v(role),
        "device": {"node": {"id": "d1", "name": _v("dev-1"), "role": _v(device_role)}},
        "ip_address": {"node": {"address": _v("203.0.113.1/31"), "ip_namespace": {"node": _ns("INTERNET", ns_type)}}},
    }


def _attachment(
    name: str,
    att_type: str,
    env: str | None,
    associated: str,
    propagates: list[str],
    endpoint_id: str | None = None,
) -> dict[str, Any]:
    account = {"node": {"id": f"acc-{name}", "name": _v(f"acc-{name}"), "environment": _v(env)}}
    return {
        "id": f"att-{name}",
        "name": _v(name),
        "attachment_type": _v(att_type),
        "owner_account": account,
        "virtual_network": {"node": {"id": f"vn-{name}", "name": _v(f"vn-{name}"), "account": account}}
        if att_type == "vpc"
        else {"node": None},
        "hybrid_endpoint": {"node": {"id": endpoint_id}} if endpoint_id else {"node": None},
        "associated_route_table": {"node": {"id": f"rt-{associated}", "name": _v(associated)}},
        "propagates_to_route_tables": {"edges": [{"node": {"id": f"rt-{rt}", "name": _v(rt)}} for rt in propagates]},
    }


def _vif(vif_id: str, handoff_type: str | None, env: str | None = None) -> dict[str, Any]:
    """A VIF whose handoff circuit terminates in a namespace of the given type (None: the VIF has no handoff)."""
    return {
        "id": vif_id,
        "name": _v(vif_id),
        "virtual_network": {
            "node": {
                "id": "vn",
                "name": _v("vn"),
                "account": {"node": {"id": "a", "name": _v("a"), "environment": _v(env)}},
            }
        }
        if env
        else {"node": None},
        "virtual_circuit": {
            "node": {
                "name": _v(f"HO-{vif_id}"),
                "ip_namespace": {"node": {"name": _v("NS"), "namespace_type": _v(handoff_type)}},
            }
        }
        if handoff_type
        else {"node": None},
    }


class TestL3VniSharing:
    """Namespaces of different types never share an l3_vni."""

    def test_shared_l3_vni_across_types_fails(self) -> None:
        """Namespaces of different types sharing an l3_vni share a route-target."""
        check = _run(IpamNamespace=[_ns("PROD", "prod", 5), _ns("NON-PROD", "non_prod", 5)])
        assert len(check._captured_errors) == 1
        assert "L3 VNI 5" in check._captured_errors[0]

    def test_distinct_vnis_pass(self) -> None:
        """Distinct l3_vni values raise nothing."""
        check = _run(IpamNamespace=[_ns("PROD", "prod", 5), _ns("NON-PROD", "non_prod", 6), _ns("default", None)])
        assert check._captured_errors == []


class TestInternetVrf:
    """The INTERNET VRF carries only transit and firewall-leg interfaces."""

    def test_transit_edge_and_provider_pass(self) -> None:
        """Edge/provider interfaces and a firewall leg may hold INTERNET addresses."""
        check = _run(
            InternetPhysicalInterfaces=[
                _interface("edge", "uplink"),
                _interface("provider", "customer"),
                _interface("firewall-active", None),
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
                "__typename": "ManagedVxlanSegment",
                "id": "s",
                "name": _v("seg"),
                "gateway": {
                    "node": {
                        "ip_prefix": {
                            "node": {"prefix": _v("10.0.0.0/24"), "ip_namespace": {"node": _ns("N", ns_type)}}
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
                {"__typename": "ManagedVxlanSegment", "id": "s", "name": _v("i"), "gateway": {"node": None}}
            ]
        )
        assert check._captured_errors == []


class TestTgwRouteTables:
    """Every attachment of one TGW route table resolves to one namespace type."""

    def test_pure_table_passes(self) -> None:
        """Prod VPCs and a prod hub attachment sharing tables are accepted."""
        check = _run(
            CloudTransitGatewayAttachment=[
                _attachment("hub", "hybrid", "p", "shared", ["shared", "isolated"]),
                _attachment("a", "vpc", "p", "shared", ["shared"]),
                _attachment("b", "vpc", "p", "isolated", ["isolated"]),
            ]
        )
        assert check._captured_errors == []

    def test_mixed_association_fails(self) -> None:
        """A prod and a staging VPC associated with one table is an error."""
        check = _run(
            CloudTransitGatewayAttachment=[
                _attachment("a", "vpc", "p", "shared", []),
                _attachment("b", "vpc", "s", "shared", []),
            ]
        )
        assert len(check._captured_errors) == 1
        assert "shared" in check._captured_errors[0]

    def test_mixed_propagation_fails(self) -> None:
        """Propagating a prod and a non-prod attachment into one table is an error."""
        check = _run(
            CloudTransitGatewayAttachment=[
                _attachment("a", "vpc", "p", "ra", ["shared"]),
                _attachment("b", "vpc", "d", "rb", ["shared"]),
            ]
        )
        assert any("'shared'" in e for e in check._captured_errors)

    def test_non_prod_environments_are_one_type(self) -> None:
        """Staging and development both resolve to non_prod, so they may share a table."""
        check = _run(
            CloudTransitGatewayAttachment=[
                _attachment("a", "vpc", "s", "shared", []),
                _attachment("b", "vpc", "d", "shared", []),
            ]
        )
        assert check._captured_errors == []

    def test_hybrid_attachment_resolves_via_the_vif_handoff(self) -> None:
        """A hybrid attachment takes the type of its VIF's handoff namespace, not its account's."""
        check = _run(
            CloudTransitGatewayAttachment=[
                _attachment("hub", "hybrid", "p", "shared", [], endpoint_id="vif-1"),
                _attachment("a", "vpc", "p", "shared", []),
            ],
            CloudVirtualInterface=[_vif("vif-1", "non_prod")],
        )
        assert len(check._captured_errors) == 1

    def test_unknown_environment_warns(self) -> None:
        """An account without an environment is a warning, not an error."""
        check = _run(CloudTransitGatewayAttachment=[_attachment("a", "vpc", None, "shared", [])])
        assert check._captured_errors == []
        assert check._captured_infos and check._captured_infos[0].startswith("WARNING")


class TestVifHandoff:
    """A VIF's handoff circuit terminates in the namespace type of its virtual network's account environment."""

    def test_handoff_in_the_account_environment_passes(self) -> None:
        """A prod handoff on a VIF of a prod virtual network, and a non-prod one on a development account."""
        assert _run(CloudVirtualInterface=[_vif("vif-1", "prod", env="p")])._captured_errors == []
        assert _run(CloudVirtualInterface=[_vif("vif-2", "non_prod", env="d")])._captured_errors == []

    def test_handoff_namespace_differs_from_the_vif_network_fails(self) -> None:
        """A non_prod handoff on a VIF of a prod virtual network is an error naming the circuit and namespace."""
        check = _run(CloudVirtualInterface=[_vif("vif-1", "non_prod", env="p")])
        assert check._captured_errors == [
            "VIF 'vif-1' belongs to a prod virtual network but its handoff circuit 'HO-vif-1' terminates in the "
            "non_prod namespace 'NS'"
        ]

    def test_vif_without_a_handoff_passes(self) -> None:
        """A VIF no handoff circuit uses yet resolves to no type and raises nothing."""
        assert _run(CloudVirtualInterface=[_vif("vif-1", None, env="p")])._captured_errors == []

    def test_handoff_in_an_untyped_namespace_passes(self) -> None:
        """A namespace with no namespace_type resolves to no type and raises nothing."""
        vif = _vif("vif-1", "prod", env="p")
        vif["virtual_circuit"]["node"]["ip_namespace"]["node"]["namespace_type"] = _v(None)
        assert _run(CloudVirtualInterface=[vif])._captured_errors == []

    def test_vif_network_without_environment_is_not_judged(self) -> None:
        """No account environment: any handoff namespace passes."""
        assert _run(CloudVirtualInterface=[_vif("vif-1", "non_prod")])._captured_errors == []


def _block(prefix: str, ns_name: str = "PROD", ns_type: str = "prod", block_id: str | None = None) -> dict[str, Any]:
    """A VPC cidr_blocks entry (an IpamPrefix) in the raw query shape."""
    return {
        "id": block_id or f"pfx-{ns_name}-{prefix}",
        "prefix": _v(prefix),
        "ip_namespace": {"node": {**_ns(ns_name, ns_type), "id": f"ns-{ns_name}"}},
    }


_CUSTOMER = ("OrganizationCustomer", "C003", "Vaultex Inc. (C003)")
_PROVIDER = ("OrganizationProvider", "P028", "Internal (P028)")


def _owner(who: tuple[str, str, str] | None) -> dict[str, Any]:
    """An `owner { node { __typename org_id display_label } }` selection (None: no owner)."""
    if who is None:
        return {"node": None}
    typename, org_id, label = who
    return {"node": {"__typename": typename, "org_id": _v(org_id), "display_label": label}}


def _vpc(
    name: str, env: str | None, *blocks: dict[str, Any], owner: tuple[str, str, str] | None = _CUSTOMER
) -> dict[str, Any]:
    """A CloudVirtualNetwork with its account environment and owner (a customer unless told otherwise) and blocks."""
    return {
        "id": f"vpc-{name}",
        "name": _v(name),
        "account": {"node": {"id": f"acc-{name}", "name": _v("acc"), "environment": _v(env), "owner": _owner(owner)}},
        "cidr_blocks": {"edges": [{"node": block} for block in blocks]},
    }


def _subnet(name: str, cidr: str | None, vpc: str) -> dict[str, Any]:
    """A CloudNetworkSegment (a subnet) of the named VPC."""
    return {
        "id": f"sub-{name}",
        "name": _v(name),
        "cidr_block": _v(cidr),
        "virtual_network": {"node": {"id": f"vpc-{vpc}", "name": _v(vpc)}},
    }


def _onprem(
    name: str, prefix: str, ns_name: str = "PROD", ns_type: str = "prod", member_type: str = "address", pid: str = "p"
) -> dict[str, Any]:
    """An on-prem segment whose gateway sits in the given prefix."""
    return {
        "__typename": "ManagedVxlanSegment",
        "id": f"seg-{name}",
        "name": _v(name),
        "gateway": {
            "node": {
                "ip_prefix": {
                    "node": {
                        "id": pid,
                        "prefix": _v(prefix),
                        "member_type": _v(member_type),
                        "ip_namespace": {"node": {**_ns(ns_name, ns_type), "id": f"ns-{ns_name}"}},
                    }
                }
            }
        },
    }


class TestVpcNamespaces:
    """A VPC block lives in the namespace type of its account's environment."""

    def test_prod_account_in_prod_passes(self) -> None:
        """Environment p with a PROD block passes."""
        assert _run(CloudVirtualNetwork=[_vpc("a", "p", _block("10.30.0.0/16"))])._captured_errors == []

    def test_non_prod_account_in_non_prod_passes(self) -> None:
        """Any non-p environment maps to the NON-PROD namespace."""
        block = _block("10.31.0.0/16", "NON-PROD", "non_prod")
        assert _run(CloudVirtualNetwork=[_vpc("a", "d", block)])._captured_errors == []

    def test_block_in_wrong_namespace_type_fails(self) -> None:
        """A prod account whose block sits in the default (or NON-PROD) namespace is an error."""
        check = _run(CloudVirtualNetwork=[_vpc("a", "p", _block("10.30.0.0/16", "NON-PROD", "non_prod"))])
        assert len(check._captured_errors) == 1
        assert "prod network" in check._captured_errors[0]

    def test_account_without_environment_is_not_judged(self) -> None:
        """No environment, nothing to compare against."""
        assert _run(CloudVirtualNetwork=[_vpc("a", None, _block("10.30.0.0/16"))])._captured_errors == []


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
            CloudVirtualNetwork=[
                _vpc("a", "p", _block("10.30.0.0/16")),
                _vpc("b", "p", _block("10.40.0.0/16")),
            ],
            ManagedNetworkSegment=[_onprem("web", "10.0.5.0/24")],
        )
        assert check._captured_errors == []

    def test_vpc_vs_vpc_overlap_fails(self) -> None:
        """A /24 inside another VPC's /16 in the same namespace is an error."""
        check = _run(
            CloudVirtualNetwork=[
                _vpc("a", "p", _block("10.30.0.0/16")),
                _vpc("b", "p", _block("10.30.4.0/24")),
            ]
        )
        assert len(check._captured_errors) == 1
        assert "overlaps VPC" in check._captured_errors[0]

    def test_vpc_overlap_in_different_namespaces_passes(self) -> None:
        """The same CIDR in PROD and NON-PROD is two VRFs, not an overlap."""
        check = _run(
            CloudVirtualNetwork=[
                _vpc("a", "p", _block("10.30.0.0/16")),
                _vpc("b", "d", _block("10.30.0.0/16", "NON-PROD", "non_prod")),
            ]
        )
        assert check._captured_errors == []

    def test_vpc_vs_onprem_overlap_fails(self) -> None:
        """A VPC block covering an on-prem segment prefix in the same namespace is an error."""
        check = _run(
            CloudVirtualNetwork=[_vpc("a", "p", _block("10.0.0.0/16"))],
            ManagedNetworkSegment=[_onprem("web", "10.0.5.0/24")],
        )
        assert len(check._captured_errors) == 1
        assert "on-prem prefix" in check._captured_errors[0]

    def test_vpc_vs_onprem_in_other_namespace_passes(self) -> None:
        """An on-prem prefix in another VRF does not overlap."""
        check = _run(
            CloudVirtualNetwork=[_vpc("a", "p", _block("10.0.0.0/16"))],
            ManagedNetworkSegment=[_onprem("web", "10.0.5.0/24", "NON-PROD", "non_prod")],
        )
        assert check._captured_errors == []

    def test_container_prefix_is_ignored(self) -> None:
        """A container prefix behind a gateway is a supernet, not a routed subnet."""
        check = _run(
            CloudVirtualNetwork=[_vpc("a", "p", _block("10.30.0.0/16"))],
            ManagedNetworkSegment=[_onprem("web", "10.0.0.0/8", member_type="prefix")],
        )
        assert check._captured_errors == []

    def test_shared_prefix_object_fails(self) -> None:
        """One prefix object referenced by a VPC and an on-prem segment is flagged once."""
        check = _run(
            CloudVirtualNetwork=[_vpc("a", "p", _block("10.30.0.0/16", block_id="shared"))],
            ManagedNetworkSegment=[_onprem("web", "10.30.0.0/16", pid="shared")],
        )
        assert len(check._captured_errors) == 1
        assert "adopted an on-prem prefix" in check._captured_errors[0]

    def test_cloud_segment_is_not_onprem(self) -> None:
        """CloudNetworkSegment inherits the managed segment query but is not an on-prem prefix."""
        cloud = _onprem("sub", "10.30.1.0/24")
        cloud["__typename"] = "CloudNetworkSegment"
        check = _run(CloudVirtualNetwork=[_vpc("a", "p", _block("10.30.0.0/16"))], ManagedNetworkSegment=[cloud])
        assert check._captured_errors == []


def _office(
    name: str,
    *prefixes: dict[str, Any],
    owner: tuple[str, str, str] | None = _CUSTOMER,
    segments: int = 0,
) -> dict[str, Any]:
    """A TopologyCustomerOffice footprint with its owner (a customer unless told otherwise), LAN prefixes and hosted
    segments (an office has no environment)."""
    return {
        "id": f"office-{name}",
        "name": _v(name),
        "owner": _owner(owner),
        "network_segments": {"edges": [{"node": {"id": f"seg-{name}-{n}"}} for n in range(segments)]},
        "lan_prefixes": {"edges": [{"node": prefix} for prefix in prefixes]},
    }


class TestOfficeLanPrefixes:
    """An office is always PROD: its LAN lives in the PROD namespace type and overlaps nothing routed there."""

    def test_prod_office_in_prod_disjoint_passes(self) -> None:
        """A prod office LAN in PROD next to disjoint VPC and on-prem prefixes passes."""
        check = _run(
            TopologyCustomerOffice=[_office("C005-OFFICE", _block("10.210.5.0/24"))],
            CloudVirtualNetwork=[_vpc("a", "p", _block("10.150.0.0/16"))],
            ManagedNetworkSegment=[_onprem("web", "10.5.10.0/24")],
        )
        assert check._captured_errors == []

    def test_office_lan_in_wrong_namespace_type_fails(self) -> None:
        """A prod office LAN registered in NON-PROD is an error naming the office."""
        office = _office("C005-OFFICE", _block("10.210.5.0/24", "NON-PROD", "non_prod"))
        check = _run(TopologyCustomerOffice=[office])
        assert len(check._captured_errors) == 1
        assert "office 'C005-OFFICE' LAN prefix" in check._captured_errors[0]
        assert "prod network" in check._captured_errors[0]

    def test_office_lan_overlapping_onprem_fails(self) -> None:
        """An office LAN covering an on-prem segment prefix in one namespace is an error."""
        check = _run(
            TopologyCustomerOffice=[_office("C005-OFFICE", _block("10.5.0.0/16"))],
            ManagedNetworkSegment=[_onprem("web", "10.5.10.0/24")],
        )
        assert len(check._captured_errors) == 1
        assert "office 'C005-OFFICE' LAN prefix 10.5.0.0/16 overlaps on-prem prefix" in check._captured_errors[0]

    def test_office_lan_overlapping_vpc_fails(self) -> None:
        """An office LAN inside a VPC block in one namespace is an error."""
        check = _run(
            TopologyCustomerOffice=[_office("C005-OFFICE", _block("10.150.5.0/24"))],
            CloudVirtualNetwork=[_vpc("a", "p", _block("10.150.0.0/16"))],
        )
        assert len(check._captured_errors) == 1
        assert "overlaps office 'C005-OFFICE' LAN prefix 10.150.5.0/24" in check._captured_errors[0]

    def test_office_lans_overlapping_each_other_fail(self) -> None:
        """Two offices announcing overlapping LANs into one namespace is an error."""
        check = _run(
            TopologyCustomerOffice=[
                _office("C002-OFFICE", _block("10.210.0.0/16")),
                _office("C005-OFFICE", _block("10.210.5.0/24")),
            ]
        )
        assert len(check._captured_errors) == 1
        assert "office 'C002-OFFICE'" in check._captured_errors[0]

    def test_office_lan_overlap_in_other_namespace_passes(self) -> None:
        """The same CIDR on a prod office and a non-prod segment sits in two VRFs."""
        check = _run(
            TopologyCustomerOffice=[_office("C005-OFFICE", _block("10.5.10.0/24"))],
            ManagedNetworkSegment=[_onprem("web", "10.5.10.0/24", "NON-PROD", "non_prod")],
        )
        assert check._captured_errors == []

    def test_office_lan_adopting_onprem_prefix_object_fails(self) -> None:
        """One prefix object referenced as an office LAN and by an on-prem segment is flagged once."""
        check = _run(
            TopologyCustomerOffice=[_office("C005-OFFICE", _block("10.5.10.0/24", block_id="shared"))],
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
    }.items():
        selected = _selected_paths(_QUERIES / "validation" / "routing_invariants.gql", root)
        assert not [path for path in reads if path not in selected]
    assert {"owner", "network_segments", "lan_prefixes"} <= relationship_names("TopologyCustomerOffice")
    assert {"account", "cidr_blocks"} <= relationship_names("CloudVirtualNetwork")
    assert "owner" in relationship_names("CloudAccount")


# ---------------------------------------------------------------------------
# 8. One owner and one attachment point per registered prefix
# ---------------------------------------------------------------------------

_C002 = ("OrganizationCustomer", "C002", "Solvex (C002)")
_C005 = ("OrganizationCustomer", "C005", "Brightwave (C005)")
_C015 = ("OrganizationCustomer", "C015", "Novantis (C015)")


def _segment(
    name: str,
    prefix: str,
    owner: tuple[str, str, str] | None,
    *,
    ns_name: str = "PROD",
    ns_type: str = "prod",
) -> dict[str, Any]:
    """An on-prem segment with its owner (what rule 8 reads next to the gateway prefix)."""
    return {**_onprem(name, prefix, ns_name, ns_type), "owner": _owner(owner)}


def _gateway_office(
    name: str,
    gateway: str | None,
    owner: tuple[str, str, str] | None,
    *lans: dict[str, Any],
    hosted: tuple[tuple[str, tuple[str, str, str] | None], ...] = (),
) -> dict[str, Any]:
    """An office behind an SD-WAN gateway with its LANs and hosted segments, (prefix, owner) each (PROD)."""
    return {
        **_office(name, *lans, owner=owner),
        "sdwan_gateway": {"node": {"name": _v(gateway)}} if gateway else {"node": None},
        "segment_deployments": {
            "edges": [
                {
                    "node": {
                        "segment": {
                            "node": {
                                "owner": _owner(segment_owner),
                                "gateway": {
                                    "node": {
                                        "ip_prefix": {
                                            "node": {
                                                "prefix": _v(prefix),
                                                "ip_namespace": {"node": {"name": _v("PROD")}},
                                            }
                                        }
                                    }
                                },
                            }
                        }
                    }
                }
                for prefix, segment_owner in hosted
            ]
        },
    }


def _private_vif(vif_id: str, vpc: dict[str, Any], circuit: str | None = "HO") -> dict[str, Any]:
    """A private VIF onto one VPC (its blocks and account owner), on the handoff circuit ``circuit``."""
    vif = _vif(vif_id, "prod", "p")
    vif["virtual_network"] = {"node": vpc}
    vif["virtual_circuit"] = (
        {"node": {"name": _v(circuit), "ip_namespace": {"node": {"name": _v("PROD"), "namespace_type": _v("prod")}}}}
        if circuit
        else {"node": None}
    )
    return vif


def _transit_vif(
    vif_id: str, table: str, attachments: list[tuple[str, dict[str, Any]]], circuit: str | None = "HO-HUB"
) -> dict[str, Any]:
    """A transit VIF whose hybrid attachment sits on ``table``; ``attachments`` are (propagated table, VPC)."""
    vif = _private_vif(vif_id, {}, circuit)
    vif["virtual_network"] = {"node": None}
    vif["transit_gateway_attachment"] = {
        "node": {
            "associated_route_table": {"node": {"id": table}},
            "transit_gateway": {
                "node": {
                    "attachments": {
                        "edges": [
                            {
                                "node": {
                                    "attachment_type": _v("vpc"),
                                    "propagates_to_route_tables": {"edges": [{"node": {"id": propagated}}]},
                                    "virtual_network": {"node": vpc},
                                }
                            }
                            for propagated, vpc in attachments
                        ]
                    }
                }
            },
        }
    }
    return vif


def _rule_eight(check: Any) -> list[str]:
    """The errors rule 8 raised (they start with the namespace)."""
    return [message for message in check._captured_errors if message.startswith("Namespace '")]


class TestOwnershipConflicts:
    """Rule 8: no prefix of two owners, no owner's prefix behind two attachment points."""

    def _demo(self, **extra: Any) -> dict[str, Any]:
        """The 30_all shape: C002 and C003 offices behind GW1, C003 and C015 VPCs on the hub transit VIF."""
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
            "CloudVirtualInterface": [
                _transit_vif(
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
        assert "handoff circuit 'HO-HUB'" in errors[0]
        assert "another owner's session" in errors[0]

    def test_a_segment_prefix_of_another_owner_fails(self) -> None:
        """An on-prem segment of C003 on the same prefix as C015's VPC block."""
        check = _run(**self._demo(ManagedNetworkSegment=[_segment("c003-web", "10.32.0.0/16", _CUSTOMER)]))
        errors = _rule_eight(check)
        assert len(errors) == 1
        assert "registered by more than one owner" in errors[0]

    def test_an_owners_prefix_behind_two_circuits_fails(self) -> None:
        """C003's VPC block reaches the hub and a private VIF on another circuit: two attachment points."""
        vpc = _vpc("c003", "p", _block("10.30.0.0/16"))
        vifs = [
            _transit_vif("hub", "rt-shared", [("rt-shared", vpc)]),
            _private_vif("direct", vpc, circuit="HO-DIRECT"),
        ]
        errors = _rule_eight(_run(CloudVirtualInterface=vifs))
        assert len(errors) == 1
        assert "10.30.0.0/16 of C003" in errors[0]
        assert "handoff circuit 'HO-DIRECT', handoff circuit 'HO-HUB'" in errors[0]
        assert "exactly one session" in errors[0]

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
            CloudVirtualInterface=[_private_vif("direct", _vpc("c015", "p", _block("10.32.0.0/16"), owner=_C015))],
        )
        assert _rule_eight(check) == []

    def test_a_vpc_that_propagates_elsewhere_is_not_behind_the_vif(self) -> None:
        """Only the VPCs propagating into the hybrid attachment's table are reachable: the isolated one is not."""
        vif = _transit_vif(
            "hub",
            "rt-shared",
            [
                ("rt-shared", _vpc("c015", "p", _block("10.32.0.0/16"), owner=_C015)),
                ("rt-isolated", _vpc("x", "p", _block("10.30.0.0/16"))),
            ],
        )
        check = _run(
            CloudVirtualInterface=[vif],
            TopologyCustomerOffice=[_gateway_office("C003-OFFICE", "GW1", _CUSTOMER, _block("10.30.0.0/16"))],
        )
        assert _rule_eight(check) == []

    def test_a_vif_without_a_handoff_circuit_has_no_session(self) -> None:
        """No circuit, no session: its blocks are not an attachment point."""
        vpc = _vpc("c003", "p", _block("10.30.0.0/16"))
        check = _run(CloudVirtualInterface=[_private_vif("direct", vpc, circuit=None), _private_vif("hub", vpc)])
        assert _rule_eight(check) == []

    def test_a_non_customer_registration_is_not_judged_here(self) -> None:
        """A provider-owned office registers nothing at the border (rule 7 reports it); rule 8 stays silent."""
        check = _run(
            TopologyCustomerOffice=[_gateway_office("P-OFFICE", "GW1", _PROVIDER, _block("10.30.0.0/16"))],
            CloudVirtualInterface=[_private_vif("direct", _vpc("c003", "p", _block("10.30.0.0/16")))],
        )
        assert _rule_eight(check) == []

    def test_an_office_without_a_gateway_is_not_an_attachment_point(self) -> None:
        """No sdwan_gateway: nothing is announced through it, so it cannot conflict."""
        check = _run(
            TopologyCustomerOffice=[_gateway_office("C003-OFFICE", None, _CUSTOMER, _block("10.32.0.0/16"))],
            CloudVirtualInterface=[_private_vif("direct", _vpc("c015", "p", _block("10.32.0.0/16"), owner=_C015))],
        )
        assert _rule_eight(check) == []

    def test_an_empty_payload_passes(self) -> None:
        """Nothing registered: nothing to report."""
        assert _rule_eight(_run()) == []


def test_the_query_selects_what_rule_eight_reads() -> None:
    """Segment owner, the office gateway and hosted segments, and the VIF chain, on real fields."""
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
        "CloudVirtualInterface": [
            "virtual_circuit.name",
            "virtual_network.cidr_blocks.prefix",
            "virtual_network.cidr_blocks.ip_namespace.name",
            "virtual_network.account.owner.org_id",
            "transit_gateway_attachment.associated_route_table.id",
            "transit_gateway_attachment.transit_gateway.attachments.attachment_type",
            "transit_gateway_attachment.transit_gateway.attachments.propagates_to_route_tables.id",
            "transit_gateway_attachment.transit_gateway.attachments.virtual_network.cidr_blocks.prefix",
            "transit_gateway_attachment.transit_gateway.attachments.virtual_network.account.owner.org_id",
        ],
    }
    for root, paths in reads.items():
        selected = _selected_paths(path, root)
        assert not [item for item in paths if item not in selected], root
    assert "owner" in relationship_names("ManagedNetworkSegment")
    assert {"sdwan_gateway", "segment_deployments", "lan_prefixes"} <= relationship_names("TopologyCustomerOffice")
    assert {"transit_gateway_attachment", "virtual_circuit", "virtual_network"} <= relationship_names(
        "CloudVirtualInterface"
    )
    assert {"transit_gateway", "associated_route_table"} <= relationship_names("CloudTransitGatewayAttachment")
    assert "attachments" in relationship_names("CloudTransitGateway")
    assert "segment" in relationship_names("ManagedSegmentDeployment")
