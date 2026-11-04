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
    """Create the ports `panel_type` asks for and pair every front port with its rear port (jack or cassette).

    The type is the layout (generators/helpers/patch_panel.py PANEL_TYPES). A run creates the missing rear ports, then
    the missing front ports already on their rear port, and pairs a front port left unpaired. It never deletes or
    re-pairs a port: one the type does not have, with another connector, or on another rear port (a hand edit, or a
    type changed to a smaller one) is warned about and left, and checks/patch_panel_pairing.py reports it, so a
    re-run over a complete panel writes nothing. Runs when a panel joins patch_panels (data/events/99_group_triggers.yml)
    and when its `panel_type` changes (data/events/99_actions.yml). The writes are untracked: no run owns a panel, so
    no cleanup may delete its ports. One lock per panel: two runs at once would both create the missing ports. Every
    port is saved as an upsert, which the server resolves by the port's uniqueness ([panel, name]): the object file
    that creates the panel may upsert the same port in the same moment (a cable end, a used jack), and both writes then
    land on one port.
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
        """Save one port as an untracked upsert and return its id.

        Two creates of one new port at the same instant (this run and an object load) can both miss it and one is
        refused; the second attempt then finds the port the other wrote.
        """
        port = await self.client.create(kind=kind, data=data)
        try:
            await port.save(allow_upsert=True, update_group_context=False)
        except GraphQLError:
            port = await self.client.create(kind=kind, data=data)
            await port.save(allow_upsert=True, update_group_context=False)
        return port.id
