"""Validate what an interface rides on (``parent_interface`` of a ``DcimSubInterface``).

``parent_interface`` is one link of the marker generic ``DcimSubInterface`` (a virtual interface and a cloud peering
interface inherit it) and the schema holds no same-device rule (no ``common_parent``):

* a sub-interface (a context leg, an SVI's port) rides on a NIC, port or LAG of ITS OWN device; so does the cloud
  peering interface of a transport, unless the cloud's port is the provider's own in a cage: a Direct Connect or
  Interconnect virtual interface rides on a port or LAG of the ``TopologyProviderConnect`` that holds it;
* a base NIC of a virtual device (a DcimVirtualInterface with the role management, uplink or ha, which
  generators/virtual_nics.py pins) rides on a port of the hosting device, and that port carries the cable, so the
  border leaf -> host port -> NIC path is a path in the graph.

Rules:

1. a parent on another device is an ERROR, except for a NIC of a virtual device whose parent is a port of its
   ``hosting_device`` and a cloud peering interface whose parent is on a ``TopologyProviderConnect``;
2. the parent of a NIC of a virtual device is a port of that ``hosting_device`` (a NIC on a device without one is
   rule 1 when its parent is elsewhere);
3. the host port of an uplink NIC is cabled (otherwise nothing reaches the border leaf through it);
4. a NIC without a parent is not reported: a machine can have more uplinks than its host;
5. the parent of a NIC is a DcimPhysicalInterface (a host port, not a LAG, a NIC or a sub-interface).
"""

from __future__ import annotations

from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from generators.virtual_nics import PINNED_ROLES
from utils.data_cleaning import clean_data

_SUB_INTERFACE = "DcimSubInterface"
_VIRTUAL_DEVICE = "DcimVirtualDevice"
_PHYSICAL_INTERFACE = "DcimPhysicalInterface"
_UPLINK_ROLE = "uplink"
_PEERING = "CloudPeeringInterface"
_PROVIDER_CONNECT = "TopologyProviderConnect"


def _label(node: dict[str, Any]) -> str:
    return str(node.get("name") or node.get("id") or "<unnamed>")


def interface_parent_errors(iface: dict[str, Any]) -> list[str]:
    """The messages (rules 1, 2, 3, 5) of one cleaned DcimSubInterface node of interface_parents.gql."""
    parent = iface.get("parent_interface")
    if not isinstance(parent, dict):
        return []
    device = iface.get("device") or {}
    parent_device = parent.get("device") or {}
    host = device.get("hosting_device")
    host_id = host.get("id") if isinstance(host, dict) else None
    where = f"{_label(device)} {_label(iface)}"
    on_other_device = parent_device.get("id") != device.get("id")
    is_nic = device.get("typename") == _VIRTUAL_DEVICE and iface.get("role") in PINNED_ROLES
    if not is_nic:
        if on_other_device and not (
            iface.get("typename") == _PEERING and parent_device.get("typename") == _PROVIDER_CONNECT
        ):
            return [
                f"{where} rides on {_label(parent)} of '{_label(parent_device)}', another device: a parent_interface "
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


class CheckInterfaceParents(InfrahubCheck):
    """A sub-interface or a peering rides on an interface of its own device; a NIC of a virtual device on a cabled host port."""

    query = "interface_parents"

    def validate(self, data: Any) -> None:
        for iface in clean_data(data).get("DcimSubInterface") or []:
            for message in interface_parent_errors(iface):
                self.log_error(
                    message=message, object_id=iface.get("id"), object_type=iface.get("typename") or _SUB_INTERFACE
                )
