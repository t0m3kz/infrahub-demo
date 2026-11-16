from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any


def template_interface_names_by_role(*, interfaces: Iterable[Any], role: str | None = None) -> list[str]:
    """Interface names from template interfaces (SDK objects or dicts), optionally filtered by role."""

    names: list[str] = []
    for interface in interfaces:
        iface_name = interface.get("name") if isinstance(interface, dict) else getattr(interface, "name", None)
        iface_role = interface.get("role") if isinstance(interface, dict) else getattr(interface, "role", None)
        if not iface_name:
            continue
        if role is not None and iface_role != role:
            continue
        names.append(str(iface_name))
    return names


@dataclass(frozen=True)
class TemplateReads:
    """What the device templates give a plan: each template's ports, and its owner (a device made from the template
    gets the template's owner from the server on create; an upsert by HFID must send it, being validated as a create).
    """

    ports: dict[str, list[TemplatePort]]
    owners: dict[str, str | None]


@dataclass(frozen=True)
class TemplatePort:
    """One port a device template gives every device built from it."""

    name: str
    role: str | None
    interface_type: str | None


async def read_template_ports(client: Any, template_ids: Iterable[str]) -> TemplateReads:
    """Every port of each template, and each template's owner. The ports of a template are the ports of every device
    made from it.

    ``include=["interfaces"]`` loads the ports through the generic interface relationship, so a peer carries only the
    generic's fields (name, role): ``interface_type`` is on the concrete kind and comes back empty (measured on 1.12).
    The ports are therefore read again by their concrete kind, one read per kind, for every field the plan needs.
    """
    ids = sorted(set(template_ids))
    if not ids:
        return TemplateReads(ports={}, owners={})
    templates = await client.filters(kind="TemplateDcimPhysicalDevice", ids=ids, include=["interfaces", "owner"])
    port_ids_by_kind: dict[str, list[str]] = {}
    for template in templates:
        for relation in template.interfaces.peers:
            port_ids_by_kind.setdefault(relation.typename, []).append(relation.id)
    concrete: dict[str, Any] = {}
    for kind, port_ids in sorted(port_ids_by_kind.items()):
        for port in await client.filters(kind=kind, ids=sorted(set(port_ids))):
            concrete[port.id] = port
    ports: dict[str, list[TemplatePort]] = {template.id: [] for template in templates}
    for template in templates:
        for relation in template.interfaces.peers:
            port = concrete.get(relation.id) or relation.peer
            ports[template.id].append(
                TemplatePort(
                    name=port.name.value,
                    role=getattr(getattr(port, "role", None), "value", None),
                    interface_type=getattr(getattr(port, "interface_type", None), "value", None),
                )
            )
    owners = {template.id: getattr(getattr(template, "owner", None), "id", None) for template in templates}
    return TemplateReads(ports=ports, owners=owners)
