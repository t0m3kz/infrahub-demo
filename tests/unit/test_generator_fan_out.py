"""Unit tests for the parent-to-child generator fan-out mechanism.

Covers:
- CommonGenerator.run_generator() — resolves the generator definition, calls
  CoreGeneratorDefinitionRun with wait_until_completion true/false depending on
  the wait= kwarg (default True), no-ops on empty input
- CommonGenerator.wait_for_parent_generator_and_refetch() — waits on an in-flight
  parent bootstrap task and re-collects data, or no-ops when nothing is in flight
- CommonGenerator._get_parent_pool_with_retry() — retries a CoreIPPrefixPool
  lookup that races the parent generator's own pool creation (e.g. add_pod
  reading add_dc's DC-level technical pool before it's committed)
- DCPodCascadeGenerator: subclasses DCTopologyGenerator's generate() and, after
  it runs (mocked here), fans out (fire-and-forget) to pod_rack_cascade for
  every existing pod
- PodRackCascadeGenerator: subclasses PodTopologyGenerator's generate() and,
  after it runs (mocked here), fans out (fire-and-forget) to add_rack for the
  racks a pod is directly responsible for (network-only under mixed)
"""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from infrahub_sdk.exceptions import NodeNotFoundError
from infrahub_sdk.protocols import CoreIPPrefixPool, CoreNumberPool
from infrahub_sdk.task.models import TaskState

from generators.common import CommonGenerator
from generators.pools import PoolMixin
from generators.topology.dc import DCTopologyGenerator
from generators.topology.dc_pod_cascade import DCPodCascadeGenerator
from generators.topology.pod_rack_cascade import PodRackCascadeGenerator
from tests.unit.simulators.fabric_designs import DESIGN_FOR_DEPLOYMENT_TYPE, pod_design


class _CommonGen(PoolMixin, CommonGenerator):
    pass


def _build_common_gen() -> Any:
    """Return a PoolMixin+CommonGenerator instance typed as Any so ty allows mock attribute assignments."""
    gen = _CommonGen.__new__(_CommonGen)
    gen.logger = MagicMock()
    gen.client = MagicMock()
    gen.branch = "test-branch"
    return gen


class TestRunGenerator:
    @pytest.mark.asyncio
    async def test_empty_node_ids_is_a_noop(self) -> None:
        gen = _build_common_gen()
        gen.client.get = AsyncMock()
        gen.client.execute_graphql = AsyncMock()

        await gen.run_generator("add_pod", [])

        gen.client.get.assert_not_awaited()
        gen.client.execute_graphql.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_defaults_to_waiting(self) -> None:
        gen = _build_common_gen()
        definition = MagicMock(id="def-123")
        gen.client.get = AsyncMock(return_value=definition)
        gen.client.execute_graphql = AsyncMock(
            return_value={"CoreGeneratorDefinitionRun": {"ok": True, "task": {"id": "task-1"}}}
        )

        await gen.run_generator("add_rack", ["rack-1", "rack-2"])

        gen.client.get.assert_awaited_once()
        get_kwargs = gen.client.get.call_args.kwargs
        assert get_kwargs["name__value"] == "add_rack"

        exec_kwargs = gen.client.execute_graphql.call_args.kwargs
        assert exec_kwargs["variables"] == {"id": "def-123", "nodes": ["rack-1", "rack-2"]}
        assert "wait_until_completion: true" in exec_kwargs["query"]
        assert exec_kwargs["branch_name"] == "test-branch"

    @pytest.mark.asyncio
    async def test_wait_false_does_not_block(self) -> None:
        gen = _build_common_gen()
        definition = MagicMock(id="def-123")
        gen.client.get = AsyncMock(return_value=definition)
        gen.client.execute_graphql = AsyncMock(
            return_value={"CoreGeneratorDefinitionRun": {"ok": True, "task": {"id": "task-1"}}}
        )

        await gen.run_generator("add_rack", ["rack-1"], wait=False)

        exec_kwargs = gen.client.execute_graphql.call_args.kwargs
        assert "wait_until_completion: false" in exec_kwargs["query"]

    @pytest.mark.asyncio
    async def test_logs_error_when_mutation_reports_not_ok(self) -> None:
        gen = _build_common_gen()
        gen.client.get = AsyncMock(return_value=MagicMock(id="def-123"))
        gen.client.execute_graphql = AsyncMock(
            return_value={"CoreGeneratorDefinitionRun": {"ok": False, "task": {"id": None}}}
        )

        await gen.run_generator("add_pod", ["pod-1"])

        gen.logger.error.assert_called_once()


