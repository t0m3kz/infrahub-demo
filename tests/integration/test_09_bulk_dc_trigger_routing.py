"""Verify automatic routing when a DC and its PODs are created together."""

from __future__ import annotations

import logging

import pytest
from infrahub_sdk import InfrahubClient, InfrahubClientSync

from .conftest import TestInfrahubDockerWithClient
from .test_constants import DEMO_DC_DATA_ROOT
from .workflow_helpers import verify_no_failed_tasks, wait_for_tasks_completion

BRANCH_NAME = "bulk-dc-trigger-routing"
DC_DATA_PATHS = f"{DEMO_DC_DATA_ROOT}/dc1"


class TestBulkDCTriggerRouting(TestInfrahubDockerWithClient):
    """Verify automatic DC/POD triggers converge without explicit add_dc execution."""

    @pytest.fixture(scope="class")
    def scenario_branch(self) -> str:
        """Return the branch dedicated to this concurrency reproduction."""
        return BRANCH_NAME

    @pytest.mark.order(90)
    @pytest.mark.dependency(scope="session", name="bulk_dc_data_loaded", depends=["repository_sync"])
    def test_01_bulk_load_dc_data(self, client_main: InfrahubClientSync, scenario_branch: str) -> None:
        """Load DC1 with nested PODs, relying only on created-node triggers."""
        if scenario_branch in client_main.branch.all():
            client_main.branch.delete(branch_name=scenario_branch)
        client_main.branch.create(branch_name=scenario_branch, sync_with_git=False, wait_until_completion=True)

        result = self.execute_command(
            f"infrahubctl object load {DC_DATA_PATHS} --branch {scenario_branch}",
            address=client_main.config.address,
        )
        assert result.returncode == 0, result.stderr

    @pytest.mark.order(91)
    @pytest.mark.dependency(scope="session", depends=["bulk_dc_data_loaded"])
    @pytest.mark.asyncio
    async def test_02_automatic_routing_tasks_converge(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Wait for automatic triggers, then fail on any routing task failure."""
        logging.info("Waiting for bulk DC/POD trigger tasks on branch %s", scenario_branch)
        await wait_for_tasks_completion(async_client_main, scenario_branch, max_wait_attempts=72, stable_zero_count=3)
        await verify_no_failed_tasks(async_client_main, scenario_branch)
