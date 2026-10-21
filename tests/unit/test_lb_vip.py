"""Unit tests for the load balancer VIP producer and its rules.

- generators/helpers/lb_vip.py: ports, serving contexts, leg prefix, identifiers,
  and the cloud branch (cloud component, load balancer name/scheme, placement,
  members)
- generators/lb_vip.py (LoadbalancerVipMixin): VIPs, pool members, R_server;
  a cloud component's CloudLoadBalancer and its address-less VIPs
- AppApplicationGenerator: R_client replaces the direct rule (strict load
  balancing) for an on-prem target, a cloud target keeps the direct rule,
  errors fail the run after the rules, and a component that stops being
  load_balanced loses its VIPs, rules and cloud load balancer to the run's
  tracking
- SegmentFirewallMixin._reconcile_rule_payload: the selectors and the
  destination segment of an existing rule
- LoadbalancerVipMixin._await_lb_inputs: the bounded re-read of a segment
  prefix or serving context another generator of the same load still writes
- queries/topology/add/application.gql: the serving context path
- load_balanced_components_in: what a deployment moving to another serving
  context re-runs (add_customer_deployment_dc's relink fan-out)
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from generators.helpers.lb_vip import (
    CLOUD_LB_NAME_MAX,
    cloud_lb_name,
    cloud_lb_scheme,
    cloud_members,
    cloud_placement,
    is_cloud_component,
    leg_prefix_id,
    member_identifier,
    serving_contexts,
    vip_identifier,
    vip_ports,
)
from generators.helpers.rules import RulesPlanner
from generators.lb_vip import CLOUD_LB_GROUP, READ_ATTEMPTS, READ_INTERVAL, load_balanced_components_in
from generators.logger import GeneratorError
from generators.protocols import (
    AppComponent,
    CloudLoadBalancer,
    DcimPhysicalDevice,
    DcimVirtualDevice,
    LoadbalancerVIP,
    SecurityPolicy,
    SecurityPolicyRule,
)
from generators.segment_firewall import SegmentFirewallMixin
from generators.topology.application_security import AppApplicationGenerator

# ---------------------------------------------------------------------------
# Fixtures (cleaned application.gql shape)
# ---------------------------------------------------------------------------


def _context(
    ctx_id: str = "ctx-1",
    name: str = "dc10-lb-PROD",
    *,
    leg: str | None = "leg-1",
    vip_prefix: str | None = "vip-pfx-1",
) -> dict[str, Any]:
    """A serving context: its member sub-interfaces carry leg addresses, its border port none."""
    capabilities: list[dict[str, Any]] = [{"typename": "DcimPhysicalInterface"}]
    if leg:
        capabilities += [
            {"typename": "DcimVirtualInterface", "ip_address": {"ip_prefix": {"id": leg}}},
            {"typename": "DcimVirtualInterface", "ip_address": {"ip_prefix": {"id": leg}}},
        ]
    return {
        "id": ctx_id,
        "name": name,
        "vip_prefix": {"id": vip_prefix, "ip_namespace": {"id": "ns-prod"}} if vip_prefix else None,
        "capabilities": capabilities,
    }


def _instance(instance_id: str, context: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "id": instance_id,
        "name": instance_id,
        "typename": "DcimVirtualDevice",
        "deployment": {"id": "dep-c001", "typename": "TopologyCustomerDC", "serving_loadbalancer_context": context},
    }


def _segment(seg_id: str, *, prefix: bool = True) -> dict[str, Any]:
    segment: dict[str, Any] = {
        "id": seg_id,
        "name": f"{seg_id}-name",
        "typename": "ManagedVxlanSegment",
        "isolation_mode": "normal",
        "environment": "p",
        "security_zone": {"name": "PROD-ZONE"},
    }
    if prefix:
        segment["gateway"] = {
            "ip_prefix": {"id": f"pfx-{seg_id}", "prefix": "10.60.0.0/24", "ip_namespace": {"id": "ns-prod"}}
        }
    return segment


def _web(
    *,
    ports: list[str] | None = None,
    instances: list[dict[str, Any]] | None = None,
    load_balanced: bool = True,
    segment: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The load-balanced backend: two instances on one context."""
    context = _context()
    return {
        "id": "comp-web",
        "name": "web",
        "fqdn": "web.c001.example",
        "ports": ["tcp/443"] if ports is None else ports,
        "component_type": "backend",
        "load_balanced": load_balanced,
        "instances": [_instance("vm-2", context), _instance("vm-1", context)] if instances is None else instances,
        "health_checks": [{"id": "hc-1"}],
        "network_segment": segment or _segment("seg-web"),
    }


def _client_comp(*deps: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "comp-portal",
        "name": "portal",
        "fqdn": "portal.c001.example",
        "ports": ["tcp/8080"],
        "component_type": "frontend",
        "network_segment": _segment("seg-portal"),
        "depends_on": list(deps),
    }


def _dep(target: dict[str, Any], ports: list[str] | None = None) -> dict[str, Any]:
    return {
        "id": "dep-1",
        "name": "portal-to-web",
        "ports": ports or [],
        "target_fqdn": None,
        "access_status": "auto",
        "description": None,
        "target": target,
    }


# ===========================================================================
# Pure planning
# ===========================================================================


class TestVipPorts:
    def test_single_ports_each_get_a_vip(self) -> None:
        ports, errors = vip_ports({"fqdn": "web", "ports": ["tcp/443", "udp/53", "tcp/443"]})

        assert ports == [("tcp", 443, None), ("udp", 53, None)]
        assert errors == []

    def test_range_is_refused(self) -> None:
        ports, errors = vip_ports({"fqdn": "web", "ports": ["tcp/443", "udp/30000-30010"]})

        assert ports == [("tcp", 443, None)]
        assert len(errors) == 1
        assert "udp/30000-30010" in errors[0]

    def test_unparsable_port_is_an_error_not_a_guess(self) -> None:
        ports, errors = vip_ports({"fqdn": "web", "ports": ["garbage"]})

        assert ports == []
        assert "garbage" in errors[0]

    def test_no_port_is_an_error(self) -> None:
        assert vip_ports({"fqdn": "web", "ports": []})[1] == ["web is load_balanced but lists no port"]


