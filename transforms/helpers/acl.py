"""ACL / security policy helpers for device transforms."""

from typing import Any

from transforms.helpers.policy import (
    active_rules,
    enabled_policies,
    inbound_permits,
    rule_endpoint,
    rule_zone,
    segment_policies,
)
from transforms.helpers.segments import _get_segment_prefix_str

_PROTO_MAP = {"any": "ip", "tcp": "tcp", "udp": "udp", "icmp": "icmp"}


def _port_match(rule: dict[str, Any], acl_proto: str) -> str | None:
    """`eq N` / `range N M` for a tcp/udp rule's port, else None."""
    port_start = rule.get("port_start")
    port_end = rule.get("port_end")
    if not port_start or acl_proto not in ("tcp", "udp"):
        return None
    if port_end and port_end != port_start:
        return f"range {port_start} {port_end}"
    return f"eq {port_start}"


def _build_acl_rule(rule: dict[str, Any]) -> dict[str, Any]:
    """Convert a SecurityPolicyRule dict (from clean_data) into an ACL rule dict."""
    acl_proto = _PROTO_MAP.get(rule.get("protocol") or "any", "ip")
    src_seg = rule.get("source_segment") or {}
    dst_seg = rule.get("destination_segment") or {}

    # Zone fields — for zone-aware platforms or remark/comment rendering
    src_zone = rule_zone(rule, "source")
    dst_zone = rule_zone(rule, "destination")

    # Customer/environment identity — per-rule, not just per-ACL, since a
    # single policy can mix rules from different customers' segments on the
    # same VLAN (via source_segment/destination_segment).
    return {
        "seq": rule.get("index"),
        "action": rule.get("action", "deny"),
        "protocol": acl_proto,
        "src": rule_endpoint(rule, "source") or "any",
        "src_port": None,
        "dst": rule_endpoint(rule, "destination") or "any",
        "dst_port": _port_match(rule, acl_proto),
        "established": False,
        "log": bool(rule.get("log")),
        "name": rule.get("name") or "",
        "src_zone": src_zone,
        "dst_zone": dst_zone,
        "src_customer": src_seg.get("customer_name") or None,
        "src_environment": src_seg.get("environment") or None,
        "dst_customer": dst_seg.get("customer_name") or None,
        "dst_environment": dst_seg.get("environment") or None,
    }


def _build_return_rule(
    rule: dict[str, Any], own_prefix: str | None, own_name: str, own_environment: str | None
) -> dict[str, Any]:
    """The reply leg of an inbound permit, for this segment's own ingress ACL.

    The forward rule A -> B:port sits on A's VLAN. B's hosts answer from
    B:port to A, so B's ACL needs src=B, src_port=port, dst=A. tcp is held
    to `established` (ACK/RST set), so B still can't open a connection to A
    on the strength of A's rule. udp has no such flag, and any/icmp have no
    port: those return legs are the plain reverse match.
    """
    acl_proto = _PROTO_MAP.get(rule.get("protocol") or "any", "ip")
    src_seg = rule.get("source_segment") or {}
    peer_prefix = rule_endpoint(rule, "source")
    peer_name = src_seg.get("customer_name") or src_seg.get("name") or "any"
    return {
        "seq": None,
        "action": "permit",
        "protocol": acl_proto,
        "src": own_prefix or "any",
        "src_port": _port_match(rule, acl_proto),
        "dst": peer_prefix or "any",
        "dst_port": None,
        "established": acl_proto == "tcp",
        "log": False,
        "name": f"return-to-{peer_name.replace(' ', '-')}-{rule.get('name') or ''}",
        "src_zone": None,
        "dst_zone": None,
        "src_customer": own_name,
        "src_environment": own_environment,
        "dst_customer": src_seg.get("customer_name") or None,
        "dst_environment": src_seg.get("environment") or None,
    }


def _inbound_permits(seg: dict[str, Any]) -> list[dict[str, Any]]:
    """Active permit rules targeting this segment from another segment."""
    seg_id = seg.get("id")
    permits = [rule for rule in inbound_permits(seg) if (rule.get("source_segment") or {}).get("id") != seg_id]
    return sorted(
        permits,
        key=lambda r: (
            str((r.get("source_segment") or {}).get("name") or ""),
            r.get("index") or 0,
            r.get("name") or "",
        ),
    )


def _leaf_enforces(seg: dict[str, Any]) -> bool:
    """Whether the segment's policy is enforced on its leaf SVI.

    A segment with a dedicated firewall (inline_service) is enforced there,
    not on the leaf, unless it is microsegmented or one of its own active
    rules is apply_on_switch: such a rule must hold on the switch regardless
    of the firewall, and an ACL is all-or-nothing on the SVI (its implicit
    deny drops whatever it does not permit), so the segment then gets its
    whole ACL, like a microsegmented one.
    """
    inline = seg.get("inline_service") or {}
    if not (inline.get("id") or inline.get("name")) or seg.get("isolation_mode") == "microsegmented":
        return True
    return any(
        rule.get("apply_on_switch")
        for policy in enabled_policies(segment_policies(seg))
        for rule in active_rules(policy)
    )


