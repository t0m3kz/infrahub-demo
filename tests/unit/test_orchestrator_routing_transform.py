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

from tests.integration.demo_data import ROOT
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
    environment: str | None = "p",
    parent_name: str | None = "FR",
) -> dict[str, Any]:
    """A raw GraphQL response; `environment=None` / `parent_name=None` leave the field out like the query does
    for a cloud footprint (no environment) and an office (no environment, parent is only a label)."""
    node: dict[str, Any] = {
        "id": f"id-{name}",
        "name": {"value": name},
        "infrastructure_orchestrator": {"value": orchestrator},
        "owner": {"node": {"org_id": {"value": org_id}}},
    }
    if environment is not None:
        node["environment"] = {"value": environment}
    if parent_name is not None:
        node["parent"] = {"node": {"name": {"value": parent_name}}}
    return {kind: {"edges": [{"node": node}]}}


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


_BUILDERS = (
    (TopologyOrchestratorAnsible, "ansible_automation_platform"),
    (TopologyOrchestratorTerraform, "terraform"),
    (TopologyOrchestratorArgoCD, "argocd"),
    (TopologyOrchestratorGitHubActions, "github_actions"),
)


def _render(builder: type[InfrahubTransform], data: dict[str, Any]) -> str:
    return asyncio.run(_make(builder).transform(data))


def _environment_of(builder: type[InfrahubTransform], rendered: str) -> str | None:
    """The environment a rendered manifest carries, None when it carries none."""
    if builder is TopologyOrchestratorAnsible:
        lines = [line.strip() for line in rendered.splitlines() if line.strip().startswith("environment:")]
        return lines[0].split(": ", 1)[1] if lines else None
    if builder is TopologyOrchestratorTerraform:
        lines = [line for line in rendered.splitlines() if line.startswith("environment")]
        return lines[0].split('"')[1] if lines else None
    payload = json.loads(rendered)
    if builder is TopologyOrchestratorArgoCD:
        return payload["metadata"]["labels"].get("environment")
    return payload["inputs"].get("environment")


class TestOfficeAndCloudCarryNoEnvironmentAttribute:
    """An office is always production and a cloud footprint is not environment-scoped (its accounts and
    segments carry the environment): neither node has an `environment` field to read."""

    def test_an_office_is_rendered_as_environment_p_by_all_four_builders(self) -> None:
        """The transform supplies `p` for the office (_OFFICE_ENVIRONMENT); the node has no environment."""
        for builder, orchestrator in _BUILDERS:
            data = _raw_deployment(
                "TopologyCustomerOffice", "C005-OFFICE", orchestrator, org_id="C005", environment=None, parent_name=None
            )
            rendered = _render(builder, data)

            assert _environment_of(builder, rendered) == "p", builder.__name__
            assert "C005" in rendered, builder.__name__

    def test_an_office_has_an_empty_parent_name_not_its_label(self) -> None:
        """The office query does not select the parent (just the Campus and Office label): parent is empty."""
        data = _raw_deployment(
            "TopologyCustomerOffice",
            "C005-OFFICE",
            "terraform",
            org_id="C005",
            environment=None,
            parent_name=None,
        )

        assert 'parent            = ""' in _render(TopologyOrchestratorTerraform, data)

    def test_a_cloud_footprint_omits_the_environment_in_all_four_builders(self) -> None:
        """No `environment:` line, `environment = ` assignment, label or input at all (not an empty one)."""
        for builder, orchestrator in _BUILDERS:
            data = _raw_deployment(
                "TopologyCustomerCloud",
                "C003-VAULTEX-AWS-EU-CENTRAL-1",
                orchestrator,
                org_id="C003",
                environment=None,
                parent_name="eu-central-1",
            )
            rendered = _render(builder, data)

            assert rendered, builder.__name__
            assert _environment_of(builder, rendered) is None, builder.__name__
            assert "environment" not in rendered, builder.__name__

    def test_a_cloud_footprint_keeps_its_owner_and_parent(self) -> None:
        """Dropping the environment must not drop the rest of the cloud manifest."""
        data = _raw_deployment(
            "TopologyCustomerCloud",
            "C003-VAULTEX-AWS-EU-CENTRAL-1",
            "ansible_automation_platform",
            org_id="C003",
            environment=None,
            parent_name="eu-central-1",
        )
        result = _render(TopologyOrchestratorAnsible, data)

        assert "c003_vaultex_aws_eu_central_1:" in result
        assert "owner_org_id: C003" in result
        assert "parent: eu-central-1" in result

    def test_dc_and_colocation_footprints_still_carry_their_environment(self) -> None:
        """Only the environment-scoped kinds read `environment` from the node."""
        for kind in ("TopologyCustomerDC", "TopologyCustomerColocation"):
            for builder, orchestrator in _BUILDERS:
                data = _raw_deployment(kind, "C001-D-FR", orchestrator, environment="d")
                assert _environment_of(builder, _render(builder, data)) == "d", (kind, builder.__name__)


class TestOrchestratorQuerySelectsNoRemovedFields:
    """queries/topology/add/topology_orchestrator_routing_data.gql selects only fields the schema has."""

    def test_only_dc_and_colocation_select_environment_and_the_office_selects_its_owner_directly(self) -> None:
        query = (ROOT / "queries" / "topology" / "add" / "topology_orchestrator_routing_data.gql").read_text()
        blocks = {
            kind: query.split(f"  {kind}(name__value: $name) {{", 1)[1].split("\n  }\n", 1)[0]
            for kind in (
                "TopologyCustomerDC",
                "TopologyCustomerColocation",
                "TopologyCustomerOffice",
                "TopologyCustomerCloud",
            )
        }

        assert "environment { value }" in blocks["TopologyCustomerDC"]
        assert "environment { value }" in blocks["TopologyCustomerColocation"]
        assert "environment" not in blocks["TopologyCustomerOffice"]
        assert "environment" not in blocks["TopologyCustomerCloud"]
        assert "owner { node { org_id { value } } }" in blocks["TopologyCustomerOffice"]
        assert "OfficeCustomer" not in query


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
