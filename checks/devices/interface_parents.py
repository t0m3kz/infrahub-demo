"""Check what an interface rides on (the ``parent`` of a ``DcimSubInterface``).

The schema holds no same-device rule, so this does; see domain-model.md "Interface encapsulation".
"""

from __future__ import annotations

from typing import Any

from checks.base import WarningCheck
from generators.dcim.virtual_nics import is_host_nic
from utils.data_cleaning import clean_data
from utils.interface_encapsulation import parent_of

SUB_INTERFACE_KIND = "DcimSubInterface"
_PHYSICAL_INTERFACE = "DcimPhysicalInterface"
_UPLINK_ROLE = "uplink"
_PEERING = "CloudPeeringInterface"
_PROVIDER_CONNECT = "TopologyProviderConnect"


def _label(node: dict[str, Any]) -> str:
    return str(node.get("name") or node.get("id") or "<unnamed>")


def interface_parent_errors(iface: dict[str, Any], parent: dict[str, Any] | None) -> list[str]:
    """Messages for rules 1, 2, 3 and 5 of one interface and its parent."""
    if not isinstance(parent, dict):
        return []
    device = iface.get("device") or {}
    parent_device = parent.get("device") or {}
    host = device.get("hosting_device")
    host_id = host.get("id") if isinstance(host, dict) else None
    where = f"{_label(device)} {_label(iface)}"
    on_other_device = parent_device.get("id") != device.get("id")
    is_nic = is_host_nic(iface)
    if not is_nic:
        if on_other_device and not (
            iface.get("typename") == _PEERING and parent_device.get("typename") == _PROVIDER_CONNECT
        ):
            return [
                f"{where} rides on {_label(parent)} of '{_label(parent_device)}', another device: a parent "
                "is an interface of the same device."
            ]
        return []
    if parent_device.get("id") != host_id:
        if host_id is None and not on_other_device:
            return []
        return [
            f"{where} is pinned to {_label(parent)} of '{_label(parent_device)}', not of its hosting device "
            f"'{_label(host) if isinstance(host, dict) else 'none'}'."
        ]
    errors = []
    if parent.get("typename") != _PHYSICAL_INTERFACE:
        errors.append(f"{where} is pinned to {_label(parent)}, a {parent.get('typename')}, not a physical host port.")
    elif iface.get("role") == _UPLINK_ROLE and not parent.get("cable"):
        errors.append(
            f"{where} is pinned to {_label(parent)} of '{_label(parent_device)}', which is not cabled: no border port "
            "reaches the virtual device through it."
        )
    return errors


class CheckInterfaceParents(WarningCheck):
    """An interface rides on one interface, of its own device (a NIC of a virtual device: a cabled host port)."""

    query = "interface_parents"

    def validate(self, data: Any) -> None:
        for iface in clean_data(data).get(SUB_INTERFACE_KIND) or []:
            parent = parent_of(iface)
            if parent is None:
                continue
            where = f"{_label(iface.get('device') or {})} {_label(iface)}"
            if parent.get("id") == iface.get("id"):
                self.log_error(
                    message=f"{where} is its own parent.",
                    object_id=iface.get("id"),
                    object_type=iface.get("typename") or SUB_INTERFACE_KIND,
                )
                continue
            for message in interface_parent_errors(iface, parent):
                self.log_error(
                    message=message,
                    object_id=iface.get("id"),
                    object_type=iface.get("typename") or SUB_INTERFACE_KIND,
                )
