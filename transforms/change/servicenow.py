"""Transform: a change request -> the body of a ServiceNow ``change_request`` record."""

from __future__ import annotations

from datetime import UTC
from typing import Any

from infrahub_sdk.transforms import InfrahubTransform

from utils.change_rules import CHANGE_REQUEST_KIND, effective_risk, parse_datetime
from utils.data_cleaning import clean_data

CATEGORY = "Network"
SERVICENOW_DATETIME = "%Y-%m-%d %H:%M:%S"


def servicenow_datetime(value: Any) -> str | None:
    """A DateTime attribute value in ServiceNow's format, UTC (None when unset)."""
    parsed = parse_datetime(value)
    return parsed.astimezone(UTC).strftime(SERVICENOW_DATETIME) if parsed else None


def _names(rows: Any) -> list[str]:
    return sorted(str(row.get("name")) for row in rows or [] if isinstance(row, dict) and row.get("name"))


def build_change_request(request: dict[str, Any]) -> dict[str, Any]:
    """The ServiceNow ``change_request`` body of one cleaned row of change_request_servicenow.gql."""
    proposed_change = request.get("proposed_change")
    return {
        "number": request.get("external_id"),
        "short_description": request.get("short_description"),
        "description": request.get("description"),
        "justification": request.get("justification"),
        "type": request.get("change_type"),
        "risk": effective_risk(request.get("risk"), request.get("risk_override")),
        "impact": request.get("impact"),
        "category": CATEGORY,
        "implementation_plan": request.get("implementation_plan"),
        "backout_plan": request.get("backout_plan"),
        "test_plan": request.get("test_plan"),
        "start_date": servicenow_datetime(request.get("planned_start")),
        "end_date": servicenow_datetime(request.get("planned_end")),
        "cmdb_ci": _names(request.get("affected_devices")),
        "correlation_id": proposed_change.get("id") if isinstance(proposed_change, dict) else proposed_change,
        "correlation_display": request.get("name"),
        "u_affected_customers": _names(request.get("affected_customers")),
        "u_conflicts": request.get("conflicts"),
    }


class ChangeRequestServiceNow(InfrahubTransform):
    """The ServiceNow change_request payload of one change request, as JSON."""

    query = "change_request_servicenow"

    async def transform(self, data: Any) -> dict[str, Any]:
        requests = clean_data(data).get(CHANGE_REQUEST_KIND) or []
        if not requests:
            return {}
        return build_change_request(requests[0])
