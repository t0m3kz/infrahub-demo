"""Unit tests for the VIP producer and the server rule of the load balancer.

- generators/lb_vip.py (LoadbalancerVipMixin): VIPs, pool members, R_server
- LoadbalancerVipMixin._await_lb_inputs: the bounded re-read of a segment
  prefix or serving context another generator of the same load still writes
- SegmentFirewallMixin._reconcile_rule_payload: the selectors and the
  destination segment of an existing rule
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from generators.helpers.rules import RulesPlanner
from generators.lb_vip import READ_ATTEMPTS, READ_INTERVAL
from generators.protocols import SecurityPolicy
from generators.segment_firewall import SegmentFirewallMixin
from tests.unit.lb_vip_helpers import (
    _client_comp,
    _cloud_frontend,
    _context,
    _dep,
    _instance,
    _lb_gen,
    _reconcile,
    _segment,
    _vip_gen,
    _vip_payloads,
    _web,
)


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