class TestServingContexts:
    def test_instances_are_grouped_by_context(self) -> None:
        a, b = _context("ctx-a"), _context("ctx-b")
        contexts, errors = serving_contexts(
            {"instances": [_instance("vm-1", b), _instance("vm-2", a), _instance("vm-3", b)]}
        )

        assert errors == []
        assert list(contexts) == ["ctx-a", "ctx-b"]
        assert [instance["id"] for instance in contexts["ctx-b"][1]] == ["vm-1", "vm-3"]

    def test_instance_without_a_serving_context_is_an_error(self) -> None:
        contexts, errors = serving_contexts({"fqdn": "web", "instances": [_instance("vm-1", None)]})

        assert contexts == {}
        assert "vm-1" in errors[0]
        assert "serving load balancer context" in errors[0]

    def test_component_without_instances_is_an_error(self) -> None:
        assert serving_contexts({"fqdn": "web", "instances": []})[1] == ["web is load_balanced but has no instances"]


class TestLegPrefix:
    def test_prefix_of_the_member_addresses(self) -> None:
        assert leg_prefix_id(_context(leg="leg-9")) == "leg-9"

    def test_unwired_context_has_none(self) -> None:
        assert leg_prefix_id(_context(leg=None)) is None

    def test_two_prefixes_are_ambiguous(self) -> None:
        context = _context(leg="leg-1")
        context["capabilities"].append({"ip_address": {"ip_prefix": {"id": "leg-2"}}})

        assert leg_prefix_id(context) is None


def test_identifiers_follow_the_contract() -> None:
    """VIP /32: {component_id}-{protocol}-{port}-vip; a member is per (instance, segment)."""
    assert vip_identifier("comp-web", ("tcp", 443, None)) == "comp-web-tcp-443-vip"
    assert member_identifier("vm-1", "seg-web") == "vm-1-seg-web-lb-member"


# ===========================================================================
# LoadbalancerVipMixin
# ===========================================================================


def _vip_gen() -> Any:
    """An AppApplicationGenerator with every Infrahub boundary of the VIP producer stubbed."""
    gen: Any = AppApplicationGenerator.__new__(AppApplicationGenerator)
    gen.client = AsyncMock()
    gen.client.filters = AsyncMock(return_value=[])
    gen.logger = MagicMock()
    gen.locks = []

    @asynccontextmanager
    async def _lock(key: str) -> AsyncGenerator[None]:
        gen.locks.append(key)
        yield

    gen.resource_lock = _lock
    gen.ensure_prefix_address_pool = AsyncMock(side_effect=lambda **kw: MagicMock(name=kw["pool_name"]))
    gen.allocate_prefix_address = AsyncMock(side_effect=lambda **kw: f"ip:{kw['identifier']}")
    gen.upsert_ip_address = AsyncMock(side_effect=lambda **kw: MagicMock(id=f"ip:{kw['address']}"))
    gen._get_or_create_by_name = AsyncMock(return_value=MagicMock(id="pol-lb"))
    gen._upsert_policy_rule = AsyncMock(return_value=100)
    # Nothing in flight, nothing more on a re-read, and no real sleep between re-reads.
    gen.wait_for_parent_generators = AsyncMock(return_value=False)
    gen.client.execute_graphql = AsyncMock(return_value={})
    gen.lb_reread_sleep = AsyncMock()
    return gen


def _vip_payloads(gen: Any) -> list[dict[str, Any]]:
    return [call.kwargs["data"] for call in gen.client.create.await_args_list if call.kwargs["kind"] is LoadbalancerVIP]


def _reconcile(
    gen: Any,
    components: list[dict[str, Any]],
    edges: list[Any] | None = None,
    security_profile: str = "internal_standard",
) -> Any:
    return asyncio.run(gen._reconcile_load_balancing(components, edges or [], {}, security_profile=security_profile))


