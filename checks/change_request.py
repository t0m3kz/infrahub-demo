"""Gate a Proposed Change on its change request (rules CR1 to CR5).

Targetless; it judges the open PC of the branch it runs on (the PC that started it, else the open PC whose source branch
is the check's branch; a canceled, merged or closed PC never counts) and the ``ChangeRequest``s that name it:

- CR1 the PC has at most one change request (ERROR when it has several). A PC with none is not gated: a change
  request is opt-in (made by the author or a change service), so a repository without one never blocks a merge (a
  failing user check refuses it, measured on 1.12);
- CR2 short_description, implementation_plan, backout_plan and test_plan are filled in, and the justification is, or
  the PC has a description (ERROR);
- CR3 a high effective risk (risk_override where set) or an emergency change has planned_start and planned_end (ERROR);
- CR4 a risk_override lower than the derived risk has a risk_override_reason (ERROR);
- CR5 the request was derived from the current artifact diff: its ``artifact_fingerprint`` equals the one of the
  branch's artifact diff now (WARNING: re-run add_change_request).

A branch with no open PC reports nothing. The artifact diff is read through utils/change_sources.py, the reader the
generator uses, and the fingerprint is utils/change_rules.py ``artifact_fingerprint``, so the two always agree.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from checks.base import WarningCheck
from utils.change_rules import CHANGE_REQUEST_KIND, artifact_fingerprint, effective_risk, risk_rank
from utils.change_sources import fetch_artifact_diff
from utils.data_cleaning import clean_data

REQUIRED_PLANS = ("short_description", "implementation_plan", "backout_plan", "test_plan")
ARTIFACT_FINGERPRINT_KEY = "current_artifact_fingerprint"


def _id(value: Any) -> str | None:
    return value.get("id") if isinstance(value, Mapping) else value


def _filled(value: Any) -> bool:
    return bool(str(value or "").strip())


def branch_change(cleaned: Mapping[str, Any], branch: str, pc_id: str = "") -> dict[str, Any] | None:
    """The open PC the check judges: the one that started it, else the open PC of ``branch``."""
    pcs = [pc for pc in cleaned.get("CoreProposedChange") or [] if isinstance(pc, Mapping)]
    return next((dict(pc) for pc in pcs if pc_id and pc.get("id") == pc_id), None) or next(
        (dict(pc) for pc in pcs if pc.get("source_branch") == branch), None
    )


def request_findings(
    request: Mapping[str, Any], pc: Mapping[str, Any], fingerprint: str | None
) -> list[tuple[str, str]]:
    """(severity, message) of CR2 to CR5 for one cleaned change request; ``fingerprint`` None skips CR5."""
    name = request.get("name") or request.get("id")
    findings = []
    missing = [field for field in REQUIRED_PLANS if not _filled(request.get(field))]
    if missing:
        findings.append(("error", f"CR2 {name}: {', '.join(missing)} empty: re-run add_change_request"))
    if not _filled(request.get("justification")) and not _filled(pc.get("description")):
        findings.append(("error", f"CR2 {name}: no justification and the proposed change has no description"))
    risk = effective_risk(request.get("risk"), request.get("risk_override"))
    if (risk == "high" or request.get("change_type") == "emergency") and not (
        request.get("planned_start") and request.get("planned_end")
    ):
        findings.append(("error", f"CR3 {name}: a {risk} risk {request.get('change_type')} change needs a window"))
    override = request.get("risk_override")
    if (
        override
        and risk_rank(override) < risk_rank(request.get("risk"))
        and not _filled(request.get("risk_override_reason"))
    ):
        findings.append(
            ("error", f"CR4 {name}: risk lowered from {request.get('risk')} to {override} without a reason")
        )
    if fingerprint is not None and request.get("artifact_fingerprint") != fingerprint:
        findings.append(
            ("warning", f"CR5 {name}: the artifacts changed since it was derived: re-run add_change_request")
        )
    return findings


class CheckChangeRequest(WarningCheck):
    """The open PC of the branch has at most one change request, complete and current (CR1 to CR5)."""

    query = "change_request_validation"

    async def collect_data(self) -> dict:
        """The query result plus the fingerprint of the branch's artifact diff now (CR5)."""
        data = await super().collect_data()
        unpacked = dict(data.get("data") or data)
        unpacked[ARTIFACT_FINGERPRINT_KEY] = artifact_fingerprint(
            await fetch_artifact_diff(self.client, self.branch_name)
        )
        return unpacked

    def validate(self, data: Any) -> None:
        cleaned = clean_data(data)
        pc = branch_change(cleaned, self.branch_name, self.initializer.proposed_change_id)
        if pc is None:
            return
        requests = [
            request
            for request in cleaned.get(CHANGE_REQUEST_KIND) or []
            if isinstance(request, Mapping) and _id(request.get("proposed_change")) == pc.get("id")
        ]
        if not requests:
            return  # not gated: a change request is opt-in
        if len(requests) > 1:
            names = ", ".join(sorted(str(r.get("name")) for r in requests))
            self.log_error(
                message=f"CR1 proposed change {pc.get('name')}: {len(requests)} change requests ({names}), "
                "expected at most one",
                object_id=pc.get("id"),
                object_type="CoreProposedChange",
            )
            return
        request = requests[0]
        for severity, message in request_findings(request, pc, cleaned.get(ARTIFACT_FINGERPRINT_KEY)):
            if severity == "error":
                self.log_error(message=message, object_id=request.get("id"), object_type=CHANGE_REQUEST_KIND)
            else:
                self.log_warning(message=message)
