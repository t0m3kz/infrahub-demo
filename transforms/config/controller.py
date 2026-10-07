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

from transforms.helpers.addressing import host_ip
from transforms.helpers.bgp import get_bgp_profile
from utils.data_cleaning import clean_data


def _device_ip(device: dict[str, Any]) -> str | None:
    return host_ip((device.get("primary_address") or {}).get("address"))


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


def _vrf_handoffs(device: dict[str, Any]) -> list[dict[str, Any]]:
    """The device's eBGP handoffs into a tenant VRF, as VCO segment handoffs.

    Sessions come from the same helper the device-config transforms use
    (transforms/helpers/bgp.py), so both ends of a handoff agree on addresses
    and ASNs; only VRF sessions qualify (the local address sits in a non-default
    IpamNamespace). The VLAN is the handoff sub-interface's dot1q tag
    (`eth0.1900` → 1900); None when the interface is not a sub-interface.
    """
    handoffs: list[dict[str, Any]] = []
    device_name = device.get("name") or ""
    for bgp_config in get_bgp_profile(device.get("capabilities") or [], device_name=device_name):
        for session in bgp_config.get("sessions") or []:
            if not session.get("vrf"):
                continue
            interface = session.get("local_interface") or ""
            _, dot, tag = interface.rpartition(".")
            handoffs.append(
                {
                    "segment": session["vrf"],
                    "interface": interface or None,
                    "vlan": int(tag) if dot and tag.isdigit() else None,
                    "local-address": (session.get("local_ip") or {}).get("address"),
                    "bgp": {
                        "local-asn": (session.get("local_as_override") or session.get("local_as") or {}).get("asn"),
                        "neighbor-ip": host_ip((session.get("remote_ip") or {}).get("address")),
                        "neighbor-asn": (session.get("remote_as") or {}).get("asn"),
                        "neighbor": session.get("remote_device"),
                    },
                }
            )
    return handoffs


def _build_velocloud_vco(controller: dict[str, Any]) -> dict[str, Any]:
    """VCO payload — the Gateway plus every office Edge it manages
    (generators/topology/sdwan_edge.py's SdwanEdgeGenerator appends Edges to
    managed_devices; the Gateway itself is added by hand, see
    data/demos/30_all/08_interconnects/04_sdwan/01_gateway.yml).

    branch_to_branch is read per-edge from its own office
    (TopologyCustomerOffice.branch_to_branch, schemas/extensions/topology/
    topology_customer.yml) rather than once for the whole VCO — real
    VeloCloud business-policy profiles are assigned per edge, and different
    offices under the same VCO can want different mesh policies. Missing for
    the Gateway itself (it has no office/deployment carrying that field).

    handoffs are the device's VRF eBGP sessions (see _vrf_handoffs) — e.g. the
    Gateway's PROD handoff to the colocation BGW. The Orchestrator pushes that
    side, so no device-config transform here renders it.
    """
    edges = [
        {
            "hostname": device.get("name"),
            "ip-address": _device_ip(device),
            "role": device.get("role"),
            "status": device.get("status"),
            "branch-to-branch": ((device.get("deployment") or {}).get("branch_to_branch")),
            "handoffs": _vrf_handoffs(device),
        }
        for device in controller.get("managed_devices") or []
    ]
    return {"enterprise": controller.get("name"), "edges": edges}


def _build_dna_center(controller: dict[str, Any]) -> dict[str, Any]:
    devices = [
        {
            "hostname": device.get("name"),
            "role": device.get("role"),
            "managementIpAddress": _device_ip(device),
        }
        for device in controller.get("managed_devices") or []
    ]
    return {"siteName": controller.get("name"), "devices": devices}


# (controller_type, vendor) -> payload builder. `vendor` is the controller's own
# platform name — None means "any platform" (fabric controller_types aren't tied to
# one vendor platform the way firewall/lb managers are).
_BUILDERS: dict[tuple[str, str | None], Any] = {
    ("aci_apic", None): _build_apic,
    ("dcnm", None): _build_dcnm,
    ("dna_center", None): _build_dna_center,
    ("security_manager", "panos"): _build_panorama,
    ("security_manager", "junos"): _build_security_director,
    ("security_manager", "checkpoint_gaia"): _build_checkpoint_sms,
    ("lb_manager", "f5_tmos"): _build_big_iq,
    ("lb_manager", "netscaler"): _build_netscaler_adm,
    ("sdwan_orchestrator", "velocloud"): _build_velocloud_vco,
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