class TestVipProducer:
    def test_one_vip_per_port_with_members_health_checks_and_component(self) -> None:
        gen = _vip_gen()

        vips, errors = _reconcile(gen, [_web(ports=["tcp/443", "tcp/8443"])])

        assert errors == []
        assert vips == {
            "comp-web": {
                ("tcp", 443, None): {"dc10-lb-PROD": "ip:comp-web-tcp-443-vip"},
                ("tcp", 8443, None): {"dc10-lb-PROD": "ip:comp-web-tcp-8443-vip"},
            }
        }
        payloads = _vip_payloads(gen)
        assert [(p["protocol"], p["port"]) for p in payloads] == [("tcp", 443), ("tcp", 8443)]
        first = payloads[0]
        assert first["hostname"] == "web.c001.example"
        assert first["load_balancer"] == {"id": "ctx-1"}
        assert first["vip_ip"] == {"id": "ip:comp-web-tcp-443-vip"}
        assert first["component"] == {"id": "comp-web"}
        assert first["health_checks"] == ["hc-1"]
        # One member per instance, sorted by name, reserved in the segment prefix.
        assert first["pool_members"] == ["ip:vm-1-seg-web-lb-member", "ip:vm-2-seg-web-lb-member"]
        member_calls = [
            c.kwargs for c in gen.allocate_prefix_address.await_args_list if "member" in c.kwargs["identifier"]
        ]
        assert {c["prefix_length"] for c in member_calls} == {24}
        vip_calls = [
            c.kwargs for c in gen.allocate_prefix_address.await_args_list if c.kwargs["identifier"].endswith("-vip")
        ]
        assert {c["prefix_length"] for c in vip_calls} == {32}

    def test_vip_is_tracked_by_the_run(self) -> None:
        """Saved as a plain upsert: the run's group claims it, so it is deleted once no longer produced."""
        gen = _vip_gen()

        _reconcile(gen, [_web()])

        vip = gen.client.create.return_value
        vip.save.assert_awaited_once_with(allow_upsert=True)

    def test_second_run_writes_the_same_vip(self) -> None:
        """Idempotent: same allocation identifiers, same payloads."""
        first, second = _vip_gen(), _vip_gen()

        _reconcile(first, [_web()])
        _reconcile(second, [_web()])

        assert _vip_payloads(first) == _vip_payloads(second)
        assert [c.kwargs["identifier"] for c in first.allocate_prefix_address.await_args_list] == [
            c.kwargs["identifier"] for c in second.allocate_prefix_address.await_args_list
        ]

    def test_shared_pools_are_untracked_and_created_under_their_lock(self) -> None:
        """The VIP range and the segment prefix serve every application: never claimed by one run."""
        gen = _vip_gen()

        _reconcile(gen, [_web()])

        pools = {c.kwargs["pool_name"]: c.kwargs for c in gen.ensure_prefix_address_pool.await_args_list}
        assert set(pools) == {"lb-vip-vip-pfx-1", "lb-members-pfx-seg-web"}
        assert all(kwargs["track"] is False for kwargs in pools.values())
        assert pools["lb-vip-vip-pfx-1"]["prefix_length"] == 32
        assert "address-pool-lb-vip-vip-pfx-1" in gen.locks

    def test_existing_pool_is_reused_without_the_lock(self) -> None:
        gen = _vip_gen()
        gen.client.filters = AsyncMock(return_value=[MagicMock()])

        _reconcile(gen, [_web()])

        gen.ensure_prefix_address_pool.assert_not_awaited()
        assert not any(key.startswith("address-pool-") for key in gen.locks)

    def test_range_is_refused_and_the_single_ports_still_get_their_vip(self) -> None:
        gen = _vip_gen()

        vips, errors = _reconcile(gen, [_web(ports=["tcp/443", "udp/30000-30010"])])

        assert list(vips["comp-web"]) == [("tcp", 443, None)]
        assert [p["port"] for p in _vip_payloads(gen)] == [443]
        assert len(errors) == 1
        assert "udp/30000-30010" in errors[0]

    def test_missing_serving_context_is_an_error_and_writes_nothing(self) -> None:
        gen = _vip_gen()

        vips, errors = _reconcile(gen, [_web(instances=[_instance("vm-1", None)])])

        assert vips == {"comp-web": {}}
        assert "serving load balancer context" in errors[0]
        gen.client.create.assert_not_awaited()
        gen._upsert_policy_rule.assert_not_awaited()

    def test_context_without_vip_prefix_is_an_error(self) -> None:
        gen = _vip_gen()
        context = _context(vip_prefix=None)

        _vips, errors = _reconcile(gen, [_web(instances=[_instance("vm-1", context)])])

        assert "has no VIP prefix in a namespace" in errors[0]
        gen.client.create.assert_not_awaited()

    def test_vip_pool_is_in_the_tenant_namespace_of_the_context(self) -> None:
        """The VIP /32 lives in the namespace of the context's VIP range, never `default`."""
        gen = _vip_gen()

        _reconcile(gen, [_web()])

        vip_pool = next(
            c.kwargs
            for c in gen.ensure_prefix_address_pool.await_args_list
            if c.kwargs["pool_name"].startswith("lb-vip-")
        )
        assert (vip_pool["namespace_id"], vip_pool["prefix_length"]) == ("ns-prod", 32)

    def test_vip_prefix_without_namespace_is_an_error(self) -> None:
        gen = _vip_gen()
        context = _context()
        context["vip_prefix"]["ip_namespace"] = None

        _vips, errors = _reconcile(gen, [_web(instances=[_instance("vm-1", context)])])

        assert "has no VIP prefix in a namespace" in errors[0]
        gen.ensure_prefix_address_pool.assert_not_awaited()

    def test_segment_without_prefix_is_an_error(self) -> None:
        gen = _vip_gen()

        _vips, errors = _reconcile(gen, [_web(segment=_segment("seg-l2", prefix=False))])

        assert "no gateway prefix" in errors[0]
        gen.client.create.assert_not_awaited()

    def test_component_that_is_not_load_balanced_touches_nothing(self) -> None:
        gen = _vip_gen()

        vips, errors = _reconcile(gen, [_web(load_balanced=False)])

        assert (vips, errors) == ({}, [])
        gen.client.filters.assert_not_awaited()
        gen.client.create.assert_not_awaited()
        gen.allocate_prefix_address.assert_not_awaited()

    def test_two_contexts_give_two_vips_per_port(self) -> None:
        """Instances in two DCs: a VIP on each context, each with its own instances."""
        gen = _vip_gen()
        a, b = (
            _context("ctx-a", "dc10-lb-PROD", vip_prefix="vip-a"),
            _context("ctx-b", "dc12-lb-PROD", vip_prefix="vip-b"),
        )

        vips, _errors = _reconcile(gen, [_web(instances=[_instance("vm-a", a), _instance("vm-b", b)])])

        assert vips["comp-web"][("tcp", 443, None)] == {
            "dc10-lb-PROD": "ip:comp-web-tcp-443-vip",
            "dc12-lb-PROD": "ip:comp-web-tcp-443-vip",
        }
        by_context = {p["load_balancer"]["id"]: p["pool_members"] for p in _vip_payloads(gen)}
        assert by_context == {"ctx-a": ["ip:vm-a-seg-web-lb-member"], "ctx-b": ["ip:vm-b-seg-web-lb-member"]}

    def test_target_of_another_application_only_resolves_its_vip_addresses(self) -> None:
        """No VIP object, member or R_server: those belong to the target's own application run."""
        gen = _vip_gen()
        web = _web()

        vips, errors = _reconcile(gen, [_client_comp(_dep(web))], edges=[({}, {}, web)])

        assert errors == []
        assert vips == {"comp-web": {("tcp", 443, None): {"dc10-lb-PROD": "ip:comp-web-tcp-443-vip"}}}
        gen.client.create.assert_not_awaited()
        gen._upsert_policy_rule.assert_not_awaited()
        assert not any("member" in c.kwargs["identifier"] for c in gen.allocate_prefix_address.await_args_list)

    def test_errors_of_a_target_of_another_application_are_not_this_runs(self) -> None:
        """A misconfigured target fails its own run; its callers only miss the VIPs it cannot have."""
        gen = _vip_gen()
        web = _web(
            ports=["tcp/443", "udp/30000-30010"], instances=[_instance("vm-1", _context()), _instance("vm-2", None)]
        )

        vips, errors = _reconcile(gen, [_client_comp(_dep(web))], edges=[({}, {}, web)])

        assert errors == []
        assert vips == {"comp-web": {("tcp", 443, None): {"dc10-lb-PROD": "ip:comp-web-tcp-443-vip"}}}

    def test_target_without_a_vip_prefix_resolves_no_vip_and_no_error(self) -> None:
        gen = _vip_gen()
        web = _web(instances=[_instance("vm-1", _context(vip_prefix=None))])

        vips, errors = _reconcile(gen, [_client_comp(_dep(web))], edges=[({}, {}, web)])

        assert (vips, errors) == ({"comp-web": {}}, [])

    def test_own_component_also_called_by_a_sibling_keeps_its_errors(self) -> None:
        """A component that is both a child and an edge target is the application's own."""
        gen = _vip_gen()
        web = _web(ports=["udp/30000-30010"])

        _vips, errors = _reconcile(gen, [_client_comp(_dep(web)), web], edges=[({}, {}, web)])

        assert len(errors) == 1
        assert "udp/30000-30010" in errors[0]

    def test_failed_vip_save_is_logged_with_its_listener_and_gets_no_r_server(self) -> None:
        gen = _vip_gen()
        gen.client.create.return_value.save = AsyncMock(side_effect=RuntimeError("boom"))

        vips, _errors = _reconcile(gen, [_web()])

        message = gen.logger.error.call_args.args[0]
        assert "web.c001.example tcp/443 on dc10-lb-PROD" in message
        assert "boom" in message
        gen._upsert_policy_rule.assert_not_awaited()
        # The address is still the caller's VIP: the allocation identifier holds it for the next run.
        assert vips == {"comp-web": {("tcp", 443, None): {"dc10-lb-PROD": "ip:comp-web-tcp-443-vip"}}}


