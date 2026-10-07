from __future__ import annotations

from typing import Any

_VALID_ACCESS_STATUSES = frozenset({"auto", "pending", "approved", "denied"})


def dependency_access_status(dep: dict[str, Any]) -> str:
    """Normalize an AppDependency's access_status; invalid/missing defaults to "auto".

    Shared by generators/helpers/rules.py's RulesPlanner (on-prem/cloud rule
    authorization) and transforms/helpers/proxy.py (ZTNA segment publishing)
    — both need the same "is this grant denied" read of the same field.
    """
    raw = str(dep.get("access_status") or "auto").strip().lower()
    return raw if raw in _VALID_ACCESS_STATUSES else "auto"
