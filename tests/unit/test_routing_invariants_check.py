"""Unit tests for CheckRoutingInvariants, fed canned routing_invariants query responses."""

from __future__ import annotations

from typing import Any, cast

from checks.routing_invariants import CheckRoutingInvariants


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


def _vif(vif_id: str, circuits: dict[str, list[str | None]], env: str | None = None) -> dict[str, Any]:
    """A VIF whose circuits terminate on interfaces addressed in namespaces of the given types."""
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
        "virtual_circuits": {
            "edges": [
                {
                    "node": {
                        "id": f"c-{circuit}",
                        "name": _v(circuit),
                        "capabilities": {
                            "edges": [
                                {
                                    "node": {
                                        "__typename": "DcimVirtualInterface",
                                        "id": f"{circuit}-{i}",
                                        "name": _v("e"),
                                        "ip_address": {"node": {"ip_namespace": {"node": _ns("X", ns_type)}}}
                                        if ns_type
                                        else {"node": None},
                                    }
                                }
                                for i, ns_type in enumerate(types)
                            ]
                        },
                    }
                }
                for circuit, types in circuits.items()
            ]
        },
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
                "__typename": "ManagedVlanSegment",
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
        """The External (internet) segment has no gateway and passes."""
        check = _run(
            ManagedNetworkSegment=[
                {"__typename": "ManagedExternalSegment", "id": "s", "name": _v("i"), "gateway": {"node": None}}
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

    def test_hybrid_attachment_resolves_via_vif_circuits(self) -> None:
        """A hybrid attachment takes the type of its VIF's circuit namespaces, not its account's."""
        check = _run(
            CloudTransitGatewayAttachment=[
                _attachment("hub", "hybrid", "p", "shared", [], endpoint_id="vif-1"),
                _attachment("a", "vpc", "p", "shared", []),
            ],
            CloudVirtualInterface=[_vif("vif-1", {"c1": ["non_prod"]})],
        )
        assert len(check._captured_errors) == 1

    def test_unknown_environment_warns(self) -> None:
        """An account without an environment is a warning, not an error."""
        check = _run(CloudTransitGatewayAttachment=[_attachment("a", "vpc", None, "shared", [])])
        assert check._captured_errors == []
        assert check._captured_infos and check._captured_infos[0].startswith("WARNING")


class TestVifCircuits:
    """A VIF's circuits all resolve to one namespace type."""

    def test_uniform_circuits_pass(self) -> None:
        """Circuits terminating in prod namespaces (or unaddressed ports) pass."""
        check = _run(CloudVirtualInterface=[_vif("vif-1", {"c1": ["prod", None], "c2": ["prod"]})])
        assert check._captured_errors == []

    def test_mixed_circuits_fail(self) -> None:
        """A prod and a non_prod circuit on one VIF is an error."""
        check = _run(CloudVirtualInterface=[_vif("vif-1", {"c1": ["prod"], "c2": ["non_prod"]})])
        assert len(check._captured_errors) == 1
        assert "never mix" in check._captured_errors[0]

    def test_circuit_type_differs_from_vif_network_fails(self) -> None:
        """A non_prod circuit on a VIF of a prod virtual network is an error."""
        check = _run(CloudVirtualInterface=[_vif("vif-1", {"c1": ["non_prod"]}, env="p")])
        assert len(check._captured_errors) == 1
        assert "prod virtual network" in check._captured_errors[0]

    def test_unaddressed_circuits_pass(self) -> None:
        """Circuits on unaddressed (underlay) ports resolve to no type and raise nothing."""
        check = _run(CloudVirtualInterface=[_vif("vif-1", {"c1": [None]})])
        assert check._captured_errors == []


def _block(prefix: str, ns_name: str = "PROD", ns_type: str = "prod", block_id: str | None = None) -> dict[str, Any]:
    """A VPC cidr_blocks entry (an IpamPrefix) in the raw query shape."""
    return {
        "id": block_id or f"pfx-{ns_name}-{prefix}",
        "prefix": _v(prefix),
        "ip_namespace": {"node": {**_ns(ns_name, ns_type), "id": f"ns-{ns_name}"}},
    }


def _vpc(name: str, env: str | None, *blocks: dict[str, Any]) -> dict[str, Any]:
    """A CloudVirtualNetwork with its account environment and aggregate blocks."""
    return {
        "id": f"vpc-{name}",
        "name": _v(name),
        "account": {"node": {"id": f"acc-{name}", "name": _v("acc"), "environment": _v(env)}},
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
        "__typename": "ManagedVlanSegment",
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


def _office(name: str, env: str | None, *prefixes: dict[str, Any]) -> dict[str, Any]:
    """A TopologyCustomerOffice footprint with its environment and LAN prefixes."""
    return {
        "id": f"office-{name}",
        "name": _v(name),
        "environment": _v(env),
        "lan_prefixes": {"edges": [{"node": prefix} for prefix in prefixes]},
    }


class TestOfficeLanPrefixes:
    """An office LAN lives in its environment's namespace type and overlaps nothing routed there."""

    def test_prod_office_in_prod_disjoint_passes(self) -> None:
        """A prod office LAN in PROD next to disjoint VPC and on-prem prefixes passes."""
        check = _run(
            TopologyCustomerOffice=[_office("C005-P", "p", _block("10.210.5.0/24"))],
            CloudVirtualNetwork=[_vpc("a", "p", _block("10.150.0.0/16"))],
            ManagedNetworkSegment=[_onprem("web", "10.5.10.0/24")],
        )
        assert check._captured_errors == []

    def test_non_prod_office_in_non_prod_passes(self) -> None:
        """A staging office LAN maps to the NON-PROD namespace type."""
        office = _office("C005-S", "s", _block("10.210.5.0/24", "NON-PROD", "non_prod"))
        assert _run(TopologyCustomerOffice=[office])._captured_errors == []

    def test_office_lan_in_wrong_namespace_type_fails(self) -> None:
        """A prod office LAN registered in NON-PROD is an error naming the office."""
        office = _office("C005-P", "p", _block("10.210.5.0/24", "NON-PROD", "non_prod"))
        check = _run(TopologyCustomerOffice=[office])
        assert len(check._captured_errors) == 1
        assert "office 'C005-P' LAN prefix" in check._captured_errors[0]
        assert "prod network" in check._captured_errors[0]

    def test_office_without_environment_is_not_judged(self) -> None:
        """No environment, nothing to compare the namespace type against."""
        office = _office("C005", None, _block("10.210.5.0/24", "NON-PROD", "non_prod"))
        assert _run(TopologyCustomerOffice=[office])._captured_errors == []

    def test_office_lan_overlapping_onprem_fails(self) -> None:
        """An office LAN covering an on-prem segment prefix in one namespace is an error."""
        check = _run(
            TopologyCustomerOffice=[_office("C005-P", "p", _block("10.5.0.0/16"))],
            ManagedNetworkSegment=[_onprem("web", "10.5.10.0/24")],
        )
        assert len(check._captured_errors) == 1
        assert "office 'C005-P' LAN prefix 10.5.0.0/16 overlaps on-prem prefix" in check._captured_errors[0]

    def test_office_lan_overlapping_vpc_fails(self) -> None:
        """An office LAN inside a VPC block in one namespace is an error."""
        check = _run(
            TopologyCustomerOffice=[_office("C005-P", "p", _block("10.150.5.0/24"))],
            CloudVirtualNetwork=[_vpc("a", "p", _block("10.150.0.0/16"))],
        )
        assert len(check._captured_errors) == 1
        assert "overlaps office 'C005-P' LAN prefix 10.150.5.0/24" in check._captured_errors[0]

    def test_office_lans_overlapping_each_other_fail(self) -> None:
        """Two offices announcing overlapping LANs into one namespace is an error."""
        check = _run(
            TopologyCustomerOffice=[
                _office("C002-P", "p", _block("10.210.0.0/16")),
                _office("C005-P", "p", _block("10.210.5.0/24")),
            ]
        )
        assert len(check._captured_errors) == 1
        assert "office 'C002-P'" in check._captured_errors[0]

    def test_office_lan_overlap_in_other_namespace_passes(self) -> None:
        """The same CIDR on a non-prod office and a prod segment sits in two VRFs."""
        check = _run(
            TopologyCustomerOffice=[_office("C005-D", "d", _block("10.5.10.0/24", "NON-PROD", "non_prod"))],
            ManagedNetworkSegment=[_onprem("web", "10.5.10.0/24")],
        )
        assert check._captured_errors == []

    def test_office_lan_adopting_onprem_prefix_object_fails(self) -> None:
        """One prefix object referenced as an office LAN and by an on-prem segment is flagged once."""
        check = _run(
            TopologyCustomerOffice=[_office("C005-P", "p", _block("10.5.10.0/24", block_id="shared"))],
            ManagedNetworkSegment=[_onprem("web", "10.5.10.0/24", pid="shared")],
        )
        assert len(check._captured_errors) == 1
        assert "the LAN prefix adopted an on-prem prefix" in check._captured_errors[0]
