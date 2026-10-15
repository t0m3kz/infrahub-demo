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


def component_owner_org_id(component: dict[str, Any]) -> str | None:
    """The org_id of the owner of a component's application (``parent.owner``), upper-cased; None if unknown."""
    owner = (component.get("parent") or {}).get("owner") or {}
    return str(owner.get("org_id") or "").strip().upper() or None


def crosses_owners(source: dict[str, Any], target: dict[str, Any]) -> bool:
    """Whether a dependency between two components joins two owners (both known, and different).

    The one definition of a cross-owner flow: RulesPlanner opens one only once
    it is approved, and checks/app_dependency.py requires an approved one to
    list its ports and its decision_reason.
    """
    source_owner, target_owner = component_owner_org_id(source), component_owner_org_id(target)
    return bool(source_owner and target_owner and source_owner != target_owner)
