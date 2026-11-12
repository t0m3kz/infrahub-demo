"""A DcimVirtualDevice is deployed to a data center or a customer footprint; an unset deployment is not reported."""

from __future__ import annotations

from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from utils.data_cleaning import clean_data

VIRTUAL_DEVICE = "DcimVirtualDevice"

ALLOWED_DEPLOYMENT_KINDS: dict[str, tuple[str, ...]] = {
    VIRTUAL_DEVICE: ("TopologyDataCenter", "TopologyCustomerDC", "TopologyCustomerColocation"),
}
"""The deployment kinds each device kind may be deployed to."""

_HINT = {VIRTUAL_DEVICE: "A function hosted by a provider is a CloudNetworkAppliance, not a virtual device."}


def _label(node: dict[str, Any]) -> str:
    return str(node.get("name") or node.get("id") or "<unnamed>")


class CheckVirtualDevicePlacement(InfrahubCheck):
    """A DcimVirtualDevice is deployed to a data center or customer footprint."""

    query = "virtual_device_placement"

    def validate(self, data: Any) -> None:
        cleaned = clean_data(data)
        for kind, allowed in ALLOWED_DEPLOYMENT_KINDS.items():
            for device in cleaned.get(kind) or []:
                deployment = device.get("deployment")
                if not isinstance(deployment, dict) or deployment.get("typename") in allowed:
                    continue
                self.log_error(
                    message=(
                        f"{kind} '{_label(device)}' is deployed to "
                        f"{deployment.get('typename') or 'a deployment of unknown kind'} '{_label(deployment)}': "
                        f"a {kind} is deployed to one of {', '.join(allowed)}. {_HINT[kind]}"
                    ),
                    object_id=device.get("id"),
                    object_type=kind,
                )
