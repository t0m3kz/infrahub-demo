"""Unit tests for the cloud branch of the load balancer VIP producer.

A cloud component's CloudLoadBalancer and its address-less VIPs: placement,
members, load balancer name and scheme.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from generators.helpers.lb_vip import (
    cloud_lb_name,
    cloud_lb_scheme,
    cloud_members,
    cloud_placement,
    is_cloud_component,
)
from generators.lb_vip import CLOUD_LB_GROUP
from generators.protocols import CloudLoadBalancer, LoadbalancerVIP
from tests.unit.lb_vip_helpers import (
    _CLOUD_FQDN,
    _CLOUD_LB,
    _client_comp,
    _cloud_frontend,
    _cloud_instance,
    _dep,
    _instance,
    _lb_gen,
    _reconcile,
    _subnet,
    _vip_gen,
    _vip_payloads,
    _vpc,
    _web,
)
from utils.limits import CLOUD_LB_NAME_MAX


def _cloud_lb_payloads(gen: Any) -> list[dict[str, Any]]:
    return [
        call.kwargs["data"] for call in gen.client.create.await_args_list if call.kwargs["kind"] is CloudLoadBalancer
    ]


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

    def test_members_are_the_private_ips_in_the_namespace_of_their_cidr_block(self) -> None:
        members, errors = cloud_members(_cloud_frontend(), _vpc())

        assert errors == []
        assert members == [("i-1", "10.30.1.11/24", "ns-prod"), ("i-2", "10.30.1.12/24", "ns-prod")]

    def test_member_without_private_ip_or_outside_every_block_is_an_error(self) -> None:
        component = _cloud_frontend(
            instances=[_cloud_instance("i-1", None), _cloud_instance("i-2", "192.0.2.10/24"), _cloud_instance("i-3")]
        )

        members, errors = cloud_members(component, _vpc())

        assert members == [("i-3", "10.30.1.11/24", "ns-prod")]
        assert "i-1 has no private_ip" in errors[0]
        assert "i-2 private_ip 192.0.2.10/24 is in no CIDR block of vpc-1-name" in errors[1]


class TestCloudProducer:
    def test_one_cloud_load_balancer_and_one_vip_per_port_without_an_address(self) -> None:
        gen = _lb_gen()

        vips, errors = _reconcile(
            gen, [_cloud_frontend(ports=["tcp/443", "udp/53"])], security_profile="internet_exposed"
        )

        # Nothing for R_client: a cloud component is reached directly.
        assert (vips, errors) == ({}, [])
        assert _cloud_lb_payloads(gen) == [
            {
                "name": _CLOUD_LB,
                "description": f"Cloud load balancer of {_CLOUD_FQDN}",
                "status": "active",
                "lb_type": "l4_cloud",
                "scheme": "internet_facing",
                "virtual_network": {"id": "vpc-1"},
                "network_segments": ["sub-a"],
                "member_of_groups": [CLOUD_LB_GROUP],
            }
        ]
        assert CLOUD_LB_GROUP == {"hfid": ["cloud_loadbalancers"], "kind": "CoreStandardGroup"}
        payloads = _vip_payloads(gen)
        assert [(p["protocol"], p["port"]) for p in payloads] == [("tcp", 443), ("udp", 53)]
        first = payloads[0]
        assert first["hostname"] == _CLOUD_FQDN
        assert first["load_balancer"] == {"id": f"id-{_CLOUD_LB}"}
        assert "vip_ip" not in first
        assert first["component"] == {"id": "comp-frontend"}
        assert first["health_checks"] == ["hc-1"]
        assert first["pool_members"] == ["ip:10.30.1.11/24", "ip:10.30.1.12/24"]
        assert f"cloud-lb-{_CLOUD_LB}" in gen.locks

    def test_no_context_pool_or_rule_for_a_cloud_component(self) -> None:
        gen = _lb_gen()

        _reconcile(gen, [_cloud_frontend()])

        gen.ensure_prefix_address_pool.assert_not_awaited()
        gen.allocate_prefix_address.assert_not_awaited()
        gen._get_or_create_by_name.assert_not_awaited()
        gen._upsert_policy_rule.assert_not_awaited()

    def test_internal_application_gets_an_internal_load_balancer(self) -> None:
        gen = _lb_gen()

        _reconcile(gen, [_cloud_frontend()], security_profile="internal_standard")

        assert _cloud_lb_payloads(gen)[0]["scheme"] == "internal"

    def test_load_balancer_and_vips_are_tracked_by_the_run(self) -> None:
        gen = _lb_gen()

        _reconcile(gen, [_cloud_frontend()])

        saved = [call.kwargs["kind"] for call in gen.client.create.await_args_list]
        assert saved == [CloudLoadBalancer, LoadbalancerVIP]
        for node in gen.created:
            node.save.assert_awaited_once_with(allow_upsert=True)

    def test_member_addresses_are_the_instances_and_untracked_under_their_lock(self) -> None:
        gen = _lb_gen()

        _reconcile(gen, [_cloud_frontend()])

        calls = [call.kwargs for call in gen.upsert_ip_address.await_args_list]
        assert [(c["address"], c["ip_namespace"], c["track"]) for c in calls] == [
            ("10.30.1.11/24", {"id": "ns-prod"}, False),
            ("10.30.1.12/24", {"id": "ns-prod"}, False),
        ]
        assert calls[0]["description"] == "Private address of cloud instance i-1"
        assert "ip-address-ns-prod-10.30.1.11/24" in gen.locks

    def test_second_run_writes_the_same_load_balancer_and_vips(self) -> None:
        first, second = _lb_gen(), _lb_gen()

        _reconcile(first, [_cloud_frontend()])
        _reconcile(second, [_cloud_frontend()])

        assert _cloud_lb_payloads(first) == _cloud_lb_payloads(second)
        assert _vip_payloads(first) == _vip_payloads(second)
        assert len(_cloud_lb_payloads(second)) == 1

    def test_missing_virtual_network_is_an_error_and_writes_nothing(self) -> None:
        gen = _lb_gen()

        _vips, errors = _reconcile(gen, [_cloud_frontend(instances=[_cloud_instance("i-1", subnet={})])])

        assert f"{_CLOUD_FQDN}: no virtual network for the cloud load balancer" in errors
        gen.client.create.assert_not_awaited()
        gen.upsert_ip_address.assert_not_awaited()

    def test_range_is_refused_and_the_single_ports_still_get_their_vip(self) -> None:
        gen = _lb_gen()

        _vips, errors = _reconcile(gen, [_cloud_frontend(ports=["tcp/443", "udp/30000-30010"])])

        assert [p["port"] for p in _vip_payloads(gen)] == [443]
        assert len(errors) == 1
        assert "udp/30000-30010" in errors[0]

    def test_unaddressable_member_is_an_error_and_left_out(self) -> None:
        gen = _lb_gen()

        _vips, errors = _reconcile(
            gen, [_cloud_frontend(instances=[_cloud_instance("i-1", None), _cloud_instance("i-2", "10.30.1.12/24")])]
        )

        assert "i-1 has no private_ip" in errors[0]
        assert _vip_payloads(gen)[0]["pool_members"] == ["ip:10.30.1.12/24"]

    def test_failed_load_balancer_save_is_an_error_and_gets_no_vip(self) -> None:
        gen = _vip_gen()
        gen.client.create.return_value.save = AsyncMock(side_effect=RuntimeError("boom"))

        _vips, errors = _reconcile(gen, [_cloud_frontend()])

        assert errors == [f"{_CLOUD_FQDN}: cloud load balancer {_CLOUD_LB}: boom"]
        assert _vip_payloads(gen) == []

    def test_cloud_target_of_another_application_writes_nothing_and_has_no_error(self) -> None:
        """Its own run builds its load balancer; this run's rule into it is the direct one."""
        gen = _lb_gen()
        frontend = _cloud_frontend(ports=["udp/30000-30010"])

        vips, errors = _reconcile(gen, [_client_comp(_dep(frontend))], edges=[({}, {}, frontend)])

        assert (vips, errors) == ({}, [])
        gen.client.create.assert_not_awaited()
        gen.upsert_ip_address.assert_not_awaited()
