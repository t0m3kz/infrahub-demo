"""Unit tests for the VNI invariant of VxlanSegmentGenerator.generate() against a stateful in-memory store."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from generators.logger import GeneratorError
from generators.protocols import ManagedSegmentDeployment
from tests.unit import segment_generators_helpers
from tests.unit.segment_generators_helpers import _DEP_1, _DEP_2, _make_gen, _seg_response

# Autouse fixture, re-exported so pytest registers it in this module.
ensure_zone = segment_generators_helpers.ensure_zone


# ===========================================================================
# TestSegmentVniInvariant — behaviour of generate() against a stateful fake
# ===========================================================================


class _FakeSegmentDeploymentStore:
    """In-memory ManagedSegmentDeployment store behind client.filters/create.

    A node created with a pool request gets the next pool number (a new
    consumer id draws a new number, as on a real Number pool); a literal VNI is
    stored as written. ``pool_draws`` counts pool allocations.
    """

    def __init__(self, first_vni: int = 10100) -> None:
        self.records: list[MagicMock] = []
        self.created_payloads: list[dict[str, Any]] = []
        self.pool_draws = 0
        self.filter_calls = 0
        self._next_vni = first_vni

    def seed(self, deployment_id: str, vni: int | None) -> None:
        self._store(deployment_id, vni)

    def _store(self, deployment_id: str, vni: int | None) -> MagicMock:
        record = MagicMock()
        record.deployment.id = deployment_id
        record.vni.value = vni
        record.save = AsyncMock()
        self.records.append(record)
        return record

    async def filters(self, *, kind: Any, **kwargs: Any) -> list[Any]:
        assert kind == ManagedSegmentDeployment
        self.filter_calls += 1
        return list(self.records)

    async def create(self, *, kind: Any, data: dict[str, Any]) -> MagicMock:
        assert kind == ManagedSegmentDeployment
        self.created_payloads.append(data)
        vni = data.get("vni")
        if isinstance(vni, dict):
            self.pool_draws += 1
            vni = self._next_vni
            self._next_vni += 1
        record = self._store(data["deployment"]["id"], vni)
        # Persist only on save(), like the SDK: a created-but-unsaved node is invisible.
        self.records.remove(record)

        async def _save(**_: Any) -> None:
            self.records.append(record)

        record.save = AsyncMock(side_effect=_save)
        return record

    def vnis(self) -> dict[str, int | None]:
        return {r.deployment.id: r.vni.value for r in self.records}


def _stateful_gen(store: _FakeSegmentDeploymentStore) -> Any:
    gen = _make_gen()
    gen.client.filters = AsyncMock(side_effect=store.filters)
    gen.client.create = AsyncMock(side_effect=store.create)
    gen.client.get = AsyncMock(return_value=MagicMock(id="pool-global"))
    gen._create_inline_sub_interfaces = AsyncMock()
    return gen


class TestSegmentVniInvariant:
    """A stretched segment carries ONE VNI, a re-run allocates nothing, a first activation allocates one."""

    def _stretched(self) -> dict:
        return _seg_response(
            seg_id="seg-1", seg_name="vxlan-1000", deployments=[_DEP_1, _DEP_2], stretch_scope="global"
        )

    def test_stretched_segment_over_two_deployments_gets_one_vni(self):
        store = _FakeSegmentDeploymentStore()
        gen = _stateful_gen(store)

        asyncio.run(gen.generate(self._stretched()))

        assert store.vnis() == {"dc-1": 10100, "dc-2": 10100}
        assert store.pool_draws == 1

    def test_rerun_of_a_stretched_segment_allocates_nothing_new(self):
        store = _FakeSegmentDeploymentStore()
        gen = _stateful_gen(store)
        asyncio.run(gen.generate(self._stretched()))
        created_before = len(store.created_payloads)

        asyncio.run(gen.generate(self._stretched()))

        assert len(store.created_payloads) == created_before
        assert store.pool_draws == 1
        assert store.vnis() == {"dc-1": 10100, "dc-2": 10100}

    def test_stretched_activation_adding_a_second_deployment_reuses_the_existing_vni(self):
        """Only the first site exists (seeded): the new site takes its VNI as a literal."""
        store = _FakeSegmentDeploymentStore()
        store.seed("dc-1", 10555)
        gen = _stateful_gen(store)

        asyncio.run(gen.generate(self._stretched()))

        assert store.pool_draws == 0
        assert store.created_payloads[0]["vni"] == 10555
        assert store.vnis() == {"dc-1": 10555, "dc-2": 10555}

    def test_stretched_segment_reads_the_store_once_up_front_and_once_after_the_draw(self):
        """No per-deployment re-queries: the prefetch plus one read-back of the freshly drawn VNI."""
        store = _FakeSegmentDeploymentStore()
        gen = _stateful_gen(store)

        asyncio.run(gen.generate(self._stretched()))

        assert store.filter_calls == 2

    def test_rerun_of_a_stretched_segment_reads_the_store_once(self):
        store = _FakeSegmentDeploymentStore()
        store.seed("dc-1", 10100)
        store.seed("dc-2", 10100)
        gen = _stateful_gen(store)

        asyncio.run(gen.generate(self._stretched()))

        assert store.filter_calls == 1
        assert store.created_payloads == []

    def test_first_local_activation_allocates_exactly_one_vni_per_deployment_from_its_own_pool(self):
        store = _FakeSegmentDeploymentStore()
        gen = _stateful_gen(store)
        data = _seg_response(seg_id="seg-1", seg_name="vxlan-1000", deployments=[_DEP_1, _DEP_2])

        asyncio.run(gen.generate(data))

        assert [p["vni"]["from_pool"]["id"] for p in store.created_payloads] == ["pool-vni-1", "pool-vni-2"]
        assert store.pool_draws == 2
        assert store.filter_calls == 1

    def test_local_rerun_allocates_nothing_new(self):
        store = _FakeSegmentDeploymentStore()
        gen = _stateful_gen(store)
        data = _seg_response(seg_id="seg-1", seg_name="vxlan-1000", deployments=[_DEP_1, _DEP_2])
        asyncio.run(gen.generate(data))

        asyncio.run(gen.generate(data))

        assert len(store.created_payloads) == 2
        assert store.pool_draws == 2

    def test_local_segment_never_reuses_another_sites_vni(self):
        """A VNI already on another site's deployment is not written as a literal on a local segment."""
        store = _FakeSegmentDeploymentStore()
        store.seed("dc-1", 10100)
        gen = _stateful_gen(store)
        data = _seg_response(seg_id="seg-1", seg_name="vxlan-1000", deployments=[_DEP_1, _DEP_2])

        asyncio.run(gen.generate(data))

        payload = store.created_payloads[0]
        assert payload["deployment"] == {"id": "dc-2"}
        assert payload["vni"]["from_pool"]["id"] == "pool-vni-2"

    def test_unreadable_existing_deployments_fail_the_run_before_any_write(self):
        """A blind prefetch would allocate a second VNI / duplicate records: fatal, nothing written."""
        gen = _make_gen()
        gen.client.filters = AsyncMock(side_effect=Exception("graphql timeout"))
        gen.client.create = AsyncMock()
        gen._activate_segment_in_deployment = AsyncMock()
        data = _seg_response(seg_id="seg-1", seg_name="vxlan-1000", deployments=[_DEP_1])

        with pytest.raises(GeneratorError, match="existing SegmentDeployments"):
            asyncio.run(gen.generate(data))

        gen.client.create.assert_not_called()
        gen._activate_segment_in_deployment.assert_not_called()

    def test_unreadable_vni_read_back_fails_instead_of_drawing_a_second_vni(self):
        """If the read-back after the first draw fails, the run stops before the second site allocates."""
        store = _FakeSegmentDeploymentStore()
        gen = _stateful_gen(store)
        calls = {"n": 0}

        async def _flaky(*, kind: Any, **kwargs: Any) -> list[Any]:
            calls["n"] += 1
            if calls["n"] == 2:
                raise Exception("graphql timeout")
            return await store.filters(kind=kind, **kwargs)

        gen.client.filters = AsyncMock(side_effect=_flaky)

        with pytest.raises(Exception, match="graphql timeout"):
            asyncio.run(gen.generate(self._stretched()))

        assert store.pool_draws == 1
        assert len(store.created_payloads) == 1
