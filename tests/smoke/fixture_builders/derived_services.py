"""The nested service data a fixture device's config is derived from, built from what the builders declare.

A builder says WHICH service contexts a border port faces, which contexts the firewalls of the deployment hold, with
which legs, and (through the VRF hook) which SVIs a switch has. The model stores none of it as an interface edge any
more: a transform derives the legs of a context from its member's interfaces, the contexts behind a border port from
the cable of the port and the steered SVIs from the segment rows (utils/service_contexts.py, utils/device_services.py).
So this hook rewrites the declared shape into the shape the queries return, in the RAW GraphQL form:

- a border port declared with context capabilities becomes a ``service_ports`` entry of a ``DeviceServices`` root: the
  port's cable, whose far end is the uplink of a member holding the contexts and their legs;
- the contexts of the devices of a deployment (the firewalls the leaf's PBR redirects to) become members with their
  legs as interfaces;
- the legs a firewall or load balancer lists on a context are already its own interfaces: the declaration is dropped;
- the pbr policy of an SVI is dropped, and every routed segment of a leaf gets a row whose footprint has a serving
  context (the fixtures steer all their SVIs, the transform still applies its own conditions).

Idempotent: a response that already has the ``DeviceServices`` root is left as it is.
"""

from __future__ import annotations

import copy
from typing import Any

from tests.smoke.fixture_builders.vrf import routing_zone
from tests.unit.vrf_instances_helpers import raw_footprint, raw_row
from transforms.common import BaseDeviceTransform
from transforms.helpers.segments import routed_activations
from utils.exchange_transit import (
    BORDER_ROLE_FOR_SERVICES,
    FIREWALL_CONTEXT_KIND,
    INTERNAL_FIREWALL_ROLE,
    SERVICE_CONTEXT_ROLES,
    TENANT_NAMESPACE_TYPES,
    TRANSIT_SLOT,
    transit_vlan,
)
from utils.routing_domain import VRF_KIND
from utils.segment_addressing import HOSTING_KINDS, routed_segment_namespace
from utils.service_contexts import DEVICE_SERVICES_ROOT, SERVICE_PORTS_KEY
from utils.vrf_demand import PARTY_DEPLOYMENT_KINDS

FIXTURE_DC = "fixture-dc"
UPLINK = "ethernet1/1"
_PHYSICAL = "DcimPhysicalInterface"
_VIRTUAL = "DcimVirtualInterface"
_MEMBER_ROLE = {role: member for member, role in BORDER_ROLE_FOR_SERVICES.items()}
_MEMBER_ROLE["firewall"] = INTERNAL_FIREWALL_ROLE


def _value(field: Any) -> Any:
    return field.get("value") if isinstance(field, dict) and "value" in field else field


def _nodes(relationship: Any) -> list[dict[str, Any]]:
    edges = relationship.get("edges") if isinstance(relationship, dict) else None
    return [edge["node"] for edge in edges or [] if isinstance(edge, dict) and isinstance(edge.get("node"), dict)]


def _edges(*nodes: dict[str, Any]) -> dict[str, Any]:
    return {"edges": [{"node": node} for node in nodes]}


def _is_context(node: dict[str, Any]) -> bool:
    return node.get("__typename") in SERVICE_CONTEXT_ROLES


def _leg_namespace(leg: dict[str, Any]) -> dict[str, Any]:
    """The security zone of a leg's VRF (the ManagedVRF among its ``zone_capabilities``), raw or already cleaned."""
    capabilities = leg.get("zone_capabilities") or []
    nodes = _nodes(capabilities) if isinstance(capabilities, dict) else capabilities
    vrf = next((node for node in nodes if (node.get("__typename") or node.get("typename")) == VRF_KIND), {})
    zone = vrf.get("security_zone") or {}
    return zone.get("node", zone) or {}


def _leg_ns_type(leg: dict[str, Any]) -> str:
    ns_type = _value(_leg_namespace(leg).get("vrf_type"))
    return ns_type if ns_type in TRANSIT_SLOT else "prod"


def _environment(legs: list[dict[str, Any]]) -> str:
    """The environment of the footprints a context serves: the tenant type of its legs (``p`` is production)."""
    types = [t for t in (_leg_ns_type(leg) for leg in legs) if t in TENANT_NAMESPACE_TYPES]
    return "d" if types and types[0] != "prod" else "p"


def _member_context(context: dict[str, Any], legs: list[dict[str, Any]]) -> dict[str, Any]:
    """The context as a member lists it under ``shared_capabilities`` (the fields of ServiceContextFields)."""
    kind = context["__typename"]
    environment = {"value": _environment(legs)}
    tenant = (context.get("tenant") or {}).get("node")
    served = [{"id": node["id"], "environment": environment} for node in _nodes(context.get("served_deployments"))]
    return {
        "__typename": kind,
        "id": context["id"],
        "name": context["name"],
        "vlan_id": context.get("vlan_id"),
        "tenant": {"node": {"id": tenant["id"], "environment": environment}} if tenant else {"node": None},
        "fw_served" if kind == FIREWALL_CONTEXT_KIND else "lb_served": _edges(*served),
    }


