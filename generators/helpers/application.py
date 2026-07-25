from __future__ import annotations

import re
from typing import Any


class ApplicationSegmentRulePlanner:
    """Preparation-only helper for segment-driven application rule planning.

    This helper must not perform database operations.
    It derives deterministic inputs for security rules based on application traffic.
    """

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
        protocol = dep.get("protocol")
        port_start = dep.get("port_start")

        if protocol is None and port_start is None:
            return None

        return (protocol or "tcp"), port_start, dep.get("port_end")

    @staticmethod
    def collect_dependency_edges(components: list[dict[str, Any]]) -> tuple[list[tuple[dict, dict, dict]], list[str]]:
        """Collect dependency edges and validation warnings.

        Returns:
            - edges: list of (src_component, dependency, dst_component)
            - warnings: message list for edges skipped due to missing target
        """
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
        """Build deterministic, human-readable rule name."""
        src_label = src.get("label") or src.get("slug") or src.get("name", "src")
        dst_label = dst.get("label") or dst.get("slug") or dst.get("name", "dst")

        # Keep full labels/slugs to avoid collisions such as
        # "payments-api" and "internal-api" both collapsing to "api".
        src_norm = str(src_label).lower().replace(" ", "-")
        dst_norm = str(dst_label).lower().replace(" ", "-")
        return f"{app_name}-{src_norm}-to-{dst_norm}"

    @staticmethod
    def rule_description(dep: dict[str, Any], src_comp: dict[str, Any], dst_comp: dict[str, Any]) -> str:
        """Build default deterministic description for generated rule."""
        if dep.get("description"):
            return dep["description"]

        src_type = src_comp.get("component_type", "backend")
        dst_type = dst_comp.get("component_type", "backend")
        return f"Auto-generated: {src_comp.get('name', '?')} ({src_type}) → {dst_comp.get('name', '?')} ({dst_type})"


class ApplicationServicePortPlanner:
    """Preparation-only helper for AppServicePort derivation."""

    VIP_TCP_ALIASES = frozenset({"https", "http", "tls"})
    SKIP_DEP_PROTOCOLS = frozenset({"icmp", "any"})
    SUPPORTED_PROTOCOLS = frozenset({"tcp", "udp", "tcp_udp"})

    @classmethod
    def normalize_protocol(cls, protocol_raw: str | None) -> str | None:
        """Normalize protocol names to AppServicePort-compatible values."""
        proto_raw = protocol_raw or ""
        proto = "tcp" if proto_raw in cls.VIP_TCP_ALIASES else proto_raw
        if proto not in cls.SUPPORTED_PROTOCOLS:
            return None
        return proto

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
        protocol_raw: str | None,
    ) -> tuple[int, int | None, str] | None:
        """Derive one service-port tuple from dependency scalar values."""
        if port_start is None:
            return None
        if (protocol_raw or "") in cls.SKIP_DEP_PROTOCOLS:
            return None
        proto = cls.normalize_protocol(protocol_raw)
        if proto is None:
            return None
        return int(port_start), int(port_end) if port_end else None, proto


class ApplicationComponentPlanner:
    """Preparation-only helper for AppComponent generator decisions."""

    @staticmethod
    def collect_host_ids(instances: list[dict[str, Any]]) -> set[str]:
        """Return unique physical host IDs behind component instances."""
        host_ids: set[str] = set()
        for inst in instances:
            typename = inst.get("typename", "")
            if typename == "DcimVirtualDevice":
                hosting = inst.get("hosting_device") or {}
                if hosting.get("id"):
                    host_ids.add(hosting["id"])
            elif typename == "DcimPhysicalDevice" and inst.get("id"):
                host_ids.add(inst["id"])
        return host_ids

    @staticmethod
    def is_cloud_segment(network_segment: dict[str, Any]) -> bool:
        """Return True when component segment is cloud-based."""
        return network_segment.get("typename", "") == "CloudNetworkSegment"

    @staticmethod
    def backend_port(service_ports: list[dict[str, Any]]) -> int | None:
        """Return first declared backend port if present."""
        if not service_ports:
            return None
        return service_ports[0].get("port")

    @staticmethod
    def select_primary_service_port(service_ports: list[dict[str, Any]]) -> tuple[int, str] | None:
        """Select a deterministic primary service port for VIP auto-creation."""
        if not service_ports:
            return None

        protocol_rank = {
            "https": 0,
            "http": 1,
            "tls": 2,
            "tcp": 3,
            "udp": 4,
            "tcp_udp": 5,
        }

        candidates: list[tuple[int, int, str]] = []
        for sp in service_ports:
            port_val = sp.get("port")
            if port_val is None:
                continue
            try:
                port_int = int(port_val)
            except (TypeError, ValueError):
                continue

            proto = str(sp.get("protocol") or "tcp").lower()
            rank = protocol_rank.get(proto, 99)
            candidates.append((rank, port_int, proto))

        if not candidates:
            return None

        _rank, selected_port, selected_proto = sorted(candidates, key=lambda x: (x[0], x[1]))[0]
        return (selected_port, selected_proto)

    @staticmethod
    def to_vip_protocol(service_port_protocol: str, port: int) -> str:
        """Map AppServicePort protocol into LoadbalancerVIP protocol choices."""
        proto = (service_port_protocol or "tcp").lower()

        if proto == "tcp_udp":
            proto = "tcp"

        if proto == "tcp" and port == 443:
            return "https"
        if proto == "tcp" and port == 80:
            return "http"

        if proto in {"http", "https", "tls", "tcp", "udp"}:
            return proto

        return "tcp"

    @staticmethod
    def extract_app_fqdn(parent: dict[str, Any]) -> str:
        """Return parent application FQDN value if present."""
        fqdn = parent.get("fqdn")
        if isinstance(fqdn, dict):
            return str(fqdn.get("value") or "").strip().lower()
        return str(fqdn or "").strip().lower()

    @staticmethod
    def _sanitize_dns_label(value: str) -> str:
        """Sanitize arbitrary text into a DNS label."""
        normalized = re.sub(r"[^a-z0-9-]", "-", value.strip().lower())
        normalized = re.sub(r"-+", "-", normalized).strip("-")
        return normalized or "component"

    @classmethod
    def derive_vip_hostname(
        cls,
        app_fqdn: str,
        component_name: str,
        component_type: str,
        component_slug: str,
    ) -> str:
        """Derive deterministic VIP hostname from app/context data."""
        safe_component = cls._sanitize_dns_label(component_name or component_slug)
        safe_slug = cls._sanitize_dns_label(component_slug or component_name)

        if app_fqdn:
            if component_type == "frontend":
                return app_fqdn
            return f"{safe_component}.{app_fqdn}"

        return f"{safe_slug}.internal"
