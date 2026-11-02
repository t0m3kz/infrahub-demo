"""Integration test: the full-topology sample on the stack's main, then the traversal checks over it.

Loads ``data/demos/00_full_topology`` with the loader the sample README documents (``tests/traversal/load_topology.py``: one ``infrahubctl object load``
of the directory, then the VLAN-domain row) and runs the cases of ``tests/traversal/cases.yml`` through the functions of
``tests/traversal/check_model.py`` (the same assertions as ``uv run invoke check-model``, no second copy of the logic).
Part of ``invoke test-integration-fast``; NOT part of the 30_all subset: the sample lands on main, and 30_all builds
its branch from main.
"""

import logging

import pytest
from infrahub_sdk import Config, InfrahubClient

from tests.traversal import load_topology
from tests.traversal.check_model import Report, assert_case, load_cases
from utils import path_trace

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

BRANCH = "main"


class TestFullTopology:
    """Load the full topology on main, then walk it."""

    @pytest.mark.order(10)
    @pytest.mark.dependency(scope="session", name="full_topology_load", depends=["triggers_active"])
    @pytest.mark.asyncio
    async def test_01_load_full_topology(self, async_client_main: InfrahubClient) -> None:
        """Load data/demos/00_full_topology onto main (skipped objects: the sentinel is already there)."""
        done = await load_topology.apply(async_client_main, BRANCH)
        logging.info("full topology: %s", done)

    @pytest.mark.order(11)
    @pytest.mark.dependency(scope="session", name="full_topology_traversal", depends=["full_topology_load"])
    @pytest.mark.asyncio
    async def test_02_traversal_checks(self, async_client_main: InfrahubClient, capsys: pytest.CaptureFixture) -> None:
        """Every traversal assertion of the case passes on the loaded graph."""
        case = load_cases("full_topology")[0]
        report = Report()
        # a dense L3 search takes the server over a minute: the tracer's own timeout, not the fixture's 60 s
        config = async_client_main.config
        client = InfrahubClient(
            config=Config(address=config.address, api_token=config.api_token, timeout=path_trace.REQUEST_TIMEOUT)
        )
        await assert_case(case, client, BRANCH, report)
        with capsys.disabled():
            report.print()
        failed = [f"{name}: {detail}" for name, ok, detail in report.rows if not ok]
        assert report.rows, "the traversal case asserted nothing"
        assert not failed, "traversal checks failed:\n" + "\n".join(failed)
