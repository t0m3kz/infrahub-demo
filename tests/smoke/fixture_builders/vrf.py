"""The VRF instances and SVI objects a fixture device's config demands, appended to its raw input.

A transform refuses a device whose config uses a VRF it has no ManagedVRF instance of, or a routed segment with no
role-``svi`` interface (transforms/helpers/vrf.py). The fixture writers (``common._write_data_fixture``) and the unit
tests that build their own device data therefore go through ``with_vrf_objects``: it asks the transform's own
``vrf_requirements`` what the device demands, so there is no second derivation, and appends the missing
nodes in the RAW GraphQL shape, as the config queries return them. Idempotent: a node already present is kept.
"""

from __future__ import annotations

import copy
from typing import Any

from transforms.common import BaseDeviceTransform
from transforms.helpers.vrf import VrfRequirements, vrf_requirements
from utils.vrf_demand import SVI_ROLE, VRF_KIND, instance_name, svi_name

# A fixture namespace dict does not always carry its namespace_type (the segment gateway's does not): the type of the
# four environment namespaces by name, the tenant default for any other name (a fixture's own VRF_A, ...).
_TYPE_BY_NAME = {
    "PROD": "prod",
    "NON-PROD": "non_prod",
    "NONPROD": "non_prod",
    "INTERNET": "internet",
    "MANAGEMENT": "management",
}
_DEFAULT_TYPE = "prod"


def _value(value: Any) -> dict[str, Any]:
    return {"value": value}


def _namespace_dicts(data: Any) -> dict[str, dict[str, Any]]:
    """Every namespace dict of the cleaned fixture (a name with an ``l3_vni`` or a ``namespace_type``), by name."""
    found: dict[str, dict[str, Any]] = {}
    pending = [data]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            if item.get("name") and ("l3_vni" in item or "namespace_type" in item):
                merged = found.setdefault(item["name"], {"name": item["name"]})
                merged.update({key: item[key] for key in ("l3_vni", "namespace_type") if item.get(key) is not None})
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return found


def _vrf_node(device_name: str, namespace: dict[str, Any]) -> dict[str, Any]:
    name = namespace["name"]
    return {
        "__typename": VRF_KIND,
        "id": f"vrf-{device_name}-{name}",
        "name": _value(instance_name(device_name, name)),
        "status": _value("active"),
        "namespace": {
            "node": {
                "name": _value(name),
                "l3_vni": _value(namespace.get("l3_vni")),
                "namespace_type": _value(namespace.get("namespace_type") or _TYPE_BY_NAME.get(name, _DEFAULT_TYPE)),
            }
        },
    }


def _svi_node(vlan_id: int, namespace_name: str | None) -> dict[str, Any]:
    return {
        "__typename": "DcimVirtualInterface",
        "name": _value(svi_name(vlan_id)),
        "description": _value(""),
        "status": _value("active"),
        "role": _value(SVI_ROLE),
        "ip_address": {"node": None},
        "vrf": {"node": {"namespace": {"node": {"name": _value(namespace_name)}}}}
        if namespace_name
        else {"node": None},
        "capabilities": {"edges": []},
    }


def _append(container: dict[str, Any], key: str, nodes: list[dict[str, Any]]) -> None:
    container.setdefault(key, {"edges": []}).setdefault("edges", []).extend({"node": node} for node in nodes)


def _raw_names(container: dict[str, Any], key: str, only: str | None = None) -> set[str]:
    return {
        edge["node"]["name"]["value"]
        for edge in (container.get(key) or {}).get("edges") or []
        if edge["node"].get("name") and (only is None or edge["node"].get("__typename") == only)
    }


def add_requirements(
    device: dict[str, Any], device_name: str, requirements: VrfRequirements, namespaces: dict[str, dict[str, Any]]
) -> None:
    """Append to the raw ``device`` node the instances and SVIs of ``requirements`` it does not have yet.

    ``namespaces`` holds the l3_vni / namespace_type of each namespace the fixture names (``_namespace_dicts``).
    """
    have = _raw_names(device, "capabilities", only=VRF_KIND)
    _append(
        device,
        "capabilities",
        [
            _vrf_node(device_name, namespaces.get(name) or {"name": name})
            for name in sorted(requirements.namespaces)
            if instance_name(device_name, name) not in have
        ],
    )
    present = _raw_names(device, "interfaces")
    _append(
        device,
        "interfaces",
        [
            _svi_node(vlan_id, namespace_name)
            for vlan_id, namespace_name in sorted(requirements.svis.items())
            if svi_name(vlan_id) not in present
        ],
    )


def with_vrf_objects(transform: BaseDeviceTransform, data: dict[str, Any]) -> dict[str, Any]:
    """A copy of the raw device query response ``data`` with the VRF instances and SVIs its config demands added."""
    result = copy.deepcopy(data)
    device_data, _, platform_name = transform.prepared_device(result)
    if not platform_name:
        return result
    requirements = vrf_requirements(device_data)
    if requirements.namespaces or requirements.svis:
        first = next(iter(result.values()))
        node = first["edges"][0]["node"] if isinstance(first, dict) and "edges" in first else first
        add_requirements(node, device_data.get("name") or "", requirements, _namespace_dicts(device_data))
    return result
