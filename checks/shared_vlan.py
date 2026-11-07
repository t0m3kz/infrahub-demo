"""A segment with vlan_scope shared has one VLAN id on every switch, derived from its VNI (rule SV1).

``vlan_scope: shared`` is for L2 that is trunked between switches that cannot do VXLAN: the id is the same on both
ends of a trunk, so it cannot be a per-switch local choice. The id is ``SHARED_VLAN_ID_MIN`` + (VNI - ``GLOBAL_L2_VNI_START``) (the shared band,
``utils/limits.py``; the VNI is the one the global pool hands out); ``generators/segment_vlan.py`` writes it and this check reports a row that differs
(a hand edit), a VNI outside the band, or two VNIs in the deployments of one segment.
"""

from __future__ import annotations

from typing import Any

from checks.base import WarningCheck
from utils.data_cleaning import clean_data
from utils.segment_vlan import shared_vlan_id, shared_vni_band

SHARED = "shared"


def shared_vlan_errors(segments: list[dict[str, Any]], deployments: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """(message, object id) for each broken shared segment of one cleaned query answer."""
    shared = {s["id"]: s.get("name") or s["id"] for s in segments if s.get("vlan_scope") == SHARED}
    by_segment: dict[str, list[dict[str, Any]]] = {}
    for item in deployments:
        segment = item.get("segment") or {}
        if segment.get("id") in shared:
            by_segment.setdefault(segment["id"], []).append(item)
    errors: list[tuple[str, str]] = []
    band = f"VNI {shared_vni_band()}"
    for segment_id, name in shared.items():
        items = by_segment.get(segment_id, [])
        vnis = {item["vni"] for item in items if item.get("vni") is not None}
        if len(vnis) != 1 or shared_vlan_id(next(iter(vnis))) is None:
            errors.append(
                (
                    f"SV1: shared segment '{name}' needs one VNI in {band}, its deployments have {sorted(vnis) or 'none'}.",
                    segment_id,
                )
            )
            continue
        expected = shared_vlan_id(next(iter(vnis)))
        for item in items:
            for row in item.get("vlans") or []:
                if row.get("vlan_id") != expected:
                    device = (row.get("device") or {}).get("name") or (row.get("device") or {}).get("id")
                    errors.append(
                        (
                            f"SV1: shared segment '{name}' has VLAN {row.get('vlan_id')} on '{device}', "
                            f"VNI {next(iter(vnis))} gives VLAN {expected}.",
                            row["id"],
                        )
                    )
    return errors


class CheckSharedVlan(WarningCheck):
    """Every row of a shared segment carries the VLAN id its VNI gives (rule SV1)."""

    query = "shared_vlan"

    def validate(self, data: Any) -> None:
        cleaned = clean_data(data)
        for message, object_id in shared_vlan_errors(
            cleaned.get("ManagedVxlanSegment") or [], cleaned.get("ManagedSegmentDeployment") or []
        ):
            self.log_error(message=message, object_id=object_id, object_type="ManagedSegmentVlan")
