"""Every patch panel holds the ports its type says, each front port on its rear port."""

from __future__ import annotations

from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from generators.helpers.patch_panel import audit
from utils.data_cleaning import clean_data

PANEL_KIND = "DcimPatchPanel"


class CheckPatchPanelPairing(InfrahubCheck):
    """A panel's ports are what add_patch_panel builds from its `panel_type` (ERRORs).

    The generator builds the ports and the pairing, and a person can edit both, so they need a guard of their own.
    Per panel, compared with its type (generators/helpers/patch_panel.py audit()):

    - the type is known and every port name is a number, used once;
    - no rear or front port the type has is missing, and none is there that the type does not have;
    - every port has the connector of its type;
    - every front port is on the rear port the type gives it (a jack: front N on rear N; a cassette: rear k carries
      front (k-1)*F+1 .. k*F).
    """

    query = "patch_panel_pairing"

    def validate(self, data: Any) -> None:
        for panel in sorted(clean_data(data).get(PANEL_KIND) or [], key=lambda item: str(item.get("panel_id"))):
            self._validate_panel(panel)

    def _validate_panel(self, panel: dict[str, Any]) -> None:
        label = f"{PANEL_KIND} '{panel.get('panel_id')}'"
        result = audit(panel.get("panel_type"), panel.get("rear_ports") or [], panel.get("front_ports") or [])
        if result.problem:
            self.log_error(message=f"{label}: {result.problem} (rule: a known type, ports numbered from 1)")
            return
        for side, numbers in (("rear", result.missing_rears), ("front", result.missing_fronts)):
            if numbers:
                self.log_error(
                    message=f"{label}: {side} port(s) {_numbers(numbers)} of its type are missing "
                    "(rule: the panel holds every port of its type; run add_patch_panel)"
                )
        if result.extra:
            self.log_error(message=f"{label}: port(s) {', '.join(result.extra)} are not in its type")
        if result.wrong_connector:
            self.log_error(
                message=f"{label}: port(s) {', '.join(result.wrong_connector)} have another connector than its type"
            )
        unpaired = sorted(
            int(front["name"]) for front in panel.get("front_ports") or [] if front["id"] in result.to_pair
        )
        if unpaired:
            self.log_error(
                message=f"{label}: front port(s) {_numbers(unpaired)} have no rear port (rule: every front is paired)"
            )
        if result.misplaced:
            self.log_error(
                message=f"{label}: front port(s) {', '.join(result.misplaced)} are on another rear port than "
                f"the {result.panel_type.layout if result.panel_type else ''} layout gives"
            )


def _numbers(numbers: list[int]) -> str:
    """1, 2, 3, 7 -> '1-3, 7': a panel of 96 ports reads in one line."""
    runs: list[tuple[int, int]] = []
    for number in sorted(numbers):
        if runs and number == runs[-1][1] + 1:
            runs[-1] = (runs[-1][0], number)
        else:
            runs.append((number, number))
    return ", ".join(str(a) if a == b else f"{a}-{b}" for a, b in runs)
