"""Materialize approved application deployment requests into catalogue objects."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from utils.data_cleaning import clean_data

from ..common import CommonGenerator

_REQUEST_QUERY_PATH = Path(__file__).resolve().parents[2] / "queries/topology/add/application_request.gql"


class AppDeploymentRequestGenerator(CommonGenerator):
    """Translate approved customer request intent into application catalogue objects."""

    async def generate(self, data: dict[str, Any]) -> None:
        cleaned = clean_data(data)
        requests = cleaned.get("AppDeploymentRequest") or []
        if not requests:
            self.logger.warning("No AppDeploymentRequest found in generator payload")
            return

        request = requests[0]
        status = str(request.get("status") or "draft")
        if status not in {"approved", "provisioning", "completed"}:
            self.logger.info("Request %s is %s; no catalogue objects will be generated", request.get("name"), status)
            return

        customer = request.get("customer") or {}
        customer_id = customer if isinstance(customer, str) else customer.get("id")
        if not customer_id:
            self.logger.error("Request %s has no customer", request.get("name", "?"))
            return

        application = await self._get_or_create_application(request=request, customer_id=customer_id)
        if application is None:
            return

        component_map: dict[str, Any] = {}
        endpoint_map: dict[str, Any] = {}
        for request_component in request.get("components") or []:
            component = await self._get_or_create_component(request_component, application.id)
            if component is None:
                continue
            component_map[str(request_component.get("id"))] = component

            for request_endpoint in request_component.get("endpoints") or []:
                endpoint = await self._get_or_create_endpoint(
                    request_endpoint,
                    component.id,
                    request.get("access_profile") or {},
                )
                if endpoint is not None:
                    await self._ensure_endpoint_port(endpoint, request_endpoint)
                    endpoint_map[str(request_endpoint.get("id"))] = endpoint

        generated_dependencies: list[Any] = []
        for dependency in request.get("dependencies") or []:
            materialized = await self._materialize_dependency(
                dependency=dependency,
                component_map=component_map,
                endpoint_map=endpoint_map,
                request_customer_id=customer_id,
            )
            if materialized is not None:
                generated_dependencies.append(materialized)

        request_obj = await self.client.get(kind="AppDeploymentRequest", id=request.get("id"))
        if request_obj is not None and status == "approved":
            generated_dependencies_rel = getattr(request_obj, "generated_dependencies")
            await generated_dependencies_rel.fetch()
            existing_dependency_ids = {peer.id for peer in generated_dependencies_rel.peers}
            for dependency in generated_dependencies:
                if dependency.id not in existing_dependency_ids:
                    generated_dependencies_rel.add(dependency)
            setattr(request_obj, "generated_application", {"id": application.id})
            setattr(request_obj, "status", "completed")
            setattr(request_obj, "completed_at", datetime.now(timezone.utc).isoformat())
            await request_obj.save(allow_upsert=True)

    async def _get_or_create_application(self, request: dict[str, Any], customer_id: str) -> Any | None:
        label = str(request.get("name") or "").strip()
        if not label:
            self.logger.error("Application request has no name")
            return None

        existing = await self.client.filters(kind="AppApplication", label__value=label, owner__ids=[customer_id])
        if existing:
            return existing[0]

        application = await self.client.create(
            kind="AppApplication",
            data={
                "label": label,
                "environment": self._application_environment(request.get("environment")),
                "criticality": request.get("criticality", "medium"),
                "security_profile": request.get("security_profile", "internal_standard"),
                "fqdn": request.get("application_fqdn"),
                "ingress_mode": "dns",
                "application_orchestrator": "manual",
                "owner": {"id": customer_id},
                "member_of_groups": ["app_applications"],
            },
        )
        await application.save(allow_upsert=True)
        return application

    @staticmethod
    def _application_environment(value: Any) -> str:
        """Translate request lifecycle labels to catalogue environment keys."""
        environments = {
            "production": "p",
            "staging": "s",
            "development": "d",
        }
        return environments.get(str(value or "production").lower(), "p")

    async def _get_or_create_component(self, request_component: dict[str, Any], application_id: str) -> Any | None:
        name = str(request_component.get("name") or "").strip()
        if not name:
            return None

        existing = await self.client.filters(kind="AppComponent", name__value=name, parent__ids=[application_id])
        if existing:
            return existing[0]

        component_data: dict[str, Any] = {
            "name": name,
            "component_type": request_component.get("component_type", "backend"),
            "parent": {"id": application_id},
        }
        network_segment = request_component.get("network_segment") or {}
        if network_segment:
            segment_id = network_segment.get("id") if isinstance(network_segment, dict) else network_segment
            component_data["network_segment"] = {"id": segment_id}
        component = await self.client.create(kind="AppComponent", data=component_data)
        await component.save(allow_upsert=True)
        return component

    async def _get_or_create_endpoint(
        self,
        request_endpoint: dict[str, Any],
        component_id: str,
        default_access_profile: dict[str, Any],
    ) -> Any | None:
        name = str(request_endpoint.get("name") or "").strip()
        if not name:
            return None

        existing = await self.client.filters(kind="AppEndpoint", name__value=name, parent__ids=[component_id])
        if existing:
            return existing[0]

        access_profile = request_endpoint.get("access_profile") or default_access_profile
        endpoint_data: dict[str, Any] = {
            "name": name,
            "endpoint_type": request_endpoint.get("endpoint_type", "internal_service"),
            "fqdn": request_endpoint.get("fqdn"),
            "parent": {"id": component_id},
        }
        access_profile_id = self._relation_id(access_profile)
        if access_profile_id:
            endpoint_data["access_profile"] = {"id": access_profile_id}

        endpoint = await self.client.create(
            kind="AppEndpoint",
            data=endpoint_data,
        )
        await endpoint.save(allow_upsert=True)
        return endpoint

    async def _ensure_endpoint_port(self, endpoint: Any, request_endpoint: dict[str, Any]) -> None:
        port = request_endpoint.get("port")
        if port is None:
            return
        port_obj = await self.client.create(
            kind="AppServicePort",
            data={"port": port, "protocol": request_endpoint.get("protocol") or "tcp"},
        )
        await port_obj.save(allow_upsert=True)
        ports_rel = getattr(endpoint, "service_ports")
        await ports_rel.fetch()
        if port_obj.id not in {peer.id for peer in ports_rel.peers}:
            ports_rel.add(port_obj)
            await endpoint.save(allow_upsert=True)

    async def _materialize_dependency(
        self,
        dependency: dict[str, Any],
        component_map: dict[str, Any],
        endpoint_map: dict[str, Any],
        request_customer_id: str,
    ) -> Any | None:
        dependency_name = dependency.get("name", "?")
        existing = await self.client.filters(kind="AppDependency", name__value=dependency_name)
        if dependency.get("access_status") != "approved" or self._is_expired(dependency.get("access_expires_at")):
            if existing:
                setattr(existing[0], "access_status", "denied")
                await existing[0].save(allow_upsert=True)
            self.logger.info("Dependency %s is not currently approved; skipping", dependency_name)
            return None

        source_request_id = str((dependency.get("source") or {}).get("id") or "")
        source = component_map.get(source_request_id)
        target = self._resolve_target(dependency=dependency, endpoint_map=endpoint_map)
        if source is None or target is None:
            self.logger.warning("Dependency %s has unresolved source or target; skipping", dependency_name)
            return None

        target_id = target.id if hasattr(target, "id") else target.get("id")
        if not target_id:
            self.logger.warning("Dependency %s target has no id; skipping", dependency_name)
            return None

        existing_target_owner = self._existing_target_owner(dependency)
        approved_by_value = dependency.get("approved_by") or {}
        approved_by = approved_by_value if isinstance(approved_by_value, str) else approved_by_value.get("id")
        if existing_target_owner and approved_by != existing_target_owner:
            self.logger.warning(
                "Dependency %s was not approved by target owner %s; skipping",
                dependency_name,
                existing_target_owner,
            )
            return None

        name = str(dependency.get("name") or "").strip()
        if not name:
            return None
        data = {
            "name": name,
            "source": {"id": source.id},
            "target": {"id": target_id},
            "protocol": dependency.get("protocol"),
            "port_start": dependency.get("port_start"),
            "port_end": dependency.get("port_end"),
            "access_status": "approved",
            "decision_reason": dependency.get("decision_reason"),
            "decision_at": dependency.get("decision_at"),
            "access_expires_at": dependency.get("access_expires_at"),
        }
        if existing:
            data["id"] = existing[0].id
        materialized = await self.client.create(kind="AppDependency", data=data)
        await materialized.save(allow_upsert=True)
        dependencies_rel = getattr(source, "depends_on")
        await dependencies_rel.fetch()
        if materialized.id not in {peer.id for peer in dependencies_rel.peers}:
            await self._safe_rel_add(dependencies_rel, {"id": materialized.id})
            await source.save(allow_upsert=True)
        return materialized

    @staticmethod
    def _is_expired(value: Any) -> bool:
        if not value:
            return False
        try:
            expires_at = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            return expires_at <= datetime.now(timezone.utc)
        except ValueError:
            return False

    @staticmethod
    def _relation_id(value: Any) -> str | None:
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            return value.get("id")
        return getattr(value, "id", None)

    @staticmethod
    def _resolve_target(dependency: dict[str, Any], endpoint_map: dict[str, Any]) -> Any | None:
        existing_target = dependency.get("existing_target") or {}
        if existing_target.get("id"):
            return existing_target
        requested_target = dependency.get("target") or {}
        return endpoint_map.get(str(requested_target.get("id")))

    @staticmethod
    def _existing_target_owner(dependency: dict[str, Any]) -> str | None:
        existing_target = dependency.get("existing_target") or {}
        parent = existing_target.get("parent") or {}
        component_parent = parent.get("parent") or {}
        application = component_parent.get("parent") or {}
        owner = application.get("owner") or {}
        return owner if isinstance(owner, str) else owner.get("id")