def _second_backend() -> dict[str, Any]:
    """Another load-balanced component on the same context as _web()."""
    return {**_web(segment=_segment("seg-api")), "id": "comp-api", "name": "api", "fqdn": "api.c001.example"}


class TestServerRule:
    def test_r_server_written_once_per_vip_port_under_the_policy_lock(self) -> None:
        gen = _vip_gen()

        _reconcile(gen, [_web(ports=["tcp/443", "tcp/8443"])])

        assert gen.locks.count("lb-policy-dc10-lb-PROD") == 1
        assert "lb-context-dc10-lb-PROD" not in gen.locks
        assert gen._upsert_policy_rule.await_count == 2
        names = [c.kwargs["rule_name"] for c in gen._upsert_policy_rule.await_args_list]
        assert names == ["web.c001.example-tcp-443", "web.c001.example-tcp-8443"]
        data = gen._upsert_policy_rule.await_args_list[0].kwargs["rule_data"]
        assert data["policy"] == {"id": "pol-lb"}
        assert data["source_prefixes"] == [{"id": "leg-1"}]
        assert data["destination_segment"] == {"id": "seg-web"}
        assert (data["protocol"], data["port_start"]) == ("tcp", 443)
        assert data["expires_at"] is None
        assert "source_segment" not in data

    def test_policy_is_segment_less_and_untracked(self) -> None:
        gen = _vip_gen()

        _reconcile(gen, [_web()])

        kwargs = gen._get_or_create_by_name.await_args.kwargs
        assert kwargs["kind"] is SecurityPolicy
        assert kwargs["name"] == "lb-dc10-lb-PROD"
        assert "segment" not in kwargs["create_data"]
        assert kwargs["track"] is False

    def test_policy_indexes_are_read_again_inside_the_lock(self) -> None:
        """Another application's run may have added a rule since this run cached the indexes."""
        gen = _vip_gen()
        rule_indexes = {"pol-lb": {100}}

        asyncio.run(gen._reconcile_load_balancing([_web()], [], rule_indexes, security_profile="internal_standard"))

        assert "pol-lb" not in rule_indexes

    def test_one_lock_and_one_policy_read_per_context_for_every_backend(self) -> None:
        """Two backends on one context: the policy is read once and its indexes once, inside one lock."""
        gen = _vip_gen()
        rule_indexes: dict[str, set[int]] = {"pol-lb": {7}}
        written: list[int] = []

        async def upsert(**kwargs: Any) -> int:
            index = 100 + len(written)
            kwargs["rule_indexes"].setdefault(kwargs["policy_id"], set()).add(index)
            written.append(index)
            return index

        gen._upsert_policy_rule = AsyncMock(side_effect=upsert)

        asyncio.run(
            gen._reconcile_load_balancing(
                [_web(), _second_backend()], [], rule_indexes, security_profile="internal_standard"
            )
        )

        assert gen.locks.count("lb-policy-dc10-lb-PROD") == 1
        gen._get_or_create_by_name.assert_awaited_once()
        assert [c.kwargs["rule_name"] for c in gen._upsert_policy_rule.await_args_list] == [
            "web.c001.example-tcp-443",
            "api.c001.example-tcp-443",
        ]
        # The stale cache is dropped once, and the first backend's index is kept for the second.
        assert rule_indexes == {"pol-lb": {100, 101}}

    def test_r_server_runs_after_every_vip_of_the_run(self) -> None:
        gen = _vip_gen()
        order: list[str] = []
        gen.client.create.return_value.save = AsyncMock(side_effect=lambda **_kw: order.append("vip"))
        gen._upsert_policy_rule = AsyncMock(side_effect=lambda **_kw: order.append("rule") or 100)

        _reconcile(gen, [_web(), _second_backend()])

        assert order == ["vip", "vip", "rule", "rule"]

    def test_failed_r_server_is_logged_and_the_next_one_still_written(self) -> None:
        gen = _vip_gen()
        gen._upsert_policy_rule = AsyncMock(side_effect=[RuntimeError("index taken"), 101])

        _reconcile(gen, [_web(ports=["tcp/443", "tcp/8443"])])

        message = gen.logger.error.call_args.args[0]
        assert "web.c001.example tcp/443 on dc10-lb-PROD" in message
        assert "index taken" in message
        assert gen._upsert_policy_rule.await_count == 2

    def test_unwired_leg_is_an_error_and_writes_no_vip(self) -> None:
        gen = _vip_gen()
        context = _context(leg=None)

        _vips, errors = _reconcile(gen, [_web(instances=[_instance("vm-1", context)])])

        assert "no single leg prefix" in errors[0]
        gen.client.create.assert_not_awaited()
        gen._upsert_policy_rule.assert_not_awaited()

    def test_microsegmented_backend_carries_its_component_tag(self) -> None:
        segment = {**_segment("seg-web"), "isolation_mode": "microsegmented"}
        payload = RulesPlanner.build_lb_server_rule_payload(
            policy_id="pol-lb",
            rule_name="web",
            context_name="dc10-lb-PROD",
            component={"fqdn": "web", "security_tag": {"id": "tag-web"}},
            segment=segment,
            leg_prefix_id="leg-1",
            port=("tcp", 443, None),
        )

        assert payload["apply_on_switch"] is True
        assert payload["destination_security_tag"] == {"id": "tag-web"}
        assert "source_security_tag" not in payload


# ===========================================================================
# Concurrent inputs: re-read what another generator of the load still writes
# ===========================================================================


def _segment_read(seg_id: str, *, prefix: bool) -> dict[str, Any]:
    """A lb_segment_gateways.gql response for one segment, with or without its gateway prefix yet."""
    gateway = None
    if prefix:
        ip_prefix = {"id": f"pfx-{seg_id}", "prefix": {"value": "10.60.0.0/24"}, "ip_namespace": {"node": {"id": "ns"}}}
        gateway = {"node": {"ip_prefix": {"node": ip_prefix}}}
    return {"ManagedVxlanSegment": {"edges": [{"node": {"id": seg_id, "gateway": gateway}}]}}


