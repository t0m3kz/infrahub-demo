"""Who owns a registered prefix and what the edge exchanges between sessions: the rules the border filters and the
routing invariants share."""

from collections.abc import Iterable, Mapping
from typing import Any, NamedTuple

CUSTOMER_TYPENAME = "OrganizationCustomer"
SHARED_PREFIX = "shared-prefix"
MULTIPLE_SESSIONS = "multiple-sessions"


def customer_owner(owner: Any) -> str | None:
    """The key of a cleaned customer owner (org_id, else display_label), None for anything that is not a customer.

    Only an ``OrganizationCustomer`` owns a prefix that may cross the border: a provider, an internal
    organisation or no owner at all registers prefixes that are never accepted nor announced
    (transforms/helpers/bgp.py), which checks/routing_invariants.py rule 7 reports.
    """
    if not isinstance(owner, dict) or owner.get("typename") != CUSTOMER_TYPENAME:
        return None
    return owner.get("org_id") or owner.get("display_label") or None


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
    segments: Mapping[str, Iterable[str]], learned: Mapping[str, Mapping[str, Iterable[str]]]
) -> dict[str, dict[str, frozenset[str]]]:
    """What each external session of ONE namespace accepts and is announced, per the same-owner exchange rule.

    ``segments`` maps an owner to the prefixes of its on-prem segments; ``learned`` maps a session to the
    prefixes registered behind it, by owner. The edge passes routes between two external sessions of the SAME
    owner and never across owners:

    - ``in``: the union of ``learned[session]`` (what the far end may send);
    - ``owners``: the owners with a prefix in ``learned[session]``;
    - local space of an owner: its segment prefixes learned on no session (a segment hosted by an office is
      registered behind the office's gateway session, so it is not local);
    - ``out``: for each of the session's owners, its local space and what it registered on every OTHER
      session, minus ``in`` (a prefix is never announced back to the session it was learned on).

    A session with no owner has empty ``owners``, ``in`` and ``out``. Nothing an owner has not registered is
    announced, and nothing of another owner. Callers group sessions by namespace.
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
        announced = {
            prefix
            for owner in by_owner
            for prefix in (
                *local.get(owner, ()),
                *(p for other, theirs in registered.items() if other != session for p in theirs.get(owner, ())),
            )
        }
        policies[session] = {
            "owners": frozenset(by_owner),
            "in": accepted,
            "out": frozenset(announced) - accepted,
        }
    return policies


def ownership_conflicts(
    segments: Mapping[str, Iterable[str]], learned: Mapping[str, Mapping[str, Iterable[str]]]
) -> list[OwnershipConflict]:
    """The registrations of ONE namespace that break the exchange rule (same arguments as exchange_policies).

    - a prefix registered by more than one owner (segments and sessions together): it would be announced to
      another owner's session;
    - a prefix of one owner registered behind more than one session: an owner's registered prefix is local or
      reachable through exactly one session.

    Exact prefix strings; overlapping but different prefixes are the business of the overlap rules.
    """
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
        if len(sessions) > 1
    ]
    return conflicts
