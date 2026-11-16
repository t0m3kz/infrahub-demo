"""Unit tests for the cloud branch of the load balancer VIP producer."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from generators.helpers.lb_vip import (
    cloud_lb_name,
    cloud_lb_scheme,
    cloud_lb_type,
    cloud_placement,
    is_cloud_component,
    lb_layer,
    listener_protocol,
)
from generators.plan import AddressAllocationSpec, NodeRef, NodeSpec
from generators.protocols import CloudLoadBalancer, LoadbalancerVIP
from tests.unit.lb_vip_helpers import (
    _CLOUD_FQDN,
    _CLOUD_LB,
    _client_comp,
    _cloud_frontend,
    _cloud_instance,
    _dep,
    _instance,
    _subnet,
    _web,
    application,
    plan_lb,
    run_application,
    specs_of,
    vip_data,
)
from utils.limits import CLOUD_LB_NAME_MAX


class TestCloudPlanning:
    def test_a_component_of_cloud_instances_only_is_a_cloud_component(self) -> None:
        assert is_cloud_component(_cloud_frontend()) is True

    def test_mixed_or_on_prem_or_no_instances_is_not(self) -> None:
        """A cloud instance next to an on-prem one stays on the on-prem path, where it is an error."""
        assert is_cloud_component(_cloud_frontend(instances=[_cloud_instance("i-1"), _instance("vm-1", None)])) is False
        assert is_cloud_component(_web()) is False
        assert is_cloud_component(_cloud_frontend(instances=[])) is False

    def test_short_fqdn_is_its_slug(self) -> None:
        assert cloud_lb_name({"fqdn": "web.c016.example"}) == "web-c016-example"

    def test_long_fqdn_is_cut_to_the_provider_limit_with_a_hash(self) -> None:
        name = cloud_lb_name({"fqdn": _CLOUD_FQDN})

        assert len(name) == CLOUD_LB_NAME_MAX
        assert name.startswith("frontend-custody-api-c0")
        assert name == cloud_lb_name({"fqdn": _CLOUD_FQDN})  # deterministic
        assert not name.startswith("-") and not name.endswith("-")

    def test_two_long_fqdns_with_one_prefix_get_two_names(self) -> None:
        """Unique per component: the hash is of the whole fqdn, not of the cut slug."""
        first = cloud_lb_name({"fqdn": "frontend.custody-api.c003.demo.local"})
        second = cloud_lb_name({"fqdn": "frontend.custody-api.c003.demo.other"})

        assert first != second
        assert first[:23] == second[:23]

    @pytest.mark.parametrize(
        ("profile", "scheme"),
        [
            ("internet_exposed", "internet_facing"),
            ("internal_standard", "internal"),
            ("fintech_strict", "internal"),
            (None, "internal"),
        ],
    )
    def test_scheme_follows_the_application_security_profile(self, profile: str | None, scheme: str) -> None:
        assert cloud_lb_scheme(profile) == scheme

    def test_placement_is_the_instances_subnets_in_their_one_vpc(self) -> None:
        component = _cloud_frontend(
            instances=[_cloud_instance("i-1", subnet=_subnet("sub-b")), _cloud_instance("i-2", subnet=_subnet("sub-a"))]
        )

        network, segments, errors = cloud_placement(component)

        assert errors == []
        assert network is not None and network["id"] == "vpc-1"
        assert [segment["id"] for segment in segments] == ["sub-a", "sub-b"]

    def test_instances_in_two_vpcs_are_an_error_and_place_nothing(self) -> None:
        component = _cloud_frontend(
            instances=[
                _cloud_instance("i-1", subnet=_subnet("sub-a", "vpc-1")),
                _cloud_instance("i-2", subnet=_subnet("sub-b", "vpc-2")),
            ]
        )

        network, segments, errors = cloud_placement(component)

        assert (network, segments) == (None, [])
        assert "several virtual networks (vpc-1-name, vpc-2-name)" in errors[0]

    def test_no_subnet_is_a_missing_virtual_network(self) -> None:
        network, _segments, errors = cloud_placement(_cloud_frontend(instances=[_cloud_instance("i-1", subnet={})]))

        assert network is None
        assert errors == [
            f"{_CLOUD_FQDN}: cloud instance i-1 is in no network segment",
            f"{_CLOUD_FQDN}: no virtual network for the cloud load balancer",
        ]

    def test_a_component_is_layer_4_unless_it_says_layer_7(self) -> None:
        assert (lb_layer({}), lb_layer({"lb_layer": "l4"}), lb_layer({"lb_layer": "l7"})) == ("l4", "l4", "l7")
        assert cloud_lb_type({"lb_layer": "l7"}) == "l7_cloud"
        assert cloud_lb_type({}) == "l4_cloud"

    def test_layer_4_listener_keeps_the_port_protocol_and_layer_7_is_http_or_https(self) -> None:
        assert listener_protocol({}, ("udp", 53, None)) == ("udp", None)
        assert listener_protocol({"lb_layer": "l7"}, ("tcp", 80, None)) == ("http", None)
        assert listener_protocol({"lb_layer": "l7", "lb_certificate": "arn:cert"}, ("tcp", 443, None)) == (
            "https",
            None,
        )

    def test_udp_cannot_be_balanced_at_layer_7(self) -> None:
        protocol, error = listener_protocol({"fqdn": "web", "lb_layer": "l7"}, ("udp", 53, None))

        assert protocol is None
        assert error == "web: udp/53 cannot be balanced at layer 7 (http/https is tcp)"


class TestCloudPlan:
    def test_one_cloud_load_balancer_and_one_vip_per_port_without_an_address(self) -> None:
        plan, balancing = plan_lb([_cloud_frontend(ports=["tcp/443", "udp/53"])], security_profile="internet_exposed")

        # Nothing for R_client: a cloud component is reached directly.
        assert (balancing.vips, balancing.errors, balancing.requests) == ({}, [], [])
        (load_balancer,) = specs_of(plan, CloudLoadBalancer)
        assert load_balancer.data == {
            "name": _CLOUD_LB,
            "description": f"Cloud load balancer of {_CLOUD_FQDN}",
            "status": "active",
            "lb_type": "l4_cloud",
            "scheme": "internet_facing",
            "virtual_network": {"id": "vpc-1"},
            "network_segments": ["sub-a"],
        }
        assert load_balancer.ownership == "owned"
        first, second = vip_data(plan)
        assert [(first["protocol"], first["port"]), (second["protocol"], second["port"])] == [("tcp", 443), ("udp", 53)]
        assert first["hostname"] == _CLOUD_FQDN
        assert first["load_balancer"] == {"id": NodeRef(f"cloud-lb-{_CLOUD_LB}")}
        assert "vip_ip" not in first
        assert first["health_checks"] == ["hc-1"]
        assert first["pool_members"] == ["i-1", "i-2"]
        assert balancing.locks == [f"cloud-lb-{_CLOUD_LB}"]

    def test_no_context_pool_address_or_rule_for_a_cloud_component(self) -> None:
        plan, balancing = plan_lb([_cloud_frontend()])

        assert plan.of(AddressAllocationSpec) == []
        assert [spec.kind for spec in plan.of(NodeSpec)] == [CloudLoadBalancer, LoadbalancerVIP]
        assert balancing.requests == []

    def test_an_internal_application_gets_an_internal_load_balancer(self) -> None:
        plan, _ = plan_lb([_cloud_frontend()], security_profile="internal_standard")

        assert specs_of(plan, CloudLoadBalancer)[0].data["scheme"] == "internal"

    def test_the_load_balancer_and_its_vips_are_tracked_by_the_run(self) -> None:
        plan, _ = plan_lb([_cloud_frontend()])

        assert [spec.ownership for spec in plan.of(NodeSpec)] == ["owned", "owned"]

    def test_the_targets_are_the_instances_and_nothing_is_written_to_ipam(self) -> None:
        plan, _ = plan_lb([_cloud_frontend()])

        assert vip_data(plan)[0]["pool_members"] == ["i-1", "i-2"]
        assert plan.of(AddressAllocationSpec) == []

    def test_a_layer_7_component_gets_a_layer_7_load_balancer_and_http_listeners(self) -> None:
        plan, _ = plan_lb([_cloud_frontend(ports=["tcp/80", "tcp/443"], lb_layer="l7", lb_certificate="arn:cert/1")])

        assert specs_of(plan, CloudLoadBalancer)[0].data["lb_type"] == "l7_cloud"
        payloads = vip_data(plan)
        assert [(p["protocol"], p["port"]) for p in payloads] == [("http", 80), ("https", 443)]
        assert [p.get("ssl_certificate") for p in payloads] == [None, "arn:cert/1"]

    def test_a_layer_7_component_without_a_certificate_is_http(self) -> None:
        plan, _ = plan_lb([_cloud_frontend(lb_layer="l7")])

        payload = vip_data(plan)[0]
        assert (payload["protocol"], "ssl_certificate" in payload) == ("http", False)

    def test_a_udp_port_of_a_layer_7_component_is_an_error_and_gets_no_listener(self) -> None:
        plan, balancing = plan_lb([_cloud_frontend(ports=["tcp/443", "udp/53"], lb_layer="l7")])

        assert [p["port"] for p in vip_data(plan)] == [443]
        assert balancing.errors == [f"{_CLOUD_FQDN}: udp/53 cannot be balanced at layer 7 (http/https is tcp)"]

    def test_a_missing_virtual_network_is_an_error_and_plans_nothing(self) -> None:
        plan, balancing = plan_lb([_cloud_frontend(instances=[_cloud_instance("i-1", subnet={})])])

        assert f"{_CLOUD_FQDN}: no virtual network for the cloud load balancer" in balancing.errors
        assert plan.specs == []

    def test_a_range_is_refused_and_the_single_ports_still_get_their_vip(self) -> None:
        plan, balancing = plan_lb([_cloud_frontend(ports=["tcp/443", "udp/30000-30010"])])

        assert [p["port"] for p in vip_data(plan)] == [443]
        assert len(balancing.errors) == 1
        assert "udp/30000-30010" in balancing.errors[0]

    def test_an_instance_without_an_address_is_still_a_member(self) -> None:
        """What a target is (an id, an address) is the provider's template to decide; the generator only links."""
        plan, balancing = plan_lb(
            [_cloud_frontend(instances=[_cloud_instance("i-1", None), _cloud_instance("i-2", "10.30.1.12/24")])]
        )

        assert balancing.errors == []
        assert vip_data(plan)[0]["pool_members"] == ["i-1", "i-2"]

    def test_a_cloud_target_of_another_application_plans_nothing_and_has_no_error(self) -> None:
        """Its own run builds its load balancer; this run's rule into it is the direct one."""
        frontend = _cloud_frontend(ports=["udp/30000-30010"])

        plan, balancing = plan_lb([_client_comp(_dep(frontend))], edges=[({}, {}, frontend)])

        assert (plan.specs, balancing.vips, balancing.errors) == ([], {}, [])


