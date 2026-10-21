from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from generators.topology.orchestrator_routing import (
    ApplicationOrchestratorRoutingGenerator,
    TopologyOrchestratorRoutingGenerator,
    VirtualColocationOrchestratorRoutingGenerator,
)


def _make_app_gen() -> Any:
    gen = ApplicationOrchestratorRoutingGenerator.__new__(ApplicationOrchestratorRoutingGenerator)
    gen.client = AsyncMock()
    gen.logger = MagicMock()
    return gen


def _make_topology_gen() -> Any:
    gen = TopologyOrchestratorRoutingGenerator.__new__(TopologyOrchestratorRoutingGenerator)
    gen.client = AsyncMock()
    gen.logger = MagicMock()
    return gen


def _make_virtual_colocation_gen() -> Any:
    gen = VirtualColocationOrchestratorRoutingGenerator.__new__(VirtualColocationOrchestratorRoutingGenerator)
    gen.client = AsyncMock()
    gen.logger = MagicMock()
    return gen


def _group(group_id: str, name: str) -> dict[str, str]:
    return {"id": group_id, "name": name}


def _saved_obj() -> MagicMock:
    obj = MagicMock()
    obj.save = AsyncMock()
    return obj


class TestApplicationOrchestratorRoutingGenerator:
    def test_assigns_github_actions_group(self) -> None:
        gen = _make_app_gen()
        app_group = MagicMock(id="g-app-gha")
        updated = _saved_obj()
        gen.client.get = AsyncMock(return_value=app_group)
        gen.client.create = AsyncMock(return_value=updated)

        payload = {
            "AppApplication": [
                {
                    "id": "app-1",
                    "typename": "AppApplication",
                    "name": "c005-payment-core-p",
                    "application_orchestrator": "github_actions",
                    "member_of_groups": [_group("g-base", "app_applications")],
                }
            ]
        }

        asyncio.run(gen.generate(payload))

        call = gen.client.create.call_args.kwargs
        assert call["kind"] == "AppApplication"
        assert call["data"]["member_of_groups"] == [{"id": "g-base"}, {"id": "g-app-gha"}]
        updated.save.assert_called_once()

    def test_manual_removes_routing_group(self) -> None:
        gen = _make_app_gen()
        updated = _saved_obj()
        gen.client.create = AsyncMock(return_value=updated)

        payload = {
            "AppApplication": [
                {
                    "id": "app-2",
                    "typename": "AppApplication",
                    "name": "c003-custody-api-p",
                    "application_orchestrator": "manual",
                    "member_of_groups": [
                        _group("g-base", "app_applications"),
                        _group("g-argo", "application_argocd"),
                    ],
                }
            ]
        }

        asyncio.run(gen.generate(payload))

        call = gen.client.create.call_args.kwargs
        assert call["data"]["member_of_groups"] == [{"id": "g-base"}]
        updated.save.assert_called_once()


class TestTopologyOrchestratorRoutingGenerator:
    def test_assigns_terraform_group_for_cloud(self) -> None:
        gen = _make_topology_gen()
        tf_group = MagicMock(id="g-topo-tf")
        updated = _saved_obj()
        gen.client.get = AsyncMock(return_value=tf_group)
        gen.client.create = AsyncMock(return_value=updated)

        payload = {
            "TopologyCustomerCloud": [
                {
                    "id": "dep-1",
                    "typename": "TopologyCustomerCloud",
                    "name": "C003-VAULTEX-AWS-EU-CENTRAL-1",
                    "infrastructure_orchestrator": "terraform",
                    "member_of_groups": [_group("g-base", "customer_deployments_cloud")],
                }
            ]
        }

        asyncio.run(gen.generate(payload))

        call = gen.client.create.call_args.kwargs
        assert call["kind"] == "TopologyCustomerCloud"
        assert call["data"]["member_of_groups"] == [{"id": "g-base"}, {"id": "g-topo-tf"}]
        updated.save.assert_called_once()

    def test_skips_save_when_membership_is_already_correct(self) -> None:
        gen = _make_topology_gen()
        argo_group = MagicMock(id="g-topo-argo")
        gen.client.get = AsyncMock(return_value=argo_group)
        gen.client.create = AsyncMock()

        payload = {
            "TopologyCustomerDC": [
                {
                    "id": "dep-2",
                    "typename": "TopologyCustomerDC",
                    "name": "C005-P-DC10",
                    "infrastructure_orchestrator": "argocd",
                    "member_of_groups": [
                        _group("g-base", "customer_deployments_dc"),
                        _group("g-topo-argo", "topology_argocd"),
                    ],
                }
            ]
        }

        asyncio.run(gen.generate(payload))

        gen.client.create.assert_not_called()

    def test_unsupported_customer_orchestrator_is_an_error_and_leaves_the_node_alone(self) -> None:
        gen = _make_topology_gen()
        gen.client.create = AsyncMock()

        payload = {
            "TopologyCustomerDC": [
                {
                    "id": "dep-3",
                    "typename": "TopologyCustomerDC",
                    "name": "C005-P-DC10",
                    "infrastructure_orchestrator": "pulumi",
                    "member_of_groups": [_group("g-base", "customer_deployments_dc")],
                }
            ]
        }

        asyncio.run(gen.generate(payload))

        gen.logger.error.assert_called_once()
        gen.client.create.assert_not_called()


