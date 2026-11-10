"""Integration test: the full-topology sample loads on its own branch.

Loads ``data/demos/00_full_topology`` with the ordinary sorted SDK loader the sample README documents
(``tests/traversal/load_topology.py``) onto the branch ``full-topology``, never onto main: the other scenarios deploy
and merge into main (the DC deployments, the 30_all demo builds its branch from main), and the sample is authored data
that no generator completes, so its objects would meet their checks and counts. The branch is not merged. Part of
``invoke test-integration-fast``; it can share a session with the other scenarios.
"""

import logging

import pytest
from infrahub_sdk import InfrahubClient

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
