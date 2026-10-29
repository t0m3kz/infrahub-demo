"""The pure readers and judges of test_66's segment lifecycle (test_04), split out so unit tests import them.

They read the raw GraphQL results of ``_HOST_CABLES_QUERY`` and ``_SWITCH_VRF_QUERY`` and say why the switches are not
in the state the test expects. No stack, no fixtures: tests/unit/test_integration_circuit_lifecycle.py runs them on
canned payloads.
"""

from __future__ import annotations

from typing import Any

SEGMENT_NAME = "infra-core-dns-ntp-p"
SEGMENT_NAMESPACE = "MANAGEMENT"
SVI_ROLE = "svi"

_HOST_CABLES_QUERY = """
query ($hosts: [String]) {
  DcimPhysicalInterface(device__name__values: $hosts) {
    edges {
      node {
        device { node { name { value } } }
        cable {
          node {
            endpoints {
              edges { node { ... on DcimPhysicalInterface { device { node { name { value } } } } } }
            }
          }
        }
      }
    }
  }
}
"""

_SWITCH_VRF_QUERY = """
query ($devices: [String]) {
  ManagedVRF {
    edges {
      node {
        id
        name { value }
        namespace { node { name { value } } }
        devices { edges { node { name { value } } } }
      }
    }
  }
  DcimVirtualInterface(device__name__values: $devices) {
    edges {
      node {
        id
        name { value }
        role { value }
        device { node { name { value } } }
        ip_address { node { ip_namespace { node { name { value } } } } }
        vrf { node { id namespace { node { name { value } } } } }
        vlan_domain_segment {
          node { segment_deployment { node { segment { node { ... on ManagedVxlanSegment { name { value } } } } } } }
        }
      }
    }
  }
  DcimPhysicalInterface(device__name__values: $devices) {
    edges { node { name { value } device { node { name { value } } } ip_address { node { ip_namespace { node { name { value } } } } } } }
  }
}
"""


