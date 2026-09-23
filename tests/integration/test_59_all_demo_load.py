"""Integration test — load the 30_all demo and verify what it dispatches.

30_all is the everything demo: three DC fabrics, six colocation operators'
cages, three clouds, a SaaS edge, four offices, sixteen customers' footprints
across all four deployment kinds, seven applications with their instances, and
the interconnect layer tying them together. Every later test_6x module asserts
against the branch this one builds, so this module owns:

1. The staged load itself (see ALL_DEMO_LOAD_STAGES for why it is staged).
2. That no task failed on any stage.
3. That Infrahub dispatched every generator the data is supposed to trigger.
   This is the assertion with teeth: the trigger rules in data/events are the
   only thing standing between "the objects got generated" and "the objects
   silently did not". A regressed rule fails nothing — it just stops firing.
4. That every declared object actually landed.
"""

import logging
from typing import Any

import pytest
from infrahub_sdk import InfrahubClient, InfrahubClientSync

from .conftest import TestInfrahubDockerWithClient
from .test_constants import (
    ALL_DEMO_BRANCH,
    ALL_DEMO_DATA,
    ALL_DEMO_EXPECTED_GENERATORS,
    ALL_DEMO_EXPECTED_OBJECTS,
    ALL_DEMO_LOAD_STAGES,
    ALL_DEMO_STAGE_MAX_ATTEMPTS,
    ALL_DEMO_STAGE_POLL_INTERVAL,
    ALL_DEMO_STAGE_STABLE_ZERO,
)
from .test_helpers import fetch_generator_runs, fetch_object_counts
from .workflow_helpers import verify_no_failed_tasks, wait_for_tasks_completion

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

SCENARIO_NAME = "Scenario 30_all: Full Demo Load"


