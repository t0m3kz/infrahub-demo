"""Give a Proposed Change its change request once its artifacts are computed (open_change_request).

The target is the PC's artifact validator (group artifact_validators); it starts when the validator joins the group
(created), not when it completes: a group trigger cannot race the group add, as a node rule on the completed state did.
The run waits for its own validator to complete; a PC has one per artifact definition, and only the last to complete acts. It creates CHG-<pc> on the PC's source branch, which starts add_change_request through the
change_requests group (add_change_request then writes the PC's "Change request" thread); or it re-runs
add_change_request on the existing one.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from generators.base import BaseGenerator
from utils.change_rules import CHANGE_REQUEST_KIND
from utils.data_cleaning import clean_data
from utils.limits import ARTIFACT_VALIDATOR_WAIT

VALIDATOR_KIND = "CoreArtifactValidator"
COMPLETED_STATE = "completed"
IN_FLIGHT_STATES = frozenset({"queued", "in_progress"})
OPEN_STATE = "open"
VALIDATOR_QUERY = Path(__file__).resolve().parents[2] / "queries" / "change" / "open_change_request.gql"
CHANGE_REQUEST_GROUP = "change_requests"
DERIVE_GENERATOR = "add_change_request"
_RUN_GENERATOR = """
mutation RunChangeRequestGenerator($id: String!, $nodes: [String!]) {
  CoreGeneratorDefinitionRun(data: { id: $id, nodes: $nodes }, wait_until_completion: false) { ok }
}
"""


def change_request_name(pc_name: str) -> str:
    """The change request of a proposed change is named after it."""
    return f"CHG-{pc_name}"


def artifact_validators_pending(pc: dict[str, Any]) -> int:
    """How many of the PC's artifact validators have not completed yet."""
    return sum(
        1
        for validator in pc.get("validations") or []
        if validator.get("typename") == VALIDATOR_KIND and validator.get("state") != COMPLETED_STATE
    )


class OpenChangeRequestGenerator(BaseGenerator):
    """open_change_request: create or re-derive the change request of a completed artifact validator's PC."""

    async def generate(self, data: dict[str, Any]) -> None:
        validators = clean_data(data).get(VALIDATOR_KIND) or []
        if not validators:
            self.fail(f"No {VALIDATOR_KIND} data in GraphQL response")
        validator = validators[0]
        # The run's own writes are untracked (another branch, or a comment): the target keeps its group non-empty
        self.client.group_context.related_node_ids.append(validator["id"])
        if (validator := await self._until_completed(validator)) is None:
            return
        pc = validator.get("proposed_change") or {}
        if pc.get("state") != OPEN_STATE or not pc.get("source_branch"):
            self.logger.info(f"Validator {validator['id']}: PC {pc.get('state')}, no source branch: nothing to do")
            return
        if pending := artifact_validators_pending(pc):
            self.logger.info(f"PC {pc['name']}: {pending} artifact validator(s) still running, the last one derives")
            return
        branch, name = pc["source_branch"], change_request_name(pc["name"])
        existing = await self.client.filters(kind=CHANGE_REQUEST_KIND, name__value=name, branch=branch)
        if existing:
            await self._rederive(existing[0].id, branch)
            return
        await self._create(name, pc["id"], branch)
        self.logger.info(f"{name}: created on {branch}")

    async def _until_completed(self, validator: dict[str, Any]) -> dict[str, Any] | None:
        """The validator once it is completed; None (logged) when it ends otherwise, vanishes or never completes."""
        waited = 0
        while True:
            state = validator.get("state")
            if state == COMPLETED_STATE:
                return validator
            if state not in IN_FLIGHT_STATES or (validator.get("proposed_change") or {}).get("state") != OPEN_STATE:
                self.logger.info(f"Validator {validator['id']}: {state}, PC closed or not running: nothing to do")
                return None
            if waited >= ARTIFACT_VALIDATOR_WAIT.timeout:
                self.logger.warning(f"Validator {validator['id']}: still {state} after {waited}s, no change request")
                return None
            await asyncio.sleep(ARTIFACT_VALIDATOR_WAIT.interval)
            waited += ARTIFACT_VALIDATOR_WAIT.interval
            result = await self.client.execute_graphql(
                query=VALIDATOR_QUERY.read_text(),
                variables={"validator_id": validator["id"]},
                branch_name=self.branch_name,
            )
            found = clean_data(result).get(VALIDATOR_KIND) or []
            if not found:
                self.logger.info(f"Validator {validator['id']}: gone, nothing to do")
                return None
            validator = found[0]

    async def _create(self, name: str, pc_id: str, branch: str) -> Any:
        """CHG-<pc> on the source branch, in change_requests: joining the group starts add_change_request."""
        group = await self.client.get(kind="CoreStandardGroup", name__value=CHANGE_REQUEST_GROUP, branch=branch)
        request = await self.client.create(
            kind=CHANGE_REQUEST_KIND, branch=branch, name=name, proposed_change=pc_id, member_of_groups=[group.id]
        )
        await request.save(allow_upsert=True, update_group_context=False)
        return request

    async def _rederive(self, request_id: str, branch: str) -> None:
        """Run add_change_request on the existing request, now that the artifact diff is complete."""
        definition = await self.client.get(kind="CoreGeneratorDefinition", name__value=DERIVE_GENERATOR)
        await self.client.execute_graphql(
            query=_RUN_GENERATOR, variables={"id": definition.id, "nodes": [request_id]}, branch_name=branch
        )
        self.logger.info(f"{request_id}: {DERIVE_GENERATOR} re-run on {branch}")