def _context_read(dep_id: str, *, linked: bool = True) -> dict[str, Any]:
    """A lb_serving_contexts.gql response for one deployment: its complete context, or none linked yet."""
    context = None
    if linked:
        leg = {"__typename": "DcimVirtualInterface", "ip_address": {"node": {"ip_prefix": {"node": {"id": "leg-1"}}}}}
        context = {
            "id": "ctx-1",
            "name": {"value": "dc10-lb-PROD"},
            "vip_prefix": {"node": {"id": "vip-pfx-1", "ip_namespace": {"node": {"id": "ns-prod"}}}},
            "capabilities": {"edges": [{"node": leg}]},
        }
    node = {"id": dep_id, "serving_loadbalancer_context": {"node": context}}
    return {"TopologyCustomerDC": {"edges": [{"node": node}]}}


def _reads(gen: Any) -> list[dict[str, Any]]:
    """The variables of every re-read query, in order."""
    return [call.kwargs["variables"] for call in gen.client.execute_graphql.await_args_list]


class TestConcurrentInputs:
    def test_segment_prefix_written_by_the_second_read_gets_the_members(self) -> None:
        """add_vxlan_segment addresses the segment while this run reads it: the second read sees the prefix."""
        gen = _vip_gen()
        gen.client.execute_graphql = AsyncMock(
            side_effect=[_segment_read("seg-web", prefix=False), _segment_read("seg-web", prefix=True)]
        )

        _vips, errors = _reconcile(gen, [_web(segment=_segment("seg-web", prefix=False))])

        assert errors == []
        gen.wait_for_parent_generators.assert_awaited_once_with(("add_vxlan_segment",), "seg-web")
        assert _reads(gen) == [{"ids": ["seg-web"]}, {"ids": ["seg-web"]}]
        gen.lb_reread_sleep.assert_awaited_once_with(READ_INTERVAL)
        assert _vip_payloads(gen)[0]["pool_members"] == ["ip:vm-1-seg-web-lb-member", "ip:vm-2-seg-web-lb-member"]

    def test_segment_prefix_that_never_lands_is_the_error_after_the_last_read(self) -> None:
        """Bounded: READ_ATTEMPTS reads with a sleep between two, then the error the payload alone gave."""
        gen = _vip_gen()
        gen.client.execute_graphql = AsyncMock(return_value=_segment_read("seg-web", prefix=False))

        _vips, errors = _reconcile(gen, [_web(segment=_segment("seg-web", prefix=False))])

        assert errors == ["web.c001.example: segment seg-web-name has no gateway prefix for the pool members"]
        assert gen.client.execute_graphql.await_count == READ_ATTEMPTS
        assert gen.lb_reread_sleep.await_count == READ_ATTEMPTS - 1
        gen.client.create.assert_not_awaited()

    def test_two_components_on_one_segment_share_one_read(self) -> None:
        """One wait and one read per attempt for the segment, written into both components' dicts."""
        gen = _vip_gen()
        gen.client.execute_graphql = AsyncMock(return_value=_segment_read("seg-web", prefix=True))
        api = {**_web(segment=_segment("seg-web", prefix=False)), "id": "comp-api", "fqdn": "api.c001.example"}

        _vips, errors = _reconcile(gen, [_web(segment=_segment("seg-web", prefix=False)), api])

        assert errors == []
        gen.wait_for_parent_generators.assert_awaited_once()
        assert _reads(gen) == [{"ids": ["seg-web"]}]
        gen.lb_reread_sleep.assert_not_awaited()
        assert {p["hostname"] for p in _vip_payloads(gen)} == {"web.c001.example", "api.c001.example"}

    def test_ready_inputs_are_never_re_read(self) -> None:
        """The normal run: the payload has the prefix and the context, no extra round trip."""
        gen = _vip_gen()

        _vips, errors = _reconcile(gen, [_web()])

        assert errors == []
        gen.wait_for_parent_generators.assert_not_awaited()
        gen.client.execute_graphql.assert_not_awaited()

    def test_vlan_segment_without_prefix_is_an_error_without_a_re_read(self) -> None:
        """No generator addresses a VLAN segment: its gateway is authored, waiting cannot bring it."""
        gen = _vip_gen()
        segment = {**_segment("seg-vlan", prefix=False), "typename": "ManagedVlanSegment"}

        _vips, errors = _reconcile(gen, [_web(segment=segment)])

        assert "no gateway prefix" in errors[0]
        gen.client.execute_graphql.assert_not_awaited()
        gen.wait_for_parent_generators.assert_not_awaited()

    def test_cloud_component_is_never_re_read(self) -> None:
        """A cloud segment has no gateway prefix and a cloud instance no context: nothing to wait for."""
        gen = _lb_gen()

        _vips, errors = _reconcile(gen, [_cloud_frontend()])

        assert errors == []
        gen.wait_for_parent_generators.assert_not_awaited()
        gen.client.execute_graphql.assert_not_awaited()
        gen.lb_reread_sleep.assert_not_awaited()

    def test_serving_context_linked_by_the_second_read_gets_the_vips(self) -> None:
        """add_customer_deployment_dc boards the deployment while this run reads it."""
        gen = _vip_gen()
        gen.client.execute_graphql = AsyncMock(
            side_effect=[_context_read("dep-c001", linked=False), _context_read("dep-c001")]
        )

        vips, errors = _reconcile(gen, [_web(instances=[_instance("vm-1", None), _instance("vm-2", None)])])

        assert errors == []
        gen.wait_for_parent_generators.assert_awaited_once_with(("add_customer_deployment_dc",), "dep-c001")
        assert _reads(gen) == [{"ids": ["dep-c001"]}, {"ids": ["dep-c001"]}]
        assert vips == {"comp-web": {("tcp", 443, None): {"dc10-lb-PROD": "ip:comp-web-tcp-443-vip"}}}
        gen._upsert_policy_rule.assert_awaited_once()

    def test_context_without_its_vip_range_yet_is_re_read(self) -> None:
        """The boarding run links the context before it allocates the VIP range and wires the leg."""
        gen = _vip_gen()
        gen.client.execute_graphql = AsyncMock(return_value=_context_read("dep-c001"))

        _vips, errors = _reconcile(gen, [_web(instances=[_instance("vm-1", _context(vip_prefix=None))])])

        assert errors == []
        assert _reads(gen) == [{"ids": ["dep-c001"]}]

    def test_context_of_an_edge_target_is_re_read_for_its_vip_addresses(self) -> None:
        """The target's VIPs are this run's R_client rules: its context is waited for too."""
        gen = _vip_gen()
        gen.client.execute_graphql = AsyncMock(return_value=_context_read("dep-c001"))
        web = _web(instances=[_instance("vm-1", None)])

        vips, errors = _reconcile(gen, [_client_comp(_dep(web))], edges=[({}, {}, web)])

        assert errors == []
        assert vips == {"comp-web": {("tcp", 443, None): {"dc10-lb-PROD": "ip:comp-web-tcp-443-vip"}}}


