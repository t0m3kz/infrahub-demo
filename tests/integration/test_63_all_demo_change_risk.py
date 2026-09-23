"""Integration test — run the change-risk check over the whole 30_all branch.

``CheckChangeRisk`` (checks/change_risk.py, documented in docs/change_risk.md)
reads a branch diff and walks outward from each changed object to work out what
a change would take down. 30_all is the only data set in this repo rich enough
to exercise it end to end: DC fabrics, cage switches, circuits into two clouds,
and applications whose instances sit on real hosts.

This module runs the check in-process against the branch test_59 built. It does
*not* create a Proposed Change and it does not merge: `.infrahub.yml` declares
artifact definitions for every device, so a PC over three full fabrics would
fan out artifact generation across a hundred-odd devices to prove something this
test can prove directly.

What is asserted is what would break silently:

  * The traversal resolved every relationship it needs against the schema. An
    ``unresolved_edge`` finding means a schema rename left a hole in the walk,
    and the check cannot report what it never walked — while still, misleadingly,
    producing a verdict.
  * The check reached a verdict at all, rather than bailing out early on a
    missing diff or an Infrahub without graph traversal.

The verdict *value* is deliberately not asserted. Diffing an entire demo load
against main is a bulk import, not a change, so the score and confidence it
produces are a property of the data volume rather than of the check.
"""

import logging
import re

import pytest
from infrahub_sdk import InfrahubClient

from checks.change_risk import CheckChangeRisk

from .conftest import TestInfrahubDockerWithClient
from .test_constants import ALL_DEMO_BRANCH
from .workflow_helpers import materialize_branch_diff

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

SCENARIO_NAME = "Scenario 30_all: Change Risk"

VERDICT_PATTERN = re.compile(
    r"Change risk (?P<verdict>PASS|REVIEW|BLOCK): score (?P<score>[\d.]+), "
    r"confidence (?P<confidence>\d+)%, (?P<impacted>\d+) impacted object\(s\) "
    r"\((?P<kinds>.*?)\) from (?P<changed>\d+) changed object\(s\)\."
)

#: The two early returns that end validate() before any traversal happens. They
#: are logged as errors, so they would otherwise be indistinguishable from a
#: real high-risk verdict.
BAILOUT_MARKERS = (
    "no traversal edge resolved",
    "does not provide graph traversal",
)


class TestAllDemoChangeRisk(TestInfrahubDockerWithClient):
    """Run the change-risk check over the 30_all branch and read its verdict."""

    @pytest.fixture(scope="class")
    def scenario_branch(self) -> str:
        return ALL_DEMO_BRANCH

    @pytest.mark.order(398)
    @pytest.mark.dependency(scope="session", name="all_demo_change_risk", depends=["all_demo_inventory"])
    @pytest.mark.asyncio
    async def test_01_change_risk_reaches_a_verdict(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Materialize the branch diff, run the check, and read its logs."""
        logging.info("=== %s - Step 1: Materialize Diff ===", SCENARIO_NAME)

        await materialize_branch_diff(client=async_client_main, branch=scenario_branch)

        logging.info("=== %s - Step 2: Run CheckChangeRisk ===", SCENARIO_NAME)

        # The check reads its base branch from the client, and the branch under
        # assessment from branch_name — main and the 30_all branch respectively.
        async_client_main.default_branch = "main"
        check = CheckChangeRisk(branch=scenario_branch, client=async_client_main)

        # validate() ignores the anchor query's output, but run() would go and
        # collect it for a falsy payload — and the declared query is a
        # placeholder that does not exist on the server.
        await check.run(data={"data": {}})

        messages = [str(entry.get("message") or "") for entry in check.logs]
        for message in messages:
            logging.info("check: %s", message)

        errors: list[str] = []

        bailouts = [m for m in messages if any(marker in m for marker in BAILOUT_MARKERS)]
        if bailouts:
            errors.append("the check bailed out before traversing anything: " + "; ".join(bailouts))

        unresolved = [m for m in messages if "[unresolved_edge]" in m]
        if unresolved:
            errors.append(
                "the traversal could not resolve a relationship against the schema, so part of the "
                "blast radius was never walked: " + "; ".join(unresolved)
            )

        verdicts = [VERDICT_PATTERN.search(m) for m in messages]
        verdict_match = next((m for m in verdicts if m), None)
        if verdict_match is None:
            errors.append(
                "no verdict line was logged — expected one 'Change risk <PASS|REVIEW|BLOCK>: score …' "
                f"entry among {len(messages)} log line(s)"
            )
        elif int(verdict_match.group("changed")) == 0:
            errors.append("the check saw 0 changed objects on a branch holding the entire 30_all demo")

        assert not errors, f"Change risk check failed on branch '{scenario_branch}':\n" + "\n".join(
            f"  - {e}" for e in errors
        )

        assert verdict_match is not None  # for the type checker; asserted above
        logging.info(
            "Change risk verdict '%s': score %s, confidence %s%%, %s impacted object(s) from %s changed",
            verdict_match.group("verdict"),
            verdict_match.group("score"),
            verdict_match.group("confidence"),
            verdict_match.group("impacted"),
            verdict_match.group("changed"),
        )
        logging.info("=== %s - COMPLETED ===", SCENARIO_NAME)
