from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any

from generators.mixins.types import DeviceOptions, RoutingOptions
from utils.data_cleaning import clean_data

from .naming import DeviceNameContext, DeviceNamingConfig

_RACK_ROLE_TO_BUCKET: dict[str, str] = {
    "leaf": "leafs",
    "tor": "tors",
    "l2-leaf": "l2_leafs",
    "access-leaf": "access_leafs",
}

_POD_SPINE_ROLES: frozenset[str] = frozenset({"spine", "border-spine"})

ALLOWED_ROLES_BY_RACK_TYPE: dict[str, frozenset[str]] = {
    "network": frozenset(
        {
            "super-spine",
            "hyper-spine",
            "border-spine",
            "spine",
            "border-leaf",
            "leaf",
            "l2-leaf",
            "access-leaf",
            "internal-firewall",
            "load-balancer",
        }
    ),
    "tor": frozenset({"tor", "l2-leaf", "access-leaf"}),
    "compute": frozenset({"tor", "l2-leaf", "access-leaf"}),
    "storage": frozenset({"tor", "l2-leaf", "access-leaf"}),
}

_DC_TIER_ROLES: frozenset[str] = frozenset(
    {"super-spine", "hyper-spine", "border-leaf", "internal-firewall", "load-balancer"}
)


def rack_type_role_errors(rack: Mapping[str, Any], roles: Collection[str]) -> list[str]:
    """Return error messages for roles forbidden on ``rack['rack_type']`` (e.g. super-spine/spine/leaf on compute)."""
    raw_type = rack.get("rack_type")
    if not raw_type:
        return []
    rack_type = str(raw_type)
    allowed = ALLOWED_ROLES_BY_RACK_TYPE.get(rack_type)
    if allowed is None:
        return []
    normalized = {str(r).replace("_", "-") for r in roles}
    forbidden = sorted(r for r in normalized if r not in allowed)
    if not forbidden:
        return []
    rack_name = rack.get("name") or rack.get("id") or "<unknown>"
    return [
        f"Rack {rack_name} has rack_type={rack_type!r} which cannot host role(s) {forbidden} "
        f"(allowed: {sorted(allowed)})"
    ]


def validate_rack_device_roles(rack: Mapping[str, Any], roles: Collection[str]) -> None:
    """Raise ValueError when any role in ``roles`` is forbidden on ``rack['rack_type']``."""
    errors = rack_type_role_errors(rack, roles)
    if errors:
        raise ValueError(errors)


def _extract_rack_entries_for_roles(
    racks: list[dict[str, Any]],
    target_roles: frozenset[str],
) -> list[dict[str, Any]]:
    """Collect template or pre-created device entries for ``target_roles`` from physical racks, preserving ``rack_id``."""
    extracted: list[dict[str, Any]] = []
    for rack in sorted(racks, key=lambda r: (r.get("row_index") or 0, r.get("index") or 0, str(r.get("name") or ""))):
        if not isinstance(rack, dict):
            continue
        rack_id = rack.get("id")
        rack_roles: list[str] = []

        # 1. Explicit fabric_templates entries on the rack
        for ft in rack.get("fabric_templates") or []:
            role = ft.get("role")
            if role:
                rack_roles.append(str(role))
                if role in target_roles:
                    extracted.append({**ft, "rack_id": rack_id})

        # 2. Primary switch_template + switch_quantity on the rack
        sw_tpl = rack.get("switch_template")
        sw_qty = rack.get("switch_quantity")
        sw_role = rack.get("switch_role")
        if sw_tpl and sw_qty is not None and int(sw_qty) > 0 and sw_role:
            rack_roles.append(str(sw_role))
            if sw_role in target_roles:
                extracted.append({"role": sw_role, "quantity": int(sw_qty), "template": sw_tpl, "rack_id": rack_id})

        # 3. Direct individual DcimPhysicalDevice nodes placed in the rack
        direct_by_role: dict[str, list[dict[str, Any]]] = {}
        for dev in rack.get("devices") or []:
            if isinstance(dev, dict) and dev.get("role"):
                d_role = str(dev["role"])
                rack_roles.append(d_role)
                if d_role in target_roles:
                    direct_by_role.setdefault(d_role, []).append(dev)
        for role, devs in direct_by_role.items():
            first_ifaces = list(devs[0].get("interfaces") or [])
            extracted.append(
                {
                    "role": role,
                    "quantity": len(devs),
                    "template": {"interfaces": first_ifaces},
                    "devices": devs,
                    "rack_id": rack_id,
                }
            )

        validate_rack_device_roles(rack, rack_roles)
    return extracted


