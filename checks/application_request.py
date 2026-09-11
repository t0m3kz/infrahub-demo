"""Validate customer application deployment requests before provisioning."""

from __future__ import annotations

from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from .common import clean_data


class CheckApplicationDeploymentRequest(InfrahubCheck):
    """Fail approved requests that cannot be safely materialized."""

    query = "application_request_validation"

    def validate(self, data: Any) -> None:
        cleaned = clean_data(data)
        requests = cleaned.get("AppDeploymentRequest") or []
        if not requests:
            self.log_error(message="Application deployment request was not found")
            return

        request = requests[0]
        name = request.get("name", "?")
        status = request.get("status", "draft")
        if status not in {"approved", "provisioning", "completed"}:
            return

        if not request.get("customer"):
            self.log_error(message=f"Request '{name}' has no customer")
        if not request.get("application_fqdn"):
            self.log_error(message=f"Request '{name}' has no application_fqdn")
        if not request.get("components"):
            self.log_error(message=f"Request '{name}' has no requested components")

        for component in request.get("components") or []:
            for endpoint in component.get("endpoints") or []:
                endpoint_name = endpoint.get("id", endpoint.get("name", "?"))
                endpoint_type = endpoint.get("endpoint_type")
                if not endpoint.get("fqdn"):
                    self.log_error(message=f"Request '{name}' endpoint '{endpoint_name}' has no fqdn")
                if endpoint_type == "private_access":
                    profile = endpoint.get("access_profile") or {}
                    if not (profile.get("allowed_groups") or []):
                        self.log_error(
                            message=f"Request '{name}' private-access endpoint '{endpoint_name}' has no allowed groups"
                        )

        for dependency in request.get("dependencies") or []:
            dependency_name = dependency.get("name", dependency.get("id", "?"))
            if not dependency.get("business_justification"):
                self.log_error(message=f"Dependency '{dependency_name}' has no business_justification")
            if not dependency.get("target") and not dependency.get("existing_target"):
                self.log_error(message=f"Dependency '{dependency_name}' has no target endpoint")
            if dependency.get("target") and dependency.get("existing_target"):
                self.log_error(message=f"Dependency '{dependency_name}' has multiple target endpoints")

            if dependency.get("access_status") == "approved":
                approved_by = dependency.get("approved_by") or {}
                if not approved_by:
                    self.log_error(message=f"Approved dependency '{dependency_name}' has no approved_by customer")
                target_owner = self._existing_target_owner(dependency)
                approved_by_id = approved_by if isinstance(approved_by, str) else approved_by.get("id")
                if target_owner and approved_by_id != target_owner:
                    self.log_error(
                        message=f"Approved dependency '{dependency_name}' was not approved by the target owner"
                    )

    @staticmethod
    def _existing_target_owner(dependency: dict[str, Any]) -> str | None:
        target = dependency.get("existing_target") or {}
        parent = target.get("parent") or {}
        component = parent.get("parent") or {}
        application = component.get("parent") or {}
        owner = application.get("owner") or {}
        return owner if isinstance(owner, str) else owner.get("id")
