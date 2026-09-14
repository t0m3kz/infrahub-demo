"""Unit tests for transforms/topology/orchestrator_routing.py.

Covers:
  - Topology transforms (ansible/terraform/argocd/github_actions) — render
    only when infrastructure_orchestrator matches the transform's own
    orchestrator, else empty string.
  - Application transforms (ansible/argocd/github_actions) — same gating on
    application_orchestrator.
  - No matching deployment/application in the query result → empty string.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, TypeVar

from infrahub_sdk.transforms import InfrahubTransform

from transforms.topology.orchestrator_routing import (
    ApplicationOrchestratorAnsible,
    ApplicationOrchestratorArgoCD,
    ApplicationOrchestratorGitHubActions,
    TopologyOrchestratorAnsible,
    TopologyOrchestratorArgoCD,
    TopologyOrchestratorGitHubActions,
    TopologyOrchestratorTerraform,
)

_T = TypeVar("_T", bound=InfrahubTransform)


def _make(cls: type[_T]) -> _T:
    return cls.__new__(cls)


def _raw_deployment(
    kind: str,
    name: str,
    orchestrator: str,
    org_id: str = "C001",
    environment: str = "p",
    parent_name: str = "FR",
) -> dict[str, Any]:
    return {
        kind: {
            "edges": [
                {
                    "node": {
                        "id": f"id-{name}",
                        "name": {"value": name},
                        "environment": {"value": environment},
                        "infrastructure_orchestrator": {"value": orchestrator},
                        "owner": {"node": {"org_id": {"value": org_id}}},
                        "parent": {"node": {"name": {"value": parent_name}}},
                    }
                }
            ]
        }
    }


def _raw_application(
    name: str,
    orchestrator: str,
    org_id: str = "C005",
    label: str = "payment-core",
    environment: str = "p",
    criticality: str = "critical",
    fqdn: str = "payment-core.c005.demo.local",
) -> dict[str, Any]:
    return {
        "AppApplication": {
            "edges": [
                {
                    "node": {
                        "id": f"id-{name}",
                        "name": {"value": name},
                        "label": {"value": label},
                        "environment": {"value": environment},
                        "criticality": {"value": criticality},
                        "fqdn": {"value": fqdn},
                        "application_orchestrator": {"value": orchestrator},
                        "owner": {"node": {"org_id": {"value": org_id}}},
                    }
                }
            ]
        }
    }


class TestTopologyOrchestratorAnsible:
    def test_renders_inventory_for_matching_orchestrator(self) -> None:
        data = _raw_deployment(
            "TopologyCustomerColocation", "C011-P-NY", "ansible_automation_platform", org_id="C011", parent_name="NY"
        )
        result = asyncio.run(_make(TopologyOrchestratorAnsible).transform(data))

        assert "c011_p_ny:" in result
        assert "owner_org_id: C011" in result
        assert "parent: NY" in result

    def test_empty_when_orchestrator_does_not_match(self) -> None:
        data = _raw_deployment("TopologyCustomerColocation", "C001-P-FR", "manual")
        result = asyncio.run(_make(TopologyOrchestratorAnsible).transform(data))

        assert result == ""

    def test_empty_when_no_deployment_in_result(self) -> None:
        result = asyncio.run(_make(TopologyOrchestratorAnsible).transform({"TopologyCustomerDC": {"edges": []}}))

        assert result == ""


class TestTopologyOrchestratorTerraform:
    def test_renders_tfvars_for_matching_orchestrator(self) -> None:
        data = _raw_deployment("TopologyCustomerColocation", "C001-P-FR", "terraform", org_id="C001")
        result = asyncio.run(_make(TopologyOrchestratorTerraform).transform(data))

        assert 'owner_org_id      = "C001"' in result
        assert 'parent            = "FR"' in result


class TestTopologyOrchestratorArgoCD:
    def test_renders_application_manifest(self) -> None:
        data = _raw_deployment("TopologyCustomerColocation", "C002-P-PA", "argocd", org_id="C002")
        payload = json.loads(asyncio.run(_make(TopologyOrchestratorArgoCD).transform(data)))

        assert payload["kind"] == "Application"
        assert payload["metadata"]["name"] == "c002-p-pa"
        assert payload["metadata"]["labels"]["owner-org-id"] == "C002"


class TestTopologyOrchestratorGitHubActions:
    def test_renders_dispatch_payload(self) -> None:
        data = _raw_deployment("TopologyCustomerColocation", "C012-P-VA", "github_actions", org_id="C012")
        payload = json.loads(asyncio.run(_make(TopologyOrchestratorGitHubActions).transform(data)))

        assert payload["inputs"]["owner_org_id"] == "C012"


class TestApplicationOrchestratorAnsible:
    def test_renders_inventory_for_matching_orchestrator(self) -> None:
        data = _raw_application("c005-payment-core-p", "ansible_automation_platform")
        result = asyncio.run(_make(ApplicationOrchestratorAnsible).transform(data))

        assert "c005_payment_core_p:" in result
        assert "owner_org_id: C005" in result
        assert "criticality: critical" in result

    def test_empty_when_orchestrator_does_not_match(self) -> None:
        data = _raw_application("c012-payment-edge-p", "manual")
        result = asyncio.run(_make(ApplicationOrchestratorAnsible).transform(data))

        assert result == ""

    def test_empty_when_no_application_in_result(self) -> None:
        result = asyncio.run(_make(ApplicationOrchestratorAnsible).transform({"AppApplication": {"edges": []}}))

        assert result == ""


class TestApplicationOrchestratorArgoCD:
    def test_renders_application_manifest(self) -> None:
        data = _raw_application("c003-custody-api-p", "argocd", org_id="C003", criticality="high")
        payload = json.loads(asyncio.run(_make(ApplicationOrchestratorArgoCD).transform(data)))

        assert payload["metadata"]["name"] == "c003-custody-api-p"
        assert payload["metadata"]["labels"]["owner-org-id"] == "C003"
        assert payload["metadata"]["labels"]["criticality"] == "high"


class TestApplicationOrchestratorGitHubActions:
    def test_renders_dispatch_payload(self) -> None:
        data = _raw_application("c006-erp-core-p", "github_actions", org_id="C006", label="erp-core")
        payload = json.loads(asyncio.run(_make(ApplicationOrchestratorGitHubActions).transform(data)))

        assert payload["inputs"]["owner_org_id"] == "C006"
        assert payload["inputs"]["label"] == "erp-core"