# ===========================================================================
# AppApplicationGenerator: R_client and the run's errors
# ===========================================================================


def _app_gen(lb_vips: dict[str, Any], lb_errors: list[str] | None = None) -> Any:
    gen = AppApplicationGenerator.__new__(AppApplicationGenerator)
    gen.client = AsyncMock()
    gen._init_client = AsyncMock()
    gen.logger = MagicMock()
    gen._ensure_segment_zones = AsyncMock()
    gen._ensure_segment_isolation_mode = AsyncMock()
    gen._reconcile_private_access_components = AsyncMock(return_value=(0, 0))
    gen._reconcile_load_balancing = AsyncMock(return_value=(lb_vips, list(lb_errors or [])))
    policy = MagicMock()
    policy.id = "policy-portal"
    gen._segment_policy = AsyncMock(return_value=policy)
    gen._find_rule_by_name = AsyncMock(return_value=None)
    gen._create_or_update_policy_rule = AsyncMock(return_value=(MagicMock(), 100))
    gen._get_profile = AsyncMock(return_value=None)
    return gen


def _rules(gen: Any) -> dict[str, dict[str, Any]]:
    return {c.kwargs["rule_name"]: c.kwargs["rule_data"] for c in gen._create_or_update_policy_rule.await_args_list}


def _application(*components: dict[str, Any]) -> dict[str, Any]:
    return {"name": "portal", "security_profile": "internal_standard", "children": list(components)}


class TestClientRule:
    def test_r_client_replaces_the_direct_rule(self) -> None:
        gen = _app_gen({"comp-web": {("tcp", 443, None): {"dc10-lb-PROD": "vip-a"}}})

        asyncio.run(gen._reconcile_application_rules(_application(_client_comp(_dep(_web())))))

        rules = _rules(gen)
        assert list(rules) == ["portal-to-web-tcp-443-vip-dc10-lb-prod"]
        data = rules["portal-to-web-tcp-443-vip-dc10-lb-prod"]
        assert data["destination_ip_addresses"] == [{"id": "vip-a"}]
        assert "destination_segment" not in data
        assert data["source_segment"] == {"id": "seg-portal"}
        assert (data["protocol"], data["port_start"]) == ("tcp", 443)
        # The rule is read with its selectors, to reconcile them in place on a later run.
        gen._find_rule_by_name.assert_awaited_once_with(
            SecurityPolicyRule,
            "policy-portal",
            "portal-to-web-tcp-443-vip-dc10-lb-prod",
            include=["destination_ip_addresses"],
        )

    def test_one_r_client_per_vip(self) -> None:
        """VIPs on two contexts: two rules, each naming one VIP (the ACL renders one address selector)."""
        gen = _app_gen({"comp-web": {("tcp", 443, None): {"dc10-lb-PROD": "vip-a", "dc12-lb-PROD": "vip-b"}}})

        asyncio.run(gen._reconcile_application_rules(_application(_client_comp(_dep(_web())))))

        rules = _rules(gen)
        assert {name: data["destination_ip_addresses"] for name, data in rules.items()} == {
            "portal-to-web-tcp-443-vip-dc10-lb-prod": [{"id": "vip-a"}],
            "portal-to-web-tcp-443-vip-dc12-lb-prod": [{"id": "vip-b"}],
        }

    def test_target_that_is_not_load_balanced_keeps_the_direct_rule(self) -> None:
        gen = _app_gen({})

        asyncio.run(gen._reconcile_application_rules(_application(_client_comp(_dep(_web(load_balanced=False))))))

        rules = _rules(gen)
        assert list(rules) == ["portal-to-web-tcp-443"]
        assert rules["portal-to-web-tcp-443"]["destination_segment"] == {"id": "seg-web"}

    def test_port_without_vip_gets_no_rule_and_fails_the_run_after_the_others(self) -> None:
        """Strict: no VIP on a port means no rule at all, never the direct one."""
        web = _web(ports=["tcp/443", "tcp/8443"])
        gen = _app_gen({"comp-web": {("tcp", 443, None): {"dc10-lb-PROD": "vip-a"}}})

        with pytest.raises(GeneratorError, match="no VIP on tcp/8443"):
            asyncio.run(gen._reconcile_application_rules(_application(_client_comp(_dep(web)))))

        assert list(_rules(gen)) == ["portal-to-web-tcp-443-vip-dc10-lb-prod"]

    def test_producer_errors_fail_the_run_without_edges_too(self) -> None:
        gen = _app_gen({"comp-web": {}}, ["web.c001.example: port range udp/1-2 cannot be load balanced"])

        with pytest.raises(GeneratorError, match="port range"):
            asyncio.run(gen._reconcile_application_rules(_application(_web())))

    def test_no_load_balancing_error_does_not_fail(self) -> None:
        gen = _app_gen({"comp-web": {("tcp", 443, None): {"dc10-lb-PROD": "vip-a"}}})

        asyncio.run(gen._reconcile_application_rules(_application(_web())))

        gen._reconcile_load_balancing.assert_awaited_once()


# ===========================================================================
# Tracking: a component that stops being load_balanced
# ===========================================================================


class _Node:
    def __init__(self, client: _TrackingClient, kind: Any, data: dict[str, Any]) -> None:
        self._client = client
        self.kind = getattr(kind, "__name__", kind)
        self.data = data
        self.id = f"id-{self.key[1]}"

    @property
    def key(self) -> tuple[str, str]:
        if self.kind == "LoadbalancerVIP":
            return self.kind, f"{self.data['hostname']}:{self.data['port']}@{self.data['load_balancer']['id']}"
        return self.kind, str(self.data.get("name"))

    async def save(self, allow_upsert: bool = False, update_group_context: bool = True) -> None:
        (self._client.tracked if update_group_context else self._client.untracked).add(self.key)


class _TrackingClient:
    """Records what a run saves into its group (tracked) and what it writes outside it."""

    def __init__(self) -> None:
        self.tracked: set[tuple[str, str]] = set()
        self.untracked: set[tuple[str, str]] = set()

    async def create(self, kind: Any, data: dict[str, Any]) -> _Node:
        return _Node(self, kind, data)

    async def filters(self, **_kwargs: Any) -> list[Any]:
        return []


