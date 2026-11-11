"""Integration test: the full-topology sample loads on its own branch.

Loads ``data/demos/00_full_topology`` with the ordinary sorted SDK loader the sample README documents
(``tests/traversal/load_topology.py``) onto the branch ``full-topology``, never onto main: the other scenarios deploy
and merge into main (the DC deployments, the 30_all demo builds its branch from main), and the sample is authored data
that no generator completes, so its objects would meet their checks and counts. The branch is not merged. Part of
``invoke test-integration-fast``; it can share a session with the other scenarios.
"""

import logging

import pytest
from infrahub_sdk import InfrahubClient, InfrahubClientSync

from tests.integration.workflow_helpers import create_and_validate_proposed_change
from tests.traversal import load_topology

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

BRANCH = "full-topology"


class TestFullTopology:
    """Load the full topology on its own branch."""

    @pytest.mark.order(10)
    @pytest.mark.dependency(scope="session", name="full_topology_load", depends=["triggers_active"])
    @pytest.mark.asyncio
    async def test_01_load_full_topology(self, async_client_main: InfrahubClient) -> None:
        """Create the branch ``full-topology`` and load data/demos/00_full_topology onto it."""
        await async_client_main.branch.create(branch_name=BRANCH, sync_with_git=False, wait_until_completion=True)
        done = await load_topology.apply(async_client_main, BRANCH)
        logging.info("full topology: %s", done)

    @pytest.mark.order(11)
    @pytest.mark.dependency(scope="session", name="full_topology_checks", depends=["full_topology_load"])
    def test_02_the_sample_passes_its_checks(self, client_main: InfrahubClientSync) -> None:
        """Open a proposed change for the branch and require every check to pass.

        The sample is authored data no generator completes, so the checks (circuit placement, quota, patch panel
        pairing, routing tags, interface parents...) judge it as written. A failing check raises with its messages.
        The proposed change is not merged.
        """
        result = create_and_validate_proposed_change(
            client=client_main, name="Full topology sample", source_branch=BRANCH
        )
        logging.info("full topology: %d validations, none failing", len(result["validations"]))
