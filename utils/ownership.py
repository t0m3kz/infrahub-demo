"""Who owns a registered prefix and what the edge exchanges between sessions (border filters and routing invariants)."""

from collections.abc import Iterable, Mapping
from typing import Any, NamedTuple

CUSTOMER_TYPENAME = "OrganizationCustomer"
SHARED_PREFIX = "shared-prefix"
MULTIPLE_SESSIONS = "multiple-sessions"


def customer_owner(owner: Any) -> str | None:
    """The key of a cleaned customer owner (org_id, else display_label), None for a non-customer."""
    if not isinstance(owner, dict) or owner.get("typename") != CUSTOMER_TYPENAME:
        return None
    return owner.get("org_id") or owner.get("display_label") or None


class CircuitOwnerError(ValueError):
    """A circuit whose locations name two offices of different owners."""


def circuit_owner(office_owners: Iterable[str | None]) -> str | None:
    """The owner of a circuit, derived: the one distinct owner key of its offices, else None; CircuitOwnerError for
    several.
    """
    distinct = sorted({owner for owner in office_owners if owner})
    if len(distinct) > 1:
        raise CircuitOwnerError(f"locations are offices of different owners ({', '.join(distinct)})")
    if distinct:
        return distinct[0]
    return None


class OwnershipConflict(NamedTuple):
    """A registration that would leak one owner's prefix to another owner, or that is reachable twice."""

    kind: str  # SHARED_PREFIX or MULTIPLE_SESSIONS
    prefix: str
    owners: tuple[str, ...]
    sessions: tuple[str, ...]

    @property
    def message(self) -> str:
        """One sentence: the prefix, who registers it where, and why it is a problem."""
        owners = ", ".join(self.owners)
        sessions = ", ".join(self.sessions) or "no external session"
        if self.kind == SHARED_PREFIX:
            return (
                f"prefix {self.prefix} is registered by more than one owner ({owners}; behind {sessions}): "
                "it would be announced to another owner's session"
            )
        return (
            f"prefix {self.prefix} of {owners} is registered behind more than one session ({sessions}): "
            "an owner's registered prefix is local or reachable through exactly one session"
        )


def exchange_policies(
    segments: Mapping[str, Iterable[str]],
    learned: Mapping[str, Mapping[str, Iterable[str]]],
    *,
    announced: Mapping[str, Iterable[str]] | None = None,
) -> dict[str, dict[str, frozenset[str]]]:
    """What each external session of ONE namespace accepts (``in``) and is announced (``out``), per owner.

    Routes pass between sessions of the SAME owner only, never back to the session they were learned on.
    """
    frozen = {
        session: {owner: frozenset(prefixes) for owner, prefixes in by_owner.items()}
        for session, by_owner in learned.items()
    }
    registered = {
        session: {owner: prefixes for owner, prefixes in by_owner.items() if prefixes}
        for session, by_owner in frozen.items()
    }
    learned_anywhere = {
        prefix for by_owner in registered.values() for prefixes in by_owner.values() for prefix in prefixes
    }
    local = {owner: frozenset(prefixes) - learned_anywhere for owner, prefixes in segments.items()}
    policies: dict[str, dict[str, frozenset[str]]] = {}
    for session, by_owner in registered.items():
        accepted = frozenset(prefix for prefixes in by_owner.values() for prefix in prefixes)
        outgoing = {
            prefix
            for owner in by_owner
            for prefix in (
                *local.get(owner, ()),
                *(p for other, theirs in registered.items() if other != session for p in theirs.get(owner, ())),
            )
        } | set((announced or {}).get(session, ()))
        policies[session] = {
            "owners": frozenset(by_owner),
            "in": accepted,
            "out": frozenset(outgoing) - accepted,
        }
    return policies


def ownership_conflicts(
    segments: Mapping[str, Iterable[str]],
    learned: Mapping[str, Mapping[str, Iterable[str]]],
    *,
    session_groups: Mapping[str, str] | None = None,
) -> list[OwnershipConflict]:
    """The registrations of ONE namespace that break the exchange rule (a prefix of several owners or attachment
    points).
    """
    groups = session_groups or {}
    owners_of: dict[str, set[str]] = {}
    sessions_of: dict[tuple[str, str], set[str]] = {}
    for owner, prefixes in segments.items():
        for prefix in prefixes:
            owners_of.setdefault(prefix, set()).add(owner)
    for session, by_owner in learned.items():
        for owner, prefixes in by_owner.items():
            for prefix in prefixes:
                owners_of.setdefault(prefix, set()).add(owner)
                sessions_of.setdefault((prefix, owner), set()).add(session)

    conflicts = [
        OwnershipConflict(
            SHARED_PREFIX,
            prefix,
            tuple(sorted(owners)),
            tuple(sorted({s for owner in owners for s in sessions_of.get((prefix, owner), ())})),
        )
        for prefix, owners in sorted(owners_of.items())
        if len(owners) > 1
    ]
    conflicts += [
        OwnershipConflict(MULTIPLE_SESSIONS, prefix, (owner,), tuple(sorted(sessions)))
        for (prefix, owner), sessions in sorted(sessions_of.items())
        if len({groups.get(session, session) for session in sessions}) > 1
    ]
    return conflicts
