"""The ProxyPolicyRules of a run (generators/applications/proxy_rules.py): the requests (what is skipped, with a warning),
the plan over what exists, and the plan applied on the HFID fake through the application run."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest

from generators.applications.proxy_rules import (
    ProxyFacts,
    egress_request,
    plan_proxy_rules,
    private_access_grants,
    private_access_requests,
    proxy_lock_keys,
)
from generators.plan import EdgesSpec, NodeRef, NodeSpec, TopologyPlan
from generators.protocols import ProxyPolicy, ProxyPolicyRule
from tests.unit.app_application_generator_helpers import _app
from tests.unit.app_plan_helpers import AppClient, make_app_generator


def _egress_dep(
    name: str = "checkout-to-stripe", target_fqdn: str = "api.stripe.com", ports: list[str] | None = None
) -> dict[str, Any]:
    """An AppDependency on an external host, as it appears in component.depends_on."""
    return {
        "id": f"dep-{name}",
        "name": name,
        "target_fqdn": target_fqdn,
        "ports": ["tcp/443"] if ports is None else ports,
        "description": None,
        "access_status": "auto",
    }


def _src_comp(comp_id: str = "comp-src", proxy_id: str = "proxy-1", **owner: Any) -> dict[str, Any]:
    return {
        "id": comp_id,
        "fqdn": "checkout.c001.demo.local",
        "name": "frontend",
        "component_type": "frontend",
        "parent": {
            "owner": {
                "id": "customer-1",
                "org_id": "C001",
                "egress_service": {"id": proxy_id, "name": "shared-cloud-proxy"},
                **owner,
            }
        },
    }


def _grant(
    profile: str = "c001-private-access-standard", status: str = "approved", ports: list[str] | None = None
) -> dict[str, Any]:
    """An AppDependency from an access profile, as it appears in component.dependents."""
    return {
        "id": f"dep-{profile}",
        "name": f"{profile}-to-checkout-frontend",
        "ports": ["tcp/443"] if ports is None else ports,
        "access_status": status,
        "source_profile": {"id": f"profile-{profile}", "name": profile},
    }


def _component(
    name: str = "frontend",
    fqdn: str = "checkout.internal.c001.demo.local",
    ports: list[str] | None = None,
    dependents: list[dict[str, Any]] | None = None,
    broker_id: str = "broker-1",
) -> dict[str, Any]:
    return {
        "id": f"comp-{name}",
        "fqdn": fqdn,
        "name": name,
        "component_type": "frontend",
        "ports": ["tcp/443"] if ports is None else ports,
        "parent": {
            "owner": {
                "id": "customer-1",
                "org_id": "C001",
                "private_access_service": ({"id": broker_id, "name": "c001-private-access"} if broker_id else {}),
            }
        },
        "dependents": [_grant()] if dependents is None else dependents,
    }


def egress(src: dict[str, Any] | None = None, dep: dict[str, Any] | None = None) -> tuple[Any, MagicMock]:
    """The request of a dependency, and the logger it warned on."""
    logger = MagicMock()
    return egress_request(logger, "checkout", src or _src_comp(), dep or _egress_dep()), logger


class TestEgressRequest:
    """A target_fqdn dependency becomes one fqdn rule in the owner's egress policy."""

    def test_the_rule_targets_the_fqdn_of_the_dependency_with_every_port_in_one_rule(self) -> None:
        request, _ = egress(dep=_egress_dep(ports=["tcp/443", "tcp/8443"]))

        assert request.policy_name == "proxy-C001-shared-cloud-proxy-egress"
        assert request.purpose == "egress"
        assert request.rule_data == {
            "name": "checkout-to-stripe",
            "action": "allow",
            "destination_type": "fqdn",
            "destination": "api.stripe.com",
            "ports": ["tcp/443", "tcp/8443"],
            "description": "Auto-generated: frontend (frontend) -> api.stripe.com (external)",
        }

    @pytest.mark.parametrize(
        ("src", "dep", "warning"),
        [
            (_src_comp(egress_service=None), _egress_dep(), "has no egress_service"),
            (_src_comp(), _egress_dep(ports=["tcp/0"]), "skipping"),
            (_src_comp(), _egress_dep(ports=[]), "without ports"),
            (_src_comp(), _egress_dep(target_fqdn="  "), "blank target_fqdn"),
        ],
    )
    def test_what_cannot_be_placed_is_skipped_with_a_warning(self, src: Any, dep: Any, warning: str) -> None:
        request, logger = egress(src, dep)

        assert request is None
        assert warning in logger.warning.call_args.args[0] % logger.warning.call_args.args[1:]


