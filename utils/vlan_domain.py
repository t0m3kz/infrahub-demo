"""The one rule for the VLAN domain of a device, for generators and transforms.

A VLAN ID is unique within a VLAN domain: an MLAG pair is one domain (both peers carry a segment on the same VLAN ID),
a device without MLAG is its own (schemas/base/dcim.yml ManagedGenericVlanDomain). A member of an MLAG is never a
domain itself, so the MLAG is chosen first. The generator reads the capabilities as SDK peers and the transform as
cleaned query data; both pass ``(typename, id)`` pairs here.
"""

from __future__ import annotations

from collections.abc import Iterable

MLAG_KIND = "ManagedMLAG"


def vlan_domain_id(device_id: str | None, capabilities: Iterable[tuple[str | None, str | None]]) -> str | None:
    """The id of the device's VLAN domain: its MLAG's when paired, else the device's own.

    ``capabilities`` are the device's ``(typename, id)`` pairs. An MLAG without an id (a query that did not select it)
    is ignored rather than resolved to None, which would silently drop every segment of the device.
    """
    for typename, capability_id in capabilities:
        if typename == MLAG_KIND and capability_id:
            return capability_id
    return device_id


def mlag_peer_devices(names: Iterable[str]) -> str:
    """The value of ``ManagedMLAG.peer_devices``: the member device names, sorted and joined with a comma.

    The attribute mirrors the many ``capabilities`` edge into one unique Text, because a uniqueness constraint cannot
    use a many relationship. The generator writes it and ``checks/mlag_domains.py`` compares against it.
    """
    return ",".join(sorted(names))
