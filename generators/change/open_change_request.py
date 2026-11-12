"""Give a Proposed Change its change request once its artifacts are computed (open_change_request).

The target is the PC's artifact validator (group artifact_validators). A completed validator of an open PC either
creates CHG-<pc> on the PC's source branch, which starts add_change_request through the change_requests group, or
re-runs add_change_request on the existing one. The first creation also posts a comment on the PC with the link.
"""

from __future__ import annotations

from typing import Any

from utils.change_rules import CHANGE_REQUEST_KIND
from utils.data_cleaning import clean_data

from ..common import CommonGenerator

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


def change_request_comment(name: str, request_id: str, branch: str) -> str:
    """The PC comment that links its change request."""
    return f"Change request [{name}](/objects/{CHANGE_REQUEST_KIND}/{request_id}?branch={branch}) is derived from this change."


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
        branch, name = pc["source_branch"], change_request_name(pc["name"])
        existing = await self.client.filters(kind=CHANGE_REQUEST_KIND, name__value=name, branch=branch)
        if existing:
            await self._rederive(existing[0].id, branch)
            return
        request = await self._create(name, pc["id"], branch)
        comment = await self.client.create(
            kind="CoreChangeComment", text=change_request_comment(name, request.id, branch), change=pc["id"]
        )
        await comment.save(update_group_context=False)
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
