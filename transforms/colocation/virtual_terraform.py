"""HCL rendering for a virtual colocation presence at Equinix (Network Edge).

A pure renderer with a plain-dict input contract: it reads nothing from Infrahub. The transform in
transforms/colocation/virtual_colocation.py builds the dict from the query (normalise_virtual_colocation) and
calls render_equinix_network_devices(), which validates in Python and prints through the Jinja2 template.

One artifact per virtual colocation: the file describes every device of that presence. Three shapes of device:

* a single device, optionally placed away from another one (`diverse_from` -> `diverse_device_id`);
* an HA pair (`ha_peer`): one `equinix_network_device` whose `secondary_device` block carries the second node,
  which may sit in another metro because Equinix allows a geo-redundant pair;
* a cluster (`cluster`): one resource with a `cluster_details` block, always inside one metro. Only the Palo Alto
  cluster (PA-VM) is rendered; any other vendor is refused rather than guessed at.

The account number, the notification list and every licence token are Terraform variables on purpose. Licences,
tokens, keys and vendor passwords are neither in Infrahub nor in an artifact.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from transforms.cloud.terraform_common import tf_id
from transforms.helpers.templates import load_template

PROVIDER = "equinix"
_TEMPLATE_DIR = Path(__file__).resolve().parents[2] / "templates" / "configs" / "equinix"
_TEMPLATE = "virtual_colocation.tf.j2"
_CLUSTER_TYPE_CODES = frozenset({"PA-VM"})

_REQUIRED_DEVICE_FIELDS = ("name", "metro_code", "type_code", "package_code", "version", "core_count", "term_length")
_SHARED_CLUSTER_FIELDS = ("metro_code", "type_code", "package_code", "version", "core_count", "term_length")


_HCL_LABEL = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")


def _q(value: Any) -> str:
    """A quoted HCL string; json.dumps escapes quotes and backslashes the way HCL expects.

    HCL also evaluates ``${...}`` and ``%{...}`` inside a quoted string, so a name that carries either would be
    run at apply time: ``$${`` and ``%%{`` are the literal spellings.
    """
    return json.dumps(str(value)).replace("${", "$${").replace("%{", "%%{")


def _label(vc_name: str, name: str) -> str:
    """The Terraform resource/variable identifier of ``name``; refuses what HCL cannot take as a label."""
    label = tf_id(name)
    if not _HCL_LABEL.match(label):
        raise ValueError(f"{vc_name}: {name!r} does not make a valid Terraform identifier ({label!r})")
    return label


def _check_device(vc_name: str, device: dict[str, Any]) -> None:
    missing = [field for field in _REQUIRED_DEVICE_FIELDS if device.get(field) in (None, "")]
    if missing:
        raise ValueError(f"{vc_name}: device {device.get('name', '?')!r} is missing {', '.join(missing)}")


def _token_variable(vc_name: str, cluster_name: str, node: int) -> str:
    return f"{_label(vc_name, f'{vc_name}_{cluster_name}')}_node{node}_license_token"


def _group_clusters(vc_name: str, devices: dict[str, dict[str, Any]]) -> dict[str, dict[int, dict[str, Any]]]:
    """cluster name -> {0: node0 device, 1: node1 device}, validated: two nodes, same template, one metro."""
    clusters: dict[str, dict[int, dict[str, Any]]] = {}
    for device in devices.values():
        cluster = device.get("cluster")
        if not cluster:
            continue
        if device.get("ha_peer") or device.get("diverse_from"):
            raise ValueError(f"{vc_name}: {device['name']} is a cluster node and cannot also be ha_peer/diverse_from")
        if device["type_code"] not in _CLUSTER_TYPE_CODES:
            raise ValueError(
                f"{vc_name}: cluster {cluster['name']!r} uses {device['type_code']!r}; only "
                f"{sorted(_CLUSTER_TYPE_CODES)} clusters are rendered"
            )
        node = cluster.get("node")
        if node not in (0, 1) or node in clusters.setdefault(cluster["name"], {}):
            raise ValueError(f"{vc_name}: cluster {cluster['name']!r} has a bad or duplicate node {node!r}")
        clusters[cluster["name"]][node] = device
    for name, nodes in clusters.items():
        if set(nodes) != {0, 1}:
            raise ValueError(f"{vc_name}: cluster {name!r} needs exactly node 0 and node 1, got {sorted(nodes)}")
        first, second = nodes[0], nodes[1]
        differing = [field for field in _SHARED_CLUSTER_FIELDS if first[field] != second[field]]
        if differing:
            raise ValueError(f"{vc_name}: cluster {name!r} nodes differ in {', '.join(differing)}")
    return clusters


def _resource_view(
    vc_name: str,
    name: str,
    devices: dict[str, dict[str, Any]],
    resource_of: dict[str, str],
    clusters: dict[str, dict[int, dict[str, Any]]],
) -> dict[str, Any]:
    """What the template prints for the Terraform resource that device `name` opens, every rule already checked."""
    device = devices[name]
    cluster = device.get("cluster")
    resource_id = resource_of[name]
    diverse_resource = None
    if device.get("diverse_from"):
        target = device["diverse_from"]
        if target not in devices:
            raise ValueError(f"{vc_name}: {name} is diverse_from {target!r}, which is not a device of this presence")
        if device.get("ha_peer") or resource_of[target] == resource_id:
            raise ValueError(
                f"{vc_name}: {name} cannot be diverse_from {target!r}: diverse_device_id is only meaningful "
                "for a single device, and never against itself"
            )
        diverse_resource = resource_of[target]
    return {
        "id": resource_id,
        "name": cluster["name"] if cluster else name,
        "metro_code": device["metro_code"],
        "type_code": device["type_code"],
        "package_code": device["package_code"],
        "version": device["version"],
        "core_count": int(device["core_count"]),
        "term_length": int(device["term_length"]),
        "diverse_resource": diverse_resource,
        "secondary": devices[device["ha_peer"]] if device.get("ha_peer") else None,
        "cluster": {
            "cluster_name": f"{cluster['name']}-cluster",
            "nodes": [
                {
                    "hostname": clusters[cluster["name"]][index]["name"],
                    "token_variable": _token_variable(vc_name, cluster["name"], index),
                }
                for index in (0, 1)
            ],
        }
        if cluster
        else None,
    }


def render_equinix_network_devices(vc: dict[str, Any]) -> str:
    """Render the HCL for one virtual colocation.

    vc: {"name", "provider", "devices": [{name, metro_code, type_code, package_code, version, core_count,
    term_length, ha_peer?, diverse_from?, cluster?: {"name", "node"}}]}. A secondary (`ha_peer` target) is rendered
    inside its primary's resource and a cluster is one resource named after the cluster, never one per node.
    All validation happens here in Python; templates/configs/equinix/virtual_colocation.tf.j2 only prints the result.
    """
    vc_name = vc.get("name") or "unnamed"
    if vc.get("provider") != PROVIDER:
        raise ValueError(f"{vc_name}: provider {vc.get('provider')!r} is not {PROVIDER!r}; only Equinix is rendered")

    devices = {device["name"]: device for device in vc.get("devices") or []}
    if not devices:
        raise ValueError(f"{vc_name}: no devices to render")
    for device in devices.values():
        _check_device(vc_name, device)

    clusters = _group_clusters(vc_name, devices)
    secondaries: set[str] = set()
    for device in devices.values():
        peer = device.get("ha_peer")
        if peer is None:
            continue
        if peer not in devices:
            raise ValueError(
                f"{vc_name}: {device['name']} names ha_peer {peer!r}, which is not a device of this presence"
            )
        if peer in secondaries or devices[peer].get("cluster"):
            raise ValueError(f"{vc_name}: {peer} cannot be the secondary of {device['name']}")
        secondaries.add(peer)

    # device name -> the Terraform resource that contains it, so diverse_from can point at any member
    resource_of = {name: _label(vc_name, f"{vc_name}_{name}") for name in devices}
    for device in devices.values():
        if device.get("ha_peer"):
            resource_of[device["ha_peer"]] = resource_of[device["name"]]
        if device.get("cluster"):
            resource_of[device["name"]] = _label(vc_name, f"{vc_name}_{device['cluster']['name']}")

    resources: list[dict[str, Any]] = []
    rendered: set[str] = set()
    for name in sorted(devices):
        if name in secondaries or resource_of[name] in rendered:
            continue
        rendered.add(resource_of[name])
        resources.append(_resource_view(vc_name, name, devices, resource_of, clusters))

    license_variables = [
        _token_variable(vc_name, cluster_name, index) for cluster_name in sorted(clusters) for index in (0, 1)
    ]
    template = load_template(
        str(_TEMPLATE_DIR), _TEMPLATE, filters={"hcl": _q}, keep_trailing_newline=True, trim_blocks=True
    )
    return template.render(resources=resources, license_variables=license_variables)
