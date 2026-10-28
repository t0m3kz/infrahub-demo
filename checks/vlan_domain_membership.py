"""Validate that an infrastructure VLAN is carried only by ports of its own VLAN domain.

A ``ManagedVlan`` is a capability of the ports that carry it and a child of one VLAN domain (a ``ManagedMLAG`` pair, or
a device without MLAG), where its ID is unique. The schema cannot say that a port's device belongs to that domain
(``common_parent`` only holds between an interface and its device), so a port on another domain would carry a VLAN
number that means something else there:

* the VLAN domain of the port's device is the VLAN's domain: its ``ManagedMLAG`` when the device is paired, else the
  device itself. A member of an MLAG is never a domain, so a VLAN created on the member is reported with the pair.
"""

from __future__ import annotations

from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from utils.data_cleaning import clean_data

_VLAN_KIND = "ManagedVlan"
_MLAG_KIND = "ManagedMLAG"


def _label(node: dict[str, Any]) -> str:
    return str(node.get("name") or node.get("id") or "<unnamed>")


def _own_domain_id(device: dict[str, Any]) -> str | None:
    """The VLAN domain of a device: its MLAG when paired, else the device itself."""
    for capability in device.get("capabilities") or []:
        if capability.get("typename") == _MLAG_KIND and capability.get("id"):
            return capability["id"]
    return device.get("id")


class CheckVlanDomainMembership(InfrahubCheck):
    """Every port carrying an infrastructure VLAN sits on a device of the VLAN's domain."""

    query = "vlan_domain_membership"

    def validate(self, data: Any) -> None:
        for vlan in clean_data(data).get(_VLAN_KIND) or []:
            domain = vlan.get("vlan_domain") or {}
            for port in vlan.get("capabilities") or []:
                device = port.get("device") or {}
                if _own_domain_id(device) != domain.get("id"):
                    self.log_error(
                        message=(
                            f"VLAN {vlan.get('vlan_id')} '{_label(vlan)}' of VLAN domain '{_label(domain)}' is carried "
                            f"by {_label(device)} {_label(port)}, which is not in that domain."
                        ),
                        object_id=port.get("id"),
                        object_type=_VLAN_KIND,
                    )
