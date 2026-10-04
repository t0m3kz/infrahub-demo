"""Unit tests for ZtnaMixin (generators/ztna.py).

Covers:
  - _reconcile_proxy_rule()                — target_fqdn egress through the
                                             owner's egress_service
  - private_access_grants()                — which dependents admit users
  - _reconcile_private_access_components() — ZTNA broker publish of
                                             access-profile grants
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from generators.common import CommonGenerator
from generators.ztna import ZtnaMixin


def _make_gen() -> Any:
    gen = ZtnaMixin.__new__(ZtnaMixin)
    gen.client = AsyncMock()
    gen.logger = MagicMock()
    gen._safe_rel_add = CommonGenerator._safe_rel_add
    return gen


def _make_gen_ready() -> Any:
    """A mixin whose policy lookup, owner attach and rule create are mocked out."""
    gen = _make_gen()
    policy = MagicMock()
    policy.id = "policy-1"
    policy.save = AsyncMock()
    gen._get_or_create_proxy_policy = AsyncMock(return_value=policy)
    gen._attach_proxy_policy_to_owner = AsyncMock()
    gen._find_existing_proxy_policy_rule = AsyncMock(return_value=None)
    created_rule = MagicMock()
    created_rule.save = AsyncMock()
    gen.client.create = AsyncMock(return_value=created_rule)
    return gen


def _egress_dep(
    name: str = "checkout-to-stripe",
    target_fqdn: str = "api.stripe.com",
    ports: list[str] | None = None,
) -> dict[str, Any]:
    """An AppDependency on an external host, as it appears in component.depends_on."""
    return {
        "id": f"dep-{name}",
        "name": name,
        "target_fqdn": target_fqdn,
        "ports": ["tcp/443"] if ports is None else ports,
        "description": None,
    }


# ===========================================================================
# TestReconcileProxyRule
# ===========================================================================


class TestReconcileProxyRule:
    """A target_fqdn dependency becomes one fqdn rule in the owner's egress policy."""

    @staticmethod
    def _src_comp(comp_id: str = "comp-src", proxy_id: str = "proxy-1") -> dict[str, Any]:
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
                }
            },
        }

    def test_creates_policy_rule_for_target_fqdn_dependency(self) -> None:
        """The rule lands in the owner's egress policy and targets the dependency's fqdn."""
        gen = _make_gen_ready()

        result = asyncio.run(
            gen._reconcile_proxy_rule(
                app_name="checkout",
                src_comp=self._src_comp(),
                dep=_egress_dep(),
                proxy_policies={},
            )
        )

        assert result is True
        gen._get_or_create_proxy_policy.assert_awaited_once_with("proxy-C001-shared-cloud-proxy-egress")
        gen._attach_proxy_policy_to_owner.assert_awaited_once_with(owner_id="customer-1", policy_id="policy-1")
        rule_data = gen.client.create.call_args.kwargs["data"]
        assert rule_data["policy"] == {"id": "policy-1"}
        assert rule_data["name"] == "checkout-to-stripe"
        assert rule_data["action"] == "allow"
        assert rule_data["destination_type"] == "fqdn"
        assert rule_data["destination"] == "api.stripe.com"
        assert rule_data["ports"] == ["tcp/443"]
        assert "id" not in rule_data

    def test_multi_port_dependency_stays_one_rule_with_formatted_ports(self) -> None:
        """A proxy rule matches a host on a port list, so ports are normalized, deduped
        and kept on a single rule rather than split per port."""
        gen = _make_gen_ready()
        dep = _egress_dep(ports=["TCP/443", " tcp/8443 ", "udp/30000-30010", "tcp/443"])

        result = asyncio.run(
            gen._reconcile_proxy_rule(app_name="checkout", src_comp=self._src_comp(), dep=dep, proxy_policies={})
        )

        assert result is True
        assert gen.client.create.call_count == 1
        rule_data = gen.client.create.call_args.kwargs["data"]
        assert rule_data["name"] == "checkout-to-stripe"
        assert rule_data["ports"] == ["tcp/443", "tcp/8443", "udp/30000-30010"]

    def test_existing_rule_is_upserted_by_id(self) -> None:
        """A rerun reuses the existing rule's id so the upsert updates it in place."""
        gen = _make_gen_ready()
        existing = MagicMock()
        existing.id = "rule-existing"
        gen._find_existing_proxy_policy_rule = AsyncMock(return_value=existing)

        result = asyncio.run(
            gen._reconcile_proxy_rule(
                app_name="checkout", src_comp=self._src_comp(), dep=_egress_dep(), proxy_policies={}
            )
        )

        assert result is True
        gen._find_existing_proxy_policy_rule.assert_awaited_once_with(
            policy_id="policy-1", rule_name="checkout-to-stripe"
        )
        assert gen.client.create.call_args.kwargs["data"]["id"] == "rule-existing"
        gen.client.create.return_value.save.assert_awaited_once_with(allow_upsert=True)

    def test_components_sharing_proxy_service_share_one_policy(self) -> None:
        """The egress policy is created once per owner/proxy and reused across components."""
        gen = _make_gen_ready()
        proxy_policies: dict[str, Any] = {}

        asyncio.run(
            gen._reconcile_proxy_rule(
                app_name="checkout",
                src_comp=self._src_comp(comp_id="comp-web"),
                dep=_egress_dep(name="web-to-stripe"),
                proxy_policies=proxy_policies,
            )
        )
        asyncio.run(
            gen._reconcile_proxy_rule(
                app_name="checkout",
                src_comp=self._src_comp(comp_id="comp-backend"),
                dep=_egress_dep(name="backend-to-github", target_fqdn="api.github.com"),
                proxy_policies=proxy_policies,
            )
        )

        gen._get_or_create_proxy_policy.assert_awaited_once()
        assert gen._attach_proxy_policy_to_owner.await_count == 2
        assert gen.client.create.call_count == 2
        destinations = [call.kwargs["data"]["destination"] for call in gen.client.create.call_args_list]
        assert destinations == ["api.stripe.com", "api.github.com"]

    def test_missing_owner_egress_service_is_skipped(self) -> None:
        """Without an egress_service there is no proxy to place the rule on."""
        gen = _make_gen_ready()
        src_comp = self._src_comp()
        src_comp["parent"]["owner"]["egress_service"] = {}

        result = asyncio.run(
            gen._reconcile_proxy_rule(app_name="checkout", src_comp=src_comp, dep=_egress_dep(), proxy_policies={})
        )

        assert result is False
        gen._get_or_create_proxy_policy.assert_not_awaited()
        gen.client.create.assert_not_called()

    def test_dependency_with_malformed_ports_is_skipped(self) -> None:
        """A port typo must never turn into an any-port egress rule."""
        gen = _make_gen_ready()
        dep = _egress_dep(ports=["tcp/443", "https"])

        result = asyncio.run(
            gen._reconcile_proxy_rule(app_name="checkout", src_comp=self._src_comp(), dep=dep, proxy_policies={})
        )

        assert result is False
        gen._get_or_create_proxy_policy.assert_not_awaited()
        gen.client.create.assert_not_called()

    def test_dependency_without_ports_is_skipped(self) -> None:
        """An external host has no component ports to fall back to, so ports are required."""
        gen = _make_gen_ready()
        dep = _egress_dep(ports=[])

        result = asyncio.run(
            gen._reconcile_proxy_rule(app_name="checkout", src_comp=self._src_comp(), dep=dep, proxy_policies={})
        )

        assert result is False
        gen._get_or_create_proxy_policy.assert_not_awaited()
        gen.client.create.assert_not_called()

    def test_blank_target_fqdn_is_skipped(self) -> None:
        """A whitespace-only target_fqdn never becomes a rule with an empty destination."""
        gen = _make_gen_ready()

        result = asyncio.run(
            gen._reconcile_proxy_rule(
                app_name="checkout", src_comp=self._src_comp(), dep=_egress_dep(target_fqdn="  "), proxy_policies={}
            )
        )

        assert result is False
        gen._get_or_create_proxy_policy.assert_not_awaited()
        gen.client.create.assert_not_called()

    def test_failed_save_reports_false(self) -> None:
        """A rejected upsert is logged and reported as not reconciled."""
        gen = _make_gen_ready()
        gen.client.create.return_value.save = AsyncMock(side_effect=RuntimeError("boom"))

        result = asyncio.run(
            gen._reconcile_proxy_rule(
                app_name="checkout", src_comp=self._src_comp(), dep=_egress_dep(), proxy_policies={}
            )
        )

        assert result is False
        gen.logger.error.assert_called_once()


