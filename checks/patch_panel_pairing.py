"""Every front port of a patch panel is paired with the rear port its layout says."""

from __future__ import annotations

from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from generators.helpers.patch_panel import LAYOUT_EMPTY, LAYOUT_UNKNOWN, plan_pairing, rear_port_id
from utils.data_cleaning import clean_data

PANEL_KIND = "DcimPatchPanel"


class CheckPatchPanelPairing(InfrahubCheck):
    """A panel's pairing is what add_patch_panel derives from its ports (ERRORs).

    The pairing is data the generator writes (an object template cannot pair a port with its sibling), and a person
    can edit it, so it needs a guard of its own; the earlier panel check was removed while the model was purely
    structural. Per panel, with the layout derived from the ports (generators/helpers/patch_panel.py):

    - the layout is derivable (a jack: equal numbered counts; a cassette: MPO-12 rear ports and a multiple of
      front ports), a panel with no ports at all is reported too;
    - every front port has a rear port;
    - every front port is on the rear port the layout gives it, which makes a jack rear port carry exactly one front
      port and a cassette rear port exactly F.
    """

    query = "patch_panel_pairing"

    def validate(self, data: Any) -> None:
        for panel in sorted(clean_data(data).get(PANEL_KIND) or [], key=lambda item: str(item.get("panel_id"))):
            self._validate_panel(panel)

    def _validate_panel(self, panel: dict[str, Any]) -> None:
        label = f"{PANEL_KIND} '{panel.get('panel_id')}'"
        rears = panel.get("rear_ports") or []
        fronts = panel.get("front_ports") or []
        pairing = plan_pairing(rears, fronts)
        if pairing.layout == LAYOUT_EMPTY:
            self.log_error(message=f"{label}: has no ports (rule: a panel is created from a template with its ports)")
            return
        if pairing.layout == LAYOUT_UNKNOWN:
            self.log_error(message=f"{label}: layout not derivable, {pairing.problem} (rule: jack or cassette)")
            return
        names = {rear["id"]: rear.get("name") for rear in rears}
        for front in sorted(fronts, key=lambda item: int(item["name"])):
            current = rear_port_id(front)
            wanted = pairing.wanted[front["id"]]
            if current is None:
                self.log_error(
                    message=f"{label}: front port {front['name']} has no rear port (rule: every front is paired)"
                )
            elif current != wanted:
                self.log_error(
                    message=f"{label}: front port {front['name']} is on rear port {names.get(current, '?')}, "
                    f"the {pairing.layout} layout gives rear port {names[wanted]}"
                )
