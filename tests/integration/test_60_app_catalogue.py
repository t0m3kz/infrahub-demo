"""Integration test — App catalogue enforcement against the 30_all demo.

Runs against the branch test_59 builds; it never loads data of its own. Where
test_61-test_63 assert on what the load *produced*, this module drives the two
enforcement paths the catalogue is for:

  1. Materialize the approved C001 checkout request and enforce its external
     payment-gateway dependency through the customer egress proxy.
  2. Enforce the predeclared C005 payment-core web-to-backend dependency
     through a firewall policy between its provisioned VXLAN segments.

The two generator runs here are the only ones in the 30_all suite the tests
invoke by hand. Everything else is trigger-dispatched, and test_59 counts those
dispatches. ``add_application_deployment_request`` has no trigger rule on
purpose — a deployment request is materialized on approval, not on creation —
and the application it materializes needs ``add_app_application`` run over it
afterwards, since it was born from a generator rather than from the loader.
"""

import logging
from typing import Any

import pytest
from infrahub_sdk import InfrahubClient

from .conftest import TestInfrahubDockerWithClient
from .test_constants import ALL_DEMO_BRANCH
from .workflow_helpers import run_generator, wait_for_tasks_completion

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

SCENARIO_NAME = "Scenario 30_all: App Catalogue Enforcement"
CHECKOUT_REQUEST_NAME = "c001-checkout-request"
CHECKOUT_DEPENDENCY_NAME = "c001-checkout-to-payment-gateway"
C005_APPLICATION_NAME = "c005-payment-core-p"
BROKER_NAME = "c001-private-access"
PROXY_POLICY_NAME = "proxy-C001-c001-web-gateway-egress"
SEGMENT_POLICY_NAME = "seg-c005-web-frontend-local-dc10-p-egress"


