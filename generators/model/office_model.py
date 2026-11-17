"""The office fabric model: which fabric_templates entries an office acts on and the sizes of its pools (pure, no
I/O)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from generators.helpers.naming import NamingConvention, naming_convention_of
from utils.fabric_roles import CAMPUS_FABRIC_ROLES, LISP_BORDER_ROLES

ACCESS_ROLE = "access-switch"
DISTRIBUTION_ROLE = "distribution-switch"
SDWAN_EDGE_ROLE = "sdwan-edge"
"""The role of the entry that builds the office's SD-WAN edge: at most one device, whatever its template."""
OFFICE_VALID_FABRIC_ROLES = CAMPUS_FABRIC_ROLES | LISP_BORDER_ROLES | {SDWAN_EDGE_ROLE}

PHYSICAL_TEMPLATE_KIND = "TemplateDcimPhysicalDevice"

# IPv4 and not design-driven (contrast the DC model), sized for a campus: a /28 for loopbacks and management, a /27 for
# the /31 links.
OFFICE_LOOPBACK_PREFIX_LENGTH = 28
OFFICE_MANAGEMENT_PREFIX_LENGTH = 28
OFFICE_TECHNICAL_PREFIX_LENGTH = 27


@dataclass(frozen=True)
class OfficeFabricModel:
    """What a TopologyCustomerOffice declares, compiled: the usable switch entries, the SD-WAN edge's template (the
    ``sdwan-edge`` entry, if any) and a warning per dropped entry; ``errors`` are declarations the run cannot act on."""

    office_id: str
    office_name: str
    fabric_name: str
    naming_convention: NamingConvention
    usable_templates: tuple[dict[str, Any], ...]
    edge_template: dict[str, Any] | None
    warnings: tuple[str, ...]
    errors: tuple[str, ...]

    @classmethod
    def compile(cls, data: Mapping[str, Any]) -> OfficeFabricModel:
        """Compile one clean_data() TopologyCustomerOffice entry; a missing id or name raises KeyError."""
        fabric_name = data["name"].lower()
        usable: list[dict[str, Any]] = []
        warnings: list[str] = []
        errors: list[str] = []
        seen_roles: set[str] = set()
        for entry in data.get("fabric_templates") or []:
            role = entry.get("role")
            if role not in OFFICE_VALID_FABRIC_ROLES:
                warnings.append(
                    f"Office {fabric_name}: fabric_templates entry with role={role!r} is not valid in an "
                    f"office fabric (expected one of {sorted(OFFICE_VALID_FABRIC_ROLES)}) — skipping this entry."
                )
                continue
            if not entry.get("quantity", 0) > 0:
                warnings.append(
                    f"Office {fabric_name}: fabric_templates entry for role={role!r} has quantity "
                    f"{entry.get('quantity')!r} — skipping this entry."
                )
                continue
            template = entry.get("template")
            if not template:
                warnings.append(
                    f"Office {fabric_name}: fabric_templates entry for role={role!r} has no template — "
                    "skipping this entry."
                )
                continue
            if template.get("template_kind") != PHYSICAL_TEMPLATE_KIND:
                warnings.append(
                    f"Office {fabric_name}: fabric_templates entry for role={role!r} uses a "
                    f"{template.get('template_kind') or 'unknown'} template — an office fabric is physical "
                    "switches only, skipping this entry."
                )
                continue
            if role in seen_roles:
                warnings.append(
                    f"Office {fabric_name}: a second fabric_templates entry for role={role!r} — only one "
                    "entry per role is supported (device names derive from the role), skipping it."
                )
                continue
            if role == SDWAN_EDGE_ROLE and entry["quantity"] > 1:
                errors.append(
                    f"Office {data['name']}: fabric_templates entry for role={role!r} has quantity "
                    f"{entry['quantity']} — an office has one SD-WAN edge (set quantity 1)."
                )
                continue
            seen_roles.add(role)
            usable.append(entry)
        edge = next((entry["template"] for entry in usable if entry["role"] == SDWAN_EDGE_ROLE), None)
        return cls(
            office_id=data["id"],
            office_name=data["name"],
            fabric_name=fabric_name,
            naming_convention=naming_convention_of(data),
            usable_templates=tuple(entry for entry in usable if entry["role"] != SDWAN_EDGE_ROLE),
            edge_template=edge,
            warnings=tuple(warnings),
            errors=tuple(errors),
        )
