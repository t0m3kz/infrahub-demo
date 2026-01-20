"""Integration tests for branch operations.

This module contains tests for:
1. Creating a new branch
2. Waiting for branch endpoint to be ready
3. Loading branch-specific data
4. Verifying data was loaded correctly
"""

import asyncio
import logging
import time
from typing import Any

import pytest
from infrahub_sdk import InfrahubClient, InfrahubClientSync

from generators.protocols import TopologyDataCenter

from .conftest import TestInfrahubDockerWithClient
from .test_constants import BRANCH_ENDPOINT_TIMEOUT, DATA_PROPAGATION_DELAY

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


class TestBranch(TestInfrahubDockerWithClient):
    """Test branch operations."""

    @pytest.mark.order(8)
    @pytest.mark.dependency(name="create_branch", depends=["repository_sync"])
    def test_01_create_branch(self, client_main: InfrahubClientSync, default_branch: str) -> None:
        """Create a new branch for the DC1 deployment."""
        logging.info("Starting test: test_01_create_branch - branch: %s", default_branch)

        # Check if branch already exists
        existing_branches = client_main.branch.all()
        if default_branch in existing_branches:
            logging.info("Branch %s already exists", default_branch)
        else:
            client_main.branch.create(
                branch_name=default_branch,
                sync_with_git=False,
                wait_until_completion=True,
            )
            logging.info("Created branch: %s", default_branch)

        # Verify branch was created
        updated_branches = client_main.branch.all()
        assert default_branch in updated_branches, (
            f"Branch creation failed.\n"
            f"  Expected branch: {default_branch}\n"
            f"  Available branches: {list(updated_branches.keys())}"
        )

    @pytest.mark.order(9)
    @pytest.mark.dependency(name="branch_endpoint_ready", depends=["create_branch"])
    def test_02_wait_for_branch_endpoint(self, client_main: InfrahubClientSync, default_branch: str) -> None:
        """Ensure branch endpoint is reachable before continuing."""
        logging.info("Starting test: test_02_wait_for_branch_endpoint")

        # Ensure branch endpoint is reachable before continuing (avoid 404 on /graphql/{branch})
        client_main.default_branch = default_branch
        branch_deadline = time.time() + BRANCH_ENDPOINT_TIMEOUT
        while True:
            try:
                client_main.execute_graphql(query="query { __typename }")
                logging.info("Branch %s endpoint is reachable", default_branch)
                break
            except Exception as exc:  # noqa: BLE001 - we want to catch URL errors here
                if time.time() > branch_deadline:
                    raise AssertionError(f"Branch endpoint for {default_branch} not reachable in time: {exc}") from exc
                logging.info(
                    "Branch %s endpoint not ready yet (%s); retrying...",
                    default_branch,
                    exc,
                )
                time.sleep(5)

    @pytest.mark.order(10)
    @pytest.mark.dependency(name="load_dc_design", depends=["branch_endpoint_ready"])
    def test_03_load_dc_design(self, client_main: InfrahubClientSync, default_branch: str) -> None:
        """Load DC1 demo data onto the branch."""
        logging.info("Starting test: test_03_load_dc_design")

        load_dc = self.execute_command(
            f"infrahubctl object load tests/integration/data/01_dc --branch {default_branch}",
            address=client_main.config.address,
        )

        logging.info("DC design load output: %s", load_dc.stdout)
        assert load_dc.returncode == 0, (
            f"DC design load failed.\n"
            f"  Branch: {default_branch}\n"
            f"  Return code: {load_dc.returncode}\n"
            f"  stdout: {load_dc.stdout}\n"
            f"  stderr: {load_dc.stderr}"
        )

    @pytest.mark.order(11)
    @pytest.mark.dependency(name="verify_dc_created", depends=["load_dc_design"])
    @pytest.mark.asyncio
    async def test_04_verify_dc_created(
        self,
        async_client_main: InfrahubClient,
        default_branch: str,
        workflow_state: dict[str, Any],
    ) -> None:
        """Verify that DC1 topology object was created."""
        logging.info("Starting test: test_04_verify_dc_created")

        client = async_client_main
        client.default_branch = default_branch

        # Wait a moment for data to be fully committed
        await asyncio.sleep(DATA_PROPAGATION_DELAY)

        # Query for DC1
        dc = await client.get(
            kind=TopologyDataCenter,
            name__value="DC1",
            populate_store=True,
            raise_when_missing=False,
        )

        assert dc, (
            f"DC1 topology not found.\n"
            f"  Branch: {default_branch}\n"
            f"  Expected: TopologyDataCenter with name 'DC1'\n"
            f"  The DC1.yml may have failed to load in test_03."
        )
        assert dc.name.value == "DC1", f"Unexpected topology name.\n  Expected: DC1\n  Got: {dc.name.value}"

        workflow_state["dc_id"] = dc.id
        logging.info("DC1 topology verified: %s (ID: %s)", dc.name.value, dc.id)
