"""Validate that the NICs of a virtual device ride on ports of the device hosting it.

A virtual device has no cable of its own; its base NICs (a DcimVirtualInterface with the role management, uplink or
ha, which generators/virtual_nics.py pins) point with ``parent_interface`` to a port of the hosting physical device,
and that port carries the cable. The border leaf -> host port -> NIC path is therefore a path in the graph. The
schema cannot say that the parent is a port of the hosting device, nor that a pinned uplink is on a cabled port:

* the parent of a NIC is a port of the device's ``hosting_device``;
* the host port an uplink NIC is pinned to is cabled (otherwise nothing reaches the border leaf through it).

A NIC without a parent is not reported: a machine can have more uplinks than its host. A device without a
``hosting_device`` is not reported either (the field is optional).
"""

from __future__ import annotations

from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from generators.virtual_nics import PINNED_ROLES
from utils.data_cleaning import clean_data

_VIRTUAL_INTERFACE = "DcimVirtualInterface"
_UPLINK_ROLE = "uplink"


def _label(node: dict[str, Any]) -> str:
    return str(node.get("name") or node.get("id") or "<unnamed>")


class CheckVirtualNicPinning(InfrahubCheck):
    """A pinned NIC of a virtual device rides on a port of its hosting device, and an uplink on a cabled one."""

    query = "virtual_nic_pinning"

    def validate(self, data: Any) -> None:
        cleaned = clean_data(data)
        for device in cleaned.get("DcimVirtualDevice") or []:
            host = device.get("hosting_device")
            if not isinstance(host, dict):
                continue
            for nic in device.get("interfaces") or []:
                if nic.get("typename") != _VIRTUAL_INTERFACE or nic.get("role") not in PINNED_ROLES:
                    continue
                parent = nic.get("parent_interface")
                if not isinstance(parent, dict):
                    continue
                parent_device = parent.get("device") or {}
                if parent_device.get("id") != host.get("id"):
                    self.log_error(
                        message=(
                            f"{_label(device)} {_label(nic)} is pinned to {_label(parent)} of "
                            f"'{_label(parent_device)}', not of its hosting device '{_label(host)}'."
                        ),
                        object_id=nic.get("id"),
                        object_type=_VIRTUAL_INTERFACE,
                    )
                elif nic.get("role") == _UPLINK_ROLE and not parent.get("cable"):
                    self.log_error(
                        message=(
                            f"{_label(device)} {_label(nic)} is pinned to {_label(parent)} of '{_label(host)}', "
                            f"which is not cabled: no border port reaches the virtual device through it."
                        ),
                        object_id=nic.get("id"),
                        object_type=_VIRTUAL_INTERFACE,
                    )