class TestPrivateAccessRequests:
    """A component is published through the owner's ZTNA broker when an access profile is granted to it."""

    def test_only_non_denied_profile_dependents_are_grants(self) -> None:
        caller = {**_grant(), "source_profile": None, "source": {"id": "comp-other"}}
        approved = _grant("approved-profile")
        auto = _grant("auto-profile", status="auto")
        denied = _grant("denied-profile", status="DENIED ")

        assert private_access_grants(_component(dependents=[caller, approved, auto, denied])) == [approved, auto]
        assert private_access_grants({"dependents": None}) == []
        assert private_access_grants({}) == []

    def test_a_granted_component_becomes_an_fqdn_rule_under_the_private_access_policy(self) -> None:
        (request,), skipped = private_access_requests(MagicMock(), "checkout", [_component()])

        assert skipped == 0
        assert request.policy_name == "proxy-C001-c001-private-access-private-access"
        assert request.rule_data == {
            "name": "publish-checkout.internal.c001.demo.local",
            "action": "allow",
            "destination_type": "fqdn",
            "destination": "checkout.internal.c001.demo.local",
            "ports": ["tcp/443"],
            "description": "Publish checkout/frontend via private access broker for c001-private-access-standard",
        }

    @pytest.mark.parametrize(
        "component",
        [
            _component(dependents=[]),
            _component(dependents=[{**_grant(), "source_profile": None}]),
            _component(dependents=[_grant(status="denied")]),
        ],
    )
    def test_a_component_without_a_grant_is_not_published_and_not_skipped(self, component: dict[str, Any]) -> None:
        assert private_access_requests(MagicMock(), "checkout", [component]) == ([], 0)

    @pytest.mark.parametrize(
        "component",
        [
            _component(broker_id=""),
            _component(fqdn=""),
            _component(dependents=[_grant(ports=["tcp/0"])]),
            _component(ports=[], dependents=[_grant(ports=[])]),
        ],
    )
    def test_a_component_that_cannot_be_published_is_skipped_with_a_warning(self, component: dict[str, Any]) -> None:
        logger = MagicMock()

        assert private_access_requests(logger, "checkout", [component]) == ([], 1)
        logger.warning.assert_called_once()

    def test_the_ports_are_the_union_across_grants_in_order(self) -> None:
        grants = [_grant("c001-ops", ports=["tcp/22"]), _grant("c001-private-access-standard", ports=[])]
        component = _component(ports=["tcp/443", "tcp/22"], dependents=grants)

        (request,), _ = private_access_requests(MagicMock(), "checkout", [component])

        assert request.rule_data["ports"] == ["tcp/22", "tcp/443"]
        assert request.rule_data["description"].endswith("for c001-ops, c001-private-access-standard")


def _plan(requests: list[Any], facts: ProxyFacts | None = None) -> tuple[TopologyPlan, tuple[int, int]]:
    plan = TopologyPlan(root_id=None)
    counts = plan_proxy_rules(plan, MagicMock(), requests, facts or ProxyFacts())
    return plan, counts


class TestProxyPlan:
    def test_a_missing_policy_is_created_shared_attached_to_the_owner_and_holds_the_rule(self) -> None:
        request, _ = egress()

        plan, counts = _plan([request])

        policy, rule = plan.of(NodeSpec)
        assert counts == (1, 0)
        assert (policy.kind, policy.ownership) == (ProxyPolicy, "shared")
        assert policy.create_only["policy_type"] == "customer"
        assert policy.create_only["description"] == "Auto-generated egress rules for proxy-C001-shared-cloud-proxy"
        assert (rule.kind, rule.ownership) == (ProxyPolicyRule, "owned")
        assert rule.data["policy"] == {"id": NodeRef(policy.key)}
        assert plan.of(EdgesSpec) == [
            EdgesSpec("customer-1", "proxy_policies", (NodeRef(policy.key),), kind="OrganizationCustomer")
        ]

    def test_a_policy_attached_to_the_owner_needs_no_policy_and_no_edge(self) -> None:
        request, _ = egress(_src_comp(proxy_policies=[{"id": "pol-1", "name": "proxy-C001-shared-cloud-proxy-egress"}]))

        plan, _ = _plan([request], ProxyFacts(rule_ids={"pol-1": {"checkout-to-stripe": "rule-1"}}))

        (rule,) = plan.of(NodeSpec)
        assert rule.data["policy"] == {"id": "pol-1"}
        assert rule.data["id"] == "rule-1"
        assert plan.of(EdgesSpec) == []

    def test_a_policy_that_exists_and_is_not_attached_is_attached_without_being_written(self) -> None:
        request, _ = egress()

        plan, _ = _plan([request], ProxyFacts(policy_ids={"proxy-C001-shared-cloud-proxy-egress": "pol-1"}))

        assert [spec.kind for spec in plan.of(NodeSpec)] == [ProxyPolicyRule]
        assert plan.of(EdgesSpec) == [
            EdgesSpec("customer-1", "proxy_policies", ("pol-1",), kind="OrganizationCustomer")
        ]

    def test_components_sharing_a_proxy_service_share_one_policy(self) -> None:
        first, _ = egress(_src_comp("comp-a"), _egress_dep("one"))
        second, _ = egress(_src_comp("comp-b"), _egress_dep("two"))

        plan, counts = _plan([first, second])

        assert counts == (2, 0)
        assert [spec.kind for spec in plan.of(NodeSpec)] == [ProxyPolicy, ProxyPolicyRule, ProxyPolicyRule]
        assert len(plan.of(EdgesSpec)) == 1

    def test_two_services_of_one_owner_give_two_policies(self) -> None:
        first, _ = egress(_src_comp("comp-a"))
        second, _ = egress(_src_comp("comp-b", egress_service={"id": "proxy-2", "name": "other-proxy"}))

        plan, _ = _plan([first, second])

        assert [spec.data["name"] for spec in plan.of(NodeSpec) if spec.kind is ProxyPolicy] == [
            "proxy-C001-shared-cloud-proxy-egress",
            "proxy-C001-other-proxy-egress",
        ]

    def test_an_owner_without_an_id_is_skipped_with_a_warning(self) -> None:
        request, _ = egress(_src_comp(id=None, org_id=None))
        logger = MagicMock()
        plan = TopologyPlan(root_id=None)

        counts = plan_proxy_rules(plan, logger, [request], ProxyFacts())

        assert counts == (0, 1)
        assert plan.specs == []
        assert "no resolvable owner" in logger.warning.call_args.args[0]
        assert proxy_lock_keys([request]) == []

    def test_a_rule_named_twice_is_written_once(self) -> None:
        request, _ = egress()

        plan, counts = _plan([request, request])

        assert counts == (1, 0)
        assert len(plan.of(NodeSpec)) == 2


