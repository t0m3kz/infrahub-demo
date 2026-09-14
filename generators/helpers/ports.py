from __future__ import annotations

from typing import Any


class PortProfileHelper:
    """Shared port/protocol utilities for app rules and LB flows."""

    VIP_TCP_ALIASES = frozenset({"https", "http", "tls"})
    SKIP_DEP_PROTOCOLS = frozenset({"icmp", "any"})
    SUPPORTED_SERVICE_PROTOCOLS = frozenset({"tcp", "udp", "tcp_udp"})

    @classmethod
    def normalize_service_protocol(cls, protocol_raw: Any) -> str | None:
        """Normalize protocol names to AppServicePort-compatible values."""
        proto_raw = str(protocol_raw or "").strip().lower()
        proto = "tcp" if proto_raw in cls.VIP_TCP_ALIASES else proto_raw
        if proto not in cls.SUPPORTED_SERVICE_PROTOCOLS:
            return None
        return proto

    @staticmethod
    def resolve_dependency_rule_port(dep: dict[str, Any]) -> tuple[str, int | None, int | None] | None:
        """Return (protocol, port_start, port_end) used for security-rule derivation."""
        protocol = dep.get("protocol")
        port_start = dep.get("port_start")
        if protocol is None and port_start is None:
            return None
        return (str(protocol or "tcp"), port_start, dep.get("port_end"))

    @classmethod
    def derive_service_port_tuple(
        cls,
        *,
        port_start: int | None,
        port_end: int | None,
        protocol_raw: Any,
    ) -> tuple[int, int | None, str] | None:
        """Derive one AppServicePort tuple from dependency values."""
        if port_start is None:
            return None

        protocol_value = str(protocol_raw or "").strip().lower()
        if protocol_value in cls.SKIP_DEP_PROTOCOLS:
            return None

        proto = cls.normalize_service_protocol(protocol_value)
        if proto is None:
            return None

        return int(port_start), int(port_end) if port_end else None, proto

    @staticmethod
    def vip_protocol_for_service_port(service_port_protocol: Any, port: int) -> str:
        """Map AppServicePort protocol into LoadbalancerVIP protocol choices."""
        proto = str(service_port_protocol or "tcp").lower()

        if proto == "tcp_udp":
            proto = "tcp"

        if proto == "tcp" and int(port) == 443:
            return "https"
        if proto == "tcp" and int(port) == 80:
            return "http"

        if proto in {"http", "https", "tls", "tcp", "udp"}:
            return proto

        return "tcp"

    @staticmethod
    def health_check_type_for_vip(vip_protocol: Any) -> str:
        """Map VIP protocol into default LB health-check type."""
        proto = str(vip_protocol or "tcp").lower()
        if proto == "http":
            return "http"
        if proto in {"https", "tls"}:
            return "ssl"
        return "tcp"


class PortsPlanner:
    """Single planner for application port preparation workflows."""

    SKIP_DEP_PROTOCOLS = PortProfileHelper.SKIP_DEP_PROTOCOLS

    @classmethod
    def normalize_protocol(cls, protocol_raw: Any) -> str | None:
        """Normalize protocol names to AppServicePort-compatible values."""
        return PortProfileHelper.normalize_service_protocol(protocol_raw)

    @classmethod
    def derive_ports_from_vips(
        cls,
        vip_services: list[dict[str, Any]],
    ) -> tuple[set[tuple[int, int | None, str]], list[str]]:
        """Derive service-port tuples from VIP service declarations."""
        ports: set[tuple[int, int | None, str]] = set()
        warnings: list[str] = []

        for vip in vip_services:
            port_val = vip.get("port")
            proto_raw = vip.get("protocol")
            if port_val is None:
                continue

            proto = cls.normalize_protocol(proto_raw)
            if proto is None:
                warnings.append(f"Skipping VIP with unknown protocol '{proto_raw}' on port {port_val}")
                continue

            ports.add((int(port_val), None, proto))

        return ports, warnings

    @classmethod
    def derive_port_from_dependency_values(
        cls,
        *,
        port_start: int | None,
        port_end: int | None,
        protocol_raw: Any,
    ) -> tuple[int, int | None, str] | None:
        """Derive one AppServicePort tuple from dependency scalar values."""
        return PortProfileHelper.derive_service_port_tuple(
            port_start=port_start,
            port_end=port_end,
            protocol_raw=protocol_raw,
        )

    @staticmethod
    def to_vip_protocol(service_port_protocol: Any, port: int) -> str:
        """Map AppServicePort protocol into LoadbalancerVIP protocol choices."""
        return PortProfileHelper.vip_protocol_for_service_port(service_port_protocol, port)

    @staticmethod
    def health_check_type_for_vip(vip_protocol: Any) -> str:
        """Map VIP protocol into default LB health-check type."""
        return PortProfileHelper.health_check_type_for_vip(vip_protocol)
