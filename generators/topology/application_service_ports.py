"""Service-port generators for AppComponent and AppDependency events."""

from __future__ import annotations

from typing import Any

from utils.data_cleaning import clean_data

from ..common import CommonGenerator
from ..helpers.ports import PortsPlanner
from ..protocols import AppComponent, AppDependency, AppServicePort


class BaseAppServicePortGenerator(CommonGenerator):
    """Common AppServicePort upsert/link helpers for service-port generators."""

    async def _upsert_service_port_object(
        self,
        port: int,
        port_end: int | None,
        protocol: str,
    ) -> Any:
        port_data: dict[str, Any] = {"port": port, "protocol": protocol}
        if port_end is not None:
            port_data["port_end"] = port_end
        port_obj = await self.client.create(kind=AppServicePort, data=port_data)
        await port_obj.save(allow_upsert=True)
        return port_obj

    async def _get_component_with_ports(
        self,
        component_id: str,
    ) -> tuple[Any, Any, set[str]] | None:
        component_obj = await self.client.get(kind=AppComponent, id=component_id)
        if component_obj is None:
            self.logger.error("Could not fetch AppComponent object for id %s", component_id)
            return None
        service_ports_rel = getattr(component_obj, "service_ports")
        await service_ports_rel.fetch()
        existing_port_ids = {peer.id for peer in service_ports_rel.peers}
        return component_obj, service_ports_rel, existing_port_ids

    @staticmethod
    def _port_range_str(port: int, port_end: int | None) -> str:
        return f"{port}-{port_end}" if port_end else str(port)


class AppServicePortGenerator(BaseAppServicePortGenerator):
    """Create/upsert global AppServicePort nodes and link them to an AppComponent."""

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
            self.logger.error("AppComponent missing id or slug - cannot proceed")
            return

        self.logger.info("Processing service ports for component: %s", component_slug)

        ports: set[tuple[int, int | None, str]] = set()

        try:
            inbound_deps = await self.client.filters(kind=AppDependency, target__ids=[component_id])
            for dep in inbound_deps:
                port_start = getattr(getattr(dep, "port_start", None), "value", None)
                port_end = getattr(getattr(dep, "port_end", None), "value", None)
                proto_raw = getattr(getattr(dep, "protocol", None), "value", None) or ""
                derived = PortsPlanner.derive_port_from_dependency_values(
                    port_start=port_start,
                    port_end=port_end,
                    protocol_raw=proto_raw,
                )
                if derived is None:
                    if port_start is not None and proto_raw not in PortsPlanner.SKIP_DEP_PROTOCOLS:
                        self.logger.warning("  Dep -> unknown protocol '%s' - skipping", proto_raw)
                    continue
                ports.add(derived)
                proto = derived[2]
                range_str = f"{port_start}-{port_end}" if port_end else str(port_start)
                self.logger.info("  Dep -> port %s/%s", range_str, proto)
        except Exception as exc:
            self.logger.warning("  Could not fetch inbound AppDependency edges: %s", exc)

        if not ports:
            self.logger.info("  No service ports derived for %s - nothing to create", component_slug)
            return

        component_state = await self._get_component_with_ports(component_id)
        if component_state is None:
            return
        component_obj, service_ports_rel, existing_port_ids = component_state
        component_updated = False

        for port, port_end, protocol in sorted(ports):
            range_str = self._port_range_str(port, port_end)
            try:
                port_obj = await self._upsert_service_port_object(port=port, port_end=port_end, protocol=protocol)
                self.logger.info("  Upserted AppServicePort %s/%s (id: %s)", range_str, protocol, port_obj.id)

                if port_obj.id not in existing_port_ids:
                    await self._safe_rel_add(service_ports_rel, port_obj)
                    existing_port_ids.add(port_obj.id)
                    component_updated = True

            except Exception as exc:
                self.logger.error(
                    "  Failed to upsert AppServicePort %s/%s for %s: %s",
                    range_str,
                    protocol,
                    component_slug,
                    exc,
                )
                continue

        if component_updated:
            try:
                await component_obj.save(allow_upsert=True)
                self.logger.info("  Updated service_ports on component %s", component_slug)
            except Exception as exc:
                self.logger.error("  Failed to save component %s service_ports: %s", component_slug, exc)
        else:
            self.logger.info("  No service_ports relationship updates needed for %s", component_slug)


class AppDependencyServicePortGenerator(BaseAppServicePortGenerator):
    """Lightweight generator for dependency-driven service-port updates."""

    async def generate(self, data: dict[str, Any]) -> None:
        cleaned = clean_data(data)
        deps = cleaned.get("AppDependency", [])
        if not deps:
            self.logger.error("No AppDependency data in GraphQL response")
            return

        dep = deps[0]
        target = dep.get("target") or {}
        component_id = target.get("id", "")
        component_slug = target.get("slug", "")

        if not component_id:
            self.logger.warning("AppDependency target missing id - cannot update service ports")
            return

        port_tuple = PortsPlanner.derive_port_from_dependency_values(
            port_start=dep.get("port_start"),
            port_end=dep.get("port_end"),
            protocol_raw=dep.get("protocol"),
        )
        if port_tuple is None:
            self.logger.info(
                "Dependency %s has no AppServicePort-compatible tuple - nothing to update",
                dep.get("name", dep.get("id", "?")),
            )
            return

        port, port_end, protocol = port_tuple
        range_str = self._port_range_str(port, port_end)

        component_state = await self._get_component_with_ports(component_id)
        if component_state is None:
            return
        component_obj, service_ports_rel, existing_port_ids = component_state

        try:
            port_obj = await self._upsert_service_port_object(port=port, port_end=port_end, protocol=protocol)

            if port_obj.id in existing_port_ids:
                self.logger.info(
                    "Dependency-driven port already linked: %s/%s for %s",
                    range_str,
                    protocol,
                    component_slug or component_id,
                )
                return

            await self._safe_rel_add(service_ports_rel, port_obj)
            await component_obj.save(allow_upsert=True)
            self.logger.info(
                "Linked dependency-driven port %s/%s to %s",
                range_str,
                protocol,
                component_slug or component_id,
            )
        except Exception as exc:
            self.logger.error(
                "Failed dependency-driven AppServicePort upsert/link %s/%s for %s: %s",
                range_str,
                protocol,
                component_slug or component_id,
                exc,
            )
