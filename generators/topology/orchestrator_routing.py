from __future__ import annotations

from typing import Any

from infrahub_sdk.exceptions import NodeNotFoundError

from utils.data_cleaning import clean_data
from utils.limits import (
    APPLICATION_ORCHESTRATOR_GROUPS,
    TOPOLOGY_ORCHESTRATOR_GROUPS,
    VIRTUAL_COLOCATION_ORCHESTRATOR_GROUPS,
)

from ..common import CommonGenerator


class BaseOrchestratorRoutingGenerator(CommonGenerator):
    """Synchronize routing groups from an orchestrator dropdown field.

    Owns nothing, so every write is untracked: the routing group is shared by
    every node routed into it, and the node is this run's target. Tracked,
    either would be deleted by the first run that does not save it again
    (an unchanged node, or an orchestrator switched to another group).
    """

    query_roots: tuple[str, ...] = ()
    orchestrator_field = ""
    routing_groups: dict[str, str] = {}
    # An orchestrator value with no routing group: an error that leaves the node untouched
    # (the default), or a warning for a kind where only some values have an artifact, after
    # which the node is moved out of every routing group like `manual`.
    unrouted_is_error = True

    def _extract_node(self, cleaned: dict[str, Any]) -> dict[str, Any] | None:
        for root in self.query_roots:
            nodes = cleaned.get(root) or []
            if nodes:
                return nodes[0]
        return None

    async def _ensure_group(self, group_name: str) -> Any:
        try:
            return await self.client.get(kind="CoreStandardGroup", name__value=group_name)
        except NodeNotFoundError:
            group = await self.client.create(
                kind="CoreStandardGroup",
                data={
                    "name": group_name,
                    "description": f"Auto-created orchestrator routing group {group_name}",
                },
            )
            await group.save(allow_upsert=True, update_group_context=False)
            self.logger.info(f"Created missing routing group '{group_name}'")
            return group

    async def generate(self, data: dict[str, Any]) -> None:
        cleaned = clean_data(data)
        node = self._extract_node(cleaned)
        if not node:
            self.logger.error(f"No supported node found in query roots {self.query_roots}")
            return

        node_id = node.get("id")
        node_name = node.get("name", "<unnamed>")
        node_kind = node.get("typename") or self.query_roots[0]
        orchestrator = node.get(self.orchestrator_field) or "manual"
        if not node_id:
            self.logger.error(f"{node_kind} '{node_name}' is missing id")
            return

        routing_group_name = self.routing_groups.get(orchestrator)
        if orchestrator != "manual" and routing_group_name is None:
            message = (
                f"{node_kind} '{node_name}' uses unsupported orchestrator '{orchestrator}' for field "
                f"'{self.orchestrator_field}'"
            )
            if self.unrouted_is_error:
                self.logger.error(message)
                return
            # FailOnErrorLogger turns error() into a task failure: a value without an artifact is not one.
            self.logger.warning(f"{message}: no routing group, no artifact")

        current_groups = node.get("member_of_groups") or []
        routing_group_names = set(self.routing_groups.values())
        existing_group_ids = [group["id"] for group in current_groups if group.get("id")]
        retained_group_ids = [
            group["id"] for group in current_groups if group.get("id") and group.get("name") not in routing_group_names
        ]

        final_group_ids = list(retained_group_ids)
        if routing_group_name:
            # The node's groups in the query payload already name the routing group it is in: no lookup.
            routing_group_id = next(
                (
                    group["id"]
                    for group in current_groups
                    if group.get("id") and group.get("name") == routing_group_name
                ),
                None,
            )
            if routing_group_id is None:
                routing_group_id = (await self._ensure_group(routing_group_name)).id
            if routing_group_id not in final_group_ids:
                final_group_ids.append(routing_group_id)

        if existing_group_ids == final_group_ids:
            self.logger.info(f"{node_kind} '{node_name}' already has correct routing groups")
            return

        obj = await self.client.create(
            kind=node_kind,
            data={
                "id": node_id,
                "member_of_groups": [{"id": group_id} for group_id in final_group_ids],
            },
        )
        await obj.save(allow_upsert=True, update_group_context=False)
        self.logger.info(
            f"Updated routing groups for {node_kind} '{node_name}' -> {routing_group_name or 'manual/no-routing-group'}"
        )


class ApplicationOrchestratorRoutingGenerator(BaseOrchestratorRoutingGenerator):
    """Route applications into delivery-specific execution groups."""

    query_roots = ("AppApplication",)
    orchestrator_field = "application_orchestrator"
    routing_groups = APPLICATION_ORCHESTRATOR_GROUPS


class TopologyOrchestratorRoutingGenerator(BaseOrchestratorRoutingGenerator):
    """Route customer deployments into infrastructure execution groups."""

    query_roots = (
        "TopologyCustomerDC",
        "TopologyCustomerColocation",
        "TopologyCustomerOffice",
        "TopologyCustomerCloud",
    )
    orchestrator_field = "infrastructure_orchestrator"
    routing_groups = TOPOLOGY_ORCHESTRATOR_GROUPS


class VirtualColocationOrchestratorRoutingGenerator(BaseOrchestratorRoutingGenerator):
    """Route a provider-hosted colocation presence into its artifact group.

    Only Terraform has an artifact for a virtual colocation (Equinix Network Edge
    devices); any other value leaves it in no routing group, with a warning.
    """

    query_roots = ("TopologyVirtualColocation",)
    orchestrator_field = "infrastructure_orchestrator"
    routing_groups = VIRTUAL_COLOCATION_ORCHESTRATOR_GROUPS
    unrouted_is_error = False
