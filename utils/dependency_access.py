from __future__ import annotations

from typing import Any

_VALID_ACCESS_STATUSES = frozenset({"auto", "pending", "approved", "denied"})


def dependency_access_status(dep: dict[str, Any]) -> str:
    """Normalize an AppDependency's access_status; invalid or missing is "auto"."""
    raw = str(dep.get("access_status") or "auto").strip().lower()
    return raw if raw in _VALID_ACCESS_STATUSES else "auto"


def component_owner_org_id(component: dict[str, Any]) -> str | None:
    """The org_id of the owner of a component's application (``parent.owner``), upper-cased; None if unknown."""
    owner = (component.get("parent") or {}).get("owner") or {}
    return str(owner.get("org_id") or "").strip().upper() or None


def crosses_owners(source: dict[str, Any], target: dict[str, Any]) -> bool:
    """Whether a dependency joins two known, different owners (the one definition of a cross-owner flow)."""
    source_owner, target_owner = component_owner_org_id(source), component_owner_org_id(target)
    return bool(source_owner and target_owner and source_owner != target_owner)
