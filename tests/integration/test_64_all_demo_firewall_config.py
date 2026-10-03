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
so the render runs on every member of the cluster serving the policy's source
segment (c001-nordix-prod-p, deployment C001-P-FR — the FR metro's firewall
pair), and once on a firewall outside it that must not carry the rules.
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

# The rule names from 05_security_policy.yml. Unlike the CIDRs, nothing else in
# the branch carries them, so their absence from a config proves the policy was
# not placed there.
EXPECTED_RULE_NAMES = ("nordix-prod-to-aws", "nordix-prod-to-partner-acme", "nordix-prod-to-saas-zscaler")

# The policy's source segment -> its colocation deployment -> the serving
# context's HA cluster, plus the firewalls of the metro the deployment sits in.
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
            ... on TopologyCustomerColocation {
              parent {
                node {
                  ... on TopologyColocationMetro {
                    devices(role__value: "firewall") { edges { node { name { value } } } }
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}
"""

FIREWALLS_QUERY = """
query {
  DcimPhysicalDevice(role__value: "firewall") {
    edges { node { name { value } platform { node { name { value } } } } }
  }
}
"""

_ROOT = str(Path(__file__).parent.parent.parent)


async def _render(client: InfrahubClient, branch: str, device: str) -> str:
    raw = await client.query_gql_query(name="firewall_config", branch_name=branch, variables={"device": device})
    data: dict[str, Any] = raw.get("data") or raw

    fw = Firewall.__new__(Firewall)
    fw.root_directory = _ROOT
    rendered = await fw.transform(data)
    assert isinstance(rendered, str) and rendered, f"firewall_config transform produced no output for device {device}"
    return rendered


class TestAllDemoFirewallConfig(TestInfrahubDockerWithClient):
    """Validate and render the colo-fr-external-egress policy on the firewalls serving it."""

    @pytest.fixture(scope="class")
    def scenario_branch(self) -> str:
        return ALL_DEMO_BRANCH

    @pytest.fixture
    async def serving_cluster(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> dict[str, Any]:
        """The firewall cluster whose context serves the policy's source
        segment: {"members", "metro_firewalls", "outsider"}, names sorted.
        outsider is a firewall outside the cluster on the members' platform,
        so its template renders rule names too. Function-scoped: an async
        fixture can't be class-scoped under this suite's function-scoped
        asyncio fixture loop (pytest-asyncio ScopeMismatch)."""
        result = await async_client_main.execute_graphql(
            query=SERVING_CLUSTER_QUERY, variables={"segment": SOURCE_SEGMENT}, branch_name=scenario_branch
        )
        segments = result["ManagedVlanSegment"]["edges"]
        assert segments, f"segment '{SOURCE_SEGMENT}' not found on branch '{scenario_branch}'"
        deployment = (segments[0]["node"].get("customer_deployment") or {}).get("node") or {}
        context = (deployment.get("serving_firewall_context") or {}).get("node") or {}
        cluster_id = ((context.get("cluster") or {}).get("node") or {}).get("id")
        assert cluster_id, f"no serving firewall context with a cluster for '{SOURCE_SEGMENT}'"
        metro = (deployment.get("parent") or {}).get("node") or {}
        metro_firewalls = sorted(
            edge["node"]["name"]["value"] for edge in (metro.get("devices") or {}).get("edges", [])
        )

        devices = await async_client_main.filters(
            kind=DcimPhysicalDevice, role__value="firewall", capabilities__ids=[cluster_id], branch=scenario_branch
        )
        assert devices, f"no firewall device in cluster {cluster_id} on branch '{scenario_branch}'"
        members = sorted(device.name.value for device in devices)

        firewalls = await async_client_main.execute_graphql(query=FIREWALLS_QUERY, branch_name=scenario_branch)
        platform_by_name = {
            edge["node"]["name"]["value"]: ((edge["node"].get("platform") or {}).get("node") or {})
            .get("name", {})
            .get("value")
            for edge in firewalls["DcimPhysicalDevice"]["edges"]
        }
        member_platforms = {platform_by_name.get(name) for name in members}
        outsiders = sorted(
            name for name, platform in platform_by_name.items() if name not in members and platform in member_platforms
        )
        return {"members": members, "metro_firewalls": metro_firewalls, "outsider": outsiders[0] if outsiders else None}

    @pytest.mark.order(398)
    @pytest.mark.dependency(scope="session", name="all_demo_firewall_cluster", depends=["all_demo_inventory"])
    @pytest.mark.asyncio
    async def test_00_serving_cluster_is_the_metro_pair(self, serving_cluster: dict[str, Any]) -> None:
        """The context serving a colocation deployment lives on the firewall
        pair of the metro the deployment sits in, not on some DC's cluster."""
        logging.info("=== %s - Step 0: Serving Cluster ===", SCENARIO_NAME)

        assert len(serving_cluster["members"]) == 2, f"serving cluster is not a pair: {serving_cluster['members']}"
        assert serving_cluster["members"] == serving_cluster["metro_firewalls"], (
            f"'{SOURCE_SEGMENT}' is served by cluster {serving_cluster['members']}, "
            f"expected its metro's firewalls {serving_cluster['metro_firewalls']}"
        )
        logging.info("Serving cluster: %s", serving_cluster["members"])

    @pytest.mark.order(399)
    @pytest.mark.dependency(scope="session", name="all_demo_firewall_check", depends=["all_demo_firewall_cluster"])
    @pytest.mark.asyncio
    async def test_01_check_firewall_passes(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
        serving_cluster: dict[str, Any],
    ) -> None:
        """CheckFirewall must find no selector/zone/tag-contract errors across
        the whole SecurityPolicy graph — including the new colo-fr-external-
        egress policy's destination_prefixes-only rules (no zone/segment)."""
        logging.info("=== %s - Step 1: CheckFirewall ===", SCENARIO_NAME)
        serving_firewall_device_name = serving_cluster["members"][0]

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
        serving_cluster: dict[str, Any],
    ) -> None:
        """Render the real firewall_config transform against live data on
        every member of the serving cluster and confirm the policy's rules and
        destination CIDRs made it all the way from the object files through
        the generator-populated graph, the firewall_config query, and
        get_zone_policies() into real device config text. A firewall outside
        the cluster on the same platform must not carry the rules: strict
        placement puts a rule only where its segment is served."""
        logging.info("=== %s - Step 2: Render firewall_config ===", SCENARIO_NAME)

        errors: list[str] = []
        for device in serving_cluster["members"]:
            rendered = await _render(async_client_main, scenario_branch, device)
            missing = [name for name in (*EXPECTED_RULE_NAMES, *EXPECTED_DESTINATION_CIDRS) if name not in rendered]
            if missing:
                errors.append(f"{device}: missing colo-fr-external-egress rule(s)/CIDR(s) {missing}")

        outsider = serving_cluster["outsider"]
        assert outsider, f"no firewall outside {serving_cluster['members']} on their platform to render against"
        rendered = await _render(async_client_main, scenario_branch, outsider)
        leaked = [name for name in EXPECTED_RULE_NAMES if name in rendered]
        if leaked:
            errors.append(f"{outsider}: does not serve '{SOURCE_SEGMENT}' but renders rule(s) {leaked}")

        assert not errors, f"firewall_config on branch '{scenario_branch}' is wrong:\n" + "\n".join(
            f"  - {e}" for e in errors
        )
        logging.info("Rendered firewall_config: rules on %s, none on %s", serving_cluster["members"], outsider)
        logging.info("=== %s - COMPLETED ===", SCENARIO_NAME)
