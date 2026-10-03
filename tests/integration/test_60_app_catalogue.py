"""Integration test — App catalogue enforcement against the 30_all demo.

Runs against the branch test_59 builds; it never loads data of its own. Where
test_61-test_63 assert on what the load *produced*, this module drives the two
enforcement paths the catalogue is for:

  1. Enforce the directly-authored C001 checkout application's external
     payment-gateway dependency through the customer egress proxy.
  2. Enforce the predeclared C005 payment-core web-to-backend dependency
     through a firewall policy between its provisioned VXLAN segments.
  3. Publish C001's checkout-web through the ZTNA broker, because an access
     profile is the source of a dependency into it.
  4. Find, for every other AppDependency on the branch, the rule the
     triggers generated for it.

Both applications are loaded straight from data files (no request/approval
object graph — a branch + proposed-change review is the approval step for
anything declared here). The two generator runs here are invoked by hand to
pin down task success/failure for these specific enforcement paths and assert
on their output deterministically, rather than relying on the trigger
dispatch that test_59 already counts for every other application.
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
CHECKOUT_APPLICATION_NAME = "c001-checkout-p"
CHECKOUT_DEPENDENCY_NAME = "c001-checkout-to-payment-gateway"
C005_APPLICATION_NAME = "c005-payment-core-p"
BROKER_NAME = "c001-private-access"
PROXY_POLICY_NAME = "proxy-C001-c001-web-gateway-egress"
SEGMENT_POLICY_NAME = "seg-c005-web-frontend-local-dc10-p-egress"
PRIVATE_ACCESS_POLICY_NAME = "proxy-C001-c001-private-access-private-access"
PRIVATE_ACCESS_RULE_NAME = "publish-c001-checkout-p-frontend-checkout-web"
DEPENDENCY_RULES_QUERY = """
query {
  AppDependency {
    edges {
      node {
        name { value }
        protocol { value }
        port_start { value }
        source_profile { node { id } }
        target {
          node {
            ... on AppEndpoint {
              name { value }
              endpoint_type { value }
              fqdn { value }
              parent { node { ... on AppComponent { slug { value } } } }
            }
          }
        }
      }
    }
  }
  SecurityPolicyRule { edges { node { name { value } protocol { value } port_start { value } } } }
  CloudSecurityGroupRule { edges { node { name { value } protocol { value } port_start { value } } } }
  ProxyPolicyRule { edges { node { name { value } destination { value } } } }
}
"""


class TestAppCatalogue(TestInfrahubDockerWithClient):
    """Test application enforcement against 30_all."""

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
        """Run add_app_application over the C001 checkout and C005 applications."""
        logging.info("=== %s - Step 1: Run Generator ===", SCENARIO_NAME)

        client = async_client_main
        client.default_branch = scenario_branch

        checkout_app = await client.get(kind="AppApplication", name__value=CHECKOUT_APPLICATION_NAME)
        assert checkout_app, f"AppApplication '{CHECKOUT_APPLICATION_NAME}' not found"
        checkout_dependency = await client.get(kind="AppDependency", name__value=CHECKOUT_DEPENDENCY_NAME)
        assert checkout_dependency, f"AppDependency '{CHECKOUT_DEPENDENCY_NAME}' not found"
        c005_app = await client.get(kind="AppApplication", name__value=C005_APPLICATION_NAME)
        assert c005_app, f"AppApplication '{C005_APPLICATION_NAME}' not found"

        checkout_result = await run_generator(
            client=client,
            generator_name="add_app_application",
            node_ids=[checkout_app.id],
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
        """Verify the application generator tasks completed."""
        logging.info("=== %s - Step 2: Verify No Failed Tasks ===", SCENARIO_NAME)

        application_results = workflow_state["app_catalogue_generator_tasks"]
        assert all(result["success"] for result in application_results), (
            f"Application generator failed: {application_results}"
        )

        logging.info("Application generator tasks completed successfully")

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

    @pytest.mark.order(406)
    @pytest.mark.dependency(
        scope="session", name="app_catalogue_verify_private_access", depends=["app_catalogue_no_failures"]
    )
    @pytest.mark.asyncio
    async def test_06_verify_private_access_publish_rule(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Verify the access-profile dependency published checkout-web through the broker."""
        logging.info("=== %s - Step 6: Verify Private Access Publish Rule ===", SCENARIO_NAME)

        client = async_client_main
        client.default_branch = scenario_branch

        policy = await client.get(kind="ProxyPolicy", name__value=PRIVATE_ACCESS_POLICY_NAME)
        assert policy, f"ProxyPolicy '{PRIVATE_ACCESS_POLICY_NAME}' not found — checkout-web was not published"

        rules = await client.filters(kind="ProxyPolicyRule", policy__ids=[policy.id])
        by_name = {getattr(getattr(r, "name", None), "value", None): r for r in rules}
        rule = by_name.get(PRIVATE_ACCESS_RULE_NAME)
        assert rule is not None, f"Expected rule '{PRIVATE_ACCESS_RULE_NAME}', found: {sorted(map(str, by_name))}"
        destination = getattr(getattr(rule, "destination", None), "value", None)
        assert destination == "checkout.internal.c001.demo.local", f"Unexpected destination '{destination}'"
        description = getattr(getattr(rule, "description", None), "value", None) or ""
        assert "c001-private-access-standard" in description, f"Grant profile missing from '{description}'"

        logging.info("checkout-web published via '%s'", PRIVATE_ACCESS_POLICY_NAME)

    @pytest.mark.order(407)
    @pytest.mark.dependency(
        scope="session", name="app_catalogue_every_dependency_enforced", depends=["app_catalogue_no_failures"]
    )
    @pytest.mark.asyncio
    async def test_07_every_dependency_has_its_rule(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Every AppDependency on the branch produced the rule named after it.

        Steps 4-6 pin three hand-run paths. This one covers the rest, which only
        the AppApplication and AppDependency triggers reconcile, so a dependency
        loaded after its application still ends up enforced. A grant publishes
        its endpoint (publish-{component}-{endpoint}); every other dependency
        gets one firewall, security-group or proxy rule named after itself.
        """
        logging.info("=== %s - Step 7: Every Dependency Has Its Rule ===", SCENARIO_NAME)

        result = await async_client_main.execute_graphql(query=DEPENDENCY_RULES_QUERY, branch_name=scenario_branch)

        def edges(kind: str) -> list[dict[str, Any]]:
            return [edge["node"] for edge in result[kind]["edges"]]

        def value(node: dict[str, Any], attribute: str) -> Any:
            return (node.get(attribute) or {}).get("value")

        port_rules = {
            value(rule, "name"): (value(rule, "protocol"), value(rule, "port_start"))
            for kind in ("SecurityPolicyRule", "CloudSecurityGroupRule")
            for rule in edges(kind)
        }
        proxy_rules = {value(rule, "name"): value(rule, "destination") for rule in edges("ProxyPolicyRule")}

        dependencies = edges("AppDependency")
        assert dependencies, "No AppDependency on the branch"
        missing: list[str] = []
        for dep in dependencies:
            name = value(dep, "name")
            endpoint = dep["target"]["node"]
            endpoint_type = value(endpoint, "endpoint_type")
            if (dep.get("source_profile") or {}).get("node"):
                component = value(endpoint["parent"]["node"], "slug")
                rule_name = f"publish-{component}-{value(endpoint, 'name')}"
                if proxy_rules.get(rule_name) != value(endpoint, "fqdn"):
                    missing.append(f"{name}: publish rule {rule_name}")
            elif endpoint_type == "external_service":
                if proxy_rules.get(name) != value(endpoint, "fqdn"):
                    missing.append(f"{name}: proxy rule to {value(endpoint, 'fqdn')}")
            elif port_rules.get(name) != (value(dep, "protocol"), value(dep, "port_start")):
                missing.append(
                    f"{name}: {value(dep, 'protocol')}/{value(dep, 'port_start')} rule, got {port_rules.get(name)}"
                )

        assert not missing, "Dependencies without their rule:\n  " + "\n  ".join(missing)
        logging.info("All %d dependencies have their rule", len(dependencies))