def _tracked_run(web_load_balanced: bool) -> set[tuple[str, str]]:
    """One add_app_application run of an application whose portal calls its own web tier."""
    gen = _vip_gen()
    gen.client = _TrackingClient()
    del gen._get_or_create_by_name
    del gen._upsert_policy_rule
    gen._ensure_segment_zones = AsyncMock()
    gen._ensure_segment_isolation_mode = AsyncMock()
    gen._reconcile_private_access_components = AsyncMock(return_value=(0, 0))
    gen._get_profile = AsyncMock(return_value=None)
    web = _web(load_balanced=web_load_balanced)
    portal = _client_comp(_dep(dict(web)))

    asyncio.run(gen._reconcile_application_rules(_application(portal, web)))
    return gen.client.tracked


class TestLoadBalancedFlip:
    def test_turning_load_balanced_off_leaves_vip_and_both_rules_to_the_cleanup(self) -> None:
        """delete_unused_nodes removes what the earlier run tracked and this one did not save."""
        on = _tracked_run(web_load_balanced=True)
        off = _tracked_run(web_load_balanced=False)

        assert on - off == {
            ("LoadbalancerVIP", "web.c001.example:443@ctx-1"),
            ("SecurityPolicyRule", "web.c001.example-tcp-443"),
            ("SecurityPolicyRule", "portal-to-web-tcp-443-vip-dc10-lb-prod"),
        }
        assert off - on == {("SecurityPolicyRule", "portal-to-web-tcp-443")}

    def test_turning_load_balanced_on_drops_the_direct_rule(self) -> None:
        off = _tracked_run(web_load_balanced=False)
        on = _tracked_run(web_load_balanced=True)

        assert off - on == {("SecurityPolicyRule", "portal-to-web-tcp-443")}

    def test_shared_policies_are_never_tracked(self) -> None:
        gen_tracked = _tracked_run(web_load_balanced=True)

        assert not any(kind == "SecurityPolicy" for kind, _name in gen_tracked)


# ===========================================================================
# Cloud: a CloudLoadBalancer per load_balanced component on cloud instances
# ===========================================================================

_CLOUD_FQDN = "frontend.custody-api.c003.demo.local"
_CLOUD_LB = cloud_lb_name({"fqdn": _CLOUD_FQDN})


def _vpc(vpc_id: str = "vpc-1") -> dict[str, Any]:
    return {
        "id": vpc_id,
        "name": f"{vpc_id}-name",
        "cidr_blocks": [{"prefix": "10.30.0.0/16", "ip_namespace": {"id": "ns-prod"}}],
    }


def _subnet(seg_id: str = "sub-a", vpc_id: str = "vpc-1") -> dict[str, Any]:
    return {"id": seg_id, "name": f"{seg_id}-name", "virtual_network": _vpc(vpc_id)}


def _cloud_instance(
    instance_id: str, private_ip: str | None = "10.30.1.11/24", subnet: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "id": instance_id,
        "name": instance_id,
        "typename": "CloudInstance",
        "private_ip": private_ip,
        "network_segment": _subnet() if subnet is None else subnet,
    }


def _cloud_frontend(
    *, ports: list[str] | None = None, instances: list[dict[str, Any]] | None = None, load_balanced: bool = True
) -> dict[str, Any]:
    """c003's api-frontend: two AWS instances in one subnet of one VPC."""
    return {
        "id": "comp-frontend",
        "name": "api-frontend",
        "fqdn": _CLOUD_FQDN,
        "ports": ["tcp/443"] if ports is None else ports,
        "component_type": "frontend",
        "load_balanced": load_balanced,
        "instances": (
            [_cloud_instance("i-2", "10.30.1.12/24"), _cloud_instance("i-1", "10.30.1.11/24")]
            if instances is None
            else instances
        ),
        "health_checks": [{"id": "hc-1"}],
        "network_segment": {
            **_subnet(),
            "typename": "CloudNetworkSegment",
            "cidr_block": "10.30.1.0/24",
            "environment": "p",
        },
    }


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


def _lb_gen() -> Any:
    """_vip_gen() whose created nodes carry their own id, so a VIP points at its load balancer."""
    gen = _vip_gen()
    gen.created = []

    def create(kind: Any, data: dict[str, Any]) -> Any:
        node = MagicMock(id=f"id-{data.get('name') or data.get('hostname')}", save=AsyncMock())
        gen.created.append(node)
        return node

    gen.client.create = AsyncMock(side_effect=create)
    return gen


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


def _manager(peer_ids: list[str]) -> Any:
    manager = MagicMock()
    manager.peer_ids = list(peer_ids)
    return manager


def _existing_rule(*, vip_ids: list[str], destination_segment: str | None = None) -> Any:
    rule = MagicMock()
    rule.log.value = False
    rule.apply_on_switch.value = False
    for field, peer_id in (
        ("destination_segment", destination_segment),
        ("security_profile", None),
        ("source_security_tag", None),
        ("destination_security_tag", None),
    ):
        getattr(rule, field).initialized = peer_id is not None
        getattr(rule, field).id = peer_id
    rule.destination_ip_addresses = _manager(vip_ids)
    return rule


class TestSelectorReconciliation:
    _CLIENT_RULE = {"log": False, "apply_on_switch": False}

    def test_stale_vip_set_is_brought_to_the_new_one(self) -> None:
        rule = _existing_rule(vip_ids=["vip-old", "vip-a"])
        data = {**self._CLIENT_RULE, "destination_ip_addresses": [{"id": "vip-a"}, {"id": "vip-b"}]}

        changed = SegmentFirewallMixin._reconcile_rule_payload(rule, data, keep_profile=False)

        assert changed == ["destination_ip_addresses"]
        rule.destination_ip_addresses.remove.assert_called_once_with("vip-old")
        rule.destination_ip_addresses.extend.assert_called_once_with(["vip-b"])

    def test_vip_set_in_the_desired_state_is_not_modified(self) -> None:
        rule = _existing_rule(vip_ids=["vip-a"])
        data = {**self._CLIENT_RULE, "destination_ip_addresses": [{"id": "vip-a"}]}

        assert SegmentFirewallMixin._reconcile_rule_payload(rule, data, keep_profile=False) == []
        rule.destination_ip_addresses.extend.assert_not_called()

    def test_rule_without_a_destination_segment_has_it_cleared(self) -> None:
        """A rule that turned from A->B into R_client loses B's segment."""
        rule = _existing_rule(vip_ids=["vip-a"], destination_segment="seg-web")
        data = {**self._CLIENT_RULE, "destination_ip_addresses": [{"id": "vip-a"}]}

        changed = SegmentFirewallMixin._reconcile_rule_payload(rule, data, keep_profile=False)

        assert changed == ["destination_segment"]
        assert rule.destination_segment is None

    def test_destination_segment_follows_a_moved_target(self) -> None:
        rule = _existing_rule(vip_ids=[], destination_segment="seg-old")
        data = {**self._CLIENT_RULE, "destination_segment": {"id": "seg-new"}}

        changed = SegmentFirewallMixin._reconcile_rule_payload(rule, data, keep_profile=False)

        assert changed == ["destination_segment"]
        assert rule.destination_segment == {"id": "seg-new"}
        rule.destination_ip_addresses.extend.assert_not_called()


