"""MLAG pair helpers shared by generators, transforms and checks.

There is no VLAN domain object: a segment's VLAN ID is a row on ONE device (``ManagedVlanDomainSegment.device``,
unique on [device, vlan_id]). The two peers of an MLAG pair must carry the same VLAN ID for a segment (a dual-homed
port-channel is one L2 broadcast domain): the allocator (generators/vlan_domain.py) gives them one id and
checks/mlag_domains.py reports a pair whose rows differ. The generator reads the capabilities as SDK peers and the
checks as cleaned query data; both pass ``(typename, id)`` pairs here.
"""

from __future__ import annotations

from collections.abc import Iterable

MLAG_KIND = "ManagedMLAG"


def mlag_id(capabilities: Iterable[tuple[str | None, str | None]]) -> str | None:
    """The id of the MLAG a device is a member of (its ``(typename, id)`` capabilities), None without one.

    An MLAG without an id (a query that did not select it) is ignored.
    """
    return next(
        (capability_id for typename, capability_id in capabilities if typename == MLAG_KIND and capability_id), None
    )


def mlag_peer_devices(names: Iterable[str]) -> str:
    """The value of ``ManagedMLAG.peer_devices``: the member device names, sorted and joined with a comma.

    The attribute mirrors the many ``capabilities`` edge into one unique Text, because a uniqueness constraint cannot
    use a many relationship. The generator writes it and ``checks/mlag_domains.py`` compares against it.
    """
    return ",".join(sorted(names))
