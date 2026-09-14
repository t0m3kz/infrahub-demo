from __future__ import annotations

from typing import Any

from infrahub_sdk.exceptions import NodeNotFoundError

from utils.data_cleaning import clean_data

from ..common import CommonGenerator


class BaseOrchestratorRoutingGenerator(CommonGenerator):
    """Synchronize routing groups from an orchestrator dropdown field."""

    query_roots: tuple[str, ...] = ()
    orchestrator_field = ""
    routing_groups: dict[str, str] = {}

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
            await group.save(allow_upsert=True)
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
            self.logger.error(
                f"{node_kind} '{node_name}' uses unsupported orchestrator '{orchestrator}' for field "
                f"'{self.orchestrator_field}'"
            )
            return

        current_groups = node.get("member_of_groups") or []
        routing_group_names = set(self.routing_groups.values())
        existing_group_ids = [group["id"] for group in current_groups if group.get("id")]
        retained_group_ids = [
            group["id"] for group in current_groups if group.get("id") and group.get("name") not in routing_group_names
        ]

        final_group_ids = list(retained_group_ids)
        if routing_group_name:
            routing_group = await self._ensure_group(routing_group_name)
            if routing_group.id not in final_group_ids:
                final_group_ids.append(routing_group.id)

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
        await obj.save(allow_upsert=True)
        self.logger.info(
            f"Updated routing groups for {node_kind} '{node_name}' -> {routing_group_name or 'manual/no-routing-group'}"
        )


class ApplicationOrchestratorRoutingGenerator(BaseOrchestratorRoutingGenerator):
    """Route applications into delivery-specific execution groups."""

    query_roots = ("AppApplication",)
    orchestrator_field = "application_orchestrator"
    routing_groups = {
        "ansible_automation_platform": "application_ansible",
        "github_actions": "application_github_actions",
        "argocd": "application_argocd",
    }


class TopologyOrchestratorRoutingGenerator(BaseOrchestratorRoutingGenerator):
    """Route customer deployments into infrastructure execution groups."""

    query_roots = (
        "TopologyCustomerDC",
        "TopologyCustomerColocation",
        "TopologyCustomerOffice",
        "TopologyCustomerCloud",
    )
    orchestrator_field = "infrastructure_orchestrator"
    routing_groups = {
        "ansible_automation_platform": "topology_ansible",
        "github_actions": "topology_github_actions",
        "terraform": "topology_terraform",
        "argocd": "topology_argocd",
    }