class TestAllDemoLoad(TestInfrahubDockerWithClient):
    """Build the shared 30_all branch and verify the load converged."""

    @pytest.fixture(scope="class")
    def scenario_branch(self) -> str:
        return ALL_DEMO_BRANCH

    # ------------------------------------------------------------------
    # Step 1: staged load
    # ------------------------------------------------------------------

    @pytest.mark.order(380)
    @pytest.mark.dependency(scope="session", name="all_demo_branch", depends=["triggers_active"])
    def test_01_create_branch(
        self,
        client_main: InfrahubClientSync,
        scenario_branch: str,
    ) -> None:
        """Create the branch every 30_all test shares.

        Demo data is never loaded into main: the whole point of the exercise is
        that it arrives as a reviewable branch.
        """
        logging.info("=== %s - Step 1: Create Branch ===", SCENARIO_NAME)

        existing_branches = client_main.branch.all()
        if scenario_branch not in existing_branches:
            client_main.branch.create(
                branch_name=scenario_branch,
                sync_with_git=False,
                wait_until_completion=True,
            )
            logging.info("Created branch: %s", scenario_branch)
        else:
            logging.info("Branch already exists: %s", scenario_branch)

    @pytest.mark.order(381)
    @pytest.mark.dependency(scope="session", name="all_demo_load", depends=["all_demo_branch"])
    @pytest.mark.asyncio
    async def test_02_load_data_in_stages(
        self,
        async_client_main: InfrahubClient,
        client_main: InfrahubClientSync,
        scenario_branch: str,
        workflow_state: dict[str, Any],
    ) -> None:
        """Load 30_all stage by stage, letting each stage's generators settle.

        Each stage is checked for failed tasks before the next one starts. A
        failure in, say, the fabric stage would otherwise cascade into dozens
        of misleading reference errors in the stages that build on it.
        """
        logging.info("=== %s - Step 2: Staged Load ===", SCENARIO_NAME)

        for stage_name, paths in ALL_DEMO_LOAD_STAGES:
            full_paths = " ".join(f"{ALL_DEMO_DATA}/{path}" for path in paths)
            logging.info("--- stage '%s': loading %s ---", stage_name, full_paths)

            load_result = self.execute_command(
                f"infrahubctl object load {full_paths} --branch {scenario_branch}",
                address=client_main.config.address,
                concurrent_execution=10,
                pagination_size=200,
            )
            assert load_result.returncode == 0, (
                f"Stage '{stage_name}' of the 30_all load failed.\n"
                f"  Paths: {full_paths}\n"
                f"  Return code: {load_result.returncode}\n"
                f"  stdout: {load_result.stdout}\n"
                f"  stderr: {load_result.stderr}"
            )

            await wait_for_tasks_completion(
                async_client_main,
                scenario_branch,
                initial_delay=10,
                max_wait_attempts=ALL_DEMO_STAGE_MAX_ATTEMPTS,
                poll_interval=ALL_DEMO_STAGE_POLL_INTERVAL,
                stable_zero_count=ALL_DEMO_STAGE_STABLE_ZERO,
            )
            await verify_no_failed_tasks(client=async_client_main, branch=scenario_branch)
            logging.info("--- stage '%s': settled, no failed tasks ---", stage_name)

        workflow_state["all_demo_stages_loaded"] = [stage for stage, _ in ALL_DEMO_LOAD_STAGES]
        logging.info("All %d stages loaded", len(ALL_DEMO_LOAD_STAGES))

    # ------------------------------------------------------------------
    # Step 2: nothing failed
    # ------------------------------------------------------------------

    @pytest.mark.order(382)
    @pytest.mark.dependency(scope="session", name="all_demo_no_failures", depends=["all_demo_load"])
    @pytest.mark.asyncio
    async def test_03_verify_no_failed_tasks(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Final branch-wide failure sweep once everything has settled."""
        logging.info("=== %s - Step 3: Verify No Failed Tasks ===", SCENARIO_NAME)

        await verify_no_failed_tasks(client=async_client_main, branch=scenario_branch)

        logging.info("No failed tasks on branch '%s'", scenario_branch)

    # ------------------------------------------------------------------
    # Step 3: the generators Infrahub dispatched on its own
    # ------------------------------------------------------------------

    @pytest.mark.order(383)
    @pytest.mark.dependency(scope="session", name="all_demo_generators", depends=["all_demo_no_failures"])
    @pytest.mark.asyncio
    async def test_04_verify_generators_dispatched(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Verify every event-driven generator ran, at least once per target.

        Nothing in this suite invokes these: they are dispatched by the
        CoreNodeTriggerRules in data/events/99_actions.yml when the loader
        creates the matching node. Counting the dispatches is the only way to
        tell "the rule fired for all seven applications" from "the rule fired
        for one and the other six were never generated" — both look identical
        in a failed-task sweep, because neither produces a failed task.
        """
        logging.info("=== %s - Step 4: Verify Generators Dispatched ===", SCENARIO_NAME)

        runs = await fetch_generator_runs(client=async_client_main, branch=scenario_branch)

        missing: list[str] = []
        short: list[str] = []
        for generator, expected in sorted(ALL_DEMO_EXPECTED_GENERATORS.items()):
            actual = runs.get(generator, 0)
            if actual == 0:
                missing.append(f"{generator}: never dispatched (expected >= {expected})")
            elif actual < expected:
                short.append(f"{generator}: {actual} dispatch(es), expected >= {expected}")

        assert not missing, (
            "Generators that the 30_all load should have triggered never ran — "
            "check the CoreNodeTriggerRule/CoreGeneratorAction pair in data/events:\n"
            + "\n".join(f"  - {m}" for m in missing)
        )
        assert not short, "Generators ran for fewer targets than the data declares:\n" + "\n".join(
            f"  - {s}" for s in short
        )

        logging.info("All %d expected generators dispatched", len(ALL_DEMO_EXPECTED_GENERATORS))

    # ------------------------------------------------------------------
    # Step 4: declared objects landed
    # ------------------------------------------------------------------

    @pytest.mark.order(384)
    @pytest.mark.dependency(scope="session", name="all_demo_inventory", depends=["all_demo_no_failures"])
    @pytest.mark.asyncio
    async def test_05_verify_object_inventory(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Verify every declared 30_all object exists on the branch."""
        logging.info("=== %s - Step 5: Verify Object Inventory ===", SCENARIO_NAME)

        counts = await fetch_object_counts(
            client=async_client_main,
            branch=scenario_branch,
            kinds=sorted(ALL_DEMO_EXPECTED_OBJECTS),
        )

        errors = [
            f"{kind}: found {counts.get(kind, 0)}, expected >= {expected}"
            for kind, expected in sorted(ALL_DEMO_EXPECTED_OBJECTS.items())
            if counts.get(kind, 0) < expected
        ]
        assert not errors, f"30_all object inventory incomplete on branch '{scenario_branch}':\n" + "\n".join(
            f"  - {e}" for e in errors
        )

        logging.info("Object inventory verified across %d kind(s)", len(ALL_DEMO_EXPECTED_OBJECTS))
        logging.info("=== %s - COMPLETED ===", SCENARIO_NAME)
