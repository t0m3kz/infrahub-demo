"""The VRF memberships and SVI objects a fixture device's config demands, appended to its raw input.

A transform refuses a device whose config uses a VRF it is no member of, or a routed segment with no
role-``svi`` interface (transforms/helpers/vrf.py). The fixture writers (``common._write_data_fixture``) and the unit
tests that build their own device data therefore go through ``with_vrf_objects``: it asks the transform's own
``vrf_requirements`` what the device demands, so there is no second derivation, and appends the missing
nodes in the RAW GraphQL shape, as the config queries return them. Idempotent: a node already present is kept.
"""

from __future__ import annotations

import copy
from typing import Any, TypedDict

from transforms.common import BaseDeviceTransform
from transforms.helpers.vrf import VrfRequirements, vrf_requirements
from utils.vrf_demand import SVI_ROLE, VRF_KIND, svi_name

# Standard routing metadata belongs to the explicit SecurityZone owner, not
# to an address's IPAM namespace or the gateway prefix.
ZONE_CATALOG: dict[str, tuple[str, int]] = {
    "PROD": ("prod", 60000),
    "NONPROD": ("non_prod", 60001),
    "INTERNET": ("internet", 60002),
    "MANAGEMENT": ("management", 60003),
}
# The old fixtures modelled VRFs and firewall labels separately. The new
# fixtures use the zone's catalog name for both routing and policy.
LEGACY_ZONE_NAMES = {
    "VRF_A": "PROD",
    "VRF-INTERNAL": "PROD",
    "internal": "PROD",
    "VRF-DMZ": "NONPROD",
    "dmz": "NONPROD",
    "VRF-EXTERNAL": "INTERNET",
    "external": "INTERNET",
    "web": "PROD",
    "prod": "PROD",
}


class ZoneMetadata(TypedDict):
    """The complete routing metadata selected on a SecurityZone."""

    name: str
    vrf_type: str
    l3_vni: int


def routing_zone(name: str, *, l3_vni: int | None = None, vrf_type: str | None = None) -> dict[str, Any]:
    """Build an explicit zone, using the fixed catalog for its four standard names."""
    name = LEGACY_ZONE_NAMES.get(name, name)
    if name in ZONE_CATALOG:
        vrf_type, l3_vni = ZONE_CATALOG[name]
    if l3_vni is None or vrf_type is None:
        raise ValueError(f"Fixture zone {name}: explicit vrf_type and l3_vni required")
    return {
        "id": f"fixture-zone-{name}",
        "name": _value(name),
        "vrf_type": _value(vrf_type),
        "l3_vni": _value(l3_vni),
    }


def routing_vrf(name: str, deployment: str = "fixture-deployment") -> dict[str, Any]:
    """A raw deployment-scoped routing owner, separate from the neutral IPAM objects it references."""
    zone = routing_zone(name)
    return {
        "__typename": VRF_KIND,
        "id": f"fixture-vrf-{deployment}-{zone['name']['value']}",
        "deployment": {"node": {"id": deployment, "name": _value(deployment)}},
        "security_zone": {"node": zone},
    }


def _value(value: Any) -> dict[str, Any]:
    return {"value": value}


def _namespace_dicts(data: Any) -> dict[str, ZoneMetadata]:
    """Complete metadata of explicit security-zone owners in the cleaned fixture."""
    found: dict[str, ZoneMetadata] = {}
    pending = [data]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            zone = item.get("security_zone")
            if isinstance(zone, dict) and isinstance(zone.get("name"), str):
                name = zone["name"]
                if name in ZONE_CATALOG:
                    vrf_type, l3_vni = ZONE_CATALOG[name]
                else:
                    vrf_type, l3_vni = zone.get("vrf_type"), zone.get("l3_vni")
                if isinstance(vrf_type, str) and isinstance(l3_vni, int):
                    metadata: ZoneMetadata = {"name": name, "vrf_type": vrf_type, "l3_vni": l3_vni}
                    if name in found and found[name] != metadata:
                        raise ValueError(f"Fixture zone {name}: conflicting routing metadata")
                    found[name] = metadata
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return found


def _vrf_node(namespace: ZoneMetadata) -> dict[str, Any]:
    name = namespace["name"]
    return routing_vrf(name)


def _svi_node(vlan_id: int, namespace: ZoneMetadata) -> dict[str, Any]:
    return {
        "__typename": "DcimVirtualInterface",
        "name": _value(svi_name(vlan_id)),
        "description": _value(""),
        "status": _value("active"),
        "role": _value(SVI_ROLE),
        "ip_address": {"node": None},
        "vrf": {"node": routing_vrf(namespace["name"])},
        # The pbr policy the VRF pass of the device attaches to every steered SVI (generators/helpers/pbr_sync.py): the
        # fixture steers all of them, the transform still applies its own conditions (policies, a context with a leg).
        "capabilities": {
            "edges": [
                {
                    "node": {
                        "__typename": "RoutingPolicy",
                        "kind": _value("pbr"),
                        "origin": _value("generated"),
                        "customer_deployment": {"node": {"id": "fixture-footprint"}},
                    }
                }
            ]
        },
    }


def _append(container: dict[str, Any], key: str, nodes: list[dict[str, Any]]) -> None:
    container.setdefault(key, {"edges": []}).setdefault("edges", []).extend({"node": node} for node in nodes)


def _raw_names(container: dict[str, Any], key: str) -> set[str]:
    return {
        edge["node"]["name"]["value"]
        for edge in (container.get(key) or {}).get("edges") or []
        if edge["node"].get("name")
    }


def add_requirements(
    device: dict[str, Any], requirements: VrfRequirements, namespaces: dict[str, ZoneMetadata]
) -> None:
    """Append to the raw ``device`` node the memberships (``shared_capabilities``) and SVIs it does not have yet.

    ``namespaces`` holds each explicitly owned zone's complete L3 VNI and purpose.
    """
    have = {
        edge["node"]["security_zone"]["node"]["name"]["value"]
        for edge in (device.get("shared_capabilities") or {}).get("edges") or []
        if edge["node"].get("__typename") == VRF_KIND and (edge["node"].get("security_zone") or {}).get("node")
    }
    _append(
        device,
        "shared_capabilities",
        [_vrf_node(namespaces[name]) for name in sorted(requirements.namespaces - have)],
    )
    present = _raw_names(device, "interfaces")
    missing_svis = []
    for vlan_id, namespace_name in sorted(requirements.svis.items()):
        if svi_name(vlan_id) in present:
            continue
        if namespace_name is None:
            raise ValueError(f"Fixture SVI {svi_name(vlan_id)}: an explicit routing zone is required")
        missing_svis.append(_svi_node(vlan_id, namespaces[namespace_name]))
    _append(
        device,
        "interfaces",
        missing_svis,
    )


def with_vrf_objects(transform: BaseDeviceTransform, data: dict[str, Any]) -> dict[str, Any]:
    """A copy of the raw device query response ``data`` with the VRF memberships and SVIs its config demands added."""
    result = copy.deepcopy(data)
    device_data, _, platform_name = transform.prepared_device(result)
    if not platform_name:
        return result
    requirements = vrf_requirements(device_data)
    if requirements.namespaces or requirements.svis:
        first = next(iter(result.values()))
        node = first["edges"][0]["node"] if isinstance(first, dict) and "edges" in first else first
        add_requirements(node, requirements, _namespace_dicts(device_data))
    return result