def normalize_dc_templates(dc: dict[str, Any]) -> dict[str, Any]:
    """Synthesize DC fabric_templates from DC-attached core racks when present, validating rack_type compatibility."""
    existing = list(dc.get("fabric_templates") or [])
    rack_entries = _extract_rack_entries_for_roles(list(dc.get("racks") or []), _DC_TIER_ROLES)
    if rack_entries:
        rack_roles = {e["role"] for e in rack_entries}
        existing = [*rack_entries, *[e for e in existing if e.get("role") not in rack_roles]]
    if existing:
        dc["fabric_templates"] = existing
    return dc


def normalize_pod_templates(pod: dict[str, Any]) -> dict[str, Any]:
    """Ensure pod['fabric_templates'] reflects typed spine_role/spine_quantity/spine_template or direct spine devices."""
    existing = list(pod.get("fabric_templates") or [])
    spine_role = pod.get("spine_role") or "spine"
    spine_tpl = pod.get("spine_template")
    spine_qty = pod.get("spine_quantity")
    if spine_tpl and spine_qty is None:
        raw_design = pod.get("design")
        design: Mapping[str, Any] = raw_design if isinstance(raw_design, Mapping) else {}
        max_sp = int(design.get("max_spines_per_pod") or 0)
        spine_qty = min(2, max_sp) if max_sp > 0 else 2
    if spine_tpl and spine_qty is not None and int(spine_qty) > 0:
        entry = {"role": spine_role, "quantity": int(spine_qty), "template": spine_tpl}
        existing = [entry, *[e for e in existing if e.get("role") not in _POD_SPINE_ROLES]]
    elif not existing:
        direct_spines = [
            d for d in (pod.get("devices") or []) if isinstance(d, dict) and d.get("role") in _POD_SPINE_ROLES
        ]
        if direct_spines:
            role = str(direct_spines[0]["role"])
            existing.append(
                {
                    "role": role,
                    "quantity": len(direct_spines),
                    "template": {"interfaces": list(direct_spines[0].get("interfaces") or [])},
                    "devices": direct_spines,
                }
            )
    rack_spines = _extract_rack_entries_for_roles(list(pod.get("racks") or []), _POD_SPINE_ROLES)
    if rack_spines:
        existing = [*rack_spines, *[e for e in existing if e.get("role") not in _POD_SPINE_ROLES]]
    if existing:
        pod["fabric_templates"] = existing
    return pod


def _default_switch_role_for_rack(rack: Mapping[str, Any]) -> str:
    """Infer default switch role from rack_type and pod deployment_type when switch_role is omitted."""
    rack_type = str(rack.get("rack_type") or "compute")
    if rack_type == "network":
        return "leaf"
    raw_pod = rack.get("deployment")
    pod: Mapping[str, Any] = raw_pod if isinstance(raw_pod, Mapping) else {}
    raw_design = pod.get("design")
    design: Mapping[str, Any] = raw_design if isinstance(raw_design, Mapping) else {}
    if design.get("max_tors_per_compute_rack") == 0:
        return "l2-leaf"
    return "tor"


def _default_switch_quantity_for_rack(rack: Mapping[str, Any], switch_role: str) -> int | None:
    """Infer default switch count from pod design capacity by rack_type when switch_quantity is omitted."""
    raw_pod = rack.get("deployment")
    pod: Mapping[str, Any] = raw_pod if isinstance(raw_pod, Mapping) else {}
    raw_design = pod.get("design")
    design: Mapping[str, Any] = raw_design if isinstance(raw_design, Mapping) else {}
    if switch_role == "leaf":
        val = int(design.get("max_leafs_per_network_rack") or 0)
        return val if val > 0 else 2
    if switch_role == "tor":
        val = int(design.get("max_tors_per_compute_rack") or 0)
        return val if val > 0 else 2
    if switch_role in ("l2-leaf", "access-leaf"):
        return 2
    return None


