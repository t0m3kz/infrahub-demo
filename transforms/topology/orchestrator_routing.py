"""Transforms: customer deployment / application → orchestrator delivery manifest.

Two query shapes, four topology kinds (TopologyCustomerDC/Colocation/Office/Cloud)
sharing one query (topology_orchestrator_routing_data.gql) and one AppApplication
query (application_orchestrator_routing_data.gql). Each artifact_definition targets
one group (topology_ansible, topology_terraform, ..., application_argocd) whose
membership is maintained by generators/topology/orchestrator_routing.py — the
transform itself only picks the builder for its own orchestrator, ignoring members
whose infrastructure_orchestrator/application_orchestrator field has since changed
(renders an empty/placeholder manifest rather than erroring, since the artifact
target group can lag one generator run behind the field).
"""

from __future__ import annotations

import json
from typing import Any

from infrahub_sdk.transforms import InfrahubTransform

from utils.data_cleaning import clean_data

_TOPOLOGY_KINDS = (
    "TopologyCustomerDC",
    "TopologyCustomerColocation",
    "TopologyCustomerOffice",
    "TopologyCustomerCloud",
)


def _first_topology_deployment(cleaned: dict[str, Any]) -> dict[str, Any] | None:
    for kind in _TOPOLOGY_KINDS:
        entries = cleaned.get(kind) or []
        if entries:
            deployment = dict(entries[0])
            deployment["kind"] = kind
            return deployment
    return None


def _deployment_org_id(deployment: dict[str, Any]) -> str:
    owner = deployment.get("owner") or {}
    if owner.get("org_id"):
        return owner["org_id"]
    # TopologyCustomerOffice has no owner of its own — it groups under
    # TopologyOfficeCustomer (deployment.parent), which carries owner.
    parent_owner = ((deployment.get("parent") or {}).get("owner")) or {}
    return parent_owner.get("org_id", "")


def _deployment_parent_name(deployment: dict[str, Any]) -> str:
    parent = deployment.get("parent") or {}
    return parent.get("name", "")


# ---------------------------------------------------------------------------
# Topology (customer deployment) manifest builders
# ---------------------------------------------------------------------------


def _topology_ansible(deployment: dict[str, Any]) -> str:
    group_name = deployment.get("name", "unnamed").lower().replace("-", "_")
    lines = [
        f"{group_name}:",
        "  hosts: {}",
        "  vars:",
        f"    infrahub_id: {deployment.get('id', '')}",
        f"    infrahub_kind: {deployment.get('kind', '')}",
        f"    owner_org_id: {_deployment_org_id(deployment)}",
        f"    environment: {deployment.get('environment', '')}",
        f"    parent: {_deployment_parent_name(deployment)}",
    ]
    return "\n".join(lines) + "\n"


def _topology_terraform(deployment: dict[str, Any]) -> str:
    lines = [
        f'infrahub_id       = "{deployment.get("id", "")}"',
        f'infrahub_kind     = "{deployment.get("kind", "")}"',
        f'owner_org_id      = "{_deployment_org_id(deployment)}"',
        f'environment       = "{deployment.get("environment", "")}"',
        f'parent            = "{_deployment_parent_name(deployment)}"',
    ]
    return "\n".join(lines) + "\n"


def _topology_argocd(deployment: dict[str, Any]) -> str:
    name = deployment.get("name", "unnamed").lower().replace("_", "-")
    manifest = {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "Application",
        "metadata": {
            "name": name,
            "labels": {
                "infrahub-id": deployment.get("id", ""),
                "infrahub-kind": deployment.get("kind", ""),
                "owner-org-id": _deployment_org_id(deployment),
                "environment": deployment.get("environment", ""),
            },
        },
        "spec": {
            "project": "default",
            "source": {
                "repoURL": "",
                "path": f"deployments/{name}",
                "targetRevision": "HEAD",
            },
            "destination": {
                "server": "https://kubernetes.default.svc",
                "namespace": name,
            },
        },
    }
    return json.dumps(manifest, indent=2)


def _topology_github_actions(deployment: dict[str, Any]) -> str:
    payload = {
        "ref": "main",
        "inputs": {
            "infrahub_id": deployment.get("id", ""),
            "infrahub_kind": deployment.get("kind", ""),
            "owner_org_id": _deployment_org_id(deployment),
            "environment": deployment.get("environment", ""),
            "parent": _deployment_parent_name(deployment),
        },
    }
    return json.dumps(payload, indent=2)