class TestCloudWrites:
    def test_the_run_writes_the_load_balancer_before_its_vips_both_tracked(self) -> None:
        _, client = run_application(application(_cloud_frontend(), profile="internet_exposed"))

        assert [kind for kind, *_ in client.saves] == ["CloudLoadBalancer", "LoadbalancerVIP"]
        load_balancer_id = client.saves[0][1]
        assert client.saved("LoadbalancerVIP")[0]["load_balancer"] == {"id": load_balancer_id}
        assert (
            client.save_kwargs("CloudLoadBalancer") + client.save_kwargs("LoadbalancerVIP")
            == [{"allow_upsert": True}] * 2
        )
        assert client.saved("CloudLoadBalancer")[0]["scheme"] == "internet_facing"

    def test_a_second_run_writes_the_same_load_balancer_and_vips(self) -> None:
        """The server finds the load balancer by its uniqueness constraint (virtual network and name), so the second run
        sends the same data; the VIP differs by the id of the load balancer the fake created anew."""
        _, client = run_application(application(_cloud_frontend()))
        load_balancers = client.saved("CloudLoadBalancer")
        vips = [{**data, "load_balancer": None} for data in client.saved("LoadbalancerVIP")]
        client.saves.clear()

        run_application(application(_cloud_frontend()), client)

        assert client.saved("CloudLoadBalancer") == load_balancers
        assert [{**data, "load_balancer": None} for data in client.saved("LoadbalancerVIP")] == vips

    def test_the_run_locks_the_load_balancer_name(self) -> None:
        gen, _ = run_application(application(_cloud_frontend()))

        assert f"cloud-lb-{_CLOUD_LB}" in gen.locked_keys

    def test_a_cloud_component_is_never_re_read(self) -> None:
        """A cloud segment has no gateway prefix and a cloud instance no context: nothing to wait for."""
        gen, client = run_application(application(_cloud_frontend()))

        gen.wait_for_parent_generators.assert_not_awaited()
        execute_graphql = client.execute_graphql
        assert isinstance(execute_graphql, AsyncMock)
        execute_graphql.assert_not_awaited()
