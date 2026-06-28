"""ACL / security policy helpers for device transforms."""

from typing import Any

from transforms.helpers.segments import _get_segment_prefix_str


def _build_acl_rule(rule: dict[str, Any], *, mirror_dst: bool = False) -> dict[str, Any]:
    """Convert a SecurityPolicyRule dict (from clean_data) into an ACL rule dict.

    Args:
        rule: Cleaned SecurityPolicyRule dict.
        mirror_dst: When True, set dst="any" (used for symmetric East-West mirroring
            where the rule fires on the *destination* segment's inbound SVI).
    """
    protocol = rule.get("protocol") or "any"
    proto_map = {"any": "ip", "tcp": "tcp", "udp": "udp", "icmp": "icmp"}
    acl_proto = proto_map.get(protocol, "ip")

    src_seg = rule.get("source_segment") or {}
    src_prefix = _get_segment_prefix_str(src_seg) if src_seg else None
    src = src_prefix or "any"

    if mirror_dst:
        dst = "any"
    else:
        dst_seg = rule.get("destination_segment") or {}
        dst_prefix = _get_segment_prefix_str(dst_seg) if dst_seg else None
        dst = dst_prefix or "any"

    port_start = rule.get("port_start")
    port_end = rule.get("port_end")
    dst_port: str | None = None
    if port_start and acl_proto in ("tcp", "udp"):
        if port_end and port_end != port_start:
            dst_port = f"range {port_start} {port_end}"
        else:
            dst_port = f"eq {port_start}"

    # Zone fields — for zone-aware platforms or remark/comment rendering
    src_zone = (rule.get("source_zone") or {}).get("name") or None
    dst_zone = (rule.get("destination_zone") or {}).get("name") or None

    return {
        "seq": rule.get("index"),
        "action": rule.get("action", "deny"),
        "protocol": acl_proto,
        "src": src,
        "dst": dst,
        "dst_port": dst_port,
        "log": bool(rule.get("log")),
        "name": rule.get("name") or "",
        "src_zone": src_zone,
        "dst_zone": dst_zone,
    }


def get_acls(activations: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Build ACL list from SegmentDeployment security policies (zero-trust).

    Generates inbound ACLs for each segment with security_policies present.
    Includes two microsegmentation enhancements:

    1. **Symmetric East-West ACLs**: When Segment A has a rule targeting Segment B,
       a mirrored rule is automatically added to Segment B's inbound ACL (src from
       A's rule, dst=any). This ensures both sides enforce the same intent.

    2. **Zone support**: source_zone / destination_zone names are passed through as
       ``src_zone`` / ``dst_zone`` fields for templates to render as remarks/comments
       or to drive zone-aware platform ACL APIs.

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
               "src": "any", "dst": "10.0.2.0/24", "dst_port": "eq 80",
               "log": False, "src_zone": None, "dst_zone": "internal"},
              {"seq": 9990, "action": "deny", "protocol": "ip",
               "src": "any", "dst": "any", "log": True, "name": "implicit-deny-all"},
            ],
          }
        ]
    """
    if not activations:
        return []

    # --- Index: segment_id → vlan_id for cross-segment rule mirroring ---
    # Also track which segments have a firewall — those are skipped for leaf ACLs.
    seg_id_to_vlan: dict[str, int] = {}
    seg_has_firewall: set[str] = set()
    for act in activations:
        vlan_id = act.get("vlan_id")
        seg = act.get("segment") or {}
        seg_id = seg.get("id")
        if vlan_id and seg_id:
            seg_id_to_vlan[seg_id] = vlan_id
        fw_node = (seg.get("inline_service") or {}).get("id") or (seg.get("inline_service") or {}).get("name")
        if fw_node and seg_id:
            seg_has_firewall.add(seg_id)

    # --- Collect mirrored rules: rules from other segments that target this segment ---
    # mirrored_by_vlan[dst_vlan] = [(original_rule, src_segment_name), ...]
    mirrored_by_vlan: dict[int, list[tuple[dict[str, Any], str]]] = {}
    for act in activations:
        src_vlan = act.get("vlan_id")
        if not src_vlan:
            continue
        seg = act.get("segment") or {}
        if "security_policies" not in seg:
            continue
        src_seg_name = seg.get("customer_name") or seg.get("name") or f"VLAN_{src_vlan}"
        for policy in seg.get("security_policies") or []:
            if not policy.get("enabled", True):
                continue
            for rule in policy.get("rules") or []:
                if rule.get("disabled"):
                    continue
                dst_seg_id = (rule.get("destination_segment") or {}).get("id")
                if not dst_seg_id:
                    continue
                dst_vlan = seg_id_to_vlan.get(dst_seg_id)
                if not dst_vlan or dst_vlan == src_vlan:
                    continue
                mirrored_by_vlan.setdefault(dst_vlan, []).append((rule, src_seg_name))

    # --- Build per-segment ACLs ---
    acls: list[dict[str, Any]] = []
    seen_vlans: set[int] = set()

    for act in activations:
        vlan_id = act.get("vlan_id")
        if not vlan_id or vlan_id in seen_vlans:
            continue

        seg = act.get("segment") or {}
        seg_id = seg.get("id")

        # Segment has a dedicated firewall — policy is enforced there, not on the leaf SVI.
        # Exception: microsegmented segments always get a leaf ACL regardless of firewall.
        isolation_mode = seg.get("isolation_mode") or "normal"
        if seg_id and seg_id in seg_has_firewall and isolation_mode != "microsegmented":
            seen_vlans.add(vlan_id)
            continue

        # Only render ACLs when security_policies is explicitly in the data
        # (i.e. the query included it). Missing key = field not queried → skip.
        if "security_policies" not in seg:
            continue
        segment_name = seg.get("customer_name") or seg.get("name") or f"VLAN_{vlan_id}"
        policies = seg.get("security_policies") or []

        rules: list[dict[str, Any]] = []

        # Own policies
        for policy in policies:
            if not policy.get("enabled", True):
                continue
            for rule in sorted(policy.get("rules") or [], key=lambda r: r.get("index") or 0):
                if rule.get("disabled"):
                    continue
                rules.append(_build_acl_rule(rule))

        # Mirrored rules from other segments' policies (symmetric East-West)
        own_max_seq = max((r["seq"] or 0 for r in rules), default=0)
        mirror_seq_start = max(own_max_seq + 10, 5000)
        for i, (orig_rule, from_name) in enumerate(
            sorted(mirrored_by_vlan.get(vlan_id, []), key=lambda t: t[0].get("index") or 0)
        ):
            mirrored = _build_acl_rule(orig_rule, mirror_dst=True)
            mirrored["seq"] = mirror_seq_start + i * 10
            safe_from = from_name.replace(" ", "-")
            mirrored["name"] = f"mirror-from-{safe_from}-{orig_rule.get('name', '')}"
            rules.append(mirrored)

        # Implicit deny
        if rules:
            last_seq = max(r["seq"] or 0 for r in rules)
            implicit_seq = max(last_seq + 10, 9990)
        else:
            implicit_seq = 9990

        rules.append(
            {
                "seq": implicit_seq,
                "action": "deny",
                "protocol": "ip",
                "src": "any",
                "dst": "any",
                "dst_port": None,
                "log": True,
                "name": "implicit-deny-all",
                "src_zone": None,
                "dst_zone": None,
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
