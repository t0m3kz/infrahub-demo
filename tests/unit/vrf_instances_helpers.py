"""Raw GraphQL builders for queries/validation/vrf_instances.gql (edges/node/value, ``__typename``)."""

from __future__ import annotations

from typing import Any

from utils.data_cleaning import clean_data
from utils.segment_addressing import HOSTING_KINDS
from utils.vrf_demand import VRF_KIND, DeviceVrfFacts, device_vrf_facts, instance_name

HOSTING_ID = "dc-1"
HOSTING_NAME = "DC1"
HOSTING_KIND = "TopologyDataCenter"


def edges(*nodes: dict[str, Any]) -> dict[str, Any]:
    """A cardinality-many relationship."""
    return {"edges": [{"node": node} for node in nodes]}


def namespace_ref(name: str, namespace_type: str | None = None) -> dict[str, Any]:
    """An ``ip_namespace`` relationship."""
    node: dict[str, Any] = {"name": {"value": name}}
    if namespace_type:
        node["namespace_type"] = {"value": namespace_type}
    return {"node": node}


def address(addr: str, namespace: str, namespace_type: str | None = None) -> dict[str, Any]:
    """An ``ip_address`` relationship."""
    return {"node": {"address": {"value": addr}, "ip_namespace": namespace_ref(namespace, namespace_type)}}


def raw_row(
    row_id: str, vlan_id: int, namespace: str | None = "PROD", segment: str = "seg", inline: bool = False
) -> dict[str, Any]:
    """A ManagedVlanDomainSegment row; ``namespace`` None means a segment without a gateway (pure L2)."""
    seg: dict[str, Any] = {
        "__typename": "ManagedVxlanSegment",
        "id": f"seg-{segment}",
        "name": {"value": segment},
        "terminate_inline": {"value": inline},
        "gateway": None,
    }
    if namespace:
        seg["gateway"] = {
            "node": {
                "address": {"value": "10.0.0.1/24"},
                "ip_prefix": {"node": {"ip_namespace": namespace_ref(namespace)}},
            }
        }
    return {"id": row_id, "vlan_id": {"value": vlan_id}, "segment_deployment": {"node": {"segment": {"node": seg}}}}


def raw_instance(
    instance_id: str,
    device: str,
    namespace: str,
    l3_vni: int | None = 100,
    ns_type: str | None = "prod",
    deployment_id: str | None = HOSTING_ID,
    hosting_name: str = HOSTING_NAME,
) -> dict[str, Any]:
    """A ManagedVRF as the device's ``shared_capabilities`` membership (VrfDemandInstanceFields); ``device`` is unused and kept for
    the callers that pass the member's name. ``deployment_id`` None means an instance with no deployment node."""
    ns: dict[str, Any] = {"name": {"value": namespace}}
    ns["l3_vni"] = {"value": l3_vni}
    ns["namespace_type"] = {"value": ns_type}
    return {
        "__typename": VRF_KIND,
        "id": instance_id,
        "name": {"value": instance_name(hosting_name, namespace)},
        "deployment": {"node": {"id": deployment_id}} if deployment_id else None,
        "namespace": {"node": ns},
    }


def raw_root_instance(instance: dict[str, Any], *member_ids: str) -> dict[str, Any]:
    """A node of the ``ManagedVRF`` root: the membership shape plus its ``shared_devices``."""
    root = {key: value for key, value in instance.items() if key != "__typename"}
    root["shared_devices"] = edges(*({"id": member} for member in member_ids))
    return root


def raw_mlag(mlag_id: str, *rows: dict[str, Any]) -> dict[str, Any]:
    """A ManagedMLAG capability carrying the domain's rows."""
    return {"__typename": "ManagedMLAG", "id": mlag_id, "vlan_domain_segments": edges(*rows)}


def raw_interface(
    iface_id: str,
    name: str = "Ethernet1",
    role: str = "leaf",
    addr: dict[str, Any] | None = None,
    capabilities: list[dict[str, Any]] | None = None,
    typename: str = "DcimPhysicalInterface",
) -> dict[str, Any]:
    """An interface of the device."""
    node: dict[str, Any] = {
        "__typename": typename,
        "id": iface_id,
        "name": {"value": name},
        "role": {"value": role},
        "capabilities": edges(*(capabilities or [])),
    }
    if addr:
        node["ip_address"] = addr
    return node


def raw_svi(
    svi_id: str,
    vlan_id: int,
    row_id: str | None,
    vrf_id: str | None = None,
    vrf_namespace: str | None = None,
    addr: dict[str, Any] | None = None,
    capabilities: list[dict[str, Any]] | None = None,
    name: str | None = None,
    vrf_deployment_id: str | None = HOSTING_ID,
) -> dict[str, Any]:
    """A role-svi DcimVirtualInterface."""
    node = raw_interface(svi_id, name or f"Vlan{vlan_id}", "svi", addr, capabilities, "DcimVirtualInterface")
    node["vrf"] = (
        {
            "node": {
                "id": vrf_id,
                "deployment": {"node": {"id": vrf_deployment_id}},
                "namespace": namespace_ref(vrf_namespace or ""),
            }
        }
        if vrf_id
        else None
    )
    node["vlan_domain_segment"] = {"node": {"id": row_id}} if row_id else None
    return node