class TestAppCatalogue(TestInfrahubDockerWithClient):
    """Test request materialization and application enforcement against 30_all."""

    @pytest.fixture(scope="class")
    def scenario_branch(self) -> str:
        return ALL_DEMO_BRANCH

    @pytest.mark.order(401)
    @pytest.mark.dependency(scope="session", name="app_catalogue_run_gen", depends=["all_demo_inventory"])
    @pytest.mark.asyncio
    async def test_01_run_generator(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
        workflow_state: dict[str, Any],
    ) -> None:
        """Materialize C001 checkout, then enforce C001 and C005 applications."""
        logging.info("=== %s - Step 1: Run Generator ===", SCENARIO_NAME)

        client = async_client_main
        client.default_branch = scenario_branch

        request = await client.get(kind="AppDeploymentRequest", name__value=CHECKOUT_REQUEST_NAME)
        assert request, f"AppDeploymentRequest '{CHECKOUT_REQUEST_NAME}' not found"

        request_result = await run_generator(
            client=client,
            generator_name="add_application_deployment_request",
            node_ids=[request.id],
            branch=scenario_branch,
        )
        workflow_state["app_catalogue_request_generator_task"] = request_result
        await wait_for_tasks_completion(async_client_main, scenario_branch)

        checkout_apps = await client.filters(kind="AppApplication", label__value=CHECKOUT_REQUEST_NAME)
        assert len(checkout_apps) == 1, (
            f"Expected request '{CHECKOUT_REQUEST_NAME}' to materialize one AppApplication, found {len(checkout_apps)}"
        )
        checkout_dependency = await client.get(kind="AppDependency", name__value=CHECKOUT_DEPENDENCY_NAME)
        assert checkout_dependency, (
            f"Expected request '{CHECKOUT_REQUEST_NAME}' to materialize dependency '{CHECKOUT_DEPENDENCY_NAME}'"
        )
        c005_app = await client.get(kind="AppApplication", name__value=C005_APPLICATION_NAME)
        assert c005_app, f"AppApplication '{C005_APPLICATION_NAME}' not found"

        checkout_result = await run_generator(
            client=client,
            generator_name="add_app_application",
            node_ids=[checkout_apps[0].id],
            branch=scenario_branch,
        )
        c005_result = await run_generator(
            client=client,
            generator_name="add_app_application",
            node_ids=[c005_app.id],
            branch=scenario_branch,
        )
        workflow_state["app_catalogue_generator_tasks"] = [checkout_result, c005_result]
        logging.info(
            "Application generator tasks completed: checkout=%s, C005=%s",
            checkout_result["task_state"],
            c005_result["task_state"],
        )
        await wait_for_tasks_completion(async_client_main, scenario_branch)

    @pytest.mark.order(402)
    @pytest.mark.dependency(scope="session", name="app_catalogue_no_failures", depends=["app_catalogue_run_gen"])
    @pytest.mark.asyncio
    async def test_02_verify_no_failed_tasks(
        self,
        workflow_state: dict[str, Any],
    ) -> None:
        """Verify the request and application generator tasks completed."""
        logging.info("=== %s - Step 2: Verify No Failed Tasks ===", SCENARIO_NAME)

        request_result = workflow_state["app_catalogue_request_generator_task"]
        application_results = workflow_state["app_catalogue_generator_tasks"]
        assert request_result["success"], f"Request generator failed: {request_result}"
        assert all(result["success"] for result in application_results), (
            f"Application generator failed: {application_results}"
        )

        logging.info("Request and application generator tasks completed successfully")

    @pytest.mark.order(403)
    @pytest.mark.dependency(scope="session", name="app_catalogue_verify_ztna", depends=["app_catalogue_no_failures"])
    @pytest.mark.asyncio
    async def test_03_verify_customer_private_access_assignment(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Verify C001's customer-level private access service assignment."""
        logging.info("=== %s - Step 3: Verify Customer Private Access Assignment ===", SCENARIO_NAME)

        client = async_client_main
        client.default_branch = scenario_branch

        broker = await client.get(kind="ManagedCloudProxy", name__value=BROKER_NAME)
        assert broker, f"ManagedCloudProxy '{BROKER_NAME}' not found"

        customer = await client.get(
            kind="OrganizationCustomer", org_id__value="C001", include=["private_access_service"]
        )
        assert customer, "OrganizationCustomer 'C001' not found"
        private_access_rel = getattr(customer, "private_access_service", None)
        assert private_access_rel is not None and private_access_rel.id, "C001 has no private_access_service assignment"
        assert private_access_rel.id == broker.id, (
            f"customer private_access_service resolved to {private_access_rel.id}, expected the "
            f"C001-owned broker {broker.id} ('{BROKER_NAME}')"
        )

        logging.info("customer private_access_service correctly assigned to '%s'", BROKER_NAME)

    @pytest.mark.order(404)
    @pytest.mark.dependency(
        scope="session", name="app_catalogue_verify_proxy_policy", depends=["app_catalogue_no_failures"]
    )
    @pytest.mark.asyncio
    async def test_04_verify_proxy_policy_rule(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Verify the external endpoint dependency produced a ProxyPolicy/ProxyPolicyRule."""
        logging.info("=== %s - Step 4: Verify Proxy Policy Rule ===", SCENARIO_NAME)

        client = async_client_main
        client.default_branch = scenario_branch

        policy = await client.get(kind="ProxyPolicy", name__value=PROXY_POLICY_NAME)
        assert policy, f"ProxyPolicy '{PROXY_POLICY_NAME}' not found — egress rule was not generated"

        rules = await client.filters(kind="ProxyPolicyRule", policy__ids=[policy.id])
        assert rules, f"No ProxyPolicyRule found under policy '{PROXY_POLICY_NAME}'"

        rule_destinations = {r.id: getattr(getattr(r, "destination", None), "value", None) for r in rules}
        matching_rule = next((r for r in rules if rule_destinations[r.id] == "api.paymentgw.example.com"), None)
        assert matching_rule is not None, (
            f"Expected a rule with destination 'api.paymentgw.example.com', found: {set(rule_destinations.values())}"
        )

        action_value = getattr(getattr(matching_rule, "action", None), "value", None)
        assert action_value == "allow", f"Expected action 'allow' for the payment-gateway rule, got '{action_value}'"

        logging.info("ProxyPolicyRule correctly generated for the external endpoint dependency")

    @pytest.mark.order(405)
    @pytest.mark.dependency(
        scope="session", name="app_catalogue_verify_firewall_policy", depends=["app_catalogue_no_failures"]
    )
    @pytest.mark.asyncio
    async def test_05_verify_firewall_policy_rule(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Verify the internal endpoint dependency produced a firewall rule."""
        logging.info("=== %s - Step 5: Verify Firewall Policy Rule ===", SCENARIO_NAME)

        client = async_client_main
        client.default_branch = scenario_branch

        policy = await client.get(kind="SecurityPolicy", name__value=SEGMENT_POLICY_NAME)
        assert policy, f"SecurityPolicy '{SEGMENT_POLICY_NAME}' not found — firewall rule was not generated"

        rules = await client.filters(kind="SecurityPolicyRule", policy__ids=[policy.id])
        assert rules, f"No SecurityPolicyRule found under policy '{SEGMENT_POLICY_NAME}'"

        matching_rules = []
        for rule in rules:
            protocol = getattr(getattr(rule, "protocol", None), "value", None)
            port_start = getattr(getattr(rule, "port_start", None), "value", None)
            if protocol == "tcp" and port_start == 8443:
                matching_rules.append(rule)

        assert matching_rules, "Expected a TCP/8443 firewall rule for frontend-to-backend dependency"
        logging.info("SecurityPolicyRule correctly generated for the internal_service dependency")