def _mock_task(*, id: str, title: str, state: TaskState = TaskState.RUNNING, updated_at: str = "") -> MagicMock:
    t = MagicMock()
    t.id = id
    t.title = title
    t.state = state
    t.updated_at = updated_at
    return t


class TestWaitForParentGeneratorAndRefetch:
    @pytest.mark.asyncio
    async def test_no_in_flight_task_returns_none(self) -> None:
        gen = _build_common_gen()
        gen.client.task = MagicMock()
        gen.client.task.filter = AsyncMock(return_value=[])

        result = await gen.wait_for_parent_generator_and_refetch("add_dc", "dc-1")

        assert result is None
        # One query covers both the in-flight and the failed-last-run checks.
        assert gen.client.task.filter.await_count == 1

    @pytest.mark.asyncio
    async def test_unrelated_task_title_returns_none(self) -> None:
        gen = _build_common_gen()
        gen.client.task = MagicMock()
        gen.client.task.filter = AsyncMock(return_value=[_mock_task(id="t-1", title="Run generator add_rack")])

        result = await gen.wait_for_parent_generator_and_refetch("add_dc", "dc-1")

        assert result is None

    @pytest.mark.asyncio
    async def test_matching_task_waits_and_refetches(self) -> None:
        gen = _build_common_gen()
        gen.client.task = MagicMock()
        gen.client.task.filter = AsyncMock(return_value=[_mock_task(id="t-1", title="Run generator add_dc")])
        gen.client.task.wait_for_completion = AsyncMock()
        gen.collect_data = AsyncMock(return_value={"refetched": True})

        result = await gen.wait_for_parent_generator_and_refetch("add_dc", "dc-1")

        gen.client.task.wait_for_completion.assert_awaited_once()
        assert gen.client.task.wait_for_completion.call_args.kwargs["id"] == "t-1"
        gen.collect_data.assert_awaited_once()
        assert result == {"refetched": True}

    @pytest.mark.asyncio
    async def test_several_names_share_one_query_and_one_refetch(self) -> None:
        """Every in-flight parent is waited on, then data is re-collected once."""
        gen = _build_common_gen()
        gen.client.task = MagicMock()
        gen.client.task.filter = AsyncMock(
            return_value=[
                _mock_task(id="t-dc", title="Run generator add_dc"),
                _mock_task(id="t-cascade", title="Run generator dc_pod_cascade"),
            ]
        )
        gen.client.task.wait_for_completion = AsyncMock()
        gen.collect_data = AsyncMock(return_value={"refetched": True})

        result = await gen.wait_for_parent_generator_and_refetch(("add_dc", "dc_pod_cascade"), "dc-1")

        assert result == {"refetched": True}
        gen.client.task.filter.assert_awaited_once()
        waited = [c.kwargs["id"] for c in gen.client.task.wait_for_completion.await_args_list]
        assert waited == ["t-dc", "t-cascade"]
        gen.collect_data.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_finished_task_is_not_waited_on(self) -> None:
        """A completed parent run is history, not something to wait for."""
        gen = _build_common_gen()
        gen.client.task = MagicMock()
        gen.client.task.filter = AsyncMock(
            return_value=[_mock_task(id="t-1", title="Run generator add_dc", state=TaskState.COMPLETED)]
        )
        gen.client.task.wait_for_completion = AsyncMock()

        assert await gen.wait_for_parent_generator_and_refetch("add_dc", "dc-1") is None
        gen.client.task.wait_for_completion.assert_not_awaited()
        gen.logger.error.assert_not_called()

    @pytest.mark.asyncio
    async def test_failed_latest_run_logs_error(self) -> None:
        """A parent whose latest run failed (newer than its last success) is reported."""
        gen = _build_common_gen()
        gen.client.task = MagicMock()
        gen.client.task.filter = AsyncMock(
            return_value=[
                _mock_task(id="t-1", title="Run generator add_dc", state=TaskState.COMPLETED, updated_at="2026-01-01"),
                _mock_task(id="t-2", title="Run generator add_dc", state=TaskState.FAILED, updated_at="2026-01-02"),
            ]
        )

        assert await gen.wait_for_parent_generator_and_refetch("add_dc", "dc-1") is None
        gen.logger.error.assert_called_once()

    @pytest.mark.asyncio
    async def test_failure_followed_by_success_is_not_an_error(self) -> None:
        """A failure a later successful run superseded does not block the child."""
        gen = _build_common_gen()
        gen.client.task = MagicMock()
        gen.client.task.filter = AsyncMock(
            return_value=[
                _mock_task(id="t-1", title="Run generator add_dc", state=TaskState.FAILED, updated_at="2026-01-01"),
                _mock_task(id="t-2", title="Run generator add_dc", state=TaskState.COMPLETED, updated_at="2026-01-02"),
            ]
        )

        assert await gen.wait_for_parent_generator_and_refetch("add_dc", "dc-1") is None
        gen.logger.error.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("state", "waited"), [(TaskState.RUNNING, True), (TaskState.COMPLETED, False)])
    async def test_wait_only_reports_whether_it_waited_and_never_refetches(
        self, state: TaskState, waited: bool
    ) -> None:
        """wait_for_parent_generators is the wait without the full re-collect, for a caller re-reading less."""
        gen = _build_common_gen()
        gen.client.task = MagicMock()
        gen.client.task.filter = AsyncMock(
            return_value=[_mock_task(id="t-1", title="Run generator add_dc", state=state)]
        )
        gen.client.task.wait_for_completion = AsyncMock()
        gen.collect_data = AsyncMock()

        assert await gen.wait_for_parent_generators(("add_dc",), "dc-1") is waited
        assert gen.client.task.wait_for_completion.await_count == int(waited)
        gen.collect_data.assert_not_awaited()


