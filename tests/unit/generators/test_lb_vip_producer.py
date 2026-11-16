"""The VIP producer and the R_server rule of the load balancer: the plan ``plan_load_balancing`` builds, the plan applied
on the HFID fake, and the inputs another generator may still be writing (``await_lb_inputs``)."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from infrahub_sdk.protocols import CoreIPAddressPool

from generators.applications.lb_vip import await_lb_inputs
from generators.helpers.lb_vip import leg_zone
from generators.helpers.rules import RulesPlanner
from generators.model.errors import GeneratorError
from generators.plan import AddressAllocationSpec, NodeRef, NodeSpec
from generators.protocols import LoadbalancerVIP
from tests.unit.app_plan_helpers import AppClient, make_app_generator
from tests.unit.encapsulation_helpers import raw_encapsulation
from tests.unit.lb_vip_helpers import (
    _client_comp,
    _context,
    _dep,
    _instance,
    _segment,
    _web,
    application,
    plan_lb,
    run_application,
    specs_of,
    vip_data,
)
from utils.limits import LB_VIP_REREAD

UNTRACKED = {"allow_upsert": True, "update_group_context": False}


def addresses(plan: Any) -> dict[str, AddressAllocationSpec]:
    """The address allocations of the plan by identifier."""
    return {spec.identifier: spec for spec in plan.of(AddressAllocationSpec)}


class TestVipPlan:
    def test_one_vip_per_port_with_members_health_checks_and_the_address_the_plan_allocates(self) -> None:
        plan, balancing = plan_lb([_web(ports=["tcp/443", "tcp/8443"])])

        assert balancing.errors == []
        assert balancing.vips == {
            "comp-web": {
                ("tcp", 443, None): {"dc10-lb-PROD": NodeRef("vip-address-lb-vip-vip-pfx-1-comp-web-tcp-443-vip")},
                ("tcp", 8443, None): {"dc10-lb-PROD": NodeRef("vip-address-lb-vip-vip-pfx-1-comp-web-tcp-8443-vip")},
            }
        }
        first, second = vip_data(plan)
        assert [(first["protocol"], first["port"]), (second["protocol"], second["port"])] == [
            ("tcp", 443),
            ("tcp", 8443),
        ]
        assert first["hostname"] == "web.c001.example"
        assert first["load_balancer"] == {"id": "ctx-1"}
        assert first["vip_ip"] == {"id": balancing.vips["comp-web"][("tcp", 443, None)]["dc10-lb-PROD"]}
        assert first["health_checks"] == ["hc-1"]
        # The members are the servers, sorted by name
        assert first["pool_members"] == ["vm-1", "vm-2"]
        assert all(spec.ownership == "owned" for spec in specs_of(plan, LoadbalancerVIP))

    def test_the_vips_are_drawn_as_a_32_and_each_server_gets_an_address_in_the_segment_prefix_as_primary(self) -> None:
        plan, _ = plan_lb([_web()])

        allocated = addresses(plan)
        assert {identifier: spec.prefix_length for identifier, spec in allocated.items()} == {
            "comp-web-tcp-443-vip": 32,
            "vm-1-seg-web-lb-member": 24,
            "vm-2-seg-web-lb-member": 24,
        }
        assert all(spec.ownership == "shared" for spec in allocated.values())
        primaries = {
            spec.data["id"]: spec.data["primary_address"]
            for spec in plan.of(NodeSpec)
            if spec.key.startswith("primary-address-")
        }
        assert primaries == {
            "vm-1": {"id": NodeRef("member-address-lb-members-pfx-seg-web-vm-1-seg-web-lb-member")},
            "vm-2": {"id": NodeRef("member-address-lb-members-pfx-seg-web-vm-2-seg-web-lb-member")},
        }

    def test_a_server_with_a_primary_address_keeps_it_and_reserves_nothing(self) -> None:
        instances = [
            {**_instance("vm-1", _context()), "primary_address": {"id": "ip-existing-1"}},
            _instance("vm-2", _context()),
        ]

        plan, balancing = plan_lb([_web(instances=instances)])

        assert balancing.errors == []
        assert vip_data(plan)[0]["pool_members"] == ["vm-1", "vm-2"]
        assert "vm-1-seg-web-lb-member" not in addresses(plan)
        assert "vm-2-seg-web-lb-member" in addresses(plan)

    def test_the_pools_are_shared_over_the_vip_range_and_the_segment_prefix_in_their_namespace(self) -> None:
        plan, balancing = plan_lb([_web()])

        pools = {spec.data["name"]: spec for spec in specs_of(plan, CoreIPAddressPool)}
        assert set(pools) == {"lb-vip-vip-pfx-1", "lb-members-pfx-seg-web"}
        assert all(spec.ownership == "shared" for spec in pools.values())
        vip_pool = pools["lb-vip-vip-pfx-1"]
        assert vip_pool.create_only["default_prefix_length"] == 32
        assert vip_pool.create_only["resources"] == ["vip-pfx-1"]
        assert vip_pool.create_only["ip_namespace"] == {"id": "ns-prod"}
        assert balancing.locks == ["address-pool-lb-vip-vip-pfx-1", "address-pool-lb-members-pfx-seg-web"]

    def test_a_range_is_refused_and_the_single_ports_still_get_their_vip(self) -> None:
        plan, balancing = plan_lb([_web(ports=["tcp/443", "udp/30000-30010"])])

        assert [data["port"] for data in vip_data(plan)] == [443]
        assert len(balancing.errors) == 1
        assert "udp/30000-30010" in balancing.errors[0]

    def test_an_instance_with_no_serving_context_is_an_error_and_writes_no_vip(self) -> None:
        plan, balancing = plan_lb([_web(instances=[_instance("vm-1", None)])])

        assert "is in no data center" in balancing.errors[0]
        assert vip_data(plan) == []

    def test_a_context_without_a_vip_prefix_is_an_error(self) -> None:
        _, balancing = plan_lb([_web(instances=[_instance("vm-1", _context(vip_prefix=None))])])

        assert any("has no VIP prefix in a namespace" in error for error in balancing.errors)

    def test_a_vip_prefix_without_a_namespace_is_an_error(self) -> None:
        context = _context()
        context["vip_prefix"] = {"id": "vip-pfx-1", "ip_namespace": None}

        _, balancing = plan_lb([_web(instances=[_instance("vm-1", context)])])

        assert any("has no VIP prefix in a namespace" in error for error in balancing.errors)

    def test_a_segment_without_a_prefix_is_an_error(self) -> None:
        plan, balancing = plan_lb([_web(segment=_segment("seg-web", prefix=False))])

        assert any("has no gateway prefix for the pool members" in error for error in balancing.errors)
        assert vip_data(plan) == []

    def test_a_component_that_is_not_load_balanced_touches_nothing(self) -> None:
        plan, balancing = plan_lb([_web(load_balanced=False)])

        assert plan.specs == []
        assert balancing.errors == [] and balancing.vips == {}

    def test_two_contexts_give_two_vips_per_port(self) -> None:
        other = _context("ctx-2", "dc12-lb-PROD", leg="leg-2", vip_prefix="vip-pfx-2")
        instances = [_instance("vm-1", _context()), _instance("vm-2", other)]

        plan, balancing = plan_lb([_web(instances=instances)])

        assert sorted(data["load_balancer"]["id"] for data in vip_data(plan)) == ["ctx-1", "ctx-2"]
        assert set(balancing.vips["comp-web"][("tcp", 443, None)]) == {"dc10-lb-PROD", "dc12-lb-PROD"}
        assert [request.policy.name for request in balancing.requests] == ["lb-dc10-lb-PROD", "lb-dc12-lb-PROD"]

    def test_a_target_of_another_application_only_resolves_its_vip_addresses(self) -> None:
        web = _web()

        plan, balancing = plan_lb([_client_comp(_dep(web))], edges=[({}, {}, web)])

        assert balancing.errors == []
        assert vip_data(plan) == []
        assert balancing.requests == []
        assert set(balancing.vips["comp-web"][("tcp", 443, None)]) == {"dc10-lb-PROD"}
        # Its VIP is drawn under the same identifier as its own run draws it
        assert list(addresses(plan)) == ["comp-web-tcp-443-vip"]

    def test_the_errors_of_a_target_of_another_application_are_not_this_runs(self) -> None:
        web = _web(instances=[_instance("vm-1", None)])

        _, balancing = plan_lb([_client_comp(_dep(web))], edges=[({}, {}, web)])

        assert balancing.errors == []

    def test_a_target_without_a_vip_prefix_resolves_no_vip_and_no_error(self) -> None:
        web = _web(instances=[_instance("vm-1", _context(vip_prefix=None))])

        _, balancing = plan_lb([_client_comp(_dep(web))], edges=[({}, {}, web)])

        assert balancing.errors == []
        assert balancing.vips == {"comp-web": {}}

    def test_an_own_component_also_called_by_a_sibling_keeps_its_errors(self) -> None:
        web = _web(instances=[_instance("vm-1", None)])

        _, balancing = plan_lb([web, _client_comp(_dep(web))], edges=[({}, {}, web)])

        assert any("is in no data center" in error for error in balancing.errors)

    def test_an_unwired_leg_is_an_error_and_writes_no_vip(self) -> None:
        plan, balancing = plan_lb([_web(instances=[_instance("vm-1", _context(leg=None))])])

        assert "no single leg prefix" in balancing.errors[0]
        assert plan.specs == [
            spec for spec in plan.specs if isinstance(spec, NodeSpec) and spec.kind is CoreIPAddressPool
        ]


class TestVipWrites:
    """The plan applied: what is written, tracked or not."""

    def test_the_run_writes_the_vip_tracked_and_the_shared_objects_untracked(self) -> None:
        _, client = run_application(application(_web(ports=["tcp/443", "tcp/8443"])))

        assert [(data["protocol"], data["port"]) for data in client.saved("LoadbalancerVIP")] == [
            ("tcp", 443),
            ("tcp", 8443),
        ]
        assert client.save_kwargs("LoadbalancerVIP") == [{"allow_upsert": True}] * 2
        assert {data["name"] for data in client.saved("CoreIPAddressPool")} == {
            "lb-vip-vip-pfx-1",
            "lb-members-pfx-seg-web",
        }
        assert set(map(tuple, (sorted(kw.items()) for kw in client.save_kwargs("CoreIPAddressPool")))) == {
            tuple(sorted(UNTRACKED.items()))
        }
        # Each server has its address in the segment prefix as primary address, untracked
        primaries = {data["id"]: data["primary_address"] for data in client.saved("DcimVirtualDevice")}
        assert set(primaries) == {"vm-1", "vm-2"}
        assert client.save_kwargs("DcimVirtualDevice") == [UNTRACKED] * 2
        identifiers = {node.data["identifier"] for node in client.address_pool_allocations.values()}
        assert identifiers == {
            "comp-web-tcp-443-vip",
            "comp-web-tcp-8443-vip",
            "vm-1-seg-web-lb-member",
            "vm-2-seg-web-lb-member",
        }
        vip = client.saved("LoadbalancerVIP")[0]
        vip_address = next(
            node
            for node in client.address_pool_allocations.values()
            if node.data["identifier"] == "comp-web-tcp-443-vip"
        )
        assert vip["vip_ip"] == {"id": vip_address.id}

    def test_a_second_run_writes_the_same_vips_and_allocates_nothing_new(self) -> None:
        _, client = run_application(application(_web()))
        objects = {node.id for node in client.store.values()}
        first = client.saved("LoadbalancerVIP")
        client.saves.clear()

        run_application(application(_web()), client)

        assert client.saved("LoadbalancerVIP") == first
        assert {node.id for node in client.store.values()} - objects <= {
            node.id for node in client.store.values() if node.kind in {"LoadbalancerVIP", "SecurityPolicyRule"}
        }
        assert client.saved("CoreIPAddressPool") == []
        assert client.saved("SecurityPolicy") == []

    def test_an_existing_pool_is_reused_and_not_written(self) -> None:
        client = AppClient()
        client.seed("CoreIPAddressPool", name="lb-vip-vip-pfx-1", resources=[{"id": "vip-pfx-1"}])
        client.seed("CoreIPAddressPool", name="lb-members-pfx-seg-web", resources=[{"id": "pfx-seg-web"}])

        run_application(application(_web()), client)

        assert client.saved("CoreIPAddressPool") == []
        assert len(client.saved("LoadbalancerVIP")) == 1

    def test_the_run_takes_the_locks_of_the_shared_objects_it_may_create(self) -> None:
        gen, _ = run_application(application(_web()))

        assert "address-pool-lb-vip-vip-pfx-1" in gen.locked_keys
        assert gen.locked_keys.count("lb-policy-dc10-lb-PROD") == 1

    def test_a_load_balancing_error_fails_the_run_before_any_write(self) -> None:
        client = AppClient()
        client.execute_graphql = AsyncMock(return_value={})

        with (
            patch("generators.applications.lb_vip.asyncio.sleep", AsyncMock()),
            pytest.raises(GeneratorError, match="load balancing: .*no single leg prefix"),
        ):
            run_application(application(_web(instances=[_instance("vm-1", _context(leg=None))])), client)

        assert client.saves == []


class TestServerRule:
    def test_one_r_server_per_vip_port_in_the_segment_less_context_policy(self) -> None:
        _, client = run_application(application(_web(ports=["tcp/443", "tcp/8443"])))

        (policy,) = client.saved("SecurityPolicy")
        assert policy["name"] == "lb-dc10-lb-PROD"
        assert "segment" not in policy
        assert client.save_kwargs("SecurityPolicy") == [UNTRACKED]
        rules = {data["name"]: data for data in client.saved("SecurityPolicyRule")}
        assert list(rules) == ["web.c001.example-tcp-443", "web.c001.example-tcp-8443"]
        data = rules["web.c001.example-tcp-443"]
        assert data["source_prefixes"] == [{"id": "leg-1"}]
        assert data["destination_segment"] == {"id": "seg-web"}
        assert (data["protocol"], data["port_start"], data["index"]) == ("tcp", 443, 100)
        assert rules["web.c001.example-tcp-8443"]["index"] == 110
        assert data["expires_at"] is None
        assert "source_segment" not in data
        # The leg /29 has no segment: the zone of the leg's VLAN slot qualifies it (transforms/helpers/policy.py).
        assert data["source_zone"] == {"hfid": ["PROD"]}
        assert client.save_kwargs("SecurityPolicyRule") == [{"allow_upsert": True}] * 2

    def test_two_backends_on_one_context_share_the_policy_and_follow_its_indexes(self) -> None:
        client = AppClient()
        policy = client.seed("SecurityPolicy", name="lb-dc10-lb-PROD")
        client.seed("SecurityPolicyRule", name="hand", index=100, policy={"id": policy.id})
        other = {
            **_web(segment=_segment("seg-api"), instances=[_instance("vm-3", _context())]),
            "id": "comp-api",
            "name": "api",
            "fqdn": "api.c001.example",
        }

        _, client = run_application(application(_web(), other), client)

        assert client.saved("SecurityPolicy") == []
        assert {data["name"]: data["index"] for data in client.saved("SecurityPolicyRule")} == {
            "web.c001.example-tcp-443": 110,
            "api.c001.example-tcp-443": 120,
        }

    def test_the_source_zone_is_the_zone_of_the_context_leg(self) -> None:
        assert leg_zone(_context()) == "PROD"
        assert leg_zone(_context(leg=None)) is None

    def test_a_leg_with_no_zone_leaves_the_source_zone_out(self) -> None:
        payload = RulesPlanner.build_lb_server_rule_payload(
            rule_name="web",
            context_name="dc10-lb-PROD",
            component={"fqdn": "web"},
            segment=_segment("seg-web"),
            leg_prefix_id="leg-1",
            leg_zone=None,
            port=("tcp", 443, None),
        )

        assert "source_zone" not in payload

    def test_a_microsegmented_backend_carries_its_component_tag(self) -> None:
        segment = {**_segment("seg-web"), "isolation_mode": "microsegmented"}
        payload = RulesPlanner.build_lb_server_rule_payload(
            rule_name="web",
            context_name="dc10-lb-PROD",
            component={"fqdn": "web", "security_tag": {"id": "tag-web"}},
            segment=segment,
            leg_prefix_id="leg-1",
            leg_zone="PROD",
            port=("tcp", 443, None),
        )

        assert payload["apply_on_switch"] is True
        assert payload["destination_security_tag"] == {"id": "tag-web"}
        assert "source_security_tag" not in payload


# Concurrent inputs: re-read what another generator of the load still writes


def _segment_read(seg_id: str, *, prefix: bool) -> dict[str, Any]:
    """A lb_segment_gateways.gql response for one segment, with or without its gateway prefix yet."""
    gateway = None
    if prefix:
        ip_prefix = {
            "security_zone": {"node": {"id": "ns"}},
            "id": f"pfx-{seg_id}",
            "prefix": {"value": "10.60.0.0/24"},
            "ip_namespace": {"node": {"name": {"value": "default"}}},
        }
        gateway = {"node": {"ip_prefix": {"node": ip_prefix}}}
    return {"ManagedVxlanSegment": {"edges": [{"node": {"id": seg_id, "gateway": gateway}}]}}


def _context_read(dep_id: str, *, linked: bool = True) -> dict[str, Any]:
    """A lb_serving_contexts.gql response for one deployment: its complete context, or none linked yet."""
    context = None
    if linked:
        leg = {
            "__typename": "DcimVirtualInterface",
            "id": "lb-1-leg",
            "name": {"value": "1.1.3100"},
            "role": {"value": "service"},
            **raw_encapsulation(3100, {"name": {"value": "1.1"}}),
            "ip_address": {"node": {"ip_prefix": {"node": {"id": "leg-1"}}}},
        }
        uplink = {
            "__typename": "DcimPhysicalInterface",
            "id": "lb-1-up",
            "name": {"value": "1.1"},
            "role": {"value": "uplink"},
        }
        member = {
            "__typename": "DcimPhysicalDevice",
            "id": "lb-1",
            "name": {"value": "lb-1"},
            "role": {"value": "load-balancer"},
            "shared_capabilities": {
                "edges": [
                    {"node": {"__typename": "ManagedLoadbalancerContext", "id": "ctx-1", "vlan_id": {"value": 3100}}}
                ]
            },
            "interfaces": {"edges": [{"node": uplink}, {"node": leg}]},
        }
        context = {
            "id": "ctx-1",
            "name": {"value": "dc10-lb-PROD"},
            "vip_prefix": {"node": {"id": "vip-pfx-1", "ip_namespace": {"node": {"id": "ns-prod"}}}},
            "shared_devices": {"edges": [{"node": member}]},
        }
    node = {"id": dep_id, "serving_loadbalancer_context": {"node": context}}
    return {"TopologyCustomerDC": {"edges": [{"node": node}]}}


def _awaiting() -> Any:
    """A generator with nothing in flight and a client whose re-reads are set by the test."""
    gen = make_app_generator()
    gen.client.execute_graphql = AsyncMock(return_value={})
    return gen


def _await(gen: Any, components: list[dict[str, Any]], edges: list[Any] | None = None) -> AsyncMock:
    """``await_lb_inputs`` with a recorded sleep between two re-reads."""
    sleep = AsyncMock()
    with patch("generators.applications.lb_vip.asyncio.sleep", sleep):
        asyncio.run(await_lb_inputs(gen, components, edges or []))
    return sleep


def _reads(gen: Any) -> list[dict[str, Any]]:
    """The variables of every re-read query, in order."""
    return [call.kwargs["variables"] for call in gen.client.execute_graphql.await_args_list]


class TestConcurrentInputs:
    def test_a_segment_prefix_written_by_the_second_read_gets_the_members(self) -> None:
        """add_vxlan_segment addresses the segment while this run reads it: the second read sees the prefix."""
        gen = _awaiting()
        gen.client.execute_graphql = AsyncMock(
            side_effect=[_segment_read("seg-web", prefix=False), _segment_read("seg-web", prefix=True)]
        )
        web = _web(segment=_segment("seg-web", prefix=False))

        sleep = _await(gen, [web])

        gen.wait_for_parent_generators.assert_awaited_once_with(("add_vxlan_segment",), "seg-web")
        assert _reads(gen) == [{"ids": ["seg-web"]}, {"ids": ["seg-web"]}]
        sleep.assert_awaited_once_with(LB_VIP_REREAD.delay)
        plan, balancing = plan_lb([web])
        assert balancing.errors == []
        assert vip_data(plan)[0]["pool_members"] == ["vm-1", "vm-2"]

    def test_a_segment_prefix_that_never_lands_is_the_error_after_the_last_read(self) -> None:
        """Bounded: LB_VIP_REREAD.attempts reads with a sleep between two, then the error the payload alone gave."""
        gen = _awaiting()
        gen.client.execute_graphql = AsyncMock(return_value=_segment_read("seg-web", prefix=False))
        web = _web(segment=_segment("seg-web", prefix=False))

        sleep = _await(gen, [web])

        assert gen.client.execute_graphql.await_count == LB_VIP_REREAD.attempts
        assert sleep.await_count == LB_VIP_REREAD.attempts - 1
        assert "still missing" in gen.logger.warning.call_args.args[0]
        _, balancing = plan_lb([web])
        assert balancing.errors == ["web.c001.example: segment seg-web-name has no gateway prefix for the pool members"]

    def test_two_components_on_one_segment_share_one_read(self) -> None:
        """One wait and one read per attempt for the segment, written into both components' dicts."""
        gen = _awaiting()
        gen.client.execute_graphql = AsyncMock(return_value=_segment_read("seg-web", prefix=True))
        web = _web(segment=_segment("seg-web", prefix=False))
        api: dict[str, Any] = {
            **_web(segment=_segment("seg-web", prefix=False)),
            "id": "comp-api",
            "fqdn": "api.c001.example",
        }

        sleep = _await(gen, [web, api])

        gen.wait_for_parent_generators.assert_awaited_once()
        assert _reads(gen) == [{"ids": ["seg-web"]}]
        sleep.assert_not_awaited()
        assert all(component.get("network_segment", {}).get("gateway") for component in (web, api))

    def test_ready_inputs_are_never_re_read(self) -> None:
        """The normal run: the payload has the prefix and the context, no extra round trip."""
        gen = _awaiting()

        _await(gen, [_web()])

        gen.wait_for_parent_generators.assert_not_awaited()
        gen.client.execute_graphql.assert_not_awaited()

    def test_a_serving_context_linked_by_the_second_read_gets_the_vips(self) -> None:
        """add_customer_deployment_dc boards the deployment while this run reads it."""
        gen = _awaiting()
        gen.client.execute_graphql = AsyncMock(
            side_effect=[_context_read("dep-c001", linked=False), _context_read("dep-c001")]
        )
        web = _web(instances=[_instance("vm-1", None), _instance("vm-2", None)])

        _await(gen, [web])

        gen.wait_for_parent_generators.assert_awaited_once_with(("add_customer_deployment_dc",), "dep-c001")
        assert _reads(gen) == [{"ids": ["dep-c001"]}, {"ids": ["dep-c001"]}]
        _, balancing = plan_lb([web])
        assert balancing.errors == []
        assert set(balancing.vips["comp-web"][("tcp", 443, None)]) == {"dc10-lb-PROD"}

    def test_a_context_without_its_vip_range_yet_is_re_read(self) -> None:
        """The boarding run links the context before it allocates the VIP range and wires the leg."""
        gen = _awaiting()
        gen.client.execute_graphql = AsyncMock(return_value=_context_read("dep-c001"))

        _await(gen, [_web(instances=[_instance("vm-1", _context(vip_prefix=None))])])

        assert _reads(gen) == [{"ids": ["dep-c001"]}]

    def test_the_context_of_an_edge_target_is_re_read_for_its_vip_addresses(self) -> None:
        """The target's VIPs are this run's R_client rules: its context is waited for too."""
        gen = _awaiting()
        gen.client.execute_graphql = AsyncMock(return_value=_context_read("dep-c001"))
        web = _web(instances=[_instance("vm-1", None)])

        _await(gen, [_client_comp(_dep(web))], [({}, {}, web)])

        assert _reads(gen) == [{"ids": ["dep-c001"]}]
        _, balancing = plan_lb([_client_comp(_dep(web))], [({}, {}, web)])
        assert set(balancing.vips["comp-web"][("tcp", 443, None)]) == {"dc10-lb-PROD"}