def _leg_interface(leg: dict[str, Any], vlan: int | None, index: int, member_id: str) -> dict[str, Any] | None:
    """The leg as a sub-interface of its member's uplink tagged with the transit VLAN (None: no VLAN, no leg)."""
    if not isinstance(vlan, int):
        return None
    node = {key: leg[key] for key in ("ip_address", "zone_capabilities") if key in leg}
    tag = transit_vlan(vlan, _leg_ns_type(leg))
    return {
        "__typename": _VIRTUAL,
        "id": f"{leg.get('name', {}).get('value', 'leg')}-{index}",
        **node,
        "name": {"value": f"{UPLINK}.{tag}"},
        "role": {"value": "service"},
        "vlan_id": {"value": tag},
        "parent_interface": {"node": {"name": {"value": UPLINK}, "device": {"node": {"id": member_id}}}},
    }


def _members(contexts: list[dict[str, Any]], role: str, prefix: str) -> list[dict[str, Any]]:
    """The member devices that hold ``contexts``: one per device name found on their stored legs, each with its legs."""
    legs_by_member: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    for context in contexts:
        for leg in _nodes(context.get("capabilities")):
            device = (leg.get("device") or {}).get("node") or {}
            if _value(device.get("role")) != role or not _value(device.get("name")):
                continue
            legs_by_member.setdefault(str(_value(device["name"])), []).append((context, leg))
    names = list(legs_by_member) or [f"{prefix}-fw"]
    members = []
    for name in names:
        interfaces: dict[str, dict[str, Any]] = {}
        for index, (context, leg) in enumerate(legs_by_member.get(name, [])):
            iface = _leg_interface(leg, _value(context.get("vlan_id")), index, f"member-{name}")
            if iface is not None:
                interfaces.setdefault(iface["name"]["value"], iface)
        members.append(
            {
                "__typename": "DcimPhysicalDevice",
                "id": f"member-{name}",
                "name": {"value": name},
                "role": {"value": role},
                "shared_capabilities": _edges(
                    *(
                        _member_context(c, [leg for ctx, leg in legs_by_member.get(name, []) if ctx is c])
                        for c in contexts
                    )
                ),
                "interfaces": _edges(
                    {
                        "__typename": _PHYSICAL,
                        "id": f"{name}:{UPLINK}",
                        "name": {"value": UPLINK},
                        "role": {"value": "uplink"},
                    },
                    *interfaces.values(),
                ),
            }
        )
    return members


def _strip_declared(interface: dict[str, Any]) -> list[dict[str, Any]]:
    """Drop the contexts and pbr policies an interface declares from its ``capabilities``; return the contexts."""
    kept, contexts = [], []
    for node in _nodes(interface.get("capabilities")):
        if _is_context(node):
            contexts.append(node)
        elif node.get("__typename") != "RoutingPolicy":
            kept.append(node)
    if "capabilities" in interface:
        interface["capabilities"] = _edges(*kept)
    return contexts


