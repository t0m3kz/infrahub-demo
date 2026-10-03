"""Integration test — validate and render the colo cloud/partner/SaaS zone
policy rules (data/demos/30_all/08_interconnects/07_zone_policies/) against a
real loaded branch.

Runs the check and the transform in-process against the branch test_59
built, rather than creating a Proposed Change. `.infrahub.yml` declares a
firewall_config artifact definition targeting every firewall in the
"firewalls" group, so a PC over the full 30_all branch would fan out artifact
generation across a hundred-odd devices to prove something one device's worth
of query+check+render already proves.

CheckFirewall (checks/firewall.py) validates the whole SecurityZone/
SecurityPolicy/SecurityTagRule graph, not a specific device — its own query
just happens to be device-shaped (queries/config/firewall.gql, $device
required) because it reuses the artifact's own query. The render is not
device-agnostic: place_policies_in_contexts() (transforms/helpers/firewall.py)
puts a rule only on the firewall context serving its segments' deployments,
so the render runs on the firewall serving the policy's source segment
(c001-nordix-prod-p, deployment C001-P-FR).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
from infrahub_sdk import InfrahubClient

from checks.firewall import CheckFirewall
from generators.protocols import DcimPhysicalDevice
from transforms.config.firewall import Firewall

from .conftest import TestInfrahubDockerWithClient
from .test_constants import ALL_DEMO_BRANCH

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

SCENARIO_NAME = "Scenario 30_all: Colo Cloud/Partner/SaaS Zone Policy"

# The three destination CIDRs from 07_zone_policies/04_destination_prefixes.yml,
# each backing one SecurityPolicyRule in 07_zone_policies/05_security_policy.yml.
EXPECTED_DESTINATION_CIDRS = ("10.40.0.0/16", "198.18.0.0/24", "198.51.100.0/24")

# The policy's source segment -> its deployment's serving context -> the HA
# cluster hosting that context, whose members render the policy's rules.
SOURCE_SEGMENT = "c001-nordix-prod-p"
SERVING_CLUSTER_QUERY = """
query ($segment: String!) {
  ManagedVlanSegment(name__value: $segment) {
    edges {
      node {
        customer_deployment {
          node {
            ... on ManagedTenantScoped {
              serving_firewall_context { node { cluster { node { id } } } }
            }
          }
        }
      }
    }
  }
}
"""

_ROOT = str(Path(__file__).parent.parent.parent)


class TestAllDemoFirewallConfig(TestInfrahubDockerWithClient):
    """Validate and render the colo-fr-external-egress policy on the firewall serving it."""

    @pytest.fixture(scope="class")
    def scenario_branch(self) -> str:
        return ALL_DEMO_BRANCH

    @pytest.fixture
    async def serving_firewall_device_name(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> str:
        """A member of the firewall cluster whose context serves the policy's
        source segment. Function-scoped: an async fixture can't be
        class-scoped under this suite's function-scoped asyncio fixture loop
        (pytest-asyncio ScopeMismatch)."""
        result = await async_client_main.execute_graphql(
            query=SERVING_CLUSTER_QUERY, variables={"segment": SOURCE_SEGMENT}, branch_name=scenario_branch
        )
        segments = result["ManagedVlanSegment"]["edges"]
        assert segments, f"segment '{SOURCE_SEGMENT}' not found on branch '{scenario_branch}'"
        deployment = (segments[0]["node"].get("customer_deployment") or {}).get("node") or {}
        context = (deployment.get("serving_firewall_context") or {}).get("node") or {}
        cluster_id = ((context.get("cluster") or {}).get("node") or {}).get("id")
        assert cluster_id, f"no serving firewall context with a cluster for '{SOURCE_SEGMENT}'"

        devices = await async_client_main.filters(
            kind=DcimPhysicalDevice, role__value="firewall", capabilities__ids=[cluster_id], branch=scenario_branch
        )
        assert devices, f"no firewall device in cluster {cluster_id} on branch '{scenario_branch}'"
        return devices[0].name.value

    @pytest.mark.order(399)
    @pytest.mark.dependency(scope="session", name="all_demo_firewall_check", depends=["all_demo_inventory"])
    @pytest.mark.asyncio
    async def test_01_check_firewall_passes(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
        serving_firewall_device_name: str,
    ) -> None:
        """CheckFirewall must find no selector/zone/tag-contract errors across
        the whole SecurityPolicy graph — including the new colo-fr-external-
        egress policy's destination_prefixes-only rules (no zone/segment)."""
        logging.info("=== %s - Step 1: CheckFirewall ===", SCENARIO_NAME)

        check = CheckFirewall(
            branch=scenario_branch,
            client=async_client_main,
            params={"device": serving_firewall_device_name},
        )
        passed = await check.run()

        messages = [str(entry.get("message") or "") for entry in check.logs]
        for message in messages:
            logging.info("check: %s", message)

        errors = [str(entry.get("message") or "") for entry in check.logs if entry.get("level") == "ERROR"]
        assert passed and not errors, (
            f"CheckFirewall failed on branch '{scenario_branch}' (device={serving_firewall_device_name}):\n"
            + "\n".join(f"  - {e}" for e in errors)
        )
        logging.info("CheckFirewall passed for device %s", serving_firewall_device_name)

    @pytest.mark.order(400)
    @pytest.mark.dependency(scope="session", name="all_demo_firewall_render", depends=["all_demo_firewall_check"])
    @pytest.mark.asyncio
    async def test_02_firewall_config_renders_colo_zone_policy_rules(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
        serving_firewall_device_name: str,
    ) -> None:
        """Render the real firewall_config transform against live data and
        confirm all three colo interconnect destination CIDRs made it all the
        way from the object files through the generator-populated graph, the
        firewall_config query, and get_zone_policies() into real device
        config text — not just through synthetic unit-test fixtures."""
        logging.info("=== %s - Step 2: Render firewall_config ===", SCENARIO_NAME)

        raw = await async_client_main.query_gql_query(
            name="firewall_config",
            branch_name=scenario_branch,
            variables={"device": serving_firewall_device_name},
        )
        data: dict[str, Any] = raw.get("data") or raw

        fw = Firewall.__new__(Firewall)
        fw.root_directory = _ROOT
        rendered = await fw.transform(data)

        assert isinstance(rendered, str) and rendered, (
            f"firewall_config transform produced no output for device {serving_firewall_device_name}"
        )

        missing = [cidr for cidr in EXPECTED_DESTINATION_CIDRS if cidr not in rendered]
        assert not missing, (
            f"rendered config for {serving_firewall_device_name} on branch '{scenario_branch}' is missing "
            f"destination CIDR(s) from the colo-fr-external-egress policy: {missing}"
        )
        logging.info(
            "Rendered %s's firewall_config: all %d colo interconnect destination CIDRs present",
            serving_firewall_device_name,
            len(EXPECTED_DESTINATION_CIDRS),
        )
        logging.info("=== %s - COMPLETED ===", SCENARIO_NAME)
