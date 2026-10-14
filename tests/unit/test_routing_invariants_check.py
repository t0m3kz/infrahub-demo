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


def _leak(ns_a: dict[str, Any], ns_z: dict[str, Any], route_target: str | None = None) -> dict[str, Any]:
    return {
        "id": "leak-1",
        "name": _v("leak-1"),
        "route_target": _v(route_target),
        "namespace_a": {"node": ns_a},
        "namespace_z": {"node": ns_z},
    }


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
                        "interface_capabilities": {
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


class TestRouteTargetJoins:
    """Route-target imports may only join namespaces of one type."""

    def test_same_type_leak_passes(self) -> None:
        """A leak between two prod namespaces is accepted."""
        check = _run(TopologyRouteLeakExchange=[_leak(_ns("A", "prod", 1), _ns("B", "prod", 2))])
        assert check._captured_errors == []

    def test_cross_type_leak_fails(self) -> None:
        """A leak joining prod and non_prod is an error."""
        check = _run(TopologyRouteLeakExchange=[_leak(_ns("A", "prod", 1), _ns("B", "non_prod", 2))])
        assert len(check._captured_errors) == 1
        assert "same type" in check._captured_errors[0]

    def test_untyped_namespace_fails(self) -> None:
        """A leak with an untyped side cannot be verified and is an error."""
        check = _run(TopologyRouteLeakExchange=[_leak(_ns("A", "prod", 1), _ns("B", None, 2))])
        assert len(check._captured_errors) == 1

    def test_explicit_route_target_naming_foreign_vni_fails(self) -> None:
        """An explicit route_target carrying another type's L3 VNI is an error."""
        prod, other = _ns("A", "prod", 100), _ns("N", "non_prod", 200)
        check = _run(
            IpamNamespace=[prod, other],
            TopologyRouteLeakExchange=[_leak(prod, _ns("B", "prod", 101), "65000:200")],
        )
        assert any("route_target" in e for e in check._captured_errors)

    def test_explicit_route_target_same_type_passes(self) -> None:
        """An explicit route_target on a same-type VNI is accepted."""
        prod = _ns("A", "prod", 100)
        check = _run(
            IpamNamespace=[prod],
            TopologyRouteLeakExchange=[_leak(prod, _ns("B", "prod", 101), "65000:100")],
        )
        assert check._captured_errors == []

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
