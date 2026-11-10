"""Validate what an interface rides on (the ``parent`` of its ``ManagedInterfaceEncapsulation``).

An interface that rides on another one has one ``ManagedInterfaceEncapsulation`` among its ``capabilities``
(schemas/extensions/capabilities/encapsulation.yml): a virtual interface, a cloud peering interface or the end of a
provider connection. The schema holds no same-device rule (no ``common_parent``):

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
4. a NIC without a parent is not reported: a machine can have more uplinks than its host (it has no encapsulation);
5. the parent of a NIC is a DcimPhysicalInterface (a host port, not a LAG, a NIC or a sub-interface);
6. an interface has at most one encapsulation, and an encapsulation is on one interface (ERROR);
7. an interface is not its own parent (ERROR);
8. (WARNING) an encapsulation on no interface: the interface it belonged to was deleted and left it behind.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from checks.base import WarningCheck
from generators.virtual_nics import is_host_nic
from utils.data_cleaning import clean_data
from utils.interface_encapsulation import ENCAPSULATION_KIND

_PHYSICAL_INTERFACE = "DcimPhysicalInterface"
_UPLINK_ROLE = "uplink"
_PEERING = "CloudPeeringInterface"
_PROVIDER_CONNECT = "TopologyProviderConnect"


def _label(node: dict[str, Any]) -> str:
    return str(node.get("name") or node.get("id") or "<unnamed>")


def interface_parent_errors(iface: dict[str, Any], parent: dict[str, Any] | None) -> list[str]:
    """The messages (rules 1, 2, 3, 5) of one cleaned interface of interface_parents.gql and the parent it rides on."""
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
        encapsulations = clean_data(data).get(ENCAPSULATION_KIND) or []
        per_interface = Counter(
            iface.get("id") for encapsulation in encapsulations for iface in encapsulation.get("capabilities") or []
        )
        reported: set[str] = set()
        for encapsulation in encapsulations:
            parent = encapsulation.get("parent")
            ifaces = encapsulation.get("capabilities") or []
            if not ifaces:
                self.log_warning(f"encapsulation '{_label(encapsulation)}' is on no interface: it was left behind.")
            if len(ifaces) > 1:
                self.log_error(
                    message=f"encapsulation '{_label(encapsulation)}' is on {len(ifaces)} interfaces: it is one "
                    "interface's.",
                    object_id=encapsulation.get("id"),
                    object_type=ENCAPSULATION_KIND,
                )
            for iface in ifaces:
                where = f"{_label(iface.get('device') or {})} {_label(iface)}"
                if per_interface[iface.get("id")] > 1 and iface.get("id") not in reported:
                    reported.add(iface.get("id"))
                    self.log_error(
                        message=f"{where} has {per_interface[iface.get('id')]} encapsulations: an interface rides "
                        "on one.",
                        object_id=iface.get("id"),
                        object_type=iface.get("typename") or ENCAPSULATION_KIND,
                    )
                if isinstance(parent, dict) and parent.get("id") == iface.get("id"):
                    self.log_error(
                        message=f"{where} is its own parent.",
                        object_id=iface.get("id"),
                        object_type=iface.get("typename") or ENCAPSULATION_KIND,
                    )
                    continue
                for message in interface_parent_errors(iface, parent):
                    self.log_error(
                        message=message,
                        object_id=iface.get("id"),
                        object_type=iface.get("typename") or ENCAPSULATION_KIND,
                    )
