"""Integration test — App catalogue enforcement against the 30_all demo.

Coverage:
     1. Load the canonical 30_all demo data.
     2. Materialize the approved C001 checkout request and enforce its external
         payment-gateway dependency through the customer egress proxy.
     3. Enforce the predeclared C005 payment-core web-to-backend dependency
         through a firewall policy between its provisioned VXLAN segments.
"""

import logging
from typing import Any

import pytest
from infrahub_sdk import InfrahubClient, InfrahubClientSync

from .conftest import TestInfrahubDockerWithClient
from .workflow_helpers import run_generator

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

SCENARIO_NAME = "App Catalogue: 30_all Enforcement"
BRANCH_NAME = "app-catalogue-scenario"
DATA_PATH = "data/demos/30_all"
CHECKOUT_REQUEST_NAME = "c001-checkout-request"
C005_APPLICATION_NAME = "c005-payment-core-p"
BROKER_NAME = "c001-private-access"
PROXY_POLICY_NAME = "proxy-C001-c001-web-gateway-egress"
SEGMENT_POLICY_NAME = "seg-c005-web-frontend-local-dc10-p-egress"


class TestAppCatalogue(TestInfrahubDockerWithClient):
    """Test request materialization and application enforcement against 30_all."""

    @pytest.fixture(scope="class")
    def scenario_branch(self) -> str:
        return BRANCH_NAME

    @pytest.mark.order(400)
    @pytest.mark.dependency(scope="session", name="app_catalogue_load", depends=["bootstrap_data"])
    def test_01_load_data(
        self,
        client_main: InfrahubClientSync,
        scenario_branch: str,
    ) -> None:
        """Create branch and load the canonical 30_all demo data."""
        logging.info("=== %s - Step 1: Load Data ===", SCENARIO_NAME)

        existing_branches = client_main.branch.all()
        if scenario_branch not in existing_branches:
            client_main.branch.create(
                branch_name=scenario_branch,
                sync_with_git=False,
                wait_until_completion=True,
            )
            logging.info("Created branch: %s", scenario_branch)

        load_result = self.execute_command(
            f"infrahubctl object load {DATA_PATH} --branch {scenario_branch}",
            address=client_main.config.address,
        )

        assert load_result.returncode == 0, (
            f"Failed to load app-catalogue data.\n"
            f"  Return code: {load_result.returncode}\n"
            f"  stdout: {load_result.stdout}\n"
            f"  stderr: {load_result.stderr}"
        )

        logging.info("App-catalogue data loaded successfully")

    @pytest.mark.order(401)
    @pytest.mark.dependency(scope="session", name="app_catalogue_run_gen", depends=["app_catalogue_load"])
    @pytest.mark.asyncio
    async def test_02_run_generator(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
        workflow_state: dict[str, Any],
    ) -> None:
        """Materialize C001 checkout, then enforce C001 and C005 applications."""
        logging.info("=== %s - Step 2: Run Generator ===", SCENARIO_NAME)

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

        checkout_apps = await client.filters(kind="AppApplication", label__value=CHECKOUT_REQUEST_NAME)
        assert len(checkout_apps) == 1, (
            f"Expected request '{CHECKOUT_REQUEST_NAME}' to materialize one AppApplication, found {len(checkout_apps)}"
        )
        c005_app = await client.get(kind="AppApplication", name__value=C005_APPLICATION_NAME)
        assert c005_app, f"AppApplication '{C005_APPLICATION_NAME}' not found"

        result = await run_generator(
            client=client,
            generator_name="add_app_application",
            node_ids=[checkout_apps[0].id, c005_app.id],
            branch=scenario_branch,
        )
        workflow_state["app_catalogue_generator_task"] = result
        logging.info("Generator task completed: %s", result["task_state"])

    @pytest.mark.order(402)
    @pytest.mark.dependency(scope="session", name="app_catalogue_no_failures", depends=["app_catalogue_run_gen"])
    @pytest.mark.asyncio
    async def test_03_verify_no_failed_tasks(
        self,
        workflow_state: dict[str, Any],
    ) -> None:
        """Verify the request and application generator tasks completed."""
        logging.info("=== %s - Step 3: Verify No Failed Tasks ===", SCENARIO_NAME)

        request_result = workflow_state["app_catalogue_request_generator_task"]
        application_result = workflow_state["app_catalogue_generator_task"]
        assert request_result["success"], f"Request generator failed: {request_result}"
        assert application_result["success"], f"Application generator failed: {application_result}"

        logging.info("Request and application generator tasks completed successfully")

    @pytest.mark.order(403)
    @pytest.mark.dependency(scope="session", name="app_catalogue_verify_ztna", depends=["app_catalogue_no_failures"])
    @pytest.mark.asyncio
    async def test_04_verify_customer_private_access_assignment(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Verify C001's customer-level private access service assignment."""
        logging.info("=== %s - Step 4: Verify Customer Private Access Assignment ===", SCENARIO_NAME)

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
    async def test_05_verify_proxy_policy_rule(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Verify the external endpoint dependency produced a ProxyPolicy/ProxyPolicyRule."""
        logging.info("=== %s - Step 5: Verify Proxy Policy Rule ===", SCENARIO_NAME)

        client = async_client_main
        client.default_branch = scenario_branch

        policy = await client.get(kind="ProxyPolicy", name__value=PROXY_POLICY_NAME)
        assert policy, f"ProxyPolicy '{PROXY_POLICY_NAME}' not found — egress rule was not generated"

        rules = await client.filters(kind="ProxyPolicyRule", policy__ids=[policy.id])
        assert rules, f"No ProxyPolicyRule found under policy '{PROXY_POLICY_NAME}'"

        rule_destinations = {r.id: getattr(getattr(r, "destination", None), "value", None) for r in rules}
        destinations = set(rule_destinations.values())
        assert "api.paymentgw.example.com" in destinations, (
            f"Expected a rule with destination 'api.paymentgw.example.com', found: {destinations}"
        )

        matching_rule = next(r for r in rules if rule_destinations[r.id] == "api.paymentgw.example.com")
        action_value = getattr(getattr(matching_rule, "action", None), "value", None)
        assert action_value == "allow", f"Expected action 'allow' for the payment-gateway rule, got '{action_value}'"

        logging.info("ProxyPolicyRule correctly generated for the external endpoint dependency")

    @pytest.mark.order(405)
    @pytest.mark.dependency(
        scope="session", name="app_catalogue_verify_firewall_policy", depends=["app_catalogue_no_failures"]
    )
    @pytest.mark.asyncio
    async def test_06_verify_firewall_policy_rule(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Verify the internal endpoint dependency produced a firewall rule."""
        logging.info("=== %s - Step 6: Verify Firewall Policy Rule ===", SCENARIO_NAME)

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