def _edges(result: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return [edge["node"] for edge in (result.get(kind) or {}).get("edges", [])]


def _value(node: dict[str, Any], attribute: str) -> Any:
    return (node.get(attribute) or {}).get("value")


def _device_name(node: dict[str, Any]) -> str:
    return str((((node.get("device") or {}).get("node") or {}).get("name") or {}).get("value"))


def _namespace_name(holder: dict[str, Any] | None, key: str) -> str | None:
    namespace = (((holder or {}).get(key) or {}).get("node") or {}).get("name") or {}
    return namespace.get("value")


def _address_namespace(node: dict[str, Any]) -> str | None:
    """The IPAM namespace of the address on an interface node (``ip_address.node.ip_namespace.node.name``)."""
    address = (node.get("ip_address") or {}).get("node") or {}
    return _namespace_name(address, "ip_namespace")


def switches_of_host(result: dict[str, Any], host: str) -> set[str]:
    """The switches at the far end of the cables of ``host``'s ports (a ``_HOST_CABLES_QUERY`` result)."""
    switches: set[str] = set()
    for port in _edges(result, "DcimPhysicalInterface"):
        if _device_name(port) != host:
            continue
        endpoints = (((port.get("cable") or {}).get("node") or {}).get("endpoints") or {}).get("edges", [])
        switches |= {_device_name(edge["node"]) for edge in endpoints if edge["node"]} - {host, "None"}
    return switches


def parse_switch_vrf_state(result: dict[str, Any], devices: list[str]) -> dict[str, dict[str, Any]]:
    """device -> {vrf: id of the instance of the segment's namespace it is a member of or None, svis: {name: {id, vrf}}
    of the segment's SVIs, foreign: what else on the device demands the namespace (an SVI of another segment, an
    address on another interface)}."""
    state: dict[str, dict[str, Any]] = {device: {"vrf": None, "svis": {}, "foreign": []} for device in devices}
    for node in _edges(result, "ManagedVRF"):
        if _namespace_name(node, "namespace") != SEGMENT_NAMESPACE:
            continue
        for edge in (node.get("devices") or {}).get("edges", []):
            device = str(_value(edge["node"], "name"))
            if device in state:
                state[device]["vrf"] = node["id"]
    for node in _edges(result, "DcimVirtualInterface"):
        device, name = _device_name(node), str(_value(node, "name"))
        vrf = (node.get("vrf") or {}).get("node") or {}
        if _value(node, "role") == SVI_ROLE:
            row = (node.get("vlan_domain_segment") or {}).get("node") or {}
            deployment = (row.get("segment_deployment") or {}).get("node") or {}
            segment = ((deployment.get("segment") or {}).get("node") or {}).get("name") or {}
            if segment.get("value") == SEGMENT_NAME:
                state[device]["svis"][name] = {"id": node["id"], "vrf": vrf.get("id")}
                continue
        if SEGMENT_NAMESPACE in (_address_namespace(node), _namespace_name(vrf, "namespace")):
            state[device]["foreign"].append(f"{name} (virtual)")
    for node in _edges(result, "DcimPhysicalInterface"):
        if _address_namespace(node) == SEGMENT_NAMESPACE:
            state[_device_name(node)]["foreign"].append(f"{_value(node, 'name')} (physical)")
    return state


def precondition_errors(state: dict[str, dict[str, Any]]) -> list[str]:
    """Why the segment is not the only demand of the namespace on these switches (it must have SVIs there and the
    switches must be members of one instance of the namespace, their DC's; nothing else may route in the namespace)."""
    errors: list[str] = []
    if len({facts["vrf"] for facts in state.values()}) > 1:
        errors.append(f"the switches are not members of one {SEGMENT_NAMESPACE} instance: {state}")
    for device, facts in sorted(state.items()):
        if facts["vrf"] is None:
            errors.append(f"{device} is a member of no {SEGMENT_NAMESPACE} instance")
        if not facts["svis"]:
            errors.append(f"{device} has no SVI of {SEGMENT_NAME}")
        elif any(svi["vrf"] != facts["vrf"] for svi in facts["svis"].values()):
            errors.append(f"{device}: an SVI of {SEGMENT_NAME} is not in the instance {facts['vrf']}")
        if facts["foreign"]:
            errors.append(f"{device} has other {SEGMENT_NAMESPACE} demand: {', '.join(facts['foreign'])}")
    return errors


def svis_gone_errors(state: dict[str, dict[str, Any]]) -> list[str]:
    """Why a switch still has SVIs of the segment."""
    return [f"{device} still has SVI(s) {sorted(f['svis'])}" for device, f in sorted(state.items()) if f["svis"]]


def vrf_gone_errors(state: dict[str, dict[str, Any]]) -> list[str]:
    """Why a switch is still a member of the instance (or has SVIs: the membership goes after them)."""
    return svis_gone_errors(state) + [
        f"{device} is still a member of {f['vrf']}" for device, f in sorted(state.items()) if f["vrf"]
    ]


def back_errors(state: dict[str, dict[str, Any]]) -> list[str]:
    """Why the switches are not members again, with the segment's SVIs in the instance."""
    return precondition_errors(state)


def sole_demand_instance_errors(vrfs: dict[str, Any], device: str, peer: str) -> list[str]:
    """Why ``device``'s instance cannot show that a member leaves while the instance stays: ``device`` and ``peer`` must
    both be members of the one instance (``parse_vrfs`` result) of the circuit's namespace."""
    name = vrfs["memberships"].get(device)
    if name is None:
        return [f"{device} is a member of no instance of the circuit's namespace"]
    members = vrfs["all"][name]["devices"]
    missing = sorted({device, peer} - members)
    if len(members) < 2 or missing:
        return [f"instance {name} has members {sorted(members)}: it needs at least {device} and {peer}"]
    return []
