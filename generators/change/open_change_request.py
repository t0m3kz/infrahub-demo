"""Give a Proposed Change its change request once its artifacts are computed (open_change_request).

The target is the PC's artifact validator (group artifact_validators); a PC has one per artifact definition, and only
the last to complete acts. It creates CHG-<pc> on the PC's source branch, which starts add_change_request through the
change_requests group (add_change_request then writes the PC's "Change request" thread); or it re-runs
add_change_request on the existing one.
"""

from __future__ import annotations

from typing import Any

from generators.mixins.common import CommonGenerator
from utils.change_rules import CHANGE_REQUEST_KIND
from utils.data_cleaning import clean_data

VALIDATOR_KIND = "CoreArtifactValidator"
COMPLETED_STATE = "completed"
OPEN_STATE = "open"
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


class OpenChangeRequestGenerator(CommonGenerator):
    """open_change_request: create or re-derive the change request of a completed artifact validator's PC."""

    async def generate(self, data: dict[str, Any]) -> None:
        validators = clean_data(data).get(VALIDATOR_KIND) or []
        if not validators:
            self.fail(f"No {VALIDATOR_KIND} data in GraphQL response")
        validator = validators[0]
        # The run's own writes are untracked (another branch, or a comment): the target keeps its group non-empty
        self.client.group_context.related_node_ids.append(validator["id"])
        pc = validator.get("proposed_change") or {}
        if validator.get("state") != COMPLETED_STATE or pc.get("state") != OPEN_STATE or not pc.get("source_branch"):
            self.logger.info(
                f"Validator {validator['id']}: {validator.get('state')}, PC {pc.get('state')}: nothing to do"
            )
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
