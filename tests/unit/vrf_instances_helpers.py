"""Raw GraphQL builders for queries/validation/vrf_instances.gql (edges/node/value, ``__typename``)."""

from __future__ import annotations

from typing import Any

from utils.data_cleaning import clean_data
from utils.vrf_demand import DeviceVrfFacts, device_vrf_facts


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
    instance_id: str, device: str, namespace: str, l3_vni: int | None = 100, ns_type: str | None = "prod"
) -> dict:
    """A ManagedVRF capability."""
    ns: dict[str, Any] = {"name": {"value": namespace}}
    ns["l3_vni"] = {"value": l3_vni}
    ns["namespace_type"] = {"value": ns_type}
    return {
        "__typename": "ManagedVRF",
        "id": instance_id,
        "name": {"value": f"{device}-vrf-{namespace.lower()}"},
        "namespace": {"node": ns},
    }


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
) -> dict[str, Any]:
    """A role-svi DcimVirtualInterface."""
    node = raw_interface(svi_id, name or f"Vlan{vlan_id}", "svi", addr, capabilities, "DcimVirtualInterface")
    node["vrf"] = {"node": {"id": vrf_id, "namespace": namespace_ref(vrf_namespace or "")}} if vrf_id else None
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
) -> dict[str, Any]:
    """One DcimDevice node of the query."""
    return {
        "__typename": typename,
        "id": device_id,
        "name": {"value": name},
        "role": {"value": role},
        "member_of_groups": edges(*({"name": {"value": group}} for group in groups)),
        "deployment": {"node": {"__typename": deployment_kind, "id": "dep-1"}},
        "vlan_domain_segments": edges(*(rows or [])),
        "capabilities": edges(*(capabilities or [])),
        "interfaces": edges(*(interfaces or [])),
    }


def raw_response(*devices: dict[str, Any]) -> dict[str, Any]:
    """The whole query response."""
    return {"IpamNamespace": edges(), "DcimDevice": edges(*devices)}


def facts_of(device: dict[str, Any]) -> DeviceVrfFacts:
    """The gatherer's facts of one raw device node."""
    return device_vrf_facts(clean_data(device))
