"""Transform: TopologyVirtualColocation -> Equinix Network Edge Terraform (HCL), one artifact per presence.

The kit of a virtual colocation is its DcimVirtualDevices, created by generators/topology/colocation_fabric.py from the
presence's `fabric_templates`. A device carries no link back to the template that produced it, but the
`fabric_templates` entries (role, quantity, template) do, so the ordering parameters of a device (type code, package,
version, term, cores) are read from the template of its role. Two entries of one role with different templates are
ambiguous and refused.

normalise_virtual_colocation() is the pure part: query result in, the plain-dict contract of
render_equinix_network_devices() out. It sets `ha_peer` and `cluster` from the device roles and counts. It never sets
`diverse_from`: no relation in the schema says that one device must sit away from another (the renderer supports it
for when one exists). Secrets and licences are never read; they are Terraform variables in the rendered file.
"""

from __future__ import annotations

import re
from typing import Any

from infrahub_sdk.transforms import InfrahubTransform

from transforms.colocation.virtual_terraform import render_equinix_network_devices
from utils.data_cleaning import clean_data

_KIND = "TopologyVirtualColocation"
_ORCHESTRATOR = "terraform"
_MAX_EDGES = 2
_MAX_FIREWALLS = 2
# renderer ordering field -> the virtual device template attribute it is read from
_TEMPLATE_FIELDS = {
    "type_code": "provider_type_code",
    "package_code": "provider_package_code",
    "version": "provider_version",
    "core_count": "cpu",
    "term_length": "provider_term_length",
}


def _virtual_colocation(cleaned: dict[str, Any]) -> dict[str, Any]:
    entries = cleaned.get(_KIND) or []
    if not entries:
        raise ValueError(f"no {_KIND} in the query result")
    return entries[0]


def _role_templates(vc_name: str, cleaned_vc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """role -> the one virtual device template its fabric_templates entries name."""
    by_role: dict[str, dict[str, dict[str, Any]]] = {}
    for entry in cleaned_vc.get("fabric_templates") or []:
        role = entry.get("role")
        template = entry.get("template") or {}
        if not template.get("template_name"):
            raise ValueError(
                f"{vc_name}: the {role!r} entry of fabric_templates does not name a virtual device template"
            )
        by_role.setdefault(role, {})[template["template_name"]] = template
    ambiguous = {role: sorted(templates) for role, templates in by_role.items() if len(templates) > 1}
    if ambiguous:
        raise ValueError(
            f"{vc_name}: fabric_templates name more than one template for a role, so the ordering parameters of its "
            f"devices are ambiguous: {ambiguous}"
        )
    return {role: next(iter(templates.values())) for role, templates in by_role.items()}


def _cluster_name(first_node: str) -> str:
    """fw-am-ne01 -> fw-am-ne: the generators number devices with two digits."""
    return re.sub(r"\d{2}$", "", first_node) or first_node


def _devices_of_role(
    vc_name: str, role: str, names: list[str], metro_code: str, template: dict[str, Any] | None
) -> list[dict[str, Any]]:
    if template is None:
        raise ValueError(f"{vc_name}: {role} devices {names} have no fabric_templates entry to take their order from")
    ordering = {field: template.get(attribute) for field, attribute in _TEMPLATE_FIELDS.items()}
    return [{"name": name, "metro_code": metro_code, **ordering} for name in names]


def normalise_virtual_colocation(cleaned: dict[str, Any]) -> dict[str, Any]:
    """The renderer's input for the virtual colocation in a cleaned `virtual_colocation_config` result.

    Provider: the colocation operator above the metro, lower-cased ("EQUINIX" -> "equinix"); the renderer refuses any
    other. metro_code is the metro's name: Equinix metro codes equal our metro names (FR, PA, AM).
    Roles: two edges are an HA pair (first by name -> second), one edge is a single device; two firewalls are one
    cluster (the renderer accepts PA-VM only), one firewall is a single device. More than that, a load balancer or
    any other role is refused. A missing ordering parameter is left empty for the renderer to report by device.
    """
    cleaned_vc = _virtual_colocation(cleaned)
    vc_name = cleaned_vc.get("name") or "unnamed"
    metro = cleaned_vc.get("parent") or {}
    operator = (metro.get("parent") or {}).get("name")
    if not metro.get("name") or not operator:
        raise ValueError(f"{vc_name}: has no parent metro with a colocation operator above it")

    templates = _role_templates(vc_name, cleaned_vc)
    names_by_role: dict[str, list[str]] = {}
    for device in cleaned_vc.get("devices") or []:
        names_by_role.setdefault(device.get("role"), []).append(device["name"])
    declared: dict[str, int] = {}
    for entry in cleaned_vc.get("fabric_templates") or []:
        declared[entry.get("role")] = declared.get(entry.get("role"), 0) + (entry.get("quantity") or 0)
    # The generator may have stopped midway (one of two edges created): rendering what exists would order half a pair.
    mismatched = {
        role: (len(names), declared.get(role, 0))
        for role, names in names_by_role.items()
        if role in declared and len(names) != declared[role]
    }
    if mismatched:
        raise ValueError(
            f"{vc_name}: devices do not match the fabric_templates quantities (role: devices, declared) "
            f"{mismatched}; run add_virtual_colocation before rendering"
        )
    unsupported = sorted(set(names_by_role) - {"edge", "firewall"})
    if unsupported:
        raise ValueError(f"{vc_name}: devices of role {unsupported} are not rendered; only edge and firewall are")

    devices: list[dict[str, Any]] = []
    edge_names = sorted(names_by_role.get("edge", []))
    if len(edge_names) > _MAX_EDGES:
        raise ValueError(f"{vc_name}: {len(edge_names)} edge devices {edge_names}; a presence has one or an HA pair")
    if edge_names:
        edges = _devices_of_role(vc_name, "edge", edge_names, metro["name"], templates.get("edge"))
        if len(edges) == _MAX_EDGES:
            edges[0]["ha_peer"] = edges[1]["name"]
        devices += edges

    firewall_names = sorted(names_by_role.get("firewall", []))
    if len(firewall_names) > _MAX_FIREWALLS:
        raise ValueError(
            f"{vc_name}: {len(firewall_names)} firewalls {firewall_names}; a presence has one or a two-node cluster"
        )
    if firewall_names:
        firewalls = _devices_of_role(vc_name, "firewall", firewall_names, metro["name"], templates.get("firewall"))
        if len(firewalls) == _MAX_FIREWALLS:
            cluster = _cluster_name(firewalls[0]["name"])
            for node, firewall in enumerate(firewalls):
                firewall["cluster"] = {"name": cluster, "node": node}
        devices += firewalls

    return {"name": vc_name, "provider": operator.lower(), "devices": devices}


class VirtualColocationTerraform(InfrahubTransform):
    """Equinix Network Edge Terraform for every device of one TopologyVirtualColocation."""

    query = "virtual_colocation_config"

    async def transform(self, data: Any) -> str:
        cleaned = clean_data(data)
        vc = _virtual_colocation(cleaned)
        if vc.get("infrastructure_orchestrator") != _ORCHESTRATOR:
            raise ValueError(
                f"{vc.get('name')}: infrastructure_orchestrator is {vc.get('infrastructure_orchestrator')!r}, "
                f"not {_ORCHESTRATOR!r}; no Terraform is rendered for it"
            )
        return render_equinix_network_devices(normalise_virtual_colocation(cleaned))
