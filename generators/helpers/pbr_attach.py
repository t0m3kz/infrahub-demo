"""Which SVIs a footprint's PBR policy is applied on: the plan of the attachments, with no I/O.

A pbr policy is one object per customer footprint in a DC (``RoutingPolicy`` with ``kind: pbr``, ``origin: generated``,
``customer_deployment``); the interfaces it steers are its ``capabilities``, the interface capability edge. The segment
reconcile of a device calls this with what the device has: the SVIs it needs (each with the footprint of its segment in
the device's DC) and the generated pbr policies already holding some of the device's SVIs. The plan is the difference:

- an SVI that is not yet on the policy of its footprint is added, a new one as well as one that has existed for a while
  (idempotent: a run that finds everything in place writes nothing, and an SVI another flow recreated, with a new id,
  is simply not attached any more and is added again);
- an SVI that is on the policy of another footprint (its row moved, or its segment left the footprint) is removed from
  it;
- a policy with ``origin: manual`` is never read for removal, and only the interfaces the caller owns (the SVIs of this
  device) are ever removed, so a policy shared by many devices keeps their attachments.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field

PBR_KIND = "pbr"
GENERATED = "generated"


def policy_name(footprint_name: str) -> str:
    """The name of the generated pbr policy of a footprint (a footprint has one)."""
    return f"pbr-{footprint_name}".lower()


@dataclass(frozen=True)
class PbrPolicyFact:
    """A pbr policy as read: its footprint (None when unset), who owns it and the interfaces it is applied on."""

    id: str
    kind: str
    origin: str
    customer_id: str | None
    attached: frozenset[str]

    @property
    def ours(self) -> bool:
        """Whether a generator may change its attachments: a generated pbr policy of a footprint."""
        return self.kind == PBR_KIND and self.origin == GENERATED and self.customer_id is not None


@dataclass(frozen=True)
class PbrPlan:
    """``create``: footprints with no policy yet; ``add``: footprint id -> SVI ids to attach; ``remove``: policy id ->
    SVI ids to detach."""

    create: tuple[str, ...] = ()
    add: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    remove: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.create or self.add or self.remove)


def _footprints(value: str | Collection[str] | None) -> frozenset[str]:
    """The footprints an interface is steered for: one (an SVI of a segment), several (the L3 VNI SVI of a VRF, shared by
    every footprint whose context is tagged on the device) or none."""
    if not value:
        return frozenset()
    return frozenset({value}) if isinstance(value, str) else frozenset(value)


def plan_pbr_attachments(
    desired: Mapping[str, str | Collection[str] | None], policies: Iterable[PbrPolicyFact], owned: Iterable[str]
) -> PbrPlan:
    """The attachments that make the device's steered interfaces exactly the desired ones.

    ``desired``: interface id -> the footprint(s) it is steered for, empty or None for an interface that is not steered
    (no footprint, or the footprint has no firewall context). ``owned``: the ids of the interfaces this caller
    reconciles. ``policies``: every pbr policy that holds, or may hold, one of them.
    """
    ours = {policy.customer_id: policy for policy in policies if policy.ours and policy.customer_id}
    owned_ids = frozenset(owned)
    wanted = {iface: _footprints(value) for iface, value in desired.items() if iface in owned_ids}
    create: list[str] = []
    add: dict[str, list[str]] = {}
    for iface, footprints in sorted(wanted.items()):
        for footprint in sorted(footprints):
            policy = ours.get(footprint)
            if policy is None:
                if footprint not in create:
                    create.append(footprint)
                add.setdefault(footprint, []).append(iface)
            elif iface not in policy.attached:
                add.setdefault(footprint, []).append(iface)
    remove: dict[str, list[str]] = {}
    for policy in ours.values():
        stale = sorted(
            iface for iface in policy.attached & owned_ids if policy.customer_id not in wanted.get(iface, frozenset())
        )
        if stale:
            remove[policy.id] = stale
    return PbrPlan(
        create=tuple(create),
        add={footprint: tuple(ifaces) for footprint, ifaces in add.items()},
        remove={policy: tuple(ifaces) for policy, ifaces in remove.items()},
    )
