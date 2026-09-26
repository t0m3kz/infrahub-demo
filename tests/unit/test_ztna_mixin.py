"""Unit tests for ZtnaMixin (generators/ztna.py).

Covers:
  - _reconcile_proxy_rule()               — external_service egress
  - _reconcile_private_access_endpoints() — ZTNA broker publish
  - _ensure_endpoint_access_profile()     — access-profile derivation
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


class TestReconcilePrivateAccessEndpoints:
    """private_access endpoints used to have no code path at all: this
    dispatch previously fell through to the segment-firewall branch, found
    no network_segment, and silently skipped — the endpoint was never
    published anywhere. Published independently of any AppDependency edge,
    since who may reach it is gated by its own access_profile, not by which
    component "depends on" it."""

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
    ) -> dict:
        return {"id": f"endpoint-{name}", "name": name, "endpoint_type": endpoint_type, "fqdn": fqdn}

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
        # Access-profile derivation is a separate concern with its own test
        # class below — stub it out here so these tests stay focused on the
        # ZTNA broker publish path.
        gen._ensure_endpoint_access_profile = AsyncMock()
        return gen

    def test_publishes_a_private_access_endpoint(self):
        gen = self._make_gen_ready()
        component = self._component([self._endpoint()])

        created, skipped = asyncio.run(gen._reconcile_private_access_endpoints("checkout", [component]))

        assert (created, skipped) == (1, 0)
        gen._get_or_create_proxy_policy.assert_awaited_once_with("proxy-C001-c001-private-access-private-access")
        gen._attach_proxy_policy_to_owner.assert_awaited_once_with(owner_id="customer-1", policy_id="policy-1")
        rule_data = gen.client.create.call_args.kwargs["data"]
        assert rule_data["destination_type"] == "fqdn"
        assert rule_data["destination"] == "checkout.internal.c001.demo.local"

    def test_internal_and_external_endpoints_are_not_published(self):
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

    def test_missing_broker_is_skipped(self):
        gen = self._make_gen_ready()
        component = self._component([self._endpoint()], broker_id="")

        created, skipped = asyncio.run(gen._reconcile_private_access_endpoints("checkout", [component]))

        assert (created, skipped) == (0, 1)
        gen.client.create.assert_not_called()

    def test_missing_fqdn_is_skipped(self):
        gen = self._make_gen_ready()
        component = self._component([self._endpoint(fqdn="")])

        created, skipped = asyncio.run(gen._reconcile_private_access_endpoints("checkout", [component]))

        assert (created, skipped) == (0, 1)
        gen.client.create.assert_not_called()

    def test_two_endpoints_sharing_a_broker_share_one_policy(self):
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

    def test_derives_access_profile_when_endpoint_has_none(self):
        gen = self._make_gen_ready()
        endpoint = self._endpoint()
        assert "access_profile" not in endpoint
        component = self._component([endpoint])

        asyncio.run(gen._reconcile_private_access_endpoints("checkout", [component], "internet_exposed"))

        gen._ensure_endpoint_access_profile.assert_awaited_once_with(
            endpoint_id="endpoint-checkout-web",
            endpoint_name="checkout-web",
            owner_org_id="C001",
            app_security_profile="internet_exposed",
        )

    def test_does_not_override_an_explicit_access_profile(self):
        gen = self._make_gen_ready()
        endpoint = self._endpoint()
        endpoint["access_profile"] = {"id": "profile-custom", "allowed_groups": []}
        component = self._component([endpoint])

        asyncio.run(gen._reconcile_private_access_endpoints("checkout", [component]))

        gen._ensure_endpoint_access_profile.assert_not_awaited()


# ===========================================================================
# TestEnsureEndpointAccessProfile
# ===========================================================================


class TestEnsureEndpointAccessProfile:
    """SecurityAccessProfile/SecurityIdentityGroup used to be hand-authored
    per customer (data/demos/30_all/07_applications/00_base_security_*.yml);
    this derives the same shape from the endpoint's owner + the app's
    security_profile so it scales past one hand-written example."""

    def _make_gen_ready(self, *, group: Any = None, profile: Any = None) -> Any:
        gen = _make_gen()
        group = group or MagicMock(id="group-1")
        profile = profile or MagicMock(id="profile-1")
        gen._get_or_create_by_name = AsyncMock(side_effect=[group, profile])
        endpoint = MagicMock()
        endpoint.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=endpoint)
        return gen

    def test_creates_group_and_profile_with_deterministic_names(self):
        gen = self._make_gen_ready()

        asyncio.run(
            gen._ensure_endpoint_access_profile(
                endpoint_id="endpoint-1",
                endpoint_name="checkout-web",
                owner_org_id="C001",
                app_security_profile="internet_exposed",
            )
        )

        group_call, profile_call = gen._get_or_create_by_name.call_args_list
        assert group_call.kwargs["kind"] == "SecurityIdentityGroup"
        assert group_call.kwargs["name"] == "c001-engineering"
        assert profile_call.kwargs["kind"] == "SecurityAccessProfile"
        assert profile_call.kwargs["name"] == "c001-private-access-standard"

    def test_profile_policy_matches_internet_exposed_defaults(self):
        gen = self._make_gen_ready()

        asyncio.run(
            gen._ensure_endpoint_access_profile(
                endpoint_id="endpoint-1",
                endpoint_name="checkout-web",
                owner_org_id="C001",
                app_security_profile="internet_exposed",
            )
        )

        _, profile_call = gen._get_or_create_by_name.call_args_list
        create_data = profile_call.kwargs["create_data"]
        assert create_data["mfa_required"] is True
        assert create_data["device_posture_required"] is True
        assert create_data["session_timeout_minutes"] == 480
        assert create_data["allowed_groups"] == ["group-1"]

    def test_sets_access_profile_on_endpoint(self):
        gen = self._make_gen_ready()

        asyncio.run(
            gen._ensure_endpoint_access_profile(
                endpoint_id="endpoint-1",
                endpoint_name="checkout-web",
                owner_org_id="C001",
                app_security_profile="internet_exposed",
            )
        )

        gen.client.create.assert_awaited_once_with(
            kind="AppEndpoint",
            data={"id": "endpoint-1", "access_profile": {"id": "profile-1"}},
        )

    def test_skips_without_endpoint_id(self):
        gen = self._make_gen_ready()

        asyncio.run(
            gen._ensure_endpoint_access_profile(
                endpoint_id="",
                endpoint_name="checkout-web",
                owner_org_id="C001",
                app_security_profile="internet_exposed",
            )
        )

        gen._get_or_create_by_name.assert_not_awaited()
        gen.client.create.assert_not_awaited()

    def test_skips_endpoint_update_when_group_lookup_fails(self):
        gen = _make_gen()
        gen._get_or_create_by_name = AsyncMock(return_value=None)
        gen.client.create = AsyncMock()

        asyncio.run(
            gen._ensure_endpoint_access_profile(
                endpoint_id="endpoint-1",
                endpoint_name="checkout-web",
                owner_org_id="C001",
                app_security_profile="internet_exposed",
            )
        )

        gen._get_or_create_by_name.assert_awaited_once()
        gen.client.create.assert_not_awaited()
