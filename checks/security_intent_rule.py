"""Security intent rule validation check.

Validates that SecurityIntentRule selectors are coherent with match_mode.
This enforces an IP-first model while still allowing segment/label/hybrid intent.
"""

from __future__ import annotations

from typing import Any

from infrahub_sdk.checks import InfrahubCheck


class CheckSecurityIntentRule(InfrahubCheck):
    """Validate selector combinations for SecurityIntentRule objects."""

    query = "security_intent_rule_validation"

    @staticmethod
    def _edge_count(node: dict[str, Any], rel_name: str) -> int:
        rel = node.get(rel_name) or {}
        edges = rel.get("edges") or []
        return len(edges)

    @staticmethod
    def _has_one(node: dict[str, Any], rel_name: str) -> bool:
        rel = node.get(rel_name) or {}
        return bool(rel.get("node"))

    def validate(self, data: Any) -> None:
        edges = data.get("SecurityIntentRule", {}).get("edges", [])
        if not edges:
            return

        for edge in edges:
            node = edge.get("node", {})
            name = node.get("name", {}).get("value", "unknown")
            mode = node.get("match_mode", {}).get("value", "")
            protocol = node.get("protocol", {}).get("value", "any")
            port_start = node.get("port_start", {}).get("value")
            port_end = node.get("port_end", {}).get("value")

            src_ip_count = self._edge_count(node, "source_ip_addresses")
            dst_ip_count = self._edge_count(node, "destination_ip_addresses")
            src_prefix_count = self._edge_count(node, "source_prefixes")
            dst_prefix_count = self._edge_count(node, "destination_prefixes")
            src_seg_count = self._edge_count(node, "source_segments")
            dst_seg_count = self._edge_count(node, "destination_segments")
            has_src_tag = self._has_one(node, "source_tag")
            has_dst_tag = self._has_one(node, "destination_tag")

            src_selector_count = src_ip_count + src_prefix_count + src_seg_count + (1 if has_src_tag else 0)
            dst_selector_count = dst_ip_count + dst_prefix_count + dst_seg_count + (1 if has_dst_tag else 0)

            if mode == "ip_exact":
                if src_ip_count == 0 or dst_ip_count == 0:
                    self.log_error(
                        message=(
                            f"SecurityIntentRule '{name}' match_mode=ip_exact requires "
                            "source_ip_addresses and destination_ip_addresses."
                        )
                    )

            elif mode == "ip_prefix":
                if src_prefix_count == 0 or dst_prefix_count == 0:
                    self.log_error(
                        message=(
                            f"SecurityIntentRule '{name}' match_mode=ip_prefix requires "
                            "source_prefixes and destination_prefixes."
                        )
                    )

            elif mode == "segment":
                if src_seg_count == 0 or dst_seg_count == 0:
                    self.log_error(
                        message=(
                            f"SecurityIntentRule '{name}' match_mode=segment requires "
                            "source_segments and destination_segments."
                        )
                    )

            elif mode == "label":
                if not has_src_tag or not has_dst_tag:
                    self.log_error(
                        message=(
                            f"SecurityIntentRule '{name}' match_mode=label requires source_tag and destination_tag."
                        )
                    )

            elif mode == "hybrid":
                if src_selector_count == 0 or dst_selector_count == 0:
                    self.log_error(
                        message=(
                            f"SecurityIntentRule '{name}' match_mode=hybrid requires at least one "
                            "selector on each side (IP/prefix/segment/tag)."
                        )
                    )

            else:
                self.log_error(message=(f"SecurityIntentRule '{name}' has unsupported match_mode '{mode}'."))

            if protocol == "any" and (port_start is not None or port_end is not None):
                self.log_error(
                    message=(
                        f"SecurityIntentRule '{name}' uses protocol=any but sets port fields. "
                        "Port constraints are only valid for tcp/udp rules."
                    )
                )

            if protocol in {"tcp", "udp"} and port_end is not None and port_start is None:
                self.log_error(message=(f"SecurityIntentRule '{name}' sets port_end without port_start."))

            if port_start is not None and port_end is not None and int(port_start) > int(port_end):
                self.log_error(message=(f"SecurityIntentRule '{name}' has port_start greater than port_end."))