_TOPOLOGY_BUILDERS = {
    "ansible_automation_platform": _topology_ansible,
    "terraform": _topology_terraform,
    "argocd": _topology_argocd,
    "github_actions": _topology_github_actions,
}


class _TopologyOrchestratorTransform(InfrahubTransform):
    """Base for the four topology_orchestrator_* transforms — subclasses set orchestrator."""

    query = "topology_orchestrator_routing_data"
    orchestrator = ""

    async def transform(self, data: Any) -> str:
        cleaned = clean_data(data)
        deployment = _first_topology_deployment(cleaned)
        if deployment is None:
            return ""
        if deployment.get("infrastructure_orchestrator") != self.orchestrator:
            return ""
        return _TOPOLOGY_BUILDERS[self.orchestrator](deployment)


class TopologyOrchestratorAnsible(_TopologyOrchestratorTransform):
    orchestrator = "ansible_automation_platform"


class TopologyOrchestratorTerraform(_TopologyOrchestratorTransform):
    orchestrator = "terraform"


class TopologyOrchestratorArgoCD(_TopologyOrchestratorTransform):
    orchestrator = "argocd"


class TopologyOrchestratorGitHubActions(_TopologyOrchestratorTransform):
    orchestrator = "github_actions"


# ---------------------------------------------------------------------------
# Application manifest builders
# ---------------------------------------------------------------------------


def _application_org_id(application: dict[str, Any]) -> str:
    owner = application.get("owner") or {}
    return owner.get("org_id", "")


def _application_ansible(application: dict[str, Any]) -> str:
    group_name = application.get("name", "unnamed").lower().replace("-", "_")
    lines = [
        f"{group_name}:",
        "  hosts: {}",
        "  vars:",
        f"    infrahub_id: {application.get('id', '')}",
        f"    label: {application.get('label', '')}",
        f"    owner_org_id: {_application_org_id(application)}",
        f"    environment: {application.get('environment', '')}",
        f"    criticality: {application.get('criticality', '')}",
        f"    fqdn: {application.get('fqdn', '')}",
    ]
    return "\n".join(lines) + "\n"


def _application_argocd(application: dict[str, Any]) -> str:
    name = application.get("name", "unnamed").lower().replace("_", "-")
    manifest = {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "Application",
        "metadata": {
            "name": name,
            "labels": {
                "infrahub-id": application.get("id", ""),
                "owner-org-id": _application_org_id(application),
                "environment": application.get("environment", ""),
                "criticality": application.get("criticality", ""),
            },
        },
        "spec": {
            "project": "default",
            "source": {
                "repoURL": "",
                "path": f"applications/{name}",
                "targetRevision": "HEAD",
            },
            "destination": {
                "server": "https://kubernetes.default.svc",
                "namespace": name,
            },
        },
    }
    return json.dumps(manifest, indent=2)


def _application_github_actions(application: dict[str, Any]) -> str:
    payload = {
        "ref": "main",
        "inputs": {
            "infrahub_id": application.get("id", ""),
            "label": application.get("label", ""),
            "owner_org_id": _application_org_id(application),
            "environment": application.get("environment", ""),
            "fqdn": application.get("fqdn", ""),
        },
    }
    return json.dumps(payload, indent=2)


_APPLICATION_BUILDERS = {
    "ansible_automation_platform": _application_ansible,
    "argocd": _application_argocd,
    "github_actions": _application_github_actions,
}


class _ApplicationOrchestratorTransform(InfrahubTransform):
    """Base for the three application_orchestrator_* transforms — subclasses set orchestrator."""

    query = "application_orchestrator_routing_data"
    orchestrator = ""

    async def transform(self, data: Any) -> str:
        cleaned = clean_data(data)
        applications = cleaned.get("AppApplication") or []
        if not applications:
            return ""
        application = applications[0]
        if application.get("application_orchestrator") != self.orchestrator:
            return ""
        return _APPLICATION_BUILDERS[self.orchestrator](application)


class ApplicationOrchestratorAnsible(_ApplicationOrchestratorTransform):
    orchestrator = "ansible_automation_platform"


class ApplicationOrchestratorArgoCD(_ApplicationOrchestratorTransform):
    orchestrator = "argocd"


class ApplicationOrchestratorGitHubActions(_ApplicationOrchestratorTransform):
    orchestrator = "github_actions"
