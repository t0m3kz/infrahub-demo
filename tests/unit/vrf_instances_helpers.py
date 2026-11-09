"""Raw GraphQL builders for queries/validation/vrf_instances.gql (edges/node/value, ``__typename``)."""

from __future__ import annotations

from typing import Any

from utils.data_cleaning import clean_data
from utils.exchange_transit import FIREWALL_CONTEXT_KIND, transit_vlan
from utils.segment_addressing import HOSTING_KINDS
from utils.vrf_demand import VRF_KIND, DeviceVrfFacts, device_vrf_facts, instance_name

HOSTING_ID = "dc-1"
HOSTING_NAME = "DC1"
HOSTING_KIND = "TopologyDataCenter"


def edges(*nodes: dict[str, Any]) -> dict[str, Any]:
    """A cardinality-many relationship."""
    return {"edges": [{"node": node} for node in nodes]}


def namespace_ref(name: str, vrf_type: str | None = None) -> dict[str, Any]:
    """An ``ip_namespace`` relationship."""
    node: dict[str, Any] = {"name": {"value": name}}
    if vrf_type:
        node["vrf_type"] = {"value": vrf_type}
    return {"node": node}


def address(addr: str, namespace: str, vrf_type: str | None = None) -> dict[str, Any]:
    """An ``ip_address`` relationship; IPAM stays in ``default``, the routing zone rides in ``_zone``.

    ``_zone`` is a fixture-only hint, popped by ``zone_vrf`` when the address is put on an interface: the query reads
    the zone from the interface's ``vrf``, never from the address."""
    return {
        "node": {
            "address": {"value": addr},
            "ip_namespace": namespace_ref("default"),
        },
        "_zone": namespace_ref(namespace, vrf_type),
    }


def raw_footprint(footprint_id: str, parent_id: str = HOSTING_ID, *, context: bool = True) -> dict[str, Any]:
    """A customer footprint of a segment: in a DC, with or without a serving firewall context."""
    return {
        "id": footprint_id,
        "name": {"value": footprint_id},
        "parent": {"node": {"id": parent_id}},
        "serving_firewall_context": {"node": {"id": "ctx-1"}} if context else {"node": None},
    }


