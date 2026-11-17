"""The colocation fabric model: which fabric_templates entries a zone or virtual colocation acts on, held to its design
envelope (pure, no I/O)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from utils.exchange_transit import BORDER_ROLE_FOR_SERVICES

# Roles a zone or virtual colocation can host; unlike a DC, `edge` is valid and DC-only fabric tiers are not
COLO_VALID_FABRIC_ROLES = frozenset({"edge", "internal-firewall", "load-balancer"})

# Service appliances hanging off the on-ramp, HA-paired and given no loopback. Their template must provide a role=ha
# interface (the sync cable needs one per member), so a model with no such port cannot be paired.
COLO_SERVICE_ROLES = frozenset({"internal-firewall", "load-balancer"})

# The template kind each class may instantiate: a zone holds our hardware, a virtual colocation provider-hosted
# appliances
COLO_TEMPLATE_KIND_PHYSICAL = "TemplateDcimPhysicalDevice"
COLO_TEMPLATE_KIND_APPLIANCE = "TemplateCloudNetworkAppliance"

# The design maximum each role is held to (TopologyTemplateColocationZone).
COLO_DESIGN_MAX_BY_ROLE = {
    "edge": "max_edges",
    "internal-firewall": "max_firewalls",
    "load-balancer": "max_load_balancers",
}


@dataclass(frozen=True)
class ColocationFabricModel:
    """What a zone or virtual colocation declares, compiled: the usable entries and a warning per dropped one
    (a warning, not an error, so the other roles are still created).
    """

    fabric_name: str
    label: str
    usable_templates: tuple[dict[str, Any], ...]
    warnings: tuple[str, ...]

    @classmethod
    def compile(cls, data: Mapping[str, Any], *, label: str, template_kind: str) -> ColocationFabricModel:
        """Compile one clean_data() zone or virtual colocation entry; ``label`` and ``template_kind`` are the class's
        (a zone, physical templates; a virtual colocation, appliances).
        """
        fabric_name = data["name"].lower()
        prefix = f"Colocation {label} {fabric_name}"
        warnings: list[str] = []
        usable: list[dict[str, Any]] = []
        for entry in data.get("fabric_templates", []):
            role = entry.get("role")
            if role not in COLO_VALID_FABRIC_ROLES:
                warnings.append(
                    f"{prefix}: fabric_templates entry with role={role!r} is not "
                    f"valid here (expected one of {sorted(COLO_VALID_FABRIC_ROLES)}) — skipping this entry."
                )
                continue
            if not entry.get("quantity", 0) > 0:
                warnings.append(
                    f"{prefix}: fabric_templates entry for role={role!r} has "
                    f"quantity {entry.get('quantity')!r} — skipping this entry."
                )
                continue
            template = entry.get("template")
            if not template:
                warnings.append(
                    f"{prefix}: fabric_templates entry for role={role!r} has no template — skipping this entry."
                )
                continue
            kind = template.get("template_kind")
            if kind != template_kind:
                warnings.append(
                    f"{prefix}: fabric_templates entry for role={role!r} uses a "
                    f"{kind or 'unknown'} template, which a {label} cannot host (expected "
                    f"{template_kind}) — skipping this entry."
                )
                continue
            usable.append(entry)

        usable, cablable_warnings = _drop_uncablable_service_entries(usable, prefix=prefix, label=label)
        usable, design_warnings = _within_design(usable, data, prefix=prefix, label=label)
        return cls(
            fabric_name=fabric_name,
            label=label,
            usable_templates=tuple(usable),
            warnings=tuple([*warnings, *cablable_warnings, *design_warnings]),
        )


def _drop_uncablable_service_entries(
    entries: list[dict[str, Any]], *, prefix: str, label: str
) -> tuple[list[dict[str, Any]], list[str]]:
    """Drop a physical firewall/load-balancer entry that no physical edge template can cable to, warning why
    (virtual entries are exempt).
    """
    physical_edge_entries = [
        entry
        for entry in entries
        if entry.get("role") == "edge"
        and (entry.get("template") or {}).get("template_kind") == COLO_TEMPLATE_KIND_PHYSICAL
    ]
    if not physical_edge_entries:
        return entries, []

    physical_edge_port_roles: set[str] = set()
    for entry in physical_edge_entries:
        for iface in (entry.get("template") or {}).get("interfaces") or []:
            role = iface.get("role")
            if role:
                physical_edge_port_roles.add(role)

    usable: list[dict[str, Any]] = []
    warnings: list[str] = []
    for entry in entries:
        role = entry.get("role")
        template = entry.get("template") or {}
        is_physical_service = (
            role in COLO_SERVICE_ROLES and template.get("template_kind") == COLO_TEMPLATE_KIND_PHYSICAL
        )
        port_role = BORDER_ROLE_FOR_SERVICES.get(str(role), str(role))
        if is_physical_service and port_role not in physical_edge_port_roles:
            warnings.append(
                f"{prefix}: fabric_templates entry for role={role!r} has no "
                f"matching {port_role!r}-role port on any physical edge template in this {label} (available "
                f"edge port roles: {sorted(physical_edge_port_roles) or 'none'}) — skipping this entry rather "
                "than creating an appliance with nothing to cable it to. Point the edge template at a device "
                f"with a {port_role!r}-role port block, or drop this entry."
            )
            continue
        usable.append(entry)
    return usable, warnings


def _within_design(
    entries: list[dict[str, Any]], data: Mapping[str, Any], *, prefix: str, label: str
) -> tuple[list[dict[str, Any]], list[str]]:
    """Hold the entries to the design envelope, warning per finding (never an error); no design means entries pass.
    A role over the design maximum has its entries skipped whole, in template-id order, before any device exists.
    """
    design = data.get("design")
    if not entries or not design:
        return entries, []

    warnings: list[str] = []
    metro_design = (data.get("parent") or {}).get("design")
    if metro_design:
        allowed = sorted(template["name"] for template in metro_design["zone_templates"])
        if design["name"] not in allowed:
            warnings.append(
                f"{prefix}: design '{design['name']}' is not one of metro "
                f"design '{metro_design['name']}' zone_templates {allowed}, so the metro's pools were not sized "
                "for it."
            )

    total_by_role: dict[str, int] = {}
    skipped: set[int] = set()
    for index in sorted(
        range(len(entries)),
        key=lambda i: (str(entries[i]["template"].get("id")), entries[i]["quantity"], entries[i]["role"]),
    ):
        entry = entries[index]
        role = entry["role"]
        maximum = design[COLO_DESIGN_MAX_BY_ROLE[role]]
        total = total_by_role.get(role, 0) + entry["quantity"]
        if total > maximum:
            warnings.append(
                f"{prefix}: fabric_templates entry for role={role!r} "
                f"(quantity {entry['quantity']}) would bring the {role} total to {total}, past the "
                f"{COLO_DESIGN_MAX_BY_ROLE[role]}={maximum} of design '{design['name']}' its pools are sized "
                "for — skipping this entry."
            )
            skipped.add(index)
            continue
        total_by_role[role] = total
    return [entry for index, entry in enumerate(entries) if index not in skipped], warnings
