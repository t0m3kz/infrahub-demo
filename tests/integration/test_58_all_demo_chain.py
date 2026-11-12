"""Integration test: 30_all as a chain of reviewed changes, each step merged before the next (own stack)."""

import logging
import time
from typing import Any

import pytest
from infrahub_sdk import InfrahubClient, InfrahubClientSync

from tasks import ALL_DEMO_CHAIN, ALL_DEMO_CHAIN_BRANCH_PREFIX, ALL_DEMO_DATA, ChainStep

from .conftest import TestInfrahubDockerWithClient
from .workflow_helpers import deploy_change, merge_proposed_change, verify_no_failed_tasks

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# Polls (5 s apart) a step's generators may take to settle: a fabric's cascade is the longest.
CHAIN_MAX_POLLS = 240

# Seconds per phase of every step that ran, in chain order; read by the report.
_TIMINGS: dict[str, dict[str, float]] = {}


def _step_params() -> list[Any]:
    """One test per step, in chain order; each depends on the merge of the step before it."""
    params = []
    previous: str | None = None
    for index, step in enumerate(ALL_DEMO_CHAIN):
        merged = f"chain_{step.name}_merged"
        params.append(
            pytest.param(
                step,
                marks=[
                    pytest.mark.order(360 + index),
                    pytest.mark.dependency(
                        scope="session",
                        name=merged,
                        depends=["triggers_active"] + ([previous] if previous else []),
                    ),
                ],
                id=step.name,
            )
        )
        previous = merged
    return params


class TestAllDemoChain(TestInfrahubDockerWithClient):
    """Deploy and merge every 30_all chain step in turn."""

    @pytest.mark.parametrize("step", _step_params())
    @pytest.mark.asyncio
    async def test_01_deploy_and_merge(
        self,
        step: ChainStep,
        client_main: InfrahubClientSync,
        async_client_main: InfrahubClient,
    ) -> None:
        """Deploy the step as a proposed change, merge it, and check that no post-merge task failed on main."""
        deployed = await deploy_change(
            config=client_main.config,
            execute_command=self.execute_command,
            name=f"chain-{step.name}",
            branch=f"{ALL_DEMO_CHAIN_BRANCH_PREFIX}{step.name}",
            paths=[f"{ALL_DEMO_DATA}/{path}" for path in step.paths],
            dispatched_generator=step.generator,
            min_artifacts=step.min_artifacts,
            max_polls=CHAIN_MAX_POLLS,
        )
        timings = dict(deployed.timings)
        _TIMINGS[step.name] = timings

        started = time.monotonic()
        result = merge_proposed_change(client=client_main, pc_id=deployed.pc_id)
        timings["merge"] = round(time.monotonic() - started, 1)
        failed_checks = result.get("failed_checks") or []
        assert result["success"], (
            f"Merge of chain step '{step.name}' failed: PC {result['pc_state_before']} -> {result['pc_state_after']}, "
            f"task {result['task_state']}"
            + ("\n  Failing checks:\n    - " + "\n    - ".join(failed_checks) if failed_checks else "")
        )
        # merge_proposed_change waited for the post-merge tasks on main; none of them may have failed.
        await verify_no_failed_tasks(client=async_client_main, branch="main")
        logging.info("Chain step '%s' merged: %s", step.name, timings)

    @pytest.mark.order(399)
    def test_02_report_timings(self) -> None:
        """Log the seconds per phase of every step, and fail unless every step of the chain ran."""
        phases = ["load", "generators", "proposed_change", "artifacts", "merge"]
        lines = [f"{'step':<14}" + "".join(f"{phase:>17}" for phase in phases) + f"{'total':>10}"]
        for name, timings in _TIMINGS.items():
            row = "".join(f"{timings.get(phase, 0.0):>17.1f}" for phase in phases)
            lines.append(f"{name:<14}{row}{sum(timings.values()):>10.1f}")
        logging.info("30_all chain timings (seconds):\n%s", "\n".join(lines))
        missing = [step.name for step in ALL_DEMO_CHAIN if step.name not in _TIMINGS]
        assert not missing, f"Chain steps that did not run: {missing}"
