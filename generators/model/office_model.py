"""Pure Office fabric model compiler: validates office fabric_templates and derives pool/role intent without I/O."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from generators.mixins.types import NamingConvention, naming_convention_of
from utils.fabric_roles import CAMPUS_FABRIC_ROLES, LISP_BORDER_ROLES

ACCESS_ROLE = "access-switch"
DISTRIBUTION_ROLE = "distribution-switch"
OFFICE_VALID_FABRIC_ROLES = CAMPUS_FABRIC_ROLES | LISP_BORDER_ROLES
PHYSICAL_TEMPLATE_KIND = "TemplateDcimPhysicalDevice"


@dataclass(frozen=True)
class OfficeFabricModel:
    """Compiled, immutable blueprint of an Office LISP fabric's valid templates and warnings."""

    office_id: str
    office_name: str
    fabric_name: str
    naming_convention: NamingConvention
    usable_templates: tuple[dict[str, Any], ...]
    warnings: tuple[str, ...]
    access_quantity: int
    distribution_quantity: int

    @property
    def is_requested(self) -> bool:
        return len(self.usable_templates) > 0

    @classmethod
    def compile(cls, data: Mapping[str, Any]) -> OfficeFabricModel:
        """Compile a clean_data() TopologyCustomerOffice dictionary into a validated OfficeFabricModel."""
        office_id: str = data["id"]
        office_name: str = data.get("name") or office_id
        fabric_name = office_name.lower()

        usable: list[dict[str, Any]] = []
        warnings: list[str] = []
        seen_roles: set[str] = set()
        access_qty = 0
        dist_qty = 0

        for entry in data.get("fabric_templates") or []:
            role = entry.get("role")
            if role not in OFFICE_VALID_FABRIC_ROLES:
                warnings.append(
                    f"Office {fabric_name}: fabric_templates entry with role={role!r} is not valid in an "
                    f"office fabric (expected one of {sorted(OFFICE_VALID_FABRIC_ROLES)}) — skipping this entry."
                )
                continue
            qty = entry.get("quantity", 0)
            if not (isinstance(qty, int) and qty > 0):
                warnings.append(
                    f"Office {fabric_name}: fabric_templates entry for role={role!r} has quantity "
                    f"{qty!r} — skipping this entry."
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
            seen_roles.add(role)
            usable.append(entry)
            if role == ACCESS_ROLE:
                access_qty = qty
            elif role == DISTRIBUTION_ROLE:
                dist_qty = qty

        return cls(
            office_id=office_id,
            office_name=office_name,
            fabric_name=fabric_name,
            naming_convention=naming_convention_of(data, lower=False),
            usable_templates=tuple(usable),
            warnings=tuple(warnings),
            access_quantity=access_qty,
            distribution_quantity=dist_qty,
        )