def raw_row(
    row_id: str,
    vlan_id: int,
    namespace: str | None = "PROD",
    segment: str = "seg",
    *,
    terminated: bool = False,
    footprints: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """A ManagedSegmentVlan row; ``namespace`` None means a segment without a gateway (pure L2).

    ``terminated`` is a segment terminated on a firewall context: it keeps its gateway (the pair's VIP) and is pure
    L2 on the fabric all the same."""
    seg: dict[str, Any] = {
        "__typename": "ManagedVxlanSegment",
        "id": f"seg-{segment}",
        "name": {"value": segment},
        "gateway": None,
        "terminated_on": {"node": {"id": "ctx-1"}} if terminated else None,
        "customer_deployments": edges(*(footprints or [])),
    }
    if namespace:
        seg["security_zone"] = namespace_ref(namespace)
        seg["gateway"] = {
            "node": {
                "address": {"value": "10.0.0.1/24"},
                "ip_prefix": {"node": {"ip_namespace": namespace_ref("default")}},
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
    ns["vrf_type"] = {"value": ns_type}
    return {
        "__typename": VRF_KIND,
        "id": instance_id,
        "name": {"value": instance_name(hosting_name, namespace)},
        "deployment": {"node": {"id": deployment_id}} if deployment_id else None,
        "security_zone": {"node": ns},
    }


def raw_root_instance(instance: dict[str, Any], *member_ids: str) -> dict[str, Any]:
    """A node of the ``ManagedVRF`` root: the membership shape plus its ``shared_devices``."""
    root = {key: value for key, value in instance.items() if key != "__typename"}
    root["shared_devices"] = edges(*({"id": member} for member in member_ids))
    return root


def raw_mlag(mlag_id: str) -> dict[str, Any]:
    """A ManagedMLAG capability (the query selects no rows on it: rows sit on the device)."""
    return {"__typename": "ManagedMLAG", "id": mlag_id}


def in_raw_vrf(vrf: dict[str, Any] | None) -> dict[str, Any]:
    """The raw ``zone_capabilities`` of an interface in the instance ``vrf`` (a raw ManagedVRF node; None: in none)."""
    return {"zone_capabilities": edges(*([{"__typename": VRF_KIND, **vrf}] if vrf else []))}


def zone_vrf(addr: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Split an ``address()`` into the real ``ip_address`` and the raw ManagedVRF the interface is in for its zone."""
    clean = {key: value for key, value in addr.items() if key != "_zone"}
    zone = addr.get("_zone")
    if not zone or zone["node"]["name"]["value"] == "default":
        return clean, None
    return clean, {"id": f"vrf-{zone['node']['name']['value']}", "security_zone": zone}


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
        node["ip_address"], vrf = zone_vrf(addr)
        if vrf:
            node.update(in_raw_vrf(vrf))
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
    node.update(
        in_raw_vrf(
            {
                "id": vrf_id,
                "deployment": {"node": {"id": vrf_deployment_id}},
                "security_zone": namespace_ref(vrf_namespace or ""),
            }
            if vrf_id
            else None
        )
    )
    node["segment_vlan"] = {"node": {"id": row_id}} if row_id else None
    return node


def raw_service_port(
    name: str,
    role: str,
    kind: str,
    vlan_id: int,
    *legs: tuple[str, str, str],
    context_id: str | None = None,
    tenant: str | None = None,
    served: tuple[tuple[str, str], ...] = (),
    member_role: str | None = None,
    raw_legs: tuple[dict[str, Any], ...] = (),
) -> dict[str, Any]:
    """A border service port whose cable ends on the first uplink of a member of one service context.

    ``raw_legs`` are ready-made leg sub-interfaces (``ip_address``, ``zone_capabilities`` and the ``ns_type`` of their VRF named
    ``_ns_type``) for a caller that holds the real addresses; ``member_role`` is the role of the member.

    Each leg is ``(member role, namespace, namespace type)``: a sub-interface ``<uplink>.<transit VLAN>`` of the member
    (the member takes the role of the first leg), addressed in that namespace. ``served`` are the footprints the context
    serves ``(id, environment)`` and ``tenant`` its dedicated one."""
    ctx_id = context_id or f"ctx-{kind}-{vlan_id}"
    served_key = "fw_served" if kind == FIREWALL_CONTEXT_KIND else "lb_served"
    context = {
        "__typename": kind,
        "id": ctx_id,
        "name": {"value": ctx_id},
        "vlan_id": {"value": vlan_id},
        "tenant": {"node": {"id": tenant, "environment": {"value": "p"}}} if tenant else {"node": None},
        served_key: edges(*({"id": fp, "environment": {"value": env}} for fp, env in served)),
    }
    member_id = f"{ctx_id}:dev"
    parent_ref = {"node": {"name": {"value": "ethernet1/1"}}}
    uplink = {
        "__typename": "DcimPhysicalInterface",
        "id": f"{ctx_id}:up",
        "name": {"value": "ethernet1/1"},
        "role": {"value": "uplink"},
    }
    subs = []
    for _role, namespace, ns_type in legs if isinstance(vlan_id, int) else ():
        ip_address, vrf = zone_vrf(address("10.1.0.1/29", namespace, ns_type))
        sub: dict[str, Any] = {
            "__typename": "DcimVirtualInterface",
            "id": f"{ctx_id}:{namespace}",
            "name": {"value": f"ethernet1/1.{transit_vlan(vlan_id, ns_type)}"},
            "vlan_id": {"value": transit_vlan(vlan_id, ns_type)},
            "parent_interface": parent_ref,
            "role": {"value": "service"},
            "ip_address": ip_address,
        }
        if vrf:
            sub.update(in_raw_vrf(vrf))
        subs.append(sub)
    for raw in raw_legs if isinstance(vlan_id, int) else ():
        subs.append(
            {
                "__typename": "DcimVirtualInterface",
                "id": f"{ctx_id}:{len(subs)}",
                "name": {"value": f"ethernet1/1.{transit_vlan(vlan_id, raw['_ns_type'])}"},
                "vlan_id": {"value": transit_vlan(vlan_id, raw["_ns_type"])},
                "parent_interface": parent_ref,
                "role": {"value": "service"},
                **{key: value for key, value in raw.items() if key != "_ns_type"},
            }
        )
    far = {
        **uplink,
        "device": {
            "node": {
                "__typename": "DcimPhysicalDevice",
                "id": member_id,
                "name": {"value": f"{ctx_id}-member"},
                "role": {"value": member_role or (legs[0][0] if legs else "internal-firewall")},
                "shared_capabilities": edges(context),
                "interfaces": edges(uplink, *subs),
            }
        },
        "sub_interfaces": edges(),
    }
    own = {
        "__typename": "DcimPhysicalInterface",
        "id": f"port-{name}",
        "name": {"value": name},
        "role": {"value": role},
    }
    return {**own, "cable": {"node": {"id": f"cable-{name}", "endpoints": edges(own, far)}}}


def raw_deployment(
    kind: str = "TopologyPod", hosting_id: str | None = HOSTING_ID, hosting_name: str = HOSTING_NAME
) -> dict[str, Any]:
    """A device's ``deployment`` (ServiceHostingFields). A hosting kind is the hosting deployment itself; a pod, zone
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
    service_ports: list[dict[str, Any]] | None = None,
    groups: tuple[str, ...] = (),
    deployment_kind: str = "TopologyPod",
    vrfs: list[dict[str, Any]] | None = None,
    hosting_id: str | None = HOSTING_ID,
    status: str | None = None,
    shared_others: list[str] | None = None,
    pbr: bool = False,
) -> dict[str, Any]:
    """One DcimDevice node of the query. ``vrfs`` are its memberships (``raw_instance``), emitted as
    ``shared_capabilities``; a ManagedVRF passed in ``capabilities`` is taken as a membership too (the pre-r3
    spelling), any other capability is dropped; ``shared_others`` are foreign shared kinds (a ``__typename`` only).
    ``pbr`` gives it a ManagedPBR among its individual ``capabilities`` (it steers). The device sits in a pod of the DC
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
        "vlans": edges(*(rows or [])),
        "interfaces": edges(*(interfaces or [])),
        "service_ports": edges(*(service_ports or [])),
        "capabilities": edges(*([{"__typename": "ManagedPBR", "id": f"pbr-{device_id}"}] if pbr else [])),
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
    return {"SecurityZone": edges(), VRF_KIND: edges(*roots, *instances), "DcimDevice": edges(*devices)}


def facts_of(device: dict[str, Any]) -> DeviceVrfFacts:
    """The gatherer's facts of one raw device node."""
    return device_vrf_facts(clean_data(device))
