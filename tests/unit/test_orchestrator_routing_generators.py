from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from generators.topology.orchestrator_routing import (
    ApplicationOrchestratorRoutingGenerator,
    TopologyOrchestratorRoutingGenerator,
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
                    "name": "C003-VAULTEX-AWS-P-EU-CENTRAL-1",
                    "infrastructure_orchestrator": "terraform",
                    "member_of_groups": [_group("g-base", "customer_deployments")],
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
                        _group("g-base", "customer_deployments"),
                        _group("g-topo-argo", "topology_argocd"),
                    ],
                }
            ]
        }

        asyncio.run(gen.generate(payload))

        gen.client.create.assert_not_called()