def _virtual_colocation_payload(orchestrator: str, groups: list[dict[str, str]]) -> dict[str, Any]:
    return {
        "TopologyVirtualColocation": [
            {
                "id": "vc-1",
                "typename": "TopologyVirtualColocation",
                "name": "AM-NE",
                "infrastructure_orchestrator": orchestrator,
                "member_of_groups": groups,
            }
        ]
    }


class TestVirtualColocationOrchestratorRoutingGenerator:
    def test_terraform_joins_the_virtual_colocation_terraform_group(self) -> None:
        gen = _make_virtual_colocation_gen()
        tf_group = MagicMock(id="g-vc-tf")
        updated = _saved_obj()
        gen.client.get = AsyncMock(return_value=tf_group)
        gen.client.create = AsyncMock(return_value=updated)

        asyncio.run(gen.generate(_virtual_colocation_payload("terraform", [_group("g-base", "virtual_colocations")])))

        assert gen.client.get.call_args.kwargs["name__value"] == "virtual_colocation_terraform"
        call = gen.client.create.call_args.kwargs
        assert call["kind"] == "TopologyVirtualColocation"
        assert call["data"]["member_of_groups"] == [{"id": "g-base"}, {"id": "g-vc-tf"}]
        updated.save.assert_called_once()
        gen.logger.error.assert_not_called()

    def test_terraform_already_routed_is_not_saved_again(self) -> None:
        gen = _make_virtual_colocation_gen()
        gen.client.get = AsyncMock(return_value=MagicMock(id="g-vc-tf"))
        gen.client.create = AsyncMock()

        groups = [_group("g-base", "virtual_colocations"), _group("g-vc-tf", "virtual_colocation_terraform")]
        asyncio.run(gen.generate(_virtual_colocation_payload("terraform", groups)))

        gen.client.create.assert_not_called()

    @pytest.mark.parametrize("orchestrator", ["ansible_automation_platform", "github_actions", "argocd"])
    def test_orchestrator_without_an_artifact_warns_and_leaves_the_routing_group(self, orchestrator: str) -> None:
        gen = _make_virtual_colocation_gen()
        updated = _saved_obj()
        gen.client.create = AsyncMock(return_value=updated)

        groups = [_group("g-base", "virtual_colocations"), _group("g-vc-tf", "virtual_colocation_terraform")]
        asyncio.run(gen.generate(_virtual_colocation_payload(orchestrator, groups)))

        gen.logger.error.assert_not_called()
        gen.logger.warning.assert_called_once()
        assert gen.client.create.call_args.kwargs["data"]["member_of_groups"] == [{"id": "g-base"}]
        updated.save.assert_called_once()

    def test_manual_without_a_routing_group_changes_nothing_and_does_not_warn(self) -> None:
        gen = _make_virtual_colocation_gen()
        gen.client.create = AsyncMock()

        asyncio.run(gen.generate(_virtual_colocation_payload("manual", [_group("g-base", "virtual_colocations")])))

        gen.client.create.assert_not_called()
        gen.logger.error.assert_not_called()
        gen.logger.warning.assert_not_called()
