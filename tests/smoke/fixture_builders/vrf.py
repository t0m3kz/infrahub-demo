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
from utils.device_services import has_pbr, pbr_bindings
from utils.segment_vlan import ROW_KIND
from utils.service_contexts import device_services
from utils.vrf_demand import PBR_KIND, SVI_ROLE, VRF_KIND, svi_name

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


def _svi_node(vlan_id: int, namespace: ZoneMetadata, gateway: dict[str, Any] | None) -> dict[str, Any]:
    """A segment SVI as the VRF pass writes it: in its VRF, carrying the segment's anycast gateway (a cleaned
    ``segment.gateway``) as its address, in the raw shape."""
    address = (gateway or {}).get("address")
    node = {"id": (gateway or {}).get("id") or f"fixture-gateway-{vlan_id}", "address": _value(address)}
    # The SVI is in its VRF and a member of the VLAN row it realizes: both are among its capabilities.
    row = {"__typename": ROW_KIND, "id": f"fixture-row-{vlan_id}", "vlan_id": _value(vlan_id)}
    capabilities = interface_vrf_capability(routing_vrf(namespace["name"]))
    capabilities["zone_capabilities"]["edges"].append({"node": row})
    return {
        "__typename": "DcimVirtualInterface",
        "name": _value(svi_name(vlan_id)),
        "description": _value(""),
        "status": _value("active"),
        "role": _value(SVI_ROLE),
        "ip_address": {"node": node if address else None},
        **capabilities,
    }


def _gateways(device_data: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """VLAN -> the cleaned anycast gateway of the segment activation of that VLAN (a prepared device)."""
    return {
        act["vlan_id"]: gateway
        for act in device_data.get("segment_deployments") or []
        if act.get("vlan_id") and (gateway := (act.get("segment") or {}).get("gateway"))
    }


def interface_vrf_capability(vrf: dict[str, Any]) -> dict[str, Any]:
    """The raw selection that puts an interface in ``vrf`` (a raw ManagedVRF node): the ManagedVRF among its capabilities,
    under the alias ``zone_capabilities`` (queries/fragments/routing_zone.gql ``InterfaceRoutingZoneFields``)."""
    return {"zone_capabilities": {"edges": [{"node": {"__typename": VRF_KIND, **vrf}}]}}


_NOT_INTERFACE_KINDS = frozenset({"TopologySessionCircuit", "ManagedStaticRoute"})
_NOT_INTERFACE_KEYS = frozenset({"circuit_type", "next_hop", "registered_prefixes", "link_prefix", "prefix"})


def _is_interface(item: dict[str, Any]) -> bool:
    """Whether a raw fixture node holding a ``vrf`` is an interface (a circuit or a static route keeps its ``vrf``)."""
    kind = str(item.get("__typename") or "")
    if kind:
        return kind not in _NOT_INTERFACE_KINDS and ("Interface" in kind)
    return "name" in item and not (_NOT_INTERFACE_KEYS & item.keys())


def with_interface_vrfs(data: Any) -> Any:
    """A copy of raw fixture data where every interface's ``vrf`` (the builders' shorthand) is its VRF capability.

    An interface is in a VRF through the instance's interface edge: the config queries select it as the ManagedVRF among
    the interface's ``zone_capabilities`` (``interface_vrf_capability``), never as a ``vrf`` of the interface."""
    result = copy.deepcopy(data)

    def walk(item: Any) -> None:
        if isinstance(item, list):
            for child in item:
                walk(child)
        elif isinstance(item, dict):
            vrf = item.get("vrf")
            if isinstance(vrf, dict) and "node" in vrf and _is_interface(item):
                item.pop("vrf")
                if vrf["node"]:
                    edges = item.setdefault("zone_capabilities", {"edges": []}).setdefault("edges", [])
                    edges.insert(0, interface_vrf_capability(vrf["node"])["zone_capabilities"]["edges"][0])
            for child in item.values():
                walk(child)

    walk(result)
    return result


def _pbr_node(device_name: str) -> dict[str, Any]:
    """The raw ManagedPBR capability of a device that steers (generators/helpers/vrf.py ``sync_pbr`` writes it)."""
    return {
        "__typename": PBR_KIND,
        "id": f"fixture-pbr-{device_name}",
        "name": _value(f"{device_name}-pbr"),
        "status": _value("active"),
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
    device: dict[str, Any],
    requirements: VrfRequirements,
    namespaces: dict[str, ZoneMetadata],
    gateways: dict[int, dict[str, Any]],
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
        missing_svis.append(_svi_node(vlan_id, namespaces[namespace_name], gateways.get(vlan_id)))
    _append(
        device,
        "interfaces",
        missing_svis,
    )


def with_vrf_objects(transform: BaseDeviceTransform, data: dict[str, Any]) -> dict[str, Any]:
    """A copy of the raw device query response ``data`` with the VRF memberships and SVIs its config demands added.

    The nested service data the config derives from (``DeviceServices``) is given first, from what the response
    declares (tests/smoke/fixture_builders/derived_services.py): the demand depends on the contexts behind the ports."""
    from tests.smoke.fixture_builders.derived_services import with_device_services  # noqa: PLC0415 - import cycle

    result = copy.deepcopy(with_device_services(transform, with_interface_vrfs(data)))
    device_data, _, platform_name = transform.prepared_device(result)
    if not platform_name:
        return result
    first = next(iter(result.values()))
    node = first["edges"][0]["node"] if isinstance(first, dict) and "edges" in first else first
    requirements = vrf_requirements(device_data)
    if requirements.namespaces or requirements.svis:
        add_requirements(node, requirements, _namespace_dicts(device_data), _gateways(device_data))
    # A device that steers an SVI to a firewall has its ManagedPBR, the way the VRF pass of the device leaves it.
    if pbr_bindings(device_services(device_data)) and not has_pbr(device_data):
        _append(node, "capabilities", [_pbr_node(str(device_data.get("name") or "device"))])
    return result
