"""Integration test: the full-topology sample loads on the stack's main.

Loads ``data/demos/00_full_topology`` with the ordinary sorted SDK loader the sample README documents
(``tests/traversal/load_topology.py``). Part of ``invoke test-integration-fast``; NOT part of the 30_all subset: the
sample lands on main, and 30_all builds its branch from main. With ``INFRAHUB_TESTING_KEEP_STACK=1`` the stack stays up
after the run with the sample on main, for a review.
"""

import logging

import pytest
from infrahub_sdk import InfrahubClient

from tests.traversal import load_topology

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

BRANCH = "main"


class TestFullTopology:
    """Load the full topology on main."""

    @pytest.mark.order(10)
    @pytest.mark.dependency(scope="session", name="full_topology_load", depends=["triggers_active"])
    @pytest.mark.asyncio
    async def test_01_load_full_topology(self, async_client_main: InfrahubClient) -> None:
        """Load data/demos/00_full_topology onto main (skipped objects: the sentinel is already there)."""
        done = await load_topology.apply(async_client_main, BRANCH)
        logging.info("full topology: %s", done)
