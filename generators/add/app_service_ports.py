"""Generator: AppServicePort nodes for each AppComponent.

Derives service ports from two sources:
  1. VIP services (LoadbalancerVIP.port + .protocol) linked to the component
  2. Inbound AppDependency edges (AppDependency.port_start/.port_end/.protocol)
     where this component is the target

AppServicePort nodes are GLOBAL — the same 443/tcp node is shared by every
component that exposes it. The generator:
  1. Creates or upserts each AppServicePort (port, port_end, protocol) globally
  2. Links the component to the node via AppComponent.service_ports

VIP protocols https/http/tls map to tcp (AppServicePort only has tcp/udp/tcp_udp).
AppDependency protocols icmp/any are skipped (no port to expose).
Duplicate (port, port_end, protocol) tuples across both sources are de-duplicated.

The 07_service_ports.yml static data files in data/demos/.../06_applications/
were the original per-component source of truth; this generator replaces them.
"""

from __future__ import annotations

from typing import Any

from utils.data_cleaning import clean_data

from ..common import CommonGenerator

_VIP_TCP_ALIASES = frozenset({"https", "http", "tls"})
_SKIP_DEP_PROTOCOLS = frozenset({"icmp", "any"})


class AppServicePortGenerator(CommonGenerator):
    """Create/upsert global AppServicePort nodes and link them to an AppComponent.

    Triggered once per AppComponent in the app_components group.
    """

    async def generate(self, data: dict[str, Any]) -> None:
        cleaned = clean_data(data)
        components = cleaned.get("AppComponent", [])
        if not components:
            self.logger.error("No AppComponent data in GraphQL response")
            return

        component = components[0]
        component_id: str = component.get("id", "")
        component_slug: str = component.get("slug", "")

        if not component_id or not component_slug:
            self.logger.error("AppComponent missing id or slug — cannot proceed")
            return

        self.logger.info("Processing service ports for component: %s", component_slug)

        # Collect (port, port_end, protocol) tuples — port_end None means single port
        ports: set[tuple[int, int | None, str]] = set()

        vip_services = component.get("vip_services") or []
        for vip in vip_services:
            port_val = vip.get("port")
            proto_raw = vip.get("protocol") or ""
            if port_val is None:
                continue
            proto = "tcp" if proto_raw in _VIP_TCP_ALIASES else proto_raw
            if proto not in ("tcp", "udp", "tcp_udp"):
                self.logger.warning("  Skipping VIP with unknown protocol '%s' on port %s", proto_raw, port_val)
                continue
            ports.add((int(port_val), None, proto))
            self.logger.info("  VIP → port %s/%s", port_val, proto)

        try:
            inbound_deps = await self.client.filters(
                kind="AppDependency",
                target__ids=[component_id],
            )
            for dep in inbound_deps:
                await dep.resolve()
                port_start = getattr(getattr(dep, "port_start", None), "value", None)
                port_end = getattr(getattr(dep, "port_end", None), "value", None)
                proto_raw = getattr(getattr(dep, "protocol", None), "value", None) or ""
                if port_start is None:
                    continue
                if proto_raw in _SKIP_DEP_PROTOCOLS:
                    continue
                proto = "tcp" if proto_raw in _VIP_TCP_ALIASES else proto_raw
                if proto not in ("tcp", "udp", "tcp_udp"):
                    self.logger.warning("  Dep → unknown protocol '%s' — skipping", proto_raw)
                    continue
                ports.add((int(port_start), int(port_end) if port_end else None, proto))
                range_str = f"{port_start}-{port_end}" if port_end else str(port_start)
                self.logger.info("  Dep → port %s/%s", range_str, proto)
        except Exception as exc:
            self.logger.warning("  Could not fetch inbound AppDependency edges: %s", exc)

        if not ports:
            self.logger.info("  No service ports derived for %s — nothing to create", component_slug)
            return

        # Fetch component SDK object to update the service_ports relationship
        component_obj = await self.client.get(kind="AppComponent", id=component_id)
        if component_obj is None:
            self.logger.error("Could not fetch AppComponent object for id %s", component_id)
            return

        service_ports_rel = getattr(component_obj, "service_ports")
        await service_ports_rel.fetch()
        existing_port_ids = {peer.id for peer in service_ports_rel.peers}

        for port, port_end, protocol in sorted(ports):
            range_str = f"{port}-{port_end}" if port_end else str(port)
            try:
                port_data: dict[str, Any] = {"port": port, "protocol": protocol}
                if port_end is not None:
                    port_data["port_end"] = port_end

                port_obj = await self.client.create(kind="AppServicePort", data=port_data)
                await port_obj.save(allow_upsert=True)
                self.logger.info("  Upserted AppServicePort %s/%s (id: %s)", range_str, protocol, port_obj.id)

                if port_obj.id not in existing_port_ids:
                    await service_ports_rel.add(port_obj)
                    existing_port_ids.add(port_obj.id)

            except Exception as exc:
                self.logger.error(
                    "  Failed to upsert AppServicePort %s/%s for %s: %s",
                    range_str,
                    protocol,
                    component_slug,
                    exc,
                )
                continue

        try:
            await component_obj.save(allow_upsert=True)
            self.logger.info("  Updated service_ports on component %s", component_slug)
        except Exception as exc:
            self.logger.error("  Failed to save component %s service_ports: %s", component_slug, exc)