def _run(app: dict[str, Any], client: AppClient | None = None) -> tuple[Any, AppClient]:
    client = client or AppClient()
    gen = make_app_generator(client)
    asyncio.run(gen._reconcile_application_rules(app))
    return gen, client


def _frontend_with_egress(**owner: Any) -> dict[str, Any]:
    return {
        **_src_comp(**owner),
        "network_segment": {"id": "seg-1", "name": "s1", "typename": "ManagedVxlanSegment"},
        "depends_on": [_egress_dep()],
    }


class TestProxyWrites:
    def test_an_egress_dependency_creates_the_policy_untracked_the_rule_tracked_and_attaches_the_policy(self) -> None:
        client = AppClient()
        client.seed("OrganizationCustomer", id="customer-1")

        _, client = _run(_app(_frontend_with_egress()), client)

        (policy,) = client.saved("ProxyPolicy")
        assert policy["name"] == "proxy-C001-shared-cloud-proxy-egress"
        assert client.save_kwargs("ProxyPolicy") == [{"allow_upsert": True, "update_group_context": False}]
        (rule,) = client.saved("ProxyPolicyRule")
        assert rule["destination"] == "api.stripe.com"
        assert client.save_kwargs("ProxyPolicyRule") == [{"allow_upsert": True}]
        policy_id = next(node.id for node in client.store.values() if node.kind == "ProxyPolicy")
        assert rule["policy"] == {"id": policy_id}
        assert client.edges == [("customer-1", "proxy_policies", [policy_id])]

    def test_a_second_run_writes_the_rule_by_id_and_attaches_nothing_more(self) -> None:
        client = AppClient()
        client.seed("OrganizationCustomer", id="customer-1")
        _run(_app(_frontend_with_egress()), client)
        policy = next(node for node in client.store.values() if node.kind == "ProxyPolicy")
        rule = next(node for node in client.store.values() if node.kind == "ProxyPolicyRule")
        client.saves.clear()
        client.edges.clear()

        _run(_app(_frontend_with_egress(proxy_policies=[{"id": policy.id, "name": policy.data["name"]}])), client)

        assert client.saved("ProxyPolicy") == []
        assert client.edges == []
        assert [data["id"] for data in client.saved("ProxyPolicyRule")] == [rule.id]

    def test_a_policy_that_exists_but_the_owner_payload_lacks_is_found_by_name_and_attached(self) -> None:
        client = AppClient()
        client.seed("OrganizationCustomer", id="customer-1")
        policy = client.seed("ProxyPolicy", name="proxy-C001-shared-cloud-proxy-egress")

        _run(_app(_frontend_with_egress()), client)

        assert client.saved("ProxyPolicy") == []
        assert client.edges == [("customer-1", "proxy_policies", [policy.id])]

    def test_a_private_access_grant_without_edges_is_published(self) -> None:
        client = AppClient()
        client.seed("OrganizationCustomer", id="customer-1")
        component = {
            **_component(),
            "network_segment": {"id": "seg-1", "name": "s1", "typename": "ManagedVxlanSegment"},
        }

        _, client = _run(_app(component), client)

        (rule,) = client.saved("ProxyPolicyRule")
        assert rule["name"] == "publish-checkout.internal.c001.demo.local"
        assert [data["name"] for data in client.saved("ProxyPolicy")] == [
            "proxy-C001-c001-private-access-private-access"
        ]

    def test_a_proxy_policy_is_locked_by_its_name(self) -> None:
        client = AppClient()
        client.seed("OrganizationCustomer", id="customer-1")

        gen, _ = _run(_app(_frontend_with_egress()), client)

        assert "proxy-policy-proxy-C001-shared-cloud-proxy-egress" in gen.locked_keys
