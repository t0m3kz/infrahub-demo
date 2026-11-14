"""Integration test: every suite kick of tests/integration/kicks/, its stages' `expect` asserted per phase."""

import logging

import pytest
from infrahub_sdk import InfrahubClientSync

from tasks import run_kick
from tests.integration.kicks_loader import Kick, load_suite_kicks

from .conftest import TestInfrahubDockerWithClient
from .kick_expect import StageChecker, parse_expectations
from .workflow_helpers import object_loader

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

KICKS, RAW_EXPECT = load_suite_kicks()
EXPECT = parse_expectations(RAW_EXPECT)

OPEN_PROPOSED_CHANGES = """
query ($branches: [String]) {
  CoreProposedChange(source_branch__values: $branches, state__value: "open") { edges { node { id } } }
}
"""
CLOSE_MUTATION = (
    'mutation ($id: String!) { CoreProposedChangeUpdate(data: {id: $id, state: {value: "closed"}}) { ok } }'
)


def _close_open_changes(client: InfrahubClientSync, kick: Kick) -> None:
    """Close the open proposed changes of the kick's unmerged stages: each re-validates whenever main changes."""
    branches = [kick.stage_branch(stage) for stage in kick.stages if not stage.merge]
    if not branches or kick.staged:
        return
    result = client.execute_graphql(query=OPEN_PROPOSED_CHANGES, variables={"branches": branches})
    for edge in result["CoreProposedChange"]["edges"]:
        client.execute_graphql(query=CLOSE_MUTATION, variables={"id": edge["node"]["id"]})


class TestSuiteChains(TestInfrahubDockerWithClient):
    """Run each suite kick against the test stack, one proposed change per stage."""

    @pytest.mark.order(10)
    @pytest.mark.dependency(scope="session", depends=["triggers_active"])
    @pytest.mark.parametrize("name", list(KICKS), ids=list(KICKS))
    def test_kick(self, client_main: InfrahubClientSync, name: str) -> None:
        """The kick deploys stage by stage and every stage's assertions hold on its branch and on main."""
        kick = KICKS[name]
        checker = StageChecker(
            client_main,
            name,
            {stage: expectation for (owner, stage), expectation in EXPECT.items() if owner == name},
        )
        loader = object_loader(self.execute_command, client_main.config.address)
        try:
            timings = run_kick(client_main, loader, kick, after_stage=checker)
        finally:
            _close_open_changes(client_main, kick)
        logging.info("%s: %s", name, timings)
