"""Mixin for syncing AppServicePort links onto AppDependency-targeted endpoints."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

from .helpers.ports import PortsPlanner
from .protocols import AppServicePort

if TYPE_CHECKING:
    import logging


class ServicePortMixin:
    """Derives AppServicePort objects from AppDependency protocol/port values
    and links them onto the dependency's target AppEndpoint — runs ahead of
    rule dispatch in _reconcile_application_rules so every rule kind (cloud,
    proxy, on-prem segment) sees the endpoint's ports already up to date.

    Expects the host class to provide: ``client``, ``logger``, ``_safe_rel_add``.
    """

    client: Any
    logger: logging.Logger
    _safe_rel_add: Callable[..., Any]

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

    async def _get_endpoint_with_ports(
        self,
        endpoint_id: str,
    ) -> tuple[Any, Any, set[str]] | None:
        endpoint_obj = await self.client.get(kind="AppEndpoint", id=endpoint_id)
        if endpoint_obj is None:
            self.logger.error("Could not fetch AppEndpoint object for id %s", endpoint_id)
            return None
        service_ports_rel = getattr(endpoint_obj, "service_ports")
        await service_ports_rel.fetch()
        existing_port_ids = {peer.id for peer in service_ports_rel.peers}
        return endpoint_obj, service_ports_rel, existing_port_ids

    @staticmethod
    def _port_range_str(port: int, port_end: int | None) -> str:
        return f"{port}-{port_end}" if port_end else str(port)

    async def _reconcile_component_service_ports(
        self,
        components: list[dict[str, Any]],
        edges: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]],
    ) -> None:
        ports_by_target: dict[str, set[tuple[int, int | None, str]]] = {}

        for _src_comp, dep, dst_endpoint in edges:
            target_id = str(dst_endpoint.get("id") or "")
            if not target_id:
                continue

            derived = PortsPlanner.derive_port_from_dependency_values(
                port_start=dep.get("port_start"),
                port_end=dep.get("port_end"),
                protocol_raw=dep.get("protocol"),
            )
            if derived is None:
                port_start = dep.get("port_start")
                protocol_raw = str(dep.get("protocol") or "").strip().lower()
                if port_start is not None and protocol_raw not in PortsPlanner.SKIP_DEP_PROTOCOLS:
                    self.logger.warning("  Dep -> unknown protocol '%s' - skipping AppServicePort", protocol_raw)
                continue

            ports_by_target.setdefault(target_id, set()).add(derived)

        if not ports_by_target:
            return

        endpoints_by_id = {
            str(endpoint.get("id") or ""): endpoint
            for component in components
            for endpoint in component.get("children") or []
            if endpoint.get("id")
        }

        for target_id, ports in sorted(ports_by_target.items()):
            endpoint_ref = endpoints_by_id.get(target_id) or {}
            endpoint_name = str(endpoint_ref.get("name") or target_id)

            endpoint_state = await self._get_endpoint_with_ports(target_id)
            if endpoint_state is None:
                continue

            endpoint_obj, service_ports_rel, existing_port_ids = endpoint_state
            endpoint_updated = False

            for port, port_end, protocol in sorted(ports):
                range_str = self._port_range_str(port, port_end)
                try:
                    port_obj = await self._upsert_service_port_object(port=port, port_end=port_end, protocol=protocol)
                    if port_obj.id in existing_port_ids:
                        continue
                    await self._safe_rel_add(service_ports_rel, port_obj)
                    existing_port_ids.add(port_obj.id)
                    endpoint_updated = True
                    self.logger.info("  Linked AppServicePort %s/%s to endpoint %s", range_str, protocol, endpoint_name)
                except Exception as exc:
                    self.logger.error(
                        "  Failed AppServicePort upsert/link %s/%s for endpoint %s: %s",
                        range_str,
                        protocol,
                        endpoint_name,
                        exc,
                    )

            if endpoint_updated:
                try:
                    # update_group_context=False: AppEndpoint is user-authored
                    # data this generator only enriches, not a generated
                    # artifact it owns. Without this, save() while self.client
                    # is in TRACKING mode registers the endpoint as a group
                    # member only on runs that link a *new* port; an
                    # idempotent re-run linking nothing then leaves it
                    # unregistered and the SDK's delete_unused_nodes tries to
                    # delete it (previously blocked only by AppDependency.target
                    # being a mandatory relationship — a near-miss data loss).
                    await endpoint_obj.save(allow_upsert=True, update_group_context=False)
                except Exception as exc:
                    self.logger.error("  Failed to save endpoint %s service_ports: %s", endpoint_name, exc)