class TestGetParentPoolWithRetry:
    @pytest.mark.asyncio
    async def test_pool_found_immediately_returns_it(self) -> None:
        gen = _build_common_gen()
        pool = MagicMock()
        gen.client.get = AsyncMock(return_value=pool)

        result = await gen._get_parent_pool_with_retry("dc5-technical-pool")

        assert result is pool
        gen.client.get.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_pool_appears_after_retry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """add_dc's own allocate_resource_pools() call hasn't committed the DC-level
        pool yet on the first lookup — retries until it has."""
        gen = _build_common_gen()
        pool = MagicMock()
        gen.client.get = AsyncMock(
            side_effect=[
                NodeNotFoundError(identifier={"name__value": ["dc5-technical-pool"]}),
                pool,
            ]
        )
        sleep_mock = AsyncMock()
        monkeypatch.setattr("generators.pools.asyncio.sleep", sleep_mock)

        result = await gen._get_parent_pool_with_retry("dc5-technical-pool")

        assert result is pool
        sleep_mock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_pool_never_appears_raises_after_exhausting_retries(self, monkeypatch: pytest.MonkeyPatch) -> None:
        gen = _build_common_gen()
        gen.client.get = AsyncMock(side_effect=NodeNotFoundError(identifier={"name__value": ["dc5-technical-pool"]}))
        monkeypatch.setattr("generators.pools.asyncio.sleep", AsyncMock())

        with pytest.raises(NodeNotFoundError):
            await gen._get_parent_pool_with_retry("dc5-technical-pool")

        assert gen.client.get.await_count == 10