def _service_port(interface: dict[str, Any], contexts: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The border port cabled to the uplink of a member of the contexts its role faces."""
    role = _value(interface.get("role"))
    wanted = [c for c in contexts if SERVICE_CONTEXT_ROLES[c["__typename"]] == role]
    if not wanted:
        return None
    members = _members(wanted, _MEMBER_ROLE[role], _value(interface["name"]))
    member = members[0]
    far = {
        "__typename": _PHYSICAL,
        "id": member["interfaces"]["edges"][0]["node"]["id"],
        "name": {"value": UPLINK},
        "role": {"value": "uplink"},
        "device": {"node": member},
        "sub_interfaces": _edges(),
    }
    own = {
        "__typename": _PHYSICAL,
        "id": f"port-{_value(interface['name'])}",
        "name": interface["name"],
        "role": interface["role"],
    }
    return {**own, "cable": {"node": {"id": f"cable-{_value(interface['name'])}", "endpoints": _edges(own, far)}}}


def _rewrite_deployment_devices(hosting: Any) -> None:
    """The firewalls a hosting deployment lists: their contexts' stored legs become the members' interfaces."""
    if not isinstance(hosting, dict):
        return
    node = hosting.get("node", hosting)
    if not isinstance(node, dict):
        return
    devices = node.get("devices")
    if devices:
        rebuilt = []
        for device in _nodes(devices):
            contexts = [
                c
                for c in _nodes(device.get("shared_capabilities"))
                if c.get("__typename") == FIREWALL_CONTEXT_KIND and "capabilities" in c
            ]
            if contexts:
                rebuilt.extend(_members(contexts, INTERNAL_FIREWALL_ROLE, "deployment"))
            else:
                rebuilt.append(device)
        node["devices"] = _edges(*rebuilt)
    parent = node.get("parent")
    if isinstance(parent, dict):
        _rewrite_deployment_devices(parent)


def _own_contexts(device: dict[str, Any]) -> None:
    """A firewall or load balancer holds its legs as its own interfaces: the stored declaration on them is dropped."""
    interfaces = _nodes(device.get("interfaces"))
    own = {_value(i.get("name")): i for i in interfaces}
    for context in _nodes(device.get("shared_capabilities")):
        if not _is_context(context):
            continue
        for leg in _nodes(context.get("capabilities")):
            owner = _value(((leg.get("device") or {}).get("node") or {}).get("name"))
            iface = own.get(_value(leg.get("name")))
            if iface is not None and owner == _value(device.get("name")):
                for key in ("ip_address", "zone_capabilities", "vlan_id", "parent_interface"):
                    if key in leg and not iface.get(key):
                        iface[key] = leg[key]
        context.pop("capabilities", None)
    for interface in interfaces:
        _strip_declared(interface)
    names = {_value(i.get("name")) for i in interfaces}
    trunks = {n.rsplit(".", 1)[0] for n in names if isinstance(n, str) and "." in n and n.rsplit(".", 1)[1].isdigit()}
    if device.get("shared_capabilities") and trunks and not any(_value(i.get("role")) == "uplink" for i in interfaces):
        # The trunk the legs ride on exists on the member (the declaration named only the legs).
        trunk = sorted(trunks)[0]
        device["interfaces"]["edges"].append(
            {
                "node": {
                    "__typename": _PHYSICAL,
                    "name": {"value": trunk},
                    "role": {"value": "uplink"},
                    "description": {"value": None},
                    "status": {"value": "active"},
                }
            }
        )


def _services_deployment(device: dict[str, Any]) -> dict[str, Any]:
    """The hosting of the services root: the fixture DC, as a pod's parent unless the device sits in a party's place."""
    kind = ((device.get("deployment") or {}).get("node") or {}).get("__typename")
    if kind in PARTY_DEPLOYMENT_KINDS or kind in HOSTING_KINDS:
        return {"node": {"__typename": kind, "id": FIXTURE_DC, "name": {"value": FIXTURE_DC}}}
    return {
        "node": {
            "__typename": "TopologyPod",
            "id": "fixture-pod",
            "name": {"value": "fixture-pod"},
            "parent": {"node": {"__typename": "TopologyDataCenter", "id": FIXTURE_DC, "name": {"value": FIXTURE_DC}}},
        }
    }


def _rows(activations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One routed row per routed segment activation of a leaf, its footprint standing in the fixture DC with a context."""
    rows, seen = [], set()
    for act in routed_activations(activations):
        vlan, segment = act.get("vlan_id"), act.get("segment") or {}
        namespace = routed_segment_namespace(segment).get("name")
        if not vlan or vlan in seen or not namespace or act.get("transit_context"):
            continue
        seen.add(vlan)
        rows.append(raw_row(f"fixture-row-{vlan}", vlan, namespace, str(segment.get("name") or vlan)))
        row = rows[-1]["segment_deployment"]["node"]["segment"]["node"]
        row["customer_deployments"] = _edges(raw_footprint("fixture-footprint", FIXTURE_DC))
        row["security_zone"] = {"node": routing_zone(namespace)}
    return rows


def with_device_services(transform: BaseDeviceTransform, data: dict[str, Any]) -> dict[str, Any]:
    """A copy of the raw device query response ``data`` with the nested data its services derive from (see the module).

    A response that already has the ``DeviceServices`` root keeps it: only what it still declares is added (nothing, for
    a persisted fixture) and the rows are derived again from the device's segments."""
    result = copy.deepcopy(data)
    first = next((value for key, value in result.items() if key != DEVICE_SERVICES_ROOT), None)
    if not isinstance(first, dict) or not first.get("edges"):
        return result
    device = first["edges"][0]["node"]
    ports = []
    for interface in _nodes(device.get("interfaces")):
        contexts = _strip_declared(interface)
        if _value(interface.get("role")) in SERVICE_CONTEXT_ROLES.values() and contexts:
            port = _service_port(interface, contexts)
            if port is not None:
                ports.append(port)
    _rewrite_deployment_devices((device.get("deployment") or {}))
    _own_contexts(device)
    if DEVICE_SERVICES_ROOT in result:
        services = result[DEVICE_SERVICES_ROOT]["edges"][0]["node"]
        services[SERVICE_PORTS_KEY]["edges"].extend({"node": port} for port in ports)
    else:
        services = {
            "__typename": device.get("__typename", "DcimPhysicalDevice"),
            "id": device.get("id", "fixture-device"),
            "name": device["name"],
            "role": device.get("role", {"value": ""}),
            "member_of_groups": _edges(),
            "deployment": _services_deployment(device),
            SERVICE_PORTS_KEY: _edges(*ports),
            "vlans": _edges(),
        }
        result[DEVICE_SERVICES_ROOT] = _edges(services)
    prepared, _, platform = transform.prepared_device(copy.deepcopy(result))
    if platform:
        services["vlans"] = _edges(*_rows(prepared.get("segment_deployments") or []))
    return result
