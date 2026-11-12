from __future__ import annotations

import re
from typing import Any

PortSpec = tuple[str, int, int | None]

_PORT_SPEC = re.compile(r"^(tcp|udp)/([1-9]\d{0,4})(?:-([1-9]\d{0,4}))?$")


class PortProfileHelper:
    """Port/protocol utilities for dependency-derived security rules (``tcp/443``, ``udp/30000-30010``)."""

    @staticmethod
    def parse_port_spec(spec: Any) -> PortSpec:
        """Return (protocol, port_start, port_end), port_end None for a single port; raise ValueError otherwise."""
        match = _PORT_SPEC.match(str(spec).strip().lower())
        if not match:
            raise ValueError(f"invalid port '{spec}', expected protocol/port or protocol/start-end (tcp/443)")
        protocol, start_raw, end_raw = match.groups()
        start = int(start_raw)
        end = int(end_raw) if end_raw is not None else None
        if not 1 <= start <= 65535 or (end is not None and not start < end <= 65535):
            raise ValueError(f"invalid port '{spec}', ports run 1-65535 and a range must ascend")
        return protocol, start, end

    @staticmethod
    def format_port_spec(port: PortSpec) -> str:
        protocol, start, end = port
        return f"{protocol}/{start}" if end is None else f"{protocol}/{start}-{end}"

    @classmethod
    def resolve_dependency_ports(cls, dep: dict[str, Any], target: dict[str, Any] | None = None) -> list[PortSpec]:
        """Ports a dependency opens: its own list, else its target component's; ValueError when one does not parse."""
        specs = dep.get("ports") or (target or {}).get("ports") or []
        ports: list[PortSpec] = []
        for spec in specs:
            port = cls.parse_port_spec(spec)
            if port not in ports:
                ports.append(port)
        return ports
