from __future__ import annotations

from typing import Any

from utils.dependency_access import dependency_access_status
from utils.exchange_transit import namespace_type_for_environment, zone_name_for_namespace_type
from utils.ports import PortProfileHelper, PortSpec


class RulesPlanner:
    """Planner for dependency-driven rule preparation without DB side effects."""

    @staticmethod
    def _name_part(value: Any, fallback: str) -> str:
        return str(value or fallback).strip().lower().replace(" ", "-")

    @staticmethod
    def seg_cidr(seg: dict[str, Any]) -> str | None:
        """Extract a CIDR string from a segment dict (on-prem or cloud)."""
        if seg.get("cidr_block"):
            return seg["cidr_block"]
        gateway = seg.get("gateway") or {}
        return (gateway.get("ip_prefix") or {}).get("prefix")

    @staticmethod
    def resolve_ports(dep: dict[str, Any], target: dict[str, Any] | None = None) -> list[PortSpec]:
        """Ports one AppDependency opens; raises ValueError on a malformed port."""
        return PortProfileHelper.resolve_dependency_ports(dep, target)

    @staticmethod
    def is_cloud_dependency(src_seg: dict[str, Any], dst_seg: dict[str, Any]) -> bool:
        """Return True when either side of dependency uses a cloud segment."""
        return (
            src_seg.get("typename", "") == "CloudNetworkSegment" or dst_seg.get("typename", "") == "CloudNetworkSegment"
        )

    @staticmethod
    def rule_name(
        app_name: str,
        src: dict[str, Any],
        dst: dict[str, Any],
        dep: dict[str, Any] | None = None,
        port: PortSpec | None = None,
    ) -> str:
        """One rule per dependency and port, named after the dependency.

        A firewall rule holds one protocol and port range, so a dependency
        on tcp/6379 and udp/30000-30010 becomes two rules; the port suffix
        keeps the second from being skipped as already existing. The
        component pair is the fallback for a dependency without a name.
        """
        dep_name = str((dep or {}).get("name") or "").strip()
        if dep_name:
            base = RulesPlanner._name_part(dep_name, "dependency")
        else:
            src_label = src.get("label") or src.get("fqdn") or src.get("name", "src")
            dst_label = dst.get("label") or dst.get("fqdn") or dst.get("name", "dst")
            src_part = RulesPlanner._name_part(src_label, "src")
            dst_part = RulesPlanner._name_part(dst_label, "dst")
            base = f"{app_name}-{src_part}-to-{dst_part}"
        if port is None:
            return base
        return f"{base}-{PortProfileHelper.format_port_spec(port).replace('/', '-')}"

    @staticmethod
    def rule_description(dep: dict[str, Any], src_comp: dict[str, Any], dst_comp: dict[str, Any]) -> str:
        """The dependency's own description, else an auto-generated src -> dst summary."""
        if dep.get("description"):
            return str(dep["description"])
        src_name = src_comp.get("name")
        src_type = src_comp.get("component_type", "backend") or "backend"
        dst_name = dst_comp.get("name") or dep.get("target_fqdn")
        dst_type = dst_comp.get("component_type") or ("external" if dep.get("target_fqdn") else "backend")
        return f"Auto-generated: {src_name or '?'} ({src_type}) -> {dst_name or '?'} ({dst_type})"

    @staticmethod
    def segment_policy_name(segment: dict[str, Any]) -> str:
        """Deterministic egress policy name for a source segment."""
        seg_name = segment.get("name") or segment.get("id") or "unknown-segment"
        return f"seg-{seg_name}-egress"

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
    def app_environment_from_component(component: dict[str, Any]) -> str | None:
        app = component.get("parent") or {}
        environment = str(app.get("environment") or "").strip().lower()
        return environment or None

    @classmethod
    def dependency_is_authorized(
        cls,
        src_comp: dict[str, Any],
        dst_comp: dict[str, Any],
        dep: dict[str, Any],
    ) -> tuple[bool, str | None]:
        status = dependency_access_status(dep)
        if status == "denied":
            return False, "explicitly denied"

        # Prod and non-prod are never connected, so no approval opens that
        # flow.
        src_env = cls.app_environment_from_component(src_comp)
        dst_env = cls.app_environment_from_component(dst_comp)
        if src_env and dst_env and src_env != dst_env and "p" in (src_env, dst_env):
            return False, f"cross-environment flow {src_env}->{dst_env} joins prod and non-prod and is never allowed"

        # Inside one owner every flow is auto-approved, across applications
        # and non-prod environments too. Another owner's endpoint opens only
        # once its owner approves; the dependency-updated trigger then
        # reconciles the calling application, which creates the rule.
        src_owner_org_id = cls.owner_org_id_from_component(src_comp)
        dst_owner_org_id = cls.owner_org_id_from_component(dst_comp)
        if src_owner_org_id and dst_owner_org_id and src_owner_org_id != dst_owner_org_id and status != "approved":
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
        status = dependency_access_status(dep)
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
        return zone_name_for_namespace_type(namespace_type_for_environment(environment))

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
        payload: dict[str, Any] = {
            "policy": {"id": policy_id},
            "name": rule_name,
            "action": "permit",
            "protocol": protocol,
            "log": cross_zone,
            "disabled": False,
            "source_segment": {"id": src_seg.get("id")},
            "destination_segment": {"id": dst_seg.get("id")},
            "apply_on_switch": "microsegmented" in (src_seg.get("isolation_mode"), dst_seg.get("isolation_mode")),
            "description": f"{cls.rule_description(dep, src_comp, dst_comp)}{cls.governance_suffix(src_comp, dst_comp, dep)}",
        }
        if port_start is not None:
            payload["port_start"] = port_start
        if port_end is not None:
            payload["port_end"] = port_end
        if dep.get("access_expires_at"):
            payload["expires_at"] = dep["access_expires_at"]
        return payload