# ===========================================================================
# Query
# ===========================================================================


def test_application_query_reads_the_serving_context_of_instances_and_targets(root_dir: Path) -> None:
    """Both device kinds carry the path; the dependency targets read it through the same fragment."""
    query = (root_dir / "queries/topology/add/application.gql").read_text()
    fragment = query.split("fragment AppInstanceFields", 1)[1].split("fragment AppComponentSecurityFields", 1)[0]
    assert fragment.count("serving_loadbalancer_context") == 2
    assert fragment.count("vip_prefix { node { id ip_namespace { node { id } } } }") == 2
    assert fragment.count("ip_address { node { ip_prefix { node { id } } } }") == 2
    target = query.split("target {", 1)[1]
    assert "load_balanced { value }" in target
    assert "instances { edges { node { ...AppInstanceFields } } }" in target
    segment = query.split("fragment SegmentSecurityFields", 1)[1].split("fragment AppInstanceFields", 1)[0]
    assert "ip_prefix {\n        node {\n          id" in segment


def test_re_read_queries_select_what_the_application_query_does(root_dir: Path) -> None:
    """A re-read replaces the payload's dict: it must carry every field the producer reads from it."""
    application = (root_dir / "queries/topology/add/application.gql").read_text()
    contexts = (root_dir / "queries/topology/add/lb_serving_contexts.gql").read_text()
    gateways = " ".join((root_dir / "queries/topology/add/lb_segment_gateways.gql").read_text().split())
    for line in (
        "vip_prefix { node { id ip_namespace { node { id } } } }",
        "edges { node { __typename ... on DcimVirtualInterface { ip_address { node { ip_prefix { node { id } } } } } } }",
    ):
        assert line in application
        assert line in contexts
    assert "ManagedVxlanSegment(ids: $ids)" in gateways
    assert "prefix { value } ip_namespace { node { id name { value } namespace_type { value } } }" in gateways


class TestCloudTracking:
    @staticmethod
    def _tracked(load_balanced: bool) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
        gen = _vip_gen()
        gen.client = _TrackingClient()
        _reconcile(gen, [_cloud_frontend(load_balanced=load_balanced)])
        return gen.client.tracked, gen.client.untracked

    def test_turning_load_balanced_off_leaves_the_load_balancer_and_its_vips_to_the_cleanup(self) -> None:
        """delete_unused_nodes removes what the earlier run tracked and this one did not save."""
        on, on_untracked = self._tracked(load_balanced=True)
        off, _ = self._tracked(load_balanced=False)

        assert on == {
            ("CloudLoadBalancer", _CLOUD_LB),
            ("LoadbalancerVIP", f"{_CLOUD_FQDN}:443@id-{_CLOUD_LB}"),
        }
        assert off == set()
        assert on_untracked == set()


class TestCloudClientRule:
    def test_dependency_into_a_cloud_target_keeps_the_direct_rule_and_no_vip_error(self) -> None:
        """No R_client for a cloud target: the 'has no VIP on port' error is for an on-prem one only."""
        gen = _app_gen({})

        asyncio.run(gen._reconcile_application_rules(_application(_client_comp(_dep(_cloud_frontend())))))

        rules = _rules(gen)
        assert list(rules) == ["portal-to-web-tcp-443"]
        assert rules["portal-to-web-tcp-443"]["destination_segment"] == {"id": "sub-a"}
        assert "destination_ip_addresses" not in rules["portal-to-web-tcp-443"]

    def test_the_application_security_profile_reaches_the_producer(self) -> None:
        gen = _app_gen({})

        asyncio.run(
            gen._reconcile_application_rules(
                {**_application(_cloud_frontend()), "security_profile": "internet_exposed"}
            )
        )

        assert gen._reconcile_load_balancing.await_args.kwargs == {"security_profile": "internet_exposed"}


def test_application_query_reads_the_cloud_placement_of_instances(root_dir: Path) -> None:
    """A cloud instance's private_ip and its subnet's virtual network with the CIDR blocks' namespaces."""
    query = (root_dir / "queries/topology/add/application.gql").read_text()
    fragment = query.split("fragment AppInstanceFields", 1)[1].split("fragment AppComponentSecurityFields", 1)[0]
    cloud = fragment.split("... on CloudInstance {", 1)[1]
    assert "private_ip { value }" in cloud
    assert "virtual_network {" in cloud
    assert "cidr_blocks { edges { node { prefix { value } ip_namespace { node { id } } } } }" in cloud


class TestLoadBalancedComponentsIn:
    """The components a relinked deployment re-runs: load_balanced, with an instance deployed to it."""

    @staticmethod
    def _client(devices: dict[str, list[str]], component_ids: list[str]) -> MagicMock:
        """``devices``: device ids by kind name, as filters(kind=..., deployment__ids=...) returns them."""

        async def filters(*, kind: Any, **kwargs: Any) -> list[MagicMock]:
            if kind is AppComponent:
                return [MagicMock(id=component_id) for component_id in component_ids]
            return [MagicMock(id=device_id) for device_id in devices.get(kind.__name__, [])]

        client = MagicMock()
        client.filters = AsyncMock(side_effect=filters)
        return client

    def test_components_running_on_the_deployment_devices(self) -> None:
        """Physical and virtual devices of the deployment, then the load_balanced components on them; sorted, unique."""
        client = self._client(
            {"DcimPhysicalDevice": ["srv-2"], "DcimVirtualDevice": ["vm-1"]}, ["comp-b", "comp-a", "comp-b"]
        )

        assert asyncio.run(load_balanced_components_in(client, "dep-1")) == ["comp-a", "comp-b"]

        calls = [call.kwargs for call in client.filters.await_args_list]
        assert calls == [
            {"kind": DcimPhysicalDevice, "deployment__ids": ["dep-1"]},
            {"kind": DcimVirtualDevice, "deployment__ids": ["dep-1"]},
            {"kind": AppComponent, "instances__ids": ["srv-2", "vm-1"], "load_balanced__value": True},
        ]

    def test_deployment_without_devices_reads_no_components(self) -> None:
        client = self._client({}, ["comp-a"])

        assert asyncio.run(load_balanced_components_in(client, "dep-1")) == []

        assert all(call.kwargs["kind"] is not AppComponent for call in client.filters.await_args_list)
