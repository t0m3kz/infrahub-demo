"""Transform: ManagedController → vendor-native onboarding/inventory payload (JSON).

One query (`controller_payload`, queries/config/controller.gql) scoped to a single
controller by name, one artifact_definition targeting the `managed_controllers` group.
Infrahub itself resolves `controller_type`/`vendor` as extra query parameters straight
off the target object (see .infrahub.yml's parameters: controller_type__value /
platform__name__value) — the transform just dispatches on those, no per-controller
querying/matching needed in Python.
"""

import json
from typing import Any

from infrahub_sdk.transforms import InfrahubTransform

from utils.data_cleaning import clean_data


def _device_ip(device: dict[str, Any]) -> str | None:
    address = (device.get("primary_address") or {}).get("address")
    return address.split("/")[0] if address else None


def _build_apic(controller: dict[str, Any]) -> dict[str, Any]:
    nodes = [
        {
            "nodeId": idx + 1,
            "name": device.get("name"),
            "role": "spine" if "spine" in (device.get("role") or "") else "leaf",
            "podId": 1,
            "oobMgmtAddr": _device_ip(device),
        }
        for idx, device in enumerate(controller.get("managed_devices") or [])
    ]
    return {"fabricName": controller.get("name"), "nodes": nodes}


def _build_dcnm(controller: dict[str, Any]) -> dict[str, Any]:
    switches = [
        {
            "switchName": device.get("name"),
            "role": device.get("role"),
            "serialNumber": device.get("id"),
            "ipAddress": _device_ip(device),
        }
        for device in controller.get("managed_devices") or []
    ]
    return {"fabric": controller.get("name"), "switches": switches}


def _build_panorama(controller: dict[str, Any]) -> dict[str, Any]:
    devices = [
        {
            "hostname": device.get("name"),
            "ip-address": _device_ip(device),
            "serial": device.get("id"),
            "status": device.get("status"),
        }
        for device in controller.get("managed_devices") or []
    ]
    return {"device-group": controller.get("name"), "devices": devices}


def _build_security_director(controller: dict[str, Any]) -> dict[str, Any]:
    devices = [
        {
            "name": device.get("name"),
            "managementIp": _device_ip(device),
            "status": device.get("status"),
        }
        for device in controller.get("managed_devices") or []
    ]
    return {"domain": controller.get("name"), "devices": devices}


def _build_checkpoint_sms(controller: dict[str, Any]) -> dict[str, Any]:
    gateways = [
        {"name": device.get("name"), "ipv4-address": _device_ip(device)}
        for device in controller.get("managed_devices") or []
    ]
    return {"sms": controller.get("name"), "gateways": gateways}


def _build_big_iq(controller: dict[str, Any]) -> dict[str, Any]:
    devices = [
        {"hostname": device.get("name"), "address": _device_ip(device)}
        for device in controller.get("managed_devices") or []
    ]
    return {"deviceGroup": controller.get("name"), "devices": devices}


def _build_netscaler_adm(controller: dict[str, Any]) -> dict[str, Any]:
    instances = [
        {"name": device.get("name"), "ip_address": _device_ip(device), "instance_state": device.get("status")}
        for device in controller.get("managed_devices") or []
    ]
    return {"profile": controller.get("name"), "instances": instances}


# (controller_type, vendor) -> payload builder. `vendor` is the controller's own
# platform name — None means "any platform" (fabric controller_types aren't tied to
# one vendor platform the way firewall/lb managers are).
_BUILDERS: dict[tuple[str, str | None], Any] = {
    ("aci_apic", None): _build_apic,
    ("dcnm", None): _build_dcnm,
    ("security_manager", "panos"): _build_panorama,
    ("security_manager", "junos"): _build_security_director,
    ("security_manager", "checkpoint_gaia"): _build_checkpoint_sms,
    ("lb_manager", "f5_tmos"): _build_big_iq,
    ("lb_manager", "netscaler"): _build_netscaler_adm,
}


class ControllerPayload(InfrahubTransform):
    """Dispatch to the right vendor payload builder for a ManagedController."""

    query = "controller_payload"

    async def transform(self, data: Any) -> str:
        cleaned = clean_data(data)
        controllers = cleaned.get("ManagedController") or []
        if not controllers:
            return json.dumps({})
        controller = controllers[0]

        controller_type = controller.get("controller_type")
        vendor = (controller.get("platform") or {}).get("name")
        builder = _BUILDERS.get((controller_type, vendor)) or _BUILDERS.get((controller_type, None))
        if builder is None:
            return json.dumps({})

        return json.dumps(builder(controller), indent=2)
