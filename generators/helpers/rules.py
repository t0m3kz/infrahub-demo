from __future__ import annotations

from typing import Any

from utils.dependency_access import component_owner_org_id, crosses_owners, dependency_access_status
from utils.exchange_transit import (
    is_exchange_zone,
    joins_prod_and_nonprod,
)
from utils.group_policy import MICROSEGMENTED, is_microsegmented
from utils.ports import PortProfileHelper, PortSpec


class RulesPlanner:
    """Planner for dependency-driven rule preparation without DB side effects."""

    @staticmethod
    def name_part(value: Any, fallback: str = "") -> str:
        """``value`` as a lowercase, space-free part of a rule name."""
        return str(value or fallback).strip().lower().replace(" ", "-")

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
            base = RulesPlanner.name_part(dep_name, "dependency")
        else:
            src_label = src.get("label") or src.get("fqdn") or src.get("name", "src")
            dst_label = dst.get("label") or dst.get("fqdn") or dst.get("name", "dst")
            src_part = RulesPlanner.name_part(src_label, "src")
            dst_part = RulesPlanner.name_part(dst_label, "dst")
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
        """Deterministic egress policy name for a source segment.

        A segment with a parent virtual network (cloud) is qualified by it: the
        computed segment name is only unique within one network, so two
        networks may hold the same one. On-prem names carry no qualifier.
        """
        seg_name = segment.get("name") or segment.get("id") or "unknown-segment"
        vnet_name = (segment.get("virtual_network") or {}).get("name")
        if vnet_name:
            return f"seg-{seg_name}-{vnet_name}-egress"
        return f"seg-{seg_name}-egress"

    @staticmethod
    def owner_name_from_component(component: dict[str, Any]) -> str | None:
        app = component.get("parent") or {}
        owner = app.get("owner") or {}
        owner_name = str(owner.get("name") or "").strip()
        return owner_name or None

    @staticmethod
    def _is_exchange_segment(segment: dict[str, Any]) -> bool:
        """Whether the segment sits in an exchange zone (INTERNET-ZONE, MANAGEMENT-ZONE)."""
        return is_exchange_zone((segment.get("security_zone") or {}).get("name"))

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
        # flow. The decision is made from each segment's own environment (the
        # namespace it lives in), not the application's, which is governance
        # input only. A flow without two segments (proxy to an FQDN) has
        # nothing to join, and an exchange-zone segment (INTERNET/MANAGEMENT)
        # is governed by EXCHANGE_PEERS instead.
        src_seg = src_comp.get("network_segment") or {}
        dst_seg = dst_comp.get("network_segment") or {}
        if src_seg and dst_seg and not cls._is_exchange_segment(src_seg) and not cls._is_exchange_segment(dst_seg):
            src_env = src_seg.get("environment")
            dst_env = dst_seg.get("environment")
            if joins_prod_and_nonprod(src_env, dst_env):
                return (
                    False,
                    f"cross-environment flow {src_env or '?'}->{dst_env or '?'} joins prod and non-prod "
                    "(or a segment environment is unknown) and is never allowed",
                )

        # Inside one owner every flow is auto-approved, across applications
        # and non-prod environments too. Another owner's endpoint opens only
        # once its owner approves; the dependency-updated trigger then
        # reconciles the calling application, which creates the rule.
        if crosses_owners(src_comp, dst_comp) and status != "approved":
            return (
                False,
                f"cross-owner flow {component_owner_org_id(src_comp)}->{component_owner_org_id(dst_comp)} "
                "requires access_status=approved",
            )

        return True, None

    @classmethod
    def governance_suffix(cls, src_comp: dict[str, Any], dst_comp: dict[str, Any], dep: dict[str, Any]) -> str:
        src_owner = component_owner_org_id(src_comp) or cls.owner_name_from_component(src_comp) or "unknown-src-owner"
        dst_owner = component_owner_org_id(dst_comp) or cls.owner_name_from_component(dst_comp) or "unknown-dst-owner"
        status = dependency_access_status(dep)
        reason = str(dep.get("decision_reason") or "").strip()
        reason_suffix = f"; reason={reason}" if reason else ""
        return f" [governance: status={status}; source_owner={src_owner}; destination_owner={dst_owner}{reason_suffix}]"

    @staticmethod
    def zone_context(src_seg: dict[str, Any], dst_seg: dict[str, Any]) -> tuple[str | None, str | None, bool]:
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
    def pick_isolation_mode(app_security_profile: str) -> str:
        """Intra-segment enforcement for a segment, derived from the
        security_profile of the application using it. fintech_strict is a
        data-sensitivity/compliance requirement (per-flow ACL enforcement
        even between hosts in the same segment) independent of network
        topology; every other profile stays at the schema default."""
        return MICROSEGMENTED if app_security_profile == "fintech_strict" else "normal"

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
        vip_id: str | None = None,
    ) -> dict[str, Any]:
        """The rule of one dependency port: into the destination segment, or,
        with ``vip_id``, R_client — into one VIP of a load-balanced target (one
        rule per VIP: the leaf ACL and the firewall render one address selector).

        The VIPs are UNCLASSIFIED service addresses (never a segment or a
        group), so R_client names no destination segment, carries no
        destination tag and is not forced onto the switch by the target's
        isolation mode; the source side is the same as a direct rule's.
        """
        reached_seg = {} if vip_id else dst_seg
        payload: dict[str, Any] = {
            "policy": {"id": policy_id},
            "name": rule_name,
            "action": "permit",
            "protocol": protocol,
            "log": cross_zone,
            "disabled": False,
            "source_segment": {"id": src_seg.get("id")},
            "apply_on_switch": MICROSEGMENTED in (src_seg.get("isolation_mode"), reached_seg.get("isolation_mode")),
            "description": f"{cls.rule_description(dep, src_comp, dst_comp)}{cls.governance_suffix(src_comp, dst_comp, dep)}",
        }
        if vip_id:
            payload["destination_ip_addresses"] = [{"id": vip_id}]
        else:
            payload["destination_segment"] = {"id": dst_seg.get("id")}
        if port_start is not None:
            payload["port_start"] = port_start
        if port_end is not None:
            payload["port_end"] = port_end
        if dep.get("access_expires_at"):
            payload["expires_at"] = dep["access_expires_at"]
        payload.update(cls.rule_tags(src_comp=src_comp, dst_comp=dst_comp, src_seg=src_seg, dst_seg=reached_seg))
        return payload

    @staticmethod
    def lb_policy_name(context_name: str) -> str:
        """The segment-less policy of a load balancer context, holding its R_server rules."""
        return f"lb-{context_name}"

    @classmethod
    def lb_server_rule_name(cls, component: dict[str, Any], port: PortSpec) -> str:
        """R_server of one VIP: unique in its context's policy, as the VIP is per (fqdn, protocol, port)."""
        fqdn = cls.name_part(component.get("fqdn") or component.get("name"), "component")
        return f"{fqdn}-{PortProfileHelper.format_port_spec(port).replace('/', '-')}"

    @classmethod
    def build_lb_server_rule_payload(
        cls,
        *,
        policy_id: str,
        rule_name: str,
        context_name: str,
        component: dict[str, Any],
        segment: dict[str, Any],
        leg_prefix_id: str,
        port: PortSpec,
    ) -> dict[str, Any]:
        """R_server: the context's leg /29 (the SNAT automap source of every
        balanced connection) to the backend segment on the VIP port.

        The leg is an UNCLASSIFIED service leg, so only the destination side
        carries a component tag, where its segment is microsegmented. Never
        expires: it lives exactly as long as its VIP.
        """
        protocol, port_start, _port_end = port
        return {
            "policy": {"id": policy_id},
            "name": rule_name,
            "action": "permit",
            "protocol": protocol,
            "port_start": port_start,
            "log": False,
            "disabled": False,
            "expires_at": None,
            "source_prefixes": [{"id": leg_prefix_id}],
            "destination_segment": {"id": segment.get("id")},
            "apply_on_switch": is_microsegmented(segment),
            "description": (
                f"Load balancer {context_name} -> {component.get('fqdn') or component.get('name')} "
                f"({PortProfileHelper.format_port_spec(port)})"
            ),
            **cls.rule_tags(src_comp={}, dst_comp=component, src_seg={}, dst_seg=segment),
        }

    @staticmethod
    def rule_tags(
        *,
        src_comp: dict[str, Any],
        dst_comp: dict[str, Any],
        src_seg: dict[str, Any],
        dst_seg: dict[str, Any],
    ) -> dict[str, Any]:
        """The component group tags a rule carries: one per side whose segment is microsegmented.

        A side's tag is its component's (in memory, from
        AppApplicationGenerator._ensure_component_tags). Inside a
        microsegmented segment it is the intra-segment contract between two
        components; on a side of a cross-segment rule it narrows the
        contract to that component, instead of every group of its segment
        (transforms/helpers/group_policy.rule_groups): a dependency on one
        component opens that component, not its neighbours. A side whose
        segment is not microsegmented carries no tag (the segment tag is the
        group there), and a side left out is cleared on an existing rule
        (_reconcile_rule_payload).
        """
        return {
            f"{side}_security_tag": {"id": tag_id}
            for side, component, segment in (("source", src_comp, src_seg), ("destination", dst_comp, dst_seg))
            if segment.get("id")
            and is_microsegmented(segment)
            and (tag_id := (component.get("security_tag") or {}).get("id"))
        }
