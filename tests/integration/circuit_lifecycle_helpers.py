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
L3VNI_ROLE = "l3vni"

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

# An SVI has no link to its row: the segment's SVIs are the role-svi interfaces that carry its anycast gateway.
_SWITCH_VRF_QUERY = """
query ($devices: [String]) {
  ManagedVxlanSegment(name__value: "SEGMENT_NAME") {
    edges { node { gateway { node { id } } } }
  }
  ManagedVRF {
    edges {
      node {
        id
        name { value }
        security_zone { node { name { value } } }
        shared_devices { edges { node { name { value } } } }
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
        ip_address { node { id ip_namespace { node { name { value } } } } }
        zone_capabilities: capabilities {
          edges { node { __typename ... on ManagedVRF { id security_zone { node { name { value } } } } } }
        }
      }
    }
  }
  DcimPhysicalInterface(device__name__values: $devices) {
    edges { node { name { value } device { node { name { value } } } ip_address { node { ip_namespace { node { name { value } } } } } } }
  }
}
""".replace("SEGMENT_NAME", SEGMENT_NAME)


def _edges(result: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return [edge["node"] for edge in (result.get(kind) or {}).get("edges", [])]


def _value(node: dict[str, Any], attribute: str) -> Any:
    return (node.get(attribute) or {}).get("value")


def _device_name(node: dict[str, Any]) -> str:
    return str((((node.get("device") or {}).get("node") or {}).get("name") or {}).get("value"))


def interface_vrf_node(node: dict[str, Any]) -> dict[str, Any]:
    """The raw ManagedVRF among an interface's ``zone_capabilities`` (its VRF instance), ``{}`` when it is in none."""
    for edge in (node.get("zone_capabilities") or {}).get("edges") or []:
        if edge["node"].get("__typename") == "ManagedVRF":
            return edge["node"]
    return {}


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
    address on another interface; not the instance's own role-l3vni SVI)}."""
    state: dict[str, dict[str, Any]] = {device: {"vrf": None, "svis": {}, "foreign": []} for device in devices}
    gateways = {
        gateway["id"]
        for node in _edges(result, "ManagedVxlanSegment")
        if (gateway := (node.get("gateway") or {}).get("node") or {}).get("id")
    }
    for node in _edges(result, "ManagedVRF"):
        if _namespace_name(node, "security_zone") != SEGMENT_NAMESPACE:
            continue
        for edge in (node.get("shared_devices") or {}).get("edges", []):
            device = str(_value(edge["node"], "name"))
            if device in state:
                state[device]["vrf"] = node["id"]
    for node in _edges(result, "DcimVirtualInterface"):
        device, name = _device_name(node), str(_value(node, "name"))
        vrf = interface_vrf_node(node)
        if (
            _value(node, "role") == SVI_ROLE
            and ((node.get("ip_address") or {}).get("node") or {}).get("id") in gateways
        ):
            state[device]["svis"][name] = {"id": node["id"], "vrf": vrf.get("id")}
            continue
        if _value(node, "role") == L3VNI_ROLE:
            # The instance's own L3 VNI SVI (VRF pass, VlanNNNN): it exists because the device is a member, it is
            # not demand of another segment, and it goes with the membership.
            continue
        if SEGMENT_NAMESPACE in (_address_namespace(node), _namespace_name(vrf, "security_zone")):
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
