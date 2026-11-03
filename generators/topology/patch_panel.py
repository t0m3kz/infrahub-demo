"""Pair the front ports of a patch panel with its rear ports."""

from __future__ import annotations

from typing import Any

from utils.data_cleaning import clean_data

from ..common import CommonGenerator
from ..helpers.patch_panel import LAYOUT_EMPTY, LAYOUT_UNKNOWN, pairing_changes, plan_pairing

PANEL_KIND = "DcimPatchPanel"
FRONT_PORT_KIND = "DcimFrontPatchPanelPort"


class PatchPanelGenerator(CommonGenerator):
    """Write ``rear_port`` on the front ports of one panel, as the ports say it belongs (jack or cassette).

    An object template creates a panel's ports unpaired (it cannot point a child at a sibling). The layout is
    derived from the ports alone (generators/helpers/patch_panel.py). Runs when a panel joins patch_panels (data/events/99_group_triggers.yml) and when
    its port edges change (data/events/99_actions.yml); the template's children may not exist yet on the created run,
    so a panel without ports is logged and left, and the port-edge rules run it again. Only unpaired front ports are
    written; one already on another rear port is left and warned about (checks/patch_panel_pairing.py reports it), so
    a re-run over a paired panel writes nothing. The writes are untracked: no run owns a panel, so no cleanup may
    delete its ports.
    """

    graphql_root_key = PANEL_KIND

    async def generate(self, data: dict[str, Any]) -> None:
        panels = clean_data(data).get(self.graphql_root_key) or []
        if not panels:
            self.fail(f"No {self.graphql_root_key} data in GraphQL response")
        panel = panels[0]
        label = panel.get("panel_id") or panel.get("name")
        rears = panel.get("rear_ports") or []
        fronts = panel.get("front_ports") or []

        pairing = plan_pairing(rears, fronts)
        if pairing.layout == LAYOUT_EMPTY:
            self.logger.info(f"Patch panel {label}: no ports yet, nothing to pair (a port-edge update runs this again)")
            return
        if pairing.layout == LAYOUT_UNKNOWN:
            # A partly created panel shows up here too; the run its remaining ports trigger converges it.
            self.fail(f"Patch panel {label}: no pairing derivable ({pairing.problem})")

        to_write, conflicts = pairing_changes(pairing, fronts)
        names = {front["id"]: front.get("name") for front in fronts}
        for front_id in conflicts:
            self.logger.warning(
                f"Patch panel {label}: front port {names[front_id]} is already paired with another rear port, left as is"
            )
        for front_id, rear_id in to_write.items():
            port = await self.client.create(kind=FRONT_PORT_KIND, data={"id": front_id, "rear_port": {"id": rear_id}})
            await port.save(allow_upsert=True, update_group_context=False)
        self.logger.info(f"Patch panel {label}: {pairing.layout}, {len(to_write)} front port(s) paired")
