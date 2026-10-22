"""Validate where a virtual device and a network appliance are deployed.

* A DcimVirtualDevice is our own virtual machine or function on our own hosts. Its ``deployment`` is the data center
  or the customer footprint it runs for (the footprints carry the environment class and the hosting scope that
  checks/hypervisor_environment.py and checks/segment_placement.py rely on): TopologyDataCenter, TopologyCustomerDC or
  TopologyCustomerColocation. A virtual device deployed to a colocation zone, a virtual colocation or any other
  deployment is the old modelling of a provider-hosted function.
* A CloudNetworkAppliance is a network function a provider hosts (Equinix Network Edge, Megaport MVE). Its
  ``deployment`` is the TopologyVirtualColocation it is rented in and nothing else. The schema cannot say that: the
  relationship has the peer of every other device kind (TopologyDeployment) so one query selects ``deployment`` across
  the device kinds, GraphQL refusing sibling fragments whose field types differ.

An unset ``deployment`` is not reported: the field is optional.
"""

from __future__ import annotations

from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from utils.data_cleaning import clean_data

VIRTUAL_DEVICE = "DcimVirtualDevice"
APPLIANCE = "CloudNetworkAppliance"

ALLOWED_DEPLOYMENT_KINDS: dict[str, tuple[str, ...]] = {
    VIRTUAL_DEVICE: ("TopologyDataCenter", "TopologyCustomerDC", "TopologyCustomerColocation"),
    APPLIANCE: ("TopologyVirtualColocation",),
}
"""The deployment kinds each device kind may be deployed to."""

_HINT = {
    VIRTUAL_DEVICE: "A function hosted by a provider is a CloudNetworkAppliance, not a virtual device.",
    APPLIANCE: "A virtual machine of ours is a DcimVirtualDevice.",
}


def _label(node: dict[str, Any]) -> str:
    return str(node.get("name") or node.get("id") or "<unnamed>")


class CheckVirtualDevicePlacement(InfrahubCheck):
    """A DcimVirtualDevice is deployed to a data center or customer footprint, a CloudNetworkAppliance to a virtual colocation."""

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
