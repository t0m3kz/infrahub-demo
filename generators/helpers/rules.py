from __future__ import annotations

from typing import Any


class RulePlanningHelper:
    """Generic helpers for deterministic rule naming and descriptions."""

    @staticmethod
    def normalize_name_part(value: Any, fallback: str) -> str:
        raw = str(value or fallback).strip().lower()
        return raw.replace(" ", "-")

    @classmethod
    def flow_rule_name(cls, scope_name: str, src_label: Any, dst_label: Any) -> str:
        """Build deterministic flow rule name from scope and endpoints."""
        src_norm = cls.normalize_name_part(src_label, "src")
        dst_norm = cls.normalize_name_part(dst_label, "dst")
        return f"{scope_name}-{src_norm}-to-{dst_norm}"

    @staticmethod
    def flow_rule_description(
        *,
        explicit_description: Any,
        src_name: Any,
        src_type: Any,
        dst_name: Any,
        dst_type: Any,
    ) -> str:
        """Build default flow description when explicit one is not set."""
        if explicit_description:
            return str(explicit_description)

        src_type_value = src_type or "backend"
        dst_type_value = dst_type or "backend"
        return f"Auto-generated: {src_name or '?'} ({src_type_value}) -> {dst_name or '?'} ({dst_type_value})"

    @staticmethod
    def source_segment_policy_name(segment_name: Any) -> str:
        """Build deterministic source-segment policy name."""
        seg_name = str(segment_name or "unknown-segment")
        return f"seg-{seg_name}-egress"

    @staticmethod
    def build_policy_rule_payload(
        *,
        policy_id: str,
        rule_name: str,
        protocol: str,
        source_segment_id: str | None,
        destination_segment_id: str | None,
        source_isolation_mode: str | None = None,
        destination_isolation_mode: str | None = None,
        description: str | None = None,
        log: bool = False,
        action: str = "permit",
        disabled: bool = False,
        port_start: int | None = None,
        port_end: int | None = None,
        expires_at: Any | None = None,
        extra_fields: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build DB-ready payload for policy rules with deterministic defaults."""
        payload: dict[str, Any] = {
            "policy": {"id": policy_id},
            "name": rule_name,
            "action": action,
            "protocol": protocol,
            "log": log,
            "disabled": disabled,
            "source_segment": {"id": source_segment_id},
            "destination_segment": {"id": destination_segment_id},
            "apply_on_switch": (source_isolation_mode or "normal") == "microsegmented"
            or (destination_isolation_mode or "normal") == "microsegmented",
        }

        if description:
            payload["description"] = description
        if port_start is not None:
            payload["port_start"] = port_start
        if port_end is not None:
            payload["port_end"] = port_end
        if expires_at:
            payload["expires_at"] = expires_at
        if extra_fields:
            payload.update(extra_fields)

        return payload


class RulesPlanner(RulePlanningHelper):
    """Planner for dependency-driven rule preparation without DB side effects."""

    @staticmethod
    def seg_cidr(seg: dict[str, Any]) -> str | None:
        """Extract a CIDR string from a segment dict (on-prem or cloud)."""
        cidr_block = seg.get("cidr_block") or {}
        if cidr_block.get("prefix"):
            return cidr_block["prefix"]
        gateway = seg.get("gateway") or {}
        return (gateway.get("ip_prefix") or {}).get("prefix")

    @staticmethod
    def resolve_port(dep: dict[str, Any]) -> tuple[str, int | None, int | None] | None:
        """Return (protocol, port_start, port_end) for one AppDependency dict."""
        from .ports import PortProfileHelper

        return PortProfileHelper.resolve_dependency_rule_port(dep)

    @staticmethod
    def collect_dependency_edges(components: list[dict[str, Any]]) -> tuple[list[tuple[dict, dict, dict]], list[str]]:
        """Collect dependency edges and validation warnings."""
        edges: list[tuple[dict, dict, dict]] = []
        warnings: list[str] = []

        for comp in components:
            for dep in comp.get("depends_on", []):
                target = dep.get("target") or {}
                if not target:
                    warnings.append(f"AppDependency '{dep.get('name', dep.get('id', '?'))}' has no target - skipping")
                    continue
                edges.append((comp, dep, target))

        return edges, warnings

    @staticmethod
    def is_cloud_dependency(src_seg: dict[str, Any], dst_seg: dict[str, Any]) -> bool:
        """Return True when either side of dependency uses a cloud segment."""
        return (
            src_seg.get("typename", "") == "CloudNetworkSegment" or dst_seg.get("typename", "") == "CloudNetworkSegment"
        )

    @staticmethod
    def rule_name(app_name: str, src: dict[str, Any], dst: dict[str, Any]) -> str:
        src_label = src.get("label") or src.get("slug") or src.get("name", "src")
        dst_label = dst.get("label") or dst.get("slug") or dst.get("name", "dst")
        return RulePlanningHelper.flow_rule_name(app_name, src_label, dst_label)

    @staticmethod
    def rule_description(dep: dict[str, Any], src_comp: dict[str, Any], dst_comp: dict[str, Any]) -> str:
        return RulePlanningHelper.flow_rule_description(
            explicit_description=dep.get("description"),
            src_name=src_comp.get("name"),
            src_type=src_comp.get("component_type", "backend"),
            dst_name=dst_comp.get("name"),
            dst_type=dst_comp.get("component_type", "backend"),
        )

    @staticmethod
    def segment_policy_name(segment: dict[str, Any]) -> str:
        seg_name = segment.get("name") or segment.get("id")
        return RulePlanningHelper.source_segment_policy_name(seg_name)

    @staticmethod
    def owner_org_id_from_component(component: dict[str, Any]) -> str | None:
        app = component.get("parent") or {}
        portfolio = app.get("parent") or {}
        owner = portfolio.get("owner") or {}
        owner_org_id = str(owner.get("org_id") or "").strip().upper()
        return owner_org_id or None

    @staticmethod
    def owner_name_from_component(component: dict[str, Any]) -> str | None:
        app = component.get("parent") or {}
        portfolio = app.get("parent") or {}
        owner = portfolio.get("owner") or {}
        owner_name = str(owner.get("name") or "").strip()
        return owner_name or None

    @staticmethod
    def dependency_access_status(dep: dict[str, Any]) -> str:
        raw = str(dep.get("access_status") or "auto").strip().lower()
        if raw in {"auto", "pending", "approved", "denied"}:
            return raw
        return "auto"

    @classmethod
    def dependency_is_authorized(
        cls,
        src_comp: dict[str, Any],
        dst_comp: dict[str, Any],
        dep: dict[str, Any],
    ) -> tuple[bool, str | None]:
        status = cls.dependency_access_status(dep)
        if status == "denied":
            return False, "explicitly denied"

        src_owner_org_id = cls.owner_org_id_from_component(src_comp)
        dst_owner_org_id = cls.owner_org_id_from_component(dst_comp)

        if not src_owner_org_id or not dst_owner_org_id:
            return True, None
        if src_owner_org_id == dst_owner_org_id:
            return True, None

        if status != "approved":
            return False, f"cross-owner flow {src_owner_org_id}->{dst_owner_org_id} requires access_status=approved"
        return True, None

    @classmethod
    def governance_suffix(cls, src_comp: dict[str, Any], dst_comp: dict[str, Any], dep: dict[str, Any]) -> str:
        src_owner = (
            cls.owner_org_id_from_component(src_comp) or cls.owner_name_from_component(src_comp) or "unknown-src-owner"
        )
        dst_owner = (
            cls.owner_org_id_from_component(dst_comp) or cls.owner_name_from_component(dst_comp) or "unknown-dst-owner"
        )
        status = cls.dependency_access_status(dep)
        reason = str(dep.get("decision_reason") or "").strip()
        reason_suffix = f"; reason={reason}" if reason else ""
        return f" [governance: status={status}; source_owner={src_owner}; destination_owner={dst_owner}{reason_suffix}]"

    @staticmethod
    def zone_context(
        src_seg: dict[str, Any], dst_seg: dict[str, Any], dep: dict[str, Any]
    ) -> tuple[str | None, str | None, bool]:
        src_zone = (src_seg.get("security_zone") or {}).get("name")
        dst_zone = (dst_seg.get("security_zone") or {}).get("name")
        if src_zone and dst_zone:
            return src_zone, dst_zone, src_zone != dst_zone
        return src_zone, dst_zone, True

    @staticmethod
    def pick_profile_name(app_security_profile: str, cross_zone: bool) -> str | None:
        if not cross_zone:
            return None
        mapping = {
            "fintech_strict": "strict",
            "internal_standard": "standard",
            "internet_exposed": "strict",
        }
        return mapping.get(app_security_profile, "standard")

    @classmethod
    def build_rule_payload(
        cls,
        *,
        policy_id: str,
        rule_name: str,
        dep: dict[str, Any],
        src_comp: dict[str, Any],
        dst_comp: dict[str, Any],
        src_seg: dict[str, Any],
        dst_seg: dict[str, Any],
        protocol: str,
        port_start: int | None,
        port_end: int | None,
        cross_zone: bool,
    ) -> dict[str, Any]:
        description = f"{cls.rule_description(dep, src_comp, dst_comp)}{cls.governance_suffix(src_comp, dst_comp, dep)}"
        return RulePlanningHelper.build_policy_rule_payload(
            policy_id=policy_id,
            rule_name=rule_name,
            protocol=protocol,
            source_segment_id=src_seg.get("id"),
            destination_segment_id=dst_seg.get("id"),
            source_isolation_mode=src_seg.get("isolation_mode"),
            destination_isolation_mode=dst_seg.get("isolation_mode"),
            description=description,
            log=cross_zone,
            disabled=False,
            port_start=port_start,
            port_end=port_end,
            expires_at=dep.get("access_expires_at"),
        )
