"""Unit tests for ZtnaMixin (generators/ztna.py).

Covers:
  - _reconcile_proxy_rule()               — external_service egress
  - _reconcile_private_access_endpoints() — ZTNA broker publish of
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


def _dep(
    protocol: str | None = None,
    port_start: int | None = None,
    port_end: int | None = None,
    name: str = "dep-1",
) -> dict:
    return {
        "id": f"dep-{name}",
        "name": name,
        "protocol": protocol,
        "port_start": port_start,
        "port_end": port_end,
        "description": None,
    }


# ===========================================================================
# TestReconcileProxyRule
# ===========================================================================


class TestReconcileProxyRule:
    @staticmethod
    def _src_comp(comp_id: str = "comp-src", proxy_id: str = "proxy-1") -> dict:
        return {
            "id": comp_id,
            "slug": "checkout-frontend",
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

    @staticmethod
    def _dst_endpoint(fqdn: str = "api.stripe.com") -> dict:
        return {
            "id": "endpoint-dst",
            "name": "stripe-api-public",
            "endpoint_type": "external_service",
            "fqdn": fqdn,
            "parent": {"id": "comp-dst", "name": "stripe-api", "component_type": "backend"},
        }

    def _make_gen_ready(self) -> Any:
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

    def test_creates_policy_rule_for_external_dependency(self):
        gen = self._make_gen_ready()
        dep = _dep(name="checkout-to-stripe")

        result = asyncio.run(
            gen._reconcile_proxy_rule(
                app_name="checkout",
                src_comp=self._src_comp(),
                dep=dep,
                dst_endpoint=self._dst_endpoint(),
                proxy_policies={},
            )
        )

        assert result is True
        gen._get_or_create_proxy_policy.assert_awaited_once_with("proxy-C001-shared-cloud-proxy-egress")
        gen._attach_proxy_policy_to_owner.assert_awaited_once_with(owner_id="customer-1", policy_id="policy-1")
        rule_data = gen.client.create.call_args.kwargs["data"]
        assert rule_data["policy"] == {"id": "policy-1"}
        assert rule_data["action"] == "allow"
        assert rule_data["destination_type"] == "fqdn"
        assert rule_data["destination"] == "api.stripe.com"

    def test_components_sharing_proxy_service_share_one_policy(self):
        gen = self._make_gen_ready()
        proxy_policies: dict[str, Any] = {}

        asyncio.run(
            gen._reconcile_proxy_rule(
                app_name="checkout",
                src_comp=self._src_comp(comp_id="comp-web"),
                dep=_dep(name="web-to-stripe"),
                dst_endpoint=self._dst_endpoint("api.stripe.com"),
                proxy_policies=proxy_policies,
            )
        )
        asyncio.run(
            gen._reconcile_proxy_rule(
                app_name="checkout",
                src_comp=self._src_comp(comp_id="comp-backend"),
                dep=_dep(name="backend-to-github"),
                dst_endpoint=self._dst_endpoint("api.github.com"),
                proxy_policies=proxy_policies,
            )
        )

        gen._get_or_create_proxy_policy.assert_awaited_once()
        assert gen._attach_proxy_policy_to_owner.await_count == 2
        assert gen.client.create.call_count == 2

    def test_missing_owner_egress_service_is_skipped(self):
        gen = self._make_gen_ready()
        src_comp = self._src_comp()
        src_comp["parent"]["owner"]["egress_service"] = {}
        dep = _dep(name="checkout-to-stripe")

        result = asyncio.run(
            gen._reconcile_proxy_rule(
                app_name="checkout", src_comp=src_comp, dep=dep, dst_endpoint=self._dst_endpoint(), proxy_policies={}
            )
        )

        assert result is False
        gen.client.create.assert_not_called()

    def test_missing_fqdn_is_skipped(self):
        gen = self._make_gen_ready()
        dep = _dep(name="checkout-to-stripe")

        result = asyncio.run(
            gen._reconcile_proxy_rule(
                app_name="checkout",
                src_comp=self._src_comp(),
                dep=dep,
                dst_endpoint=self._dst_endpoint(fqdn=""),
                proxy_policies={},
            )
        )

        assert result is False


# ===========================================================================
# TestReconcilePrivateAccessEndpoints
# ===========================================================================


def _grant(profile: str = "c001-private-access-standard", status: str = "approved") -> dict:
    """An AppDependency from an access profile, as it appears in endpoint.dependents."""
    return {
        "id": f"dep-{profile}",
        "name": f"{profile}-to-checkout-web",
        "protocol": "tcp",
        "port_start": 443,
        "port_end": None,
        "access_status": status,
        "source_profile": {"id": f"profile-{profile}", "name": profile},
    }


class TestReconcilePrivateAccessEndpoints:
    """A private_access endpoint is published through the owner's ZTNA broker
    when an access profile is granted to it: an AppDependency whose
    source_profile is set, found in the endpoint's dependents."""

    @staticmethod
    def _component(endpoints: list[dict], comp_id: str = "comp-frontend", broker_id: str = "broker-1") -> dict:
        return {
            "id": comp_id,
            "slug": "checkout-frontend",
            "name": "frontend",
            "component_type": "frontend",
            "parent": {
                "owner": {
                    "id": "customer-1",
                    "org_id": "C001",
                    "private_access_service": ({"id": broker_id, "name": "c001-private-access"} if broker_id else {}),
                }
            },
            "children": endpoints,
        }

    @staticmethod
    def _endpoint(
        name: str = "checkout-web",
        endpoint_type: str = "private_access",
        fqdn: str = "checkout.internal.c001.demo.local",
        dependents: list[dict] | None = None,
    ) -> dict:
        return {
            "id": f"endpoint-{name}",
            "name": name,
            "endpoint_type": endpoint_type,
            "fqdn": fqdn,
            "dependents": [_grant()] if dependents is None else dependents,
        }

    def _make_gen_ready(self) -> Any:
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

    def test_publishes_a_granted_private_access_endpoint(self) -> None:
        """A granted endpoint becomes an fqdn rule under the owner's private-access policy."""
        gen = self._make_gen_ready()
        component = self._component([self._endpoint()])

        created, skipped = asyncio.run(gen._reconcile_private_access_endpoints("checkout", [component]))

        assert (created, skipped) == (1, 0)
        gen._get_or_create_proxy_policy.assert_awaited_once_with("proxy-C001-c001-private-access-private-access")
        gen._attach_proxy_policy_to_owner.assert_awaited_once_with(owner_id="customer-1", policy_id="policy-1")
        rule_data = gen.client.create.call_args.kwargs["data"]
        assert rule_data["destination_type"] == "fqdn"
        assert rule_data["destination"] == "checkout.internal.c001.demo.local"
        assert rule_data["description"].endswith("for c001-private-access-standard")

    def test_endpoint_without_a_grant_is_not_published(self) -> None:
        """Nobody is granted to the endpoint, so the broker must not publish it."""
        gen = self._make_gen_ready()
        component = self._component([self._endpoint(dependents=[])])

        created, skipped = asyncio.run(gen._reconcile_private_access_endpoints("checkout", [component]))

        assert (created, skipped) == (0, 1)
        gen.client.create.assert_not_called()

    def test_denied_grant_does_not_publish(self) -> None:
        """A denied access-profile dependency admits nobody."""
        gen = self._make_gen_ready()
        component = self._component([self._endpoint(dependents=[_grant(status="denied")])])

        created, skipped = asyncio.run(gen._reconcile_private_access_endpoints("checkout", [component]))

        assert (created, skipped) == (0, 1)
        gen.client.create.assert_not_called()

    def test_component_caller_is_not_a_grant(self) -> None:
        """A component-sourced dependency on a private_access endpoint grants no ZTNA access."""
        gen = self._make_gen_ready()
        caller = {**_grant(), "source_profile": None, "source": {"id": "comp-other"}}
        component = self._component([self._endpoint(dependents=[caller])])

        created, skipped = asyncio.run(gen._reconcile_private_access_endpoints("checkout", [component]))

        assert (created, skipped) == (0, 1)
        gen.client.create.assert_not_called()

    def test_internal_and_external_endpoints_are_not_published(self) -> None:
        """Only private_access endpoints go to the ZTNA broker."""
        gen = self._make_gen_ready()
        component = self._component(
            [
                self._endpoint(name="internal-api", endpoint_type="internal_service"),
                self._endpoint(name="external-api", endpoint_type="external_service"),
            ]
        )

        created, skipped = asyncio.run(gen._reconcile_private_access_endpoints("checkout", [component]))

        assert (created, skipped) == (0, 0)
        gen.client.create.assert_not_called()

    def test_missing_broker_is_skipped(self) -> None:
        """An owner without private_access_service has nowhere to publish."""
        gen = self._make_gen_ready()
        component = self._component([self._endpoint()], broker_id="")

        created, skipped = asyncio.run(gen._reconcile_private_access_endpoints("checkout", [component]))

        assert (created, skipped) == (0, 1)
        gen.client.create.assert_not_called()

    def test_missing_fqdn_is_skipped(self) -> None:
        """The broker publishes by fqdn, so an endpoint without one is skipped."""
        gen = self._make_gen_ready()
        component = self._component([self._endpoint(fqdn="")])

        created, skipped = asyncio.run(gen._reconcile_private_access_endpoints("checkout", [component]))

        assert (created, skipped) == (0, 1)
        gen.client.create.assert_not_called()

    def test_two_endpoints_sharing_a_broker_share_one_policy(self) -> None:
        """Endpoints of one owner on one broker land in a single policy."""
        gen = self._make_gen_ready()
        component = self._component(
            [
                self._endpoint(name="checkout-web"),
                self._endpoint(name="admin-web", fqdn="admin.internal.c001.demo.local"),
            ]
        )

        created, skipped = asyncio.run(gen._reconcile_private_access_endpoints("checkout", [component]))

        assert (created, skipped) == (2, 0)
        gen._get_or_create_proxy_policy.assert_awaited_once()
        assert gen.client.create.call_count == 2

    def test_rule_description_lists_every_granted_profile(self) -> None:
        """Two profiles granted to one endpoint still give one rule, naming both."""
        gen = self._make_gen_ready()
        grants = [_grant("c001-private-access-standard"), _grant("c001-ops")]
        component = self._component([self._endpoint(dependents=grants)])

        created, _ = asyncio.run(gen._reconcile_private_access_endpoints("checkout", [component]))

        assert created == 1
        description = gen.client.create.call_args.kwargs["data"]["description"]
        assert description.endswith("for c001-ops, c001-private-access-standard")
