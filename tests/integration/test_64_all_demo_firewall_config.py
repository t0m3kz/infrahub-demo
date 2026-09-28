"""Integration test — validate and render the colo cloud/partner/SaaS zone
policy rules (data/demos/30_all/08_interconnects/07_zone_policies/) against a
real loaded branch.

Mirrors test_63_all_demo_change_risk.py's approach: run the check and the
transform in-process against the branch test_59 built, rather than creating a
Proposed Change. `.infrahub.yml` declares a firewall_config artifact
definition targeting every firewall in the "firewalls" group, so a PC over
the full 30_all branch would fan out artifact generation across a hundred-odd
devices to prove something one device's worth of query+check+render already
proves — see test_63's docstring for the same reasoning.

CheckFirewall (checks/firewall.py) validates the whole SecurityZone/
SecurityPolicy/SecurityTagRule graph, not a specific device — its own query
just happens to be device-shaped (queries/config/firewall.gql, $device
required) because it reuses the artifact's own query. get_zone_policies()
(transforms/helpers/firewall.py) also merges every *global* SecurityPolicy
onto every firewall's rendered output (transforms/config/firewall.py's
_merge_policies), so any one real firewall device is sufficient to exercise
both the check and the render against the new colo-fr-external-egress policy
— it does not need to be the FR metro's own firewall specifically.
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

_ROOT = str(Path(__file__).parent.parent.parent)


class TestAllDemoFirewallConfig(TestInfrahubDockerWithClient):
    """Validate and render the colo-fr-external-egress policy against a real firewall device."""

    @pytest.fixture(scope="class")
    def scenario_branch(self) -> str:
        return ALL_DEMO_BRANCH

    @pytest.fixture
    async def any_firewall_device_name(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> str:
        """Any one firewall device on the branch — get_zone_policies() merges
        every global SecurityPolicy onto every firewall's render regardless
        of which one it is (see module docstring). Function-scoped: an async
        fixture can't be class-scoped under this suite's function-scoped
        asyncio fixture loop (pytest-asyncio ScopeMismatch) — the extra query
        this costs per test is negligible next to the staged load above it."""
        devices = await async_client_main.filters(
            kind=DcimPhysicalDevice, role__value="firewall", branch=scenario_branch
        )
        assert devices, f"no firewall device found on branch '{scenario_branch}' — cannot validate zone policies"
        return devices[0].name.value

    @pytest.mark.order(399)
    @pytest.mark.dependency(scope="session", name="all_demo_firewall_check", depends=["all_demo_change_risk"])
    @pytest.mark.asyncio
    async def test_01_check_firewall_passes(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
        any_firewall_device_name: str,
    ) -> None:
        """CheckFirewall must find no selector/zone/tag-contract errors across
        the whole SecurityPolicy graph — including the new colo-fr-external-
        egress policy's destination_prefixes-only rules (no zone/segment)."""
        logging.info("=== %s - Step 1: CheckFirewall ===", SCENARIO_NAME)

        check = CheckFirewall(
            branch=scenario_branch,
            client=async_client_main,
            params={"device": any_firewall_device_name},
        )
        passed = await check.run()

        messages = [str(entry.get("message") or "") for entry in check.logs]
        for message in messages:
            logging.info("check: %s", message)

        errors = [str(entry.get("message") or "") for entry in check.logs if entry.get("level") == "ERROR"]
        assert passed and not errors, (
            f"CheckFirewall failed on branch '{scenario_branch}' (device={any_firewall_device_name}):\n"
            + "\n".join(f"  - {e}" for e in errors)
        )
        logging.info("CheckFirewall passed for device %s", any_firewall_device_name)

    @pytest.mark.order(400)
    @pytest.mark.dependency(scope="session", name="all_demo_firewall_render", depends=["all_demo_firewall_check"])
    @pytest.mark.asyncio
    async def test_02_firewall_config_renders_colo_zone_policy_rules(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
        any_firewall_device_name: str,
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
            variables={"device": any_firewall_device_name},
        )
        data: dict[str, Any] = raw.get("data") or raw

        fw = Firewall.__new__(Firewall)
        fw.root_directory = _ROOT
        rendered = await fw.transform(data)

        assert isinstance(rendered, str) and rendered, (
            f"firewall_config transform produced no output for device {any_firewall_device_name}"
        )

        missing = [cidr for cidr in EXPECTED_DESTINATION_CIDRS if cidr not in rendered]
        assert not missing, (
            f"rendered config for {any_firewall_device_name} on branch '{scenario_branch}' is missing "
            f"destination CIDR(s) from the colo-fr-external-egress policy: {missing}"
        )
        logging.info(
            "Rendered %s's firewall_config: all %d colo interconnect destination CIDRs present",
            any_firewall_device_name,
            len(EXPECTED_DESTINATION_CIDRS),
        )
        logging.info("=== %s - COMPLETED ===", SCENARIO_NAME)