def get_acls(activations: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Build ACL list from SegmentDeployment security policies (zero-trust).

    Generates an ingress ACL on each segment's SVI, i.e. for traffic its own
    hosts send:

    1. **Own rules**: the segment's own egress policies (A -> B on A's VLAN).

    2. **Return rules**: every permit whose destination is this segment
       (``inbound_rules``, from any segment's policy, on any leaf) adds its
       reply leg here: src=own prefix, src_port=the rule's port,
       dst=the rule's source, ``established`` for tcp. Without it the
       implicit deny drops B's answers to A. They are read from the segment
       itself, so they don't depend on A being on the same leaf.

    3. **Zone support**: the source/destination segments' security_zone names are passed
       through as ``src_zone`` / ``dst_zone`` fields for templates to render as remarks/comments
       or to drive zone-aware platform ACL APIs.

    4. **Customer/environment attribution**: each rule also carries
       ``src_customer``/``src_environment``/``dst_customer``/``dst_environment``
       so a policy mixing rules from different customers' segments on the
       same VLAN can be attributed per-rule, not just at the whole-ACL
       ``segment_name`` level.

    Args:
        activations: List of SegmentDeployment dicts (after clean_data).

    Returns:
        List of ACL dicts:
        [
          {
            "name": "ACL-VLAN100-IN",
            "vlan_id": 100,
            "segment_name": "...",
            "rules": [
              {"seq": 10, "action": "permit", "protocol": "tcp",
               "src": "10.0.1.0/24", "src_port": None,
               "dst": "10.0.2.0/24", "dst_port": "eq 80", "established": False,
               "log": False, "src_zone": None, "dst_zone": "internal"},
              {"seq": 5000, "action": "permit", "protocol": "tcp",
               "src": "10.0.1.0/24", "src_port": "eq 5432",
               "dst": "10.0.3.0/24", "dst_port": None, "established": True,
               "name": "return-to-db-clients-app-to-db"},
              {"seq": 9990, "action": "deny", "protocol": "ip",
               "src": "any", "dst": "any", "log": True, "name": "implicit-deny-all"},
            ],
          }
        ]
    """
    if not activations:
        return []

    acls: list[dict[str, Any]] = []
    seen_vlans: set[int] = set()

    for act in activations:
        vlan_id = act.get("vlan_id")
        if not vlan_id or vlan_id in seen_vlans:
            continue

        seg = act.get("segment") or {}

        isolation_mode = seg.get("isolation_mode") or "normal"
        if not _leaf_enforces(seg):
            seen_vlans.add(vlan_id)
            continue

        # Only render ACLs when security_policy is explicitly in the data
        # (i.e. the query included it). Missing key = field not queried → skip.
        if "security_policy" not in seg:
            continue
        segment_name = seg.get("customer_name") or seg.get("name") or f"VLAN_{vlan_id}"
        segment_environment = seg.get("environment")
        policies = segment_policies(seg)

        rules: list[dict[str, Any]] = []

        # Own policies
        for policy in enabled_policies(policies):
            rules.extend(_build_acl_rule(rule) for rule in active_rules(policy))

        # Return legs of the permits into this segment
        own_prefix = _get_segment_prefix_str(seg)
        own_max_seq = max((r["seq"] or 0 for r in rules), default=0)
        seq = max(own_max_seq + 10, 5000)
        seen_returns: set[tuple[Any, ...]] = set()
        for inbound in _inbound_permits(seg):
            ret = _build_return_rule(inbound, own_prefix, segment_name, segment_environment)
            key = (ret["protocol"], ret["src"], ret["src_port"], ret["dst"])
            if key in seen_returns:
                continue
            seen_returns.add(key)
            ret["seq"] = seq
            seq += 10
            rules.append(ret)

        # Closing rule: the segment policy's default action (deny unless the
        # enabled policy says permit), for everything the rules above miss.
        if rules:
            last_seq = max(r["seq"] or 0 for r in rules)
            implicit_seq = max(last_seq + 10, 9990)
        else:
            implicit_seq = 9990
        default_permit = any(policy.get("default_action") == "permit" for policy in enabled_policies(policies))

        rules.append(
            {
                "seq": implicit_seq,
                "action": "permit" if default_permit else "deny",
                "protocol": "ip",
                "src": "any",
                "src_port": None,
                "dst": "any",
                "dst_port": None,
                "established": False,
                "log": not default_permit,
                "name": "default-permit-all" if default_permit else "implicit-deny-all",
                "src_zone": None,
                "dst_zone": None,
                "src_customer": None,
                "src_environment": None,
                "dst_customer": None,
                "dst_environment": None,
            }
        )

        acls.append(
            {
                "name": f"ACL-VLAN{vlan_id}-IN",
                "vlan_id": vlan_id,
                "segment_name": segment_name,
                "isolation_mode": isolation_mode,
                "rules": rules,
            }
        )
        seen_vlans.add(vlan_id)

    acls.sort(key=lambda a: a.get("vlan_id") or 0)
    return acls
