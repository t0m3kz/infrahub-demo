"""Unit tests for the parent-to-child generator fan-out mechanism.

Covers:
- CommonGenerator.run_generator_and_wait() — resolves the generator definition,
  calls CoreGeneratorDefinitionRun with wait_until_completion, no-ops on empty input
- DCTopologyGenerator: fans out to add_pod for every existing pod
- PodTopologyGenerator._fan_out_to_racks(): protects all racks, fans out to add_rack
  for the racks this pod is directly responsible for (network-only under mixed)
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from generators.common import CommonGenerator
from generators.models import PodDesign, PodModel, PodParent, Template
from generators.topology.pod import PodTopologyGenerator


def _build_common_gen() -> Any:
    """Return a CommonGenerator typed as Any so ty allows mock attribute assignments."""
    gen = CommonGenerator.__new__(CommonGenerator)
    gen.logger = MagicMock()
    gen.client = MagicMock()
    gen.branch = "test-branch"
    return gen


class TestRunGeneratorAndWait:
    @pytest.mark.asyncio
    async def test_empty_node_ids_is_a_noop(self) -> None:
        gen = _build_common_gen()
        gen.client.get = AsyncMock()
        gen.client.execute_graphql = AsyncMock()

        await gen.run_generator_and_wait("add_pod", [])

        gen.client.get.assert_not_awaited()
        gen.client.execute_graphql.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_resolves_definition_and_calls_mutation_with_wait(self) -> None:
        gen = _build_common_gen()
        definition = MagicMock(id="def-123")
        gen.client.get = AsyncMock(return_value=definition)
        gen.client.execute_graphql = AsyncMock(return_value={"CoreGeneratorDefinitionRun": {"ok": True}})

        await gen.run_generator_and_wait("add_rack", ["rack-1", "rack-2"])

        gen.client.get.assert_awaited_once()
        get_kwargs = gen.client.get.call_args.kwargs
        assert get_kwargs["name__value"] == "add_rack"

        gen.client.execute_graphql.assert_awaited_once()
        exec_kwargs = gen.client.execute_graphql.call_args.kwargs
        assert exec_kwargs["variables"] == {"id": "def-123", "nodes": ["rack-1", "rack-2"]}
        assert "wait_until_completion" in exec_kwargs["query"]
        assert exec_kwargs["branch_name"] == "test-branch"

    @pytest.mark.asyncio
    async def test_logs_error_when_mutation_reports_not_ok(self) -> None:
        gen = _build_common_gen()
        gen.client.get = AsyncMock(return_value=MagicMock(id="def-123"))
        gen.client.execute_graphql = AsyncMock(return_value={"CoreGeneratorDefinitionRun": {"ok": False}})

        await gen.run_generator_and_wait("add_pod", ["pod-1"])

        gen.logger.error.assert_called_once()


def _mock_rack(name: str, rack_type: str) -> MagicMock:
    r = MagicMock()
    r.id = f"id-{name}"
    r.name = MagicMock(value=name)
    r.rack_type = MagicMock(value=rack_type)
    return r


def _build_pod_gen(*, deployment_type: str) -> Any:
    """Return a PodTopologyGenerator typed as Any so ty allows mock attribute assignments."""
    design = PodDesign(
        id="design-1",
        name="test-design",
        rows=1,
        compute_racks_per_row=1,
        network_racks_per_row=0 if deployment_type == "tor" else 1,
        max_tors_per_compute_rack=0 if deployment_type == "middle_rack" else 1,
    )
    parent = PodParent(id="dc-1", name="DC1", index=1, devices=[])
    pod = PodModel(
        id="pod-1",
        name="pod-1",
        index=1,
        parent=parent,
        spine_template=Template(id="tmpl-spine"),
        design=design,
    )
    gen = PodTopologyGenerator.__new__(PodTopologyGenerator)
    gen.data = pod
    gen.logger = MagicMock()
    gen.client = MagicMock()
    gen.client.group_context = MagicMock()
    gen.client.group_context.related_node_ids = []
    gen.run_generator_and_wait = AsyncMock()
    return gen


class TestFanOutToRacks:
    """PodTopologyGenerator._fan_out_to_racks(): protect every rack from the
    tracking group's delete-unused-nodes cleanup, then fan out to add_rack only
    for the racks this pod is directly responsible for starting."""

    @pytest.mark.asyncio
    async def test_middle_rack_fans_out_to_every_rack(self) -> None:
        gen = _build_pod_gen(deployment_type="middle_rack")
        network_rack = _mock_rack("NET-1", "network")
        gen.client.filters = AsyncMock(return_value=[network_rack])

        await gen._fan_out_to_racks()

        gen.run_generator_and_wait.assert_awaited_once_with("add_rack", [network_rack.id])
        assert network_rack.id in gen.client.group_context.related_node_ids

    @pytest.mark.asyncio
    async def test_tor_fans_out_to_every_rack(self) -> None:
        gen = _build_pod_gen(deployment_type="tor")
        tor_rack = _mock_rack("TOR-1", "tor")
        gen.client.filters = AsyncMock(return_value=[tor_rack])

        await gen._fan_out_to_racks()

        gen.run_generator_and_wait.assert_awaited_once_with("add_rack", [tor_rack.id])

    @pytest.mark.asyncio
    async def test_mixed_fans_out_to_network_racks_only(self) -> None:
        """mixed's tor/compute racks are started by their network rack's own
        _fan_out_to_row_dependent_racks(), not by the pod directly — but they're
        still protected from the tracking group's cleanup here."""
        gen = _build_pod_gen(deployment_type="mixed")
        network_rack = _mock_rack("NET-1", "network")
        tor_rack = _mock_rack("TOR-1", "tor")
        compute_rack = _mock_rack("COMP-1", "compute")
        gen.client.filters = AsyncMock(return_value=[network_rack, tor_rack, compute_rack])

        await gen._fan_out_to_racks()

        gen.run_generator_and_wait.assert_awaited_once_with("add_rack", [network_rack.id])
        related = gen.client.group_context.related_node_ids
        assert network_rack.id in related
        assert tor_rack.id in related
        assert compute_rack.id in related

    @pytest.mark.asyncio
    async def test_no_racks_calls_with_empty_list(self) -> None:
        gen = _build_pod_gen(deployment_type="mixed")
        gen.client.filters = AsyncMock(return_value=[])

        await gen._fan_out_to_racks()

        gen.run_generator_and_wait.assert_awaited_once_with("add_rack", [])