# ===========================================================================
# TestReconcilePrivateAccessComponents
# ===========================================================================


def _grant(
    profile: str = "c001-private-access-standard",
    status: str = "approved",
    ports: list[str] | None = None,
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


class TestPrivateAccessGrants:
    def test_only_non_denied_profile_dependents_are_grants(self) -> None:
        """Component callers and denied grants admit nobody; auto/pending/approved do."""
        caller = {**_grant(), "source_profile": None, "source": {"id": "comp-other"}}
        approved = _grant("approved-profile")
        auto = _grant("auto-profile", status="auto")
        denied = _grant("denied-profile", status="DENIED ")
        component = _component(dependents=[caller, approved, auto, denied])

        assert ZtnaMixin.private_access_grants(component) == [approved, auto]

    def test_component_without_dependents_has_no_grants(self) -> None:
        assert ZtnaMixin.private_access_grants({"dependents": None}) == []
        assert ZtnaMixin.private_access_grants({}) == []


class TestReconcilePrivateAccessComponents:
    """A component is published through the owner's ZTNA broker when an access
    profile is granted to it: an AppDependency whose source_profile is set,
    found in the component's dependents."""

    def test_publishes_a_granted_component(self) -> None:
        """A granted component becomes an fqdn rule under the owner's private-access policy."""
        gen = _make_gen_ready()

        created, skipped = asyncio.run(gen._reconcile_private_access_components("checkout", [_component()]))

        assert (created, skipped) == (1, 0)
        gen._get_or_create_proxy_policy.assert_awaited_once_with("proxy-C001-c001-private-access-private-access")
        gen._attach_proxy_policy_to_owner.assert_awaited_once_with(owner_id="customer-1", policy_id="policy-1")
        rule_data = gen.client.create.call_args.kwargs["data"]
        assert rule_data["name"] == "publish-checkout.internal.c001.demo.local"
        assert rule_data["action"] == "allow"
        assert rule_data["destination_type"] == "fqdn"
        assert rule_data["destination"] == "checkout.internal.c001.demo.local"
        assert rule_data["ports"] == ["tcp/443"]
        assert rule_data["description"] == (
            "Publish checkout/frontend via private access broker for c001-private-access-standard"
        )

    def test_existing_publish_rule_is_upserted_by_id(self) -> None:
        """A rerun reuses the existing publish rule instead of creating a duplicate."""
        gen = _make_gen_ready()
        existing = MagicMock()
        existing.id = "rule-existing"
        gen._find_existing_proxy_policy_rule = AsyncMock(return_value=existing)

        created, _ = asyncio.run(gen._reconcile_private_access_components("checkout", [_component()]))

        assert created == 1
        assert gen.client.create.call_args.kwargs["data"]["id"] == "rule-existing"

    def test_component_without_a_grant_is_not_published(self) -> None:
        """Nobody is granted to the component, so it is neither published nor counted as skipped."""
        gen = _make_gen_ready()

        created, skipped = asyncio.run(
            gen._reconcile_private_access_components("checkout", [_component(dependents=[])])
        )

        assert (created, skipped) == (0, 0)
        gen._get_or_create_proxy_policy.assert_not_awaited()
        gen.client.create.assert_not_called()

    def test_denied_grant_does_not_publish(self) -> None:
        """A denied access-profile dependency admits nobody."""
        gen = _make_gen_ready()
        component = _component(dependents=[_grant(status="denied")])

        created, skipped = asyncio.run(gen._reconcile_private_access_components("checkout", [component]))

        assert (created, skipped) == (0, 0)
        gen.client.create.assert_not_called()

    def test_component_caller_is_not_a_grant(self) -> None:
        """A component-sourced dependency grants no ZTNA access."""
        gen = _make_gen_ready()
        caller = {**_grant(), "source_profile": None, "source": {"id": "comp-other"}}
        component = _component(dependents=[caller])

        created, skipped = asyncio.run(gen._reconcile_private_access_components("checkout", [component]))

        assert (created, skipped) == (0, 0)
        gen.client.create.assert_not_called()

    def test_missing_broker_is_skipped(self) -> None:
        """An owner without private_access_service has nowhere to publish."""
        gen = _make_gen_ready()

        created, skipped = asyncio.run(gen._reconcile_private_access_components("checkout", [_component(broker_id="")]))

        assert (created, skipped) == (0, 1)
        gen.client.create.assert_not_called()

    def test_missing_fqdn_is_skipped(self) -> None:
        """The broker publishes by fqdn, so a component without one is skipped."""
        gen = _make_gen_ready()

        created, skipped = asyncio.run(gen._reconcile_private_access_components("checkout", [_component(fqdn="")]))

        assert (created, skipped) == (0, 1)
        gen.client.create.assert_not_called()

    def test_unresolvable_owner_is_skipped(self) -> None:
        """A broker without an owner id cannot be attached to a policy owner."""
        gen = _make_gen_ready()
        component = _component()
        owner = component["parent"]["owner"]
        del owner["id"]
        del owner["org_id"]

        created, skipped = asyncio.run(gen._reconcile_private_access_components("checkout", [component]))

        assert (created, skipped) == (0, 1)
        gen._get_or_create_proxy_policy.assert_not_awaited()
        gen.client.create.assert_not_called()

    def test_publish_ports_are_the_union_across_grants(self) -> None:
        """Each grant opens its own ports; the rule carries their deduplicated union in order."""
        gen = _make_gen_ready()
        grants = [
            _grant("c001-private-access-standard", ports=["tcp/443"]),
            _grant("c001-ops", ports=["tcp/22", "tcp/443"]),
        ]
        component = _component(ports=["tcp/443", "tcp/22", "tcp/8443"], dependents=grants)

        created, _ = asyncio.run(gen._reconcile_private_access_components("checkout", [component]))

        assert created == 1
        assert gen.client.create.call_args.kwargs["data"]["ports"] == ["tcp/443", "tcp/22"]

    def test_grant_without_ports_falls_back_to_component_ports(self) -> None:
        """A grant listing no ports opens every port of the component."""
        gen = _make_gen_ready()
        component = _component(ports=["tcp/443", "udp/30000-30010"], dependents=[_grant(ports=[])])

        created, _ = asyncio.run(gen._reconcile_private_access_components("checkout", [component]))

        assert created == 1
        assert gen.client.create.call_args.kwargs["data"]["ports"] == ["tcp/443", "udp/30000-30010"]

    def test_mixed_grants_union_explicit_and_component_ports(self) -> None:
        """One grant with explicit ports plus one falling back give the union of both."""
        gen = _make_gen_ready()
        grants = [_grant("c001-ops", ports=["tcp/22"]), _grant("c001-private-access-standard", ports=[])]
        component = _component(ports=["tcp/443", "tcp/22"], dependents=grants)

        asyncio.run(gen._reconcile_private_access_components("checkout", [component]))

        assert gen.client.create.call_args.kwargs["data"]["ports"] == ["tcp/22", "tcp/443"]

    def test_malformed_grant_port_skips_publish(self) -> None:
        """A port typo on any grant skips the component rather than publishing any port."""
        gen = _make_gen_ready()
        grants = [_grant("c001-ops", ports=["tcp/22"]), _grant("c001-private-access-standard", ports=["tcp/0"])]
        component = _component(dependents=grants)

        created, skipped = asyncio.run(gen._reconcile_private_access_components("checkout", [component]))

        assert (created, skipped) == (0, 1)
        gen._get_or_create_proxy_policy.assert_not_awaited()
        gen.client.create.assert_not_called()

    def test_grant_resolving_to_no_ports_skips_publish(self) -> None:
        """No grant ports and no component ports would publish every port, so nothing is published."""
        gen = _make_gen_ready()
        component = _component(ports=[], dependents=[_grant(ports=[])])

        created, skipped = asyncio.run(gen._reconcile_private_access_components("checkout", [component]))

        assert (created, skipped) == (0, 1)
        gen._get_or_create_proxy_policy.assert_not_awaited()
        gen.client.create.assert_not_called()

    def test_two_components_sharing_a_broker_share_one_policy(self) -> None:
        """Components of one owner on one broker land in a single policy, one rule each."""
        gen = _make_gen_ready()
        components = [
            _component(name="frontend"),
            _component(name="admin", fqdn="admin.internal.c001.demo.local"),
        ]

        created, skipped = asyncio.run(gen._reconcile_private_access_components("checkout", components))

        assert (created, skipped) == (2, 0)
        gen._get_or_create_proxy_policy.assert_awaited_once()
        names = [call.kwargs["data"]["name"] for call in gen.client.create.call_args_list]
        assert names == ["publish-checkout.internal.c001.demo.local", "publish-admin.internal.c001.demo.local"]

    def test_rule_description_lists_every_granted_profile(self) -> None:
        """Two profiles granted to one component still give one rule, naming both."""
        gen = _make_gen_ready()
        grants = [_grant("c001-private-access-standard"), _grant("c001-ops")]

        created, _ = asyncio.run(gen._reconcile_private_access_components("checkout", [_component(dependents=grants)]))

        assert created == 1
        assert gen.client.create.call_count == 1
        description = gen.client.create.call_args.kwargs["data"]["description"]
        assert description.endswith("for c001-ops, c001-private-access-standard")

    def test_failed_save_counts_as_skipped(self) -> None:
        """A rejected upsert is logged and counted as skipped."""
        gen = _make_gen_ready()
        gen.client.create.return_value.save = AsyncMock(side_effect=RuntimeError("boom"))

        created, skipped = asyncio.run(gen._reconcile_private_access_components("checkout", [_component()]))

        assert (created, skipped) == (0, 1)
        gen.logger.error.assert_called_once()
