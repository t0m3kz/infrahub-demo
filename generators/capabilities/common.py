"""References shared by the capability plan builders: a pool or object given by id or as a ``NodeRef`` of the plan."""

from __future__ import annotations

from typing import Any

from generators.dcim.cabling_plan import Link
from generators.plan import NodeRef

Ref = str | NodeRef


class RoutingPlanError(ValueError):
    """The routing of a layer pair cannot be planned: a shared object it needs does not exist, or a device has no
    router id.
    """


def pair_ends(pair: tuple[Link, str, str, bool]) -> tuple[Any, Any]:
    """The ports of a pair, the one of the lower-named device first."""
    link, _, _, swapped = pair
    return (link.top, link.bottom) if swapped else (link.bottom, link.top)


def id_ref(ref: Ref | None) -> Any:
    """``{"id": ...}`` for an id or for an object of the plan (a ``NodeRef`` the reconciler resolves to its id)."""
    return {"id": ref}


def password_ref(ref: Ref | None) -> dict[str, Any]:
    return {"password": id_ref(ref)} if ref else {}
