from __future__ import annotations

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
        src_slug = src.get("slug") or src.get("name", "src")
        dst_slug = dst.get("slug") or dst.get("name", "dst")
        src_short = src_slug.split("-")[-1] if "-" in src_slug else src_slug
        dst_short = dst_slug.split("-")[-1] if "-" in dst_slug else dst_slug
        return f"{app_name}-{src_short}-to-{dst_short}"

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