def raw_context(kind: str, vlan_id: int, *legs: tuple[str, str, str]) -> dict[str, Any]:
    """A service context capability; each leg is ``(member role, namespace, namespace type)``."""
    members = [
        {"device": {"node": {"role": {"value": role}}}, "ip_address": address("10.1.0.1/29", ns, ns_type)}
        for role, ns, ns_type in legs
    ]
    return {
        "__typename": kind,
        "id": f"ctx-{kind}-{vlan_id}",
        "vlan_id": {"value": vlan_id},
        "capabilities": edges(*members),
    }


def raw_deployment(
    kind: str = "TopologyPod", hosting_id: str | None = HOSTING_ID, hosting_name: str = HOSTING_NAME
) -> dict[str, Any]:
    """A device's ``deployment`` (VrfDemandHostingFields). A hosting kind is the hosting deployment itself; a pod, zone
    or virtual colocation names it as ``parent``; any other kind (or ``hosting_id`` None) has no hosting deployment."""
    if kind in HOSTING_KINDS:
        return {"node": {"__typename": kind, "id": hosting_id or "dep-1", "name": {"value": hosting_name}}}
    node: dict[str, Any] = {"__typename": kind, "id": "dep-1", "name": {"value": "dep-1"}}
    if hosting_id and kind in {"TopologyPod", "TopologyColocationZone", "TopologyVirtualColocation"}:
        node["parent"] = {"node": {"__typename": HOSTING_KIND, "id": hosting_id, "name": {"value": hosting_name}}}
    return {"node": node}


def raw_device(
    device_id: str,
    name: str,
    role: str = "leaf",
    typename: str = "DcimPhysicalDevice",
    rows: list[dict[str, Any]] | None = None,
    capabilities: list[dict[str, Any]] | None = None,
    interfaces: list[dict[str, Any]] | None = None,
    groups: tuple[str, ...] = (),
    deployment_kind: str = "TopologyPod",
    vrfs: list[dict[str, Any]] | None = None,
    hosting_id: str | None = HOSTING_ID,
    status: str | None = None,
    shared_others: list[str] | None = None,
) -> dict[str, Any]:
    """One DcimDevice node of the query. ``vrfs`` are its memberships (``raw_instance``), emitted as
    ``shared_capabilities``; a ManagedVRF passed in ``capabilities`` is taken as a membership too (the pre-r3
    spelling); ``shared_others`` are foreign shared kinds (a ``__typename`` only). The device sits in a pod of the DC
    ``hosting_id`` (None: a deployment with no hosting parent)."""
    caps = capabilities or []
    members = [*(vrfs or []), *(cap for cap in caps if cap.get("__typename") == VRF_KIND)]
    node: dict[str, Any] = {
        "__typename": typename,
        "id": device_id,
        "name": {"value": name},
        "role": {"value": role},
        "member_of_groups": edges(*({"name": {"value": group}} for group in groups)),
        "shared_capabilities": edges(*members, *({"__typename": kind} for kind in shared_others or [])),
        "deployment": raw_deployment(deployment_kind, hosting_id),
        "vlan_domain_segments": edges(*(rows or [])),
        "capabilities": edges(*(cap for cap in caps if cap.get("__typename") != VRF_KIND)),
        "interfaces": edges(*(interfaces or [])),
    }
    if status:
        node["status"] = {"value": status}
    return node


def raw_response(*devices: dict[str, Any], instances: tuple[dict[str, Any], ...] = ()) -> dict[str, Any]:
    """The whole query response. The ``ManagedVRF`` root lists every instance the devices are members of (with those
    members) plus the extra ``instances`` (raw_root_instance nodes, for instances with no member)."""
    members: dict[str, list[str]] = {}
    shapes: dict[str, dict[str, Any]] = {}
    for device in devices:
        for edge in device["shared_capabilities"]["edges"]:
            if edge["node"]["__typename"] != VRF_KIND:
                continue
            members.setdefault(edge["node"]["id"], []).append(device["id"])
            shapes[edge["node"]["id"]] = edge["node"]
    roots = [raw_root_instance(shapes[vrf_id], *ids) for vrf_id, ids in members.items()]
    return {"IpamNamespace": edges(), VRF_KIND: edges(*roots, *instances), "DcimDevice": edges(*devices)}


def facts_of(device: dict[str, Any]) -> DeviceVrfFacts:
    """The gatherer's facts of one raw device node."""
    return device_vrf_facts(clean_data(device))
