from __future__ import annotations

from typing import Any


class PortProfileHelper:
    """Port/protocol utilities for dependency-derived security rules."""

    @staticmethod
    def resolve_dependency_rule_port(dep: dict[str, Any]) -> tuple[str, int | None, int | None] | None:
        """Return (protocol, port_start, port_end) used for security-rule derivation."""
        protocol = dep.get("protocol")
        port_start = dep.get("port_start")
        if protocol is None and port_start is None:
            return None
        return (str(protocol or "tcp"), port_start, dep.get("port_end"))