def resolve_rack_row_and_suite(rack: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Resolve optional LocationRackRow and parent LocationSuite from ``rack['parent']``."""
    parent = rack.get("parent") if isinstance(rack.get("parent"), dict) else None
    if not parent:
        return None, None
    # LocationRackRow has 'row_type' or a nested 'parent' (LocationSuite); LocationSuite has 'rows'.
    if "row_type" in parent or ("rows" not in parent and isinstance(parent.get("parent"), dict)):
        suite = parent.get("parent") if isinstance(parent.get("parent"), dict) else None
        return parent, suite
    return None, parent


def validate_rack_row_compatibility(rack: Mapping[str, Any], row: Mapping[str, Any] | None) -> None:
    """Enforce that a rack's ``rack_type`` matches its parent ``LocationRackRow.row_type`` when row_type != 'mixed'."""
    if not row:
        return
    row_type = str(row.get("row_type") or "mixed")
    if row_type == "mixed":
        return
    rack_type = str(rack.get("rack_type") or "")
    if rack_type and rack_type != row_type:
        rack_name = rack.get("name") or rack.get("id") or "<unknown>"
        row_name = row.get("name") or f"row-{row.get('index', '?')}"
        raise ValueError(
            f"Rack {rack_name} has rack_type={rack_type!r} which is incompatible with row {row_name} "
            f"(row_type={row_type!r})"
        )


def normalize_rack_templates(rack: dict[str, Any]) -> dict[str, Any]:
    """Resolve LocationRackRow/LocationSuite context and ensure rack role buckets reflect templates or devices."""
    row, suite = resolve_rack_row_and_suite(rack)
    if row is not None:
        rack["row"] = row
        if rack.get("row_index") is None and row.get("index") is not None:
            rack["row_index"] = int(row["index"])
        validate_rack_row_compatibility(rack, row)
    if suite is not None:
        rack["suite"] = suite
    switch_role = rack.get("switch_role") or _default_switch_role_for_rack(rack)
    rack_roles_present: list[str] = [
        *(str(ft["role"]) for ft in (rack.get("fabric_templates") or []) if ft.get("role")),
        *(str(d["role"]) for d in (rack.get("devices") or []) if isinstance(d, dict) and d.get("role")),
    ]
    if rack.get("switch_template"):
        rack_roles_present.append(switch_role)
    validate_rack_device_roles(rack, rack_roles_present)
    switch_tpl = rack.get("switch_template")
    switch_qty = rack.get("switch_quantity")
    if switch_tpl and switch_qty is None:
        switch_qty = _default_switch_quantity_for_rack(rack, switch_role)
    if switch_role in _RACK_ROLE_TO_BUCKET and switch_tpl and switch_qty is not None and int(switch_qty) > 0:
        bucket = _RACK_ROLE_TO_BUCKET[switch_role]
        rack[bucket] = [{"role": switch_role, "quantity": int(switch_qty), "template": switch_tpl}]
        # Also materialize any secondary roles present in fabric_templates (e.g. l2-leaf below a primary leaf pair)
        for ft in rack.get("fabric_templates") or []:
            ft_role = ft.get("role")
            if ft_role in _RACK_ROLE_TO_BUCKET and ft_role != switch_role:
                sec_bucket = _RACK_ROLE_TO_BUCKET[ft_role]
                if not rack.get(sec_bucket):
                    rack[sec_bucket] = [ft]
    else:
        direct_by_role: dict[str, list[dict[str, Any]]] = {}
        for dev in rack.get("devices") or []:
            if isinstance(dev, dict) and dev.get("role") in _RACK_ROLE_TO_BUCKET:
                direct_by_role.setdefault(str(dev["role"]), []).append(dev)
        for role, devs in direct_by_role.items():
            bucket = _RACK_ROLE_TO_BUCKET[role]
            if not rack.get(bucket):
                rack[bucket] = [
                    {
                        "role": role,
                        "quantity": len(devs),
                        "template": {"interfaces": list(devs[0].get("interfaces") or [])},
                        "devices": devs,
                    }
                ]
    if isinstance(rack.get("deployment"), dict):
        normalize_pod_templates(rack["deployment"])
    return rack


def pod_row_racks(
    pod: Mapping[str, Any], row_index: int, rack_types: Collection[str] | None = None
) -> list[dict[str, Any]]:
    """The racks of one row of a pod from its ``racks`` projection, ordered by (row, index, name).
    ``rack_types`` narrows them; None keeps every type."""
    return sorted(
        (
            rack
            for rack in pod.get("racks", [])
            if rack["row_index"] == row_index and (rack_types is None or rack["rack_type"] in rack_types)
        ),
        key=lambda rack: (rack["row_index"], rack["index"], rack["name"]),
    )


def parse_rack_data(data: dict[str, Any]) -> dict[str, Any]:
    """Normalize trigger/query data into a plain LocationRack dict.
    If data["name"] is a wrapped dict it is returned as-is; a GraphQL result goes through clean_data()."""
    if "name" in data and isinstance(data.get("name"), dict):
        return data
    if "LocationRack" in data:
        raw = data["LocationRack"]
        if isinstance(raw, dict) and "edges" in raw and not raw["edges"]:
            raise ValueError(
                "GraphQL query returned no edges for LocationRack — "
                "rack may not exist or query parameters may be incorrect."
            )
        deployment_list = clean_data(data).get("LocationRack", [])
        if not deployment_list:
            raise ValueError("No rack found after clean_data — rack exists but has an invalid data structure.")
        return normalize_rack_templates(deployment_list[0])
    raise ValueError(f"Unknown data structure. Keys: {list(data.keys())}")


def expected_device_names(
    *,
    naming_config: DeviceNamingConfig,
    fabric_name: str,
    device_indexes: list[int],
    role: str,
    quantity: int,
) -> set[str]:
    """Deterministic device names for one role template."""
    return {
        naming_config.format_device_name(
            DeviceNameContext.from_indexes(
                fabric_name=fabric_name,
                device_role=role,
                role_index=idx,
                indexes=device_indexes,
            )
        )
        for idx in range(1, quantity + 1)
    }


class RackRolesHelper:
    """Preparation-only helper for rack role generation: no database operations."""

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx

    def expected_names(self, *, role: str, quantity: int) -> set[str]:
        """Deterministic device names for one role template."""
        return expected_device_names(
            naming_config=DeviceNamingConfig(strategy=self.ctx._naming_conv),
            fabric_name=self.ctx.fabric_name,
            device_indexes=self.ctx._device_indexes,
            role=role,
            quantity=quantity,
        )

    def build_device_options(
        self,
        *,
        allocate_loopback: bool,
        mlag: bool = False,
        mlag_supports_virtual: bool = True,
    ) -> DeviceOptions:
        """DeviceOptions payload for role device creation; mlag=True passes the pod's mlag_create setting."""
        options = DeviceOptions(
            indexes=self.ctx._device_indexes,
            allocate_loopback=allocate_loopback,
            rack=self.ctx.data["id"],
            management_pool=self.ctx._management_pool_id,
        )
        if allocate_loopback:
            options["loopback_pool"] = self.ctx._loopback_pool_id
            options["loopback_prefix_length"] = self.ctx._loopback_prefix_length
        if mlag:
            options["mlag_create"] = self.ctx.data["deployment"].get("mlag_create", "no")
            options["mlag_supports_virtual"] = mlag_supports_virtual
        return options

    def overlay_only_routing_options(self) -> RoutingOptions:
        """Routing options payload for overlay-only access-leaf peering."""
        return {**self.ctx._routing_options, "skip_underlay": True}


def rack_row_index(rack: Mapping[str, Any]) -> int:
    """Return the rack's row index from its parent LocationRackRow (falling back to legacy row_index if present)."""
    parent = rack.get("parent")
    if isinstance(parent, Mapping) and "index" in parent and ("row_type" in parent or "rows" not in parent):
        return int(parent["index"])
    row = rack.get("row")
    if isinstance(row, Mapping) and row.get("index") is not None:
        return int(row["index"])
    return int(rack["row_index"])
