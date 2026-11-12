"""Build the ports of a patch panel from its type and pair each front port with its rear port."""

from __future__ import annotations

from typing import Any

from infrahub_sdk.exceptions import GraphQLError

from utils.data_cleaning import clean_data

from ..common import CommonGenerator
from ..helpers.patch_panel import audit
from ..pools import PoolMixin

PANEL_KIND = "DcimPatchPanel"
REAR_PORT_KIND = "DcimRearPatchPanelPort"
FRONT_PORT_KIND = "DcimFrontPatchPanelPort"


class PatchPanelGenerator(PoolMixin, CommonGenerator):
    """Create the ports `panel_type` asks for and pair every front port with its rear port; never deletes or re-pairs.
    Untracked, one lock per panel, every port an upsert by [panel, name]; mismatches are warned about
    (checks/patch_panel_pairing.py).
    """

    graphql_root_key = PANEL_KIND

    async def generate(self, data: dict[str, Any]) -> None:
        panels = clean_data(data).get(self.graphql_root_key) or []
        if not panels:
            self.fail(f"No {self.graphql_root_key} data in GraphQL response")
        panel = panels[0]
        label = panel.get("panel_id") or panel.get("name")
        async with self.resource_lock(f"patch-panel-{panel['id']}"):
            await self._build(panel, label)

    async def _build(self, panel: dict[str, Any], label: str) -> None:
        rears = panel.get("rear_ports") or []
        fronts = panel.get("front_ports") or []
        result = audit(panel.get("panel_type"), rears, fronts)
        if result.problem or result.panel_type is None:
            self.fail(f"Patch panel {label}: cannot build its ports ({result.problem})")
        panel_type = result.panel_type
        rear_ids = {int(str(port["name"])): port["id"] for port in rears}

        for number in result.missing_rears:
            rear_ids[number] = await self._upsert(
                REAR_PORT_KIND,
                {"name": str(number), "connector": panel_type.rear_connector, "panel": {"id": panel["id"]}},
            )
        for number in result.missing_fronts:
            await self._upsert(
                FRONT_PORT_KIND,
                {
                    "name": str(number),
                    "connector": panel_type.front_connector,
                    "panel": {"id": panel["id"]},
                    "rear_port": {"id": rear_ids[panel_type.rear_of(number)]},
                },
            )
        for front_id, rear_number in result.to_pair.items():
            await self._upsert(FRONT_PORT_KIND, {"id": front_id, "rear_port": {"id": rear_ids[rear_number]}})

        for kind, names in (
            ("not in its type", result.extra),
            ("with another connector than its type", result.wrong_connector),
            ("on another rear port than its type gives", [f"front {name}" for name in result.misplaced]),
        ):
            if names:
                self.logger.warning(f"Patch panel {label}: port(s) {', '.join(names)} {kind}, left as is")
        self.logger.info(
            f"Patch panel {label}: {panel.get('panel_type')} ({panel_type.layout}), created "
            f"{len(result.missing_rears)} rear and {len(result.missing_fronts)} front port(s), "
            f"paired {len(result.to_pair)}"
        )

    async def _upsert(self, kind: str, data: dict[str, Any]) -> str:
        """Save one port as an untracked upsert and return its id; one retry covers a concurrent create."""
        port = await self.client.create(kind=kind, data=data)
        try:
            await port.save(allow_upsert=True, update_group_context=False)
        except GraphQLError:
            port = await self.client.create(kind=kind, data=data)
            await port.save(allow_upsert=True, update_group_context=False)
        return port.id
