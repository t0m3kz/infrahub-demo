"""Pure Colocation fabric model compiler: supports physical, virtual, and mixed colocation flavors without I/O."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from utils.exchange_transit import BORDER_ROLE_FOR_SERVICES

COLO_VALID_FABRIC_ROLES = frozenset({"edge", "internal-firewall", "load-balancer"})
COLO_SERVICE_ROLES = frozenset({"internal-firewall", "load-balancer"})
COLO_TEMPLATE_KIND_PHYSICAL = "TemplateDcimPhysicalDevice"
COLO_TEMPLATE_KIND_APPLIANCE = "TemplateCloudNetworkAppliance"

COLO_DESIGN_MAX_BY_ROLE: dict[str, str] = {
    "edge": "max_edges",
    "internal-firewall": "max_firewalls",
    "load-balancer": "max_load_balancers",
}

ColocationFlavor = Literal["physical", "virtual", "mixed"]

ALLOWED_TEMPLATE_KINDS_BY_FLAVOR: dict[ColocationFlavor, frozenset[str]] = {
    "physical": frozenset({COLO_TEMPLATE_KIND_PHYSICAL}),
    "virtual": frozenset({COLO_TEMPLATE_KIND_APPLIANCE}),
    "mixed": frozenset({COLO_TEMPLATE_KIND_PHYSICAL, COLO_TEMPLATE_KIND_APPLIANCE}),
}


@dataclass(frozen=True)
class ColocationFabricModel:
    """Compiled, immutable blueprint of a Colocation deployment (physical, virtual, or mixed)."""

    node_id: str
    node_name: str
    fabric_name: str
    label: str
    flavor: ColocationFlavor
    effective_flavor: Literal["physical", "virtual", "mixed", "empty"]
    usable_templates: tuple[dict[str, Any], ...]
    physical_templates: tuple[dict[str, Any], ...]
    virtual_templates: tuple[dict[str, Any], ...]
    warnings: tuple[str, ...]
    has_design: bool
    slices_from_metro: bool
    metro_id: str | None

    @property
    def is_requested(self) -> bool:
        return len(self.usable_templates) > 0

    @classmethod
    def compile(
        cls,
        data: Mapping[str, Any],
        *,
        flavor: ColocationFlavor = "physical",
        label: str = "zone",
    ) -> ColocationFabricModel:
        """Compile a clean_data() TopologyColocationZone / TopologyVirtualColocation payload."""
        node_id: str = data["id"]
        node_name: str = data["name"]
        fabric_name = node_name.lower()
        design: dict[str, Any] | None = data.get("design")
        metro: dict[str, Any] = data.get("parent") or {}
        metro_design: dict[str, Any] | None = metro.get("design")
        slices_from_metro = bool(metro_design and metro.get("id"))

        allowed_kinds = ALLOWED_TEMPLATE_KINDS_BY_FLAVOR[flavor]
        warnings: list[str] = []
        raw_usable: list[dict[str, Any]] = []

        for entry in data.get("fabric_templates") or []:
            role = entry.get("role")
            if role not in COLO_VALID_FABRIC_ROLES:
                warnings.append(
                    f"Colocation {label} {fabric_name}: fabric_templates entry with role={role!r} is not "
                    f"valid here (expected one of {sorted(COLO_VALID_FABRIC_ROLES)}) — skipping this entry."
                )
                continue
            qty = entry.get("quantity", 0)
            if not (isinstance(qty, int) and qty > 0):
                warnings.append(
                    f"Colocation {label} {fabric_name}: fabric_templates entry for role={role!r} has "
                    f"quantity {qty!r} — skipping this entry."
                )
                continue
            template = entry.get("template")
            if not template:
                warnings.append(
                    f"Colocation {label} {fabric_name}: fabric_templates entry for role={role!r} has "
                    "no template — skipping this entry."
                )
                continue
            template_kind = template.get("template_kind")
            if template_kind not in allowed_kinds:
                expected_str = (
                    next(iter(allowed_kinds)) if len(allowed_kinds) == 1 else f"one of {sorted(allowed_kinds)}"
                )
                warnings.append(
                    f"Colocation {label} {fabric_name}: fabric_templates entry for role={role!r} uses a "
                    f"{template_kind or 'unknown'} template, which a {label} cannot host (expected "
                    f"{expected_str}) — skipping this entry."
                )
                continue
            raw_usable.append(entry)

        physical_edge_entries = [
            e
            for e in raw_usable
            if e.get("role") == "edge" and (e.get("template") or {}).get("template_kind") == COLO_TEMPLATE_KIND_PHYSICAL
        ]
        cablable: list[dict[str, Any]] = []
        if not physical_edge_entries:
            cablable = raw_usable
        else:
            physical_edge_port_roles: set[str] = set()
            for edge_entry in physical_edge_entries:
                for iface in (edge_entry.get("template") or {}).get("interfaces") or []:
                    port_role = iface.get("role")
                    if port_role:
                        physical_edge_port_roles.add(port_role)

            for entry in raw_usable:
                role = entry.get("role")
                template = entry.get("template") or {}
                is_physical_service = (
                    role in COLO_SERVICE_ROLES and template.get("template_kind") == COLO_TEMPLATE_KIND_PHYSICAL
                )
                needed_port_role = BORDER_ROLE_FOR_SERVICES.get(str(role), str(role))
                if is_physical_service and needed_port_role not in physical_edge_port_roles:
                    warnings.append(
                        f"Colocation {label} {fabric_name}: fabric_templates entry for role={role!r} has no "
                        f"matching {needed_port_role!r}-role port on any physical edge template in this {label} "
                        f"(available edge port roles: {sorted(physical_edge_port_roles) or 'none'}) — skipping "
                        "this entry rather than creating an appliance with nothing to cable it to. Point the edge "
                        f"template at a device with a {needed_port_role!r}-role port block, or drop this entry."
                    )
                    continue
                cablable.append(entry)

        bounded: list[dict[str, Any]] = cablable
        if cablable and design:
            if metro_design:
                allowed = sorted(template["name"] for template in metro_design["zone_templates"])
                if design["name"] not in allowed:
                    warnings.append(
                        f"Colocation {label} {fabric_name}: design {design['name']!r} is not one of metro "
                        f"design {metro_design['name']!r} zone_templates {allowed}, so the metro's pools were "
                        "not sized for it."
                    )

            total_by_role: dict[str, int] = {}
            skipped: set[int] = set()
            for index in sorted(
                range(len(cablable)),
                key=lambda i: (str(cablable[i]["template"].get("id")), cablable[i]["quantity"], cablable[i]["role"]),
            ):
                entry = cablable[index]
                role = entry["role"]
                maximum = design[COLO_DESIGN_MAX_BY_ROLE[role]]
                total = total_by_role.get(role, 0) + entry["quantity"]
                if total > maximum:
                    warnings.append(
                        f"Colocation {label} {fabric_name}: fabric_templates entry for role={role!r} "
                        f"(quantity {entry['quantity']}) would bring the {role} total to {total}, past the "
                        f"{COLO_DESIGN_MAX_BY_ROLE[role]}={maximum} of design {design['name']!r} its pools are "
                        "sized for — skipping this entry."
                    )
                    skipped.add(index)
                    continue
                total_by_role[role] = total
            bounded = [entry for index, entry in enumerate(cablable) if index not in skipped]

        physical_templates = tuple(
            e for e in bounded if (e.get("template") or {}).get("template_kind") == COLO_TEMPLATE_KIND_PHYSICAL
        )
        virtual_templates = tuple(
            e for e in bounded if (e.get("template") or {}).get("template_kind") == COLO_TEMPLATE_KIND_APPLIANCE
        )

        if physical_templates and virtual_templates:
            effective_flavor: Literal["physical", "virtual", "mixed", "empty"] = "mixed"
        elif physical_templates:
            effective_flavor = "physical"
        elif virtual_templates:
            effective_flavor = "virtual"
        else:
            effective_flavor = "empty"

        return cls(
            node_id=node_id,
            node_name=node_name,
            fabric_name=fabric_name,
            label=label,
            flavor=flavor,
            effective_flavor=effective_flavor,
            usable_templates=tuple(bounded),
            physical_templates=physical_templates,
            virtual_templates=virtual_templates,
            warnings=tuple(warnings),
            has_design=design is not None,
            slices_from_metro=slices_from_metro,
            metro_id=metro.get("id"),
        )