class TestGetParentPoolWithRetryKind:
    """The retry is not tied to prefix pools: a zone waits for its metro's ASN pool (a CoreNumberPool)."""

    @pytest.mark.asyncio
    async def test_default_kind_is_the_ip_prefix_pool(self) -> None:
        gen = _build_common_gen()
        gen.client.get = AsyncMock(return_value=MagicMock())

        await gen._get_parent_pool_with_retry("dc5-technical-pool")

        assert gen.client.get.await_args_list[-1].kwargs["kind"] is CoreIPPrefixPool

    @pytest.mark.asyncio
    async def test_kind_is_passed_to_the_lookup(self) -> None:
        gen = _build_common_gen()
        pool = MagicMock()
        gen.client.get = AsyncMock(return_value=pool)

        result = await gen._get_parent_pool_with_retry("fr-asn-pool", kind=CoreNumberPool)

        assert result is pool
        gen.client.get.assert_awaited_once_with(kind=CoreNumberPool, name__value="fr-asn-pool")

    @pytest.mark.asyncio
    async def test_number_pool_that_appears_after_the_metro_generator_finishes_is_found(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """add_colocation_metro has not attached its ASN pool on the first lookup; the second finds it."""
        gen = _build_common_gen()
        pool = MagicMock()
        gen.client.get = AsyncMock(side_effect=[NodeNotFoundError(identifier={"name__value": ["fr-asn-pool"]}), pool])
        sleep_mock = AsyncMock()
        monkeypatch.setattr("generators.pools.asyncio.sleep", sleep_mock)

        result = await gen._get_parent_pool_with_retry("fr-asn-pool", kind=CoreNumberPool)

        assert result is pool
        sleep_mock.assert_awaited_once()
        assert {call.kwargs["kind"] for call in gen.client.get.await_args_list} == {CoreNumberPool}

    @pytest.mark.asyncio
    async def test_number_pool_that_never_appears_raises_not_found_after_ten_attempts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The caller (the zone generator) turns the final NodeNotFoundError into a failed run."""
        gen = _build_common_gen()
        gen.client.get = AsyncMock(side_effect=NodeNotFoundError(identifier={"name__value": ["fr-asn-pool"]}))
        monkeypatch.setattr("generators.pools.asyncio.sleep", AsyncMock())

        with pytest.raises(NodeNotFoundError):
            await gen._get_parent_pool_with_retry("fr-asn-pool", kind=CoreNumberPool)

        assert gen.client.get.await_count == 10


def _pod(name: str) -> dict[str, Any]:
    """A TopologyPod child as the DC query returns it after clean_data()."""
    return {"id": f"id-{name}", "name": name, "index": 1, "design": {"id": "pod-design"}}


def _build_dc_cascade_gen() -> Any:
    """Return a DCPodCascadeGenerator with its inherited bootstrap generate()
    replaced by a stub that just sets self.data, isolating the cascade logic
    added in DCPodCascadeGenerator.generate() from DCTopologyGenerator's own
    (extensively covered elsewhere) pool/device/routing work."""
    gen = cast(Any, DCPodCascadeGenerator.__new__(DCPodCascadeGenerator))
    gen.logger = MagicMock()
    gen.client = MagicMock()
    gen.client.group_context = MagicMock()
    gen.client.group_context.related_node_ids = []
    gen.run_generator = AsyncMock()

    async def _fake_bootstrap(data: dict[str, Any]) -> None:
        deployment_list = data.get("TopologyDeployment", [])
        if not deployment_list:
            gen.logger.error("No TopologyDeployment data found in GraphQL response")
            return
        gen.data = deployment_list[0]
        # Mirrors DCTopologyGenerator.generate(): the DC's pods ride its own query
        # (read, never saved); keep them for the cascade.
        gen._existing_pods = DCTopologyGenerator.pods_of(gen.data)

    gen._bootstrap_generate = _fake_bootstrap
    return gen


class TestDCPodCascadeGenerator:
    @pytest.mark.asyncio
    async def test_fans_out_to_every_existing_pod(self, monkeypatch: pytest.MonkeyPatch) -> None:
        gen = _build_dc_cascade_gen()
        monkeypatch.setattr(
            "generators.topology.dc.DCTopologyGenerator.generate",
            lambda self, data: gen._bootstrap_generate(data),
        )
        pod1, pod2 = _pod("POD-1"), _pod("POD-2")

        await gen.generate(
            {"TopologyDeployment": [{"id": "dc-1", "name": "DC1", "index": 1, "children": [pod1, "not-a-pod", pod2]}]}
        )

        gen.run_generator.assert_awaited_once_with("pod_rack_cascade", [pod1["id"], pod2["id"]], wait=False)
        assert gen.client.group_context.related_node_ids == []

    @pytest.mark.asyncio
    async def test_no_existing_pods_skips_fan_out(self, monkeypatch: pytest.MonkeyPatch) -> None:
        gen = _build_dc_cascade_gen()
        monkeypatch.setattr(
            "generators.topology.dc.DCTopologyGenerator.generate",
            lambda self, data: gen._bootstrap_generate(data),
        )

        await gen.generate({"TopologyDeployment": [{"id": "dc-1", "name": "DC1", "index": 1, "children": []}]})

        gen.run_generator.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_bootstrap_failure_skips_fan_out(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """If the inherited bootstrap bails out early (e.g. bad data), self.data
        never gets set — the cascade must not fan out on incomplete state."""
        gen = _build_dc_cascade_gen()
        monkeypatch.setattr(
            "generators.topology.dc.DCTopologyGenerator.generate",
            lambda self, data: gen._bootstrap_generate(data),
        )

        await gen.generate({"TopologyDeployment": []})

        gen.logger.error.assert_called_once()
        gen.run_generator.assert_not_awaited()


def _mock_rack(name: str, rack_type: str) -> MagicMock:
    r = MagicMock()
    r.id = f"id-{name}"
    r.name = MagicMock(value=name)
    r.rack_type = MagicMock(value=rack_type)
    return r


def _pod_data(*, deployment_type: str) -> dict[str, Any]:
    """deployment_type picks the real pod design the generator derives it from
    (max_leafs_per_network_rack=0 for tor, max_tors_per_compute_rack=0 for middle_rack)."""
    return {
        "TopologyPod": [
            {
                "id": "pod-1",
                "name": "pod-1",
                "index": 1,
                "design": pod_design(DESIGN_FOR_DEPLOYMENT_TYPE[deployment_type]),
                "fabric_templates": [{"role": "spine", "quantity": 2, "template": {"node": {"id": "tmpl-spine"}}}],
                "parent": {
                    "node": {
                        "id": "dc-1",
                        "name": "DC1",
                        "index": 1,
                        "devices": {"edges": []},
                    }
                },
            }
        ]
    }


def _build_pod_rack_cascade_gen() -> Any:
    """Return a PodRackCascadeGenerator with its inherited bootstrap generate()
    replaced by a stub that just sets self.data, isolating the cascade logic
    added in PodRackCascadeGenerator.generate() from PodTopologyGenerator's own
    (extensively covered elsewhere) pool/device/routing work."""
    gen = cast(Any, PodRackCascadeGenerator.__new__(PodRackCascadeGenerator))
    gen.logger = MagicMock()
    gen.client = MagicMock()
    gen.client.group_context = MagicMock()
    gen.client.group_context.related_node_ids = []
    gen.run_generator = AsyncMock()

    async def _fake_bootstrap(data: dict[str, Any]) -> None:
        from utils.data_cleaning import clean_data

        deployment_list = clean_data(data).get("TopologyPod", [])
        if not deployment_list:
            gen.logger.error("No Pod Deployment data found in GraphQL response")
            return
        gen.data = deployment_list[0]

    gen._bootstrap_generate = _fake_bootstrap
    return gen


class TestPodRackCascadeGenerator:
    """PodRackCascadeGenerator.generate(): read the pod's racks (data, never
    tracked), then fan out to add_rack only for the racks this pod is directly
    responsible for starting."""

    @pytest.mark.asyncio
    async def test_middle_rack_fans_out_to_every_rack(self, monkeypatch: pytest.MonkeyPatch) -> None:
        gen = _build_pod_rack_cascade_gen()
        monkeypatch.setattr(
            "generators.topology.pod.PodTopologyGenerator.generate",
            lambda self, data: gen._bootstrap_generate(data),
        )
        network_rack = _mock_rack("NET-1", "network")
        gen.client.filters = AsyncMock(return_value=[network_rack])

        await gen.generate(_pod_data(deployment_type="middle_rack"))

        gen.run_generator.assert_awaited_once_with("add_rack", [network_rack.id], wait=False)
        # Racks are data: tracked, a rack moved to another pod would be
        # deleted by this pod's next run.
        assert gen.client.group_context.related_node_ids == []

    @pytest.mark.asyncio
    async def test_tor_fans_out_to_every_rack(self, monkeypatch: pytest.MonkeyPatch) -> None:
        gen = _build_pod_rack_cascade_gen()
        monkeypatch.setattr(
            "generators.topology.pod.PodTopologyGenerator.generate",
            lambda self, data: gen._bootstrap_generate(data),
        )
        tor_rack = _mock_rack("TOR-1", "tor")
        gen.client.filters = AsyncMock(return_value=[tor_rack])

        await gen.generate(_pod_data(deployment_type="tor"))

        gen.run_generator.assert_awaited_once_with("add_rack", [tor_rack.id], wait=False)

    @pytest.mark.asyncio
    async def test_mixed_fans_out_to_network_racks_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """mixed's tor/compute racks are started by their network rack's own
        _fan_out_to_row_dependent_racks(), not by this cascade directly. No
        rack is tracked by this run."""
        gen = _build_pod_rack_cascade_gen()
        monkeypatch.setattr(
            "generators.topology.pod.PodTopologyGenerator.generate",
            lambda self, data: gen._bootstrap_generate(data),
        )
        network_rack = _mock_rack("NET-1", "network")
        tor_rack = _mock_rack("TOR-1", "tor")
        compute_rack = _mock_rack("COMP-1", "compute")
        gen.client.filters = AsyncMock(return_value=[network_rack, tor_rack, compute_rack])

        await gen.generate(_pod_data(deployment_type="mixed"))

        gen.run_generator.assert_awaited_once_with("add_rack", [network_rack.id], wait=False)
        assert gen.client.group_context.related_node_ids == []

    @pytest.mark.asyncio
    async def test_no_racks_skips_fan_out(self, monkeypatch: pytest.MonkeyPatch) -> None:
        gen = _build_pod_rack_cascade_gen()
        monkeypatch.setattr(
            "generators.topology.pod.PodTopologyGenerator.generate",
            lambda self, data: gen._bootstrap_generate(data),
        )
        gen.client.filters = AsyncMock(return_value=[])

        await gen.generate(_pod_data(deployment_type="mixed"))

        gen.run_generator.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_bootstrap_failure_skips_fan_out(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """If the inherited bootstrap bails out early (e.g. bad data), self.data
        never gets set — the cascade must not fan out on incomplete state."""
        gen = _build_pod_rack_cascade_gen()
        monkeypatch.setattr(
            "generators.topology.pod.PodTopologyGenerator.generate",
            lambda self, data: gen._bootstrap_generate(data),
        )
        gen.client.filters = AsyncMock(return_value=[])

        await gen.generate({"TopologyPod": []})

        gen.logger.error.assert_called_once()
        gen.client.filters.assert_not_awaited()
        gen.run_generator.assert_not_awaited()
