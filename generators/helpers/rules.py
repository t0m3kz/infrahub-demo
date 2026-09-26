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
        if seg.get("cidr_block"):
            return seg["cidr_block"]
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
        owner = app.get("owner") or {}
        owner_org_id = str(owner.get("org_id") or "").strip().upper()
        return owner_org_id or None

    @staticmethod
    def owner_name_from_component(component: dict[str, Any]) -> str | None:
        app = component.get("parent") or {}
        owner = app.get("owner") or {}
        owner_name = str(owner.get("name") or "").strip()
        return owner_name or None

    @staticmethod
    def app_name_from_component(component: dict[str, Any]) -> str | None:
        app = component.get("parent") or {}
        app_name = str(app.get("name") or "").strip()
        return app_name or None

    @staticmethod
    def app_environment_from_component(component: dict[str, Any]) -> str | None:
        app = component.get("parent") or {}
        environment = str(app.get("environment") or "").strip().lower()
        return environment or None

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

        if src_owner_org_id and dst_owner_org_id and src_owner_org_id != dst_owner_org_id:
            if status != "approved":
                return False, f"cross-owner flow {src_owner_org_id}->{dst_owner_org_id} requires access_status=approved"
            return True, None

        src_app_name = cls.app_name_from_component(src_comp)
        dst_app_name = cls.app_name_from_component(dst_comp)
        if src_app_name and dst_app_name and src_app_name != dst_app_name:
            if status != "approved":
                return (
                    False,
                    f"cross-application flow {src_app_name}->{dst_app_name} requires access_status=approved",
                )
            return True, None

        src_env = cls.app_environment_from_component(src_comp)
        dst_env = cls.app_environment_from_component(dst_comp)
        if src_env and dst_env and src_env != dst_env:
            if status != "approved":
                return False, f"cross-environment flow {src_env}->{dst_env} requires access_status=approved"
            return True, None

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
        """Threat-inspection profile for a generated SecurityPolicyRule.

        `fintech_strict` always gets full inspection (antivirus/DLP) — that's
        a data-sensitivity/compliance requirement independent of where the
        traffic goes, unlike `internet_exposed`, whose "strict" handling is
        specifically about the perimeter/exposure boundary and so only
        applies when the flow actually crosses zones.
        """
        if app_security_profile == "fintech_strict":
            return "strict"
        if not cross_zone:
            return None
        mapping = {
            "internal_standard": "standard",
            "internet_exposed": "strict",
        }
        return mapping.get(app_security_profile, "standard")

    @staticmethod
    def pick_zone_name(environment: str) -> str:
        """Macro trust zone for a segment, derived from its own `environment`."""
        return "PROD-ZONE" if environment == "p" else "NONPROD-ZONE"

    @staticmethod
    def zone_seed(zone_name: str) -> dict[str, Any]:
        """Fixed classification for a generator-owned SecurityZone, keyed by name."""
        seeds = {
            "PROD-ZONE": {
                "description": "Production workload zone — web, app, and database tiers",
                "trust_level": 70,
                "zone_type": "internal",
            },
            "NONPROD-ZONE": {
                "description": "Non-production zone — dev, staging, QA environments",
                "trust_level": 50,
                "zone_type": "internal",
            },
        }
        return seeds[zone_name]

    @staticmethod
    def pick_access_policy(app_security_profile: str) -> dict[str, Any]:
        """Default ZTNA access-profile policy for a private_access endpoint,
        derived from its application's security_profile."""
        mapping = {
            "internet_exposed": {"mfa_required": True, "device_posture_required": True, "session_timeout_minutes": 480},
            "fintech_strict": {"mfa_required": True, "device_posture_required": True, "session_timeout_minutes": 480},
            "internal_standard": {
                "mfa_required": True,
                "device_posture_required": False,
                "session_timeout_minutes": 720,
            },
        }
        return mapping.get(app_security_profile, mapping["internal_standard"])

    @staticmethod
    def pick_isolation_mode(app_security_profile: str) -> str:
        """Intra-segment enforcement for a segment, derived from the
        security_profile of the application using it. fintech_strict is a
        data-sensitivity/compliance requirement (per-flow ACL enforcement
        even between hosts in the same segment) independent of network
        topology; every other profile stays at the schema default."""
        return "microsegmented" if app_security_profile == "fintech_strict" else "normal"

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
