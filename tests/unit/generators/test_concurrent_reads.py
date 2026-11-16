"""Independent reads and waits run together: segment VLAN devices, LB rules and re-reads, the
application's wait for segment addressing.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from generators.applications.application_security import AppApplicationGenerator
from generators.applications.lb_vip import _reread_until_ready
from generators.protocols import DcimPhysicalDevice
from generators.segments import segment_vlan
from tests.unit.app_plan_helpers import make_app_generator
from utils.limits import WRITE_CONCURRENCY, RetryPolicy


class _Gauge:
    """Counts the calls in flight and the highest count seen."""

    def __init__(self) -> None:
        self.running = 0
        self.peak = 0

    async def hold(self, delay: float = 0.01) -> None:
        self.running += 1
        self.peak = max(self.peak, self.running)
        try:
            await asyncio.sleep(delay)
        finally:
            self.running -= 1


def _device(name: str) -> MagicMock:
    device = MagicMock(id=f"id-{name}")
    device.name = MagicMock(value=name)
    device.deployment = MagicMock(initialized=False)
    device.platform = MagicMock(initialized=True)
    device.platform.peer.name = MagicMock(value="arista_eos")
    return device


class TestSegmentVlanDevices:
    @staticmethod
    def _host(gauge: _Gauge, *, tagged: list[Any], vteps: list[Any]) -> Any:
        host: Any = SimpleNamespace()
        host.logger = MagicMock()
        host.client = MagicMock()

        async def filters(kind: Any, **kwargs: Any) -> list[Any]:
            assert kind is DcimPhysicalDevice
            await gauge.hold()
            return tagged if "ids" in kwargs else vteps

        host.client.filters = filters
        return host

    @staticmethod
    def _segment() -> dict[str, Any]:
        return {"stretch_scope": "global", "customer_deployments": [{"parent": {"id": "dc-1", "children": []}}]}

    @pytest.mark.asyncio
    async def test_the_port_devices_and_the_vteps_are_read_together_and_a_vtep_wins_over_the_same_port_device(
        self,
    ) -> None:
        gauge = _Gauge()
        port_leaf, other = _device("leaf-01"), _device("leaf-02")
        vtep_leaf = _device("leaf-01")
        host = self._host(gauge, tagged=[port_leaf, other], vteps=[vtep_leaf])

        devices = await segment_vlan._segment_vlan_devices(host, self._segment(), {"id-leaf-01", "id-leaf-02"})

        assert gauge.peak == 2
        assert [d.id for d in devices] == ["id-leaf-01", "id-leaf-02"]
        assert devices[0] is vtep_leaf and devices[1] is other

    @pytest.mark.asyncio
    async def test_a_local_segment_reads_only_the_port_devices(self) -> None:
        gauge = _Gauge()
        leaf = _device("leaf-01")
        host = self._host(gauge, tagged=[leaf], vteps=[_device("other")])

        devices = await segment_vlan._segment_vlan_devices(host, {"stretch_scope": "local"}, {"id-leaf-01"})

        assert devices == [leaf]
        assert gauge.peak == 1

    @pytest.mark.asyncio
    async def test_nothing_to_read_gives_no_devices(self) -> None:
        host = self._host(_Gauge(), tagged=[], vteps=[])

        assert await segment_vlan._segment_vlan_devices(host, {"stretch_scope": "local"}, set()) == []


class TestLbRereadWaits:
    @pytest.mark.asyncio
    async def test_the_waits_on_the_nodes_overlap_within_the_bound_and_precede_the_first_read(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("generators.applications.lb_vip.LB_VIP_REREAD", RetryPolicy(attempts=1, delay=0))
        gauge = _Gauge()
        gen = make_app_generator()
        waited: list[str] = []

        async def wait(owners: Any, node_id: str) -> bool:  # noqa: ARG001
            waited.append(node_id)
            await gauge.hold()
            return False

        gen.wait_for_parent_generators = wait
        gen.client.execute_graphql = AsyncMock(
            side_effect=lambda **_: (_ for _ in ()).throw(AssertionError) if gauge.running else {}
        )
        dicts: dict[str, list[dict[str, Any]]] = {f"seg-{i:02d}": [{}] for i in range(20)}

        await _reread_until_ready(
            gen,
            dicts,
            owner="add_vxlan_segment",
            query=_vip_query(),
            kind="Seg",
            field="gateway",
            ready=lambda _n: True,
        )

        assert 1 < gauge.peak <= WRITE_CONCURRENCY
        assert sorted(waited) == sorted(dicts)

    @pytest.mark.asyncio
    async def test_a_failing_wait_raises_before_any_read(self) -> None:
        gen = make_app_generator()

        async def wait(owners: Any, node_id: str) -> bool:  # noqa: ARG001
            if node_id == "seg-1":
                raise RuntimeError("parent failed")
            return False

        gen.wait_for_parent_generators = wait

        with pytest.raises(RuntimeError, match="parent failed"):
            await _reread_until_ready(
                gen,
                {"seg-0": [{}], "seg-1": [{}]},
                owner="add_vxlan_segment",
                query=_vip_query(),
                kind="Seg",
                field="gateway",
                ready=lambda _n: True,
            )

        gen.client.execute_graphql.assert_not_awaited()


def _vip_query() -> Any:
    from generators.applications import lb_vip

    return lb_vip._SEGMENT_GATEWAYS_QUERY


class TestApplicationWaitsForSegments:
    @staticmethod
    def _gen(gauge: _Gauge, *, in_flight: bool, refetched: dict[str, Any] | None) -> Any:
        gen: Any = AppApplicationGenerator.__new__(AppApplicationGenerator)
        gen.client = AsyncMock()
        gen.logger = MagicMock()
        gen.waited = []

        async def wait(owners: Any, parent_id: str) -> bool:
            gen.waited.append((tuple(owners), parent_id))
            await gauge.hold()
            return in_flight

        gen.wait_for_parent_generators = wait
        gen.collect_data = AsyncMock(return_value=refetched)
        return gen

    @staticmethod
    def _app(count: int) -> dict[str, Any]:
        segments = [{"id": f"seg-{i:02d}", "typename": "ManagedVxlanSegment", "gateway": None} for i in range(count)]
        return {
            "id": "app-1",
            "name": "first-read",
            "children": [{"id": f"comp-{i}", "network_segment": segment} for i, segment in enumerate(segments)],
        }

    @pytest.mark.asyncio
    async def test_the_segments_are_waited_for_together_and_the_data_is_re_read_once(self) -> None:
        gauge = _Gauge()
        refetched = {"AppApplication": {"edges": [{"node": {"id": "app-1", "name": {"value": "re-read"}}}]}}
        gen = self._gen(gauge, in_flight=True, refetched=refetched)

        result = await gen._await_segment_addressing(self._app(20))

        assert 1 < gauge.peak <= WRITE_CONCURRENCY
        assert [seg for _, seg in gen.waited] == sorted(seg for _, seg in gen.waited)
        assert len(gen.waited) == 20
        gen.collect_data.assert_awaited_once()
        assert result["name"] == "re-read"

    @pytest.mark.asyncio
    async def test_nothing_in_flight_keeps_the_application_and_reads_nothing(self) -> None:
        gen = self._gen(_Gauge(), in_flight=False, refetched={"AppApplication": []})
        app = self._app(3)

        assert await gen._await_segment_addressing(app) is app
        gen.collect_data.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_second_run_waits_for_the_same_segments(self) -> None:
        gen = self._gen(_Gauge(), in_flight=False, refetched=None)

        for _ in range(2):
            await gen._await_segment_addressing(self._app(4))

        assert sorted(gen.waited[:4]) == sorted(gen.waited[4:])

    @pytest.mark.asyncio
    async def test_a_failing_wait_raises(self) -> None:
        gen = self._gen(_Gauge(), in_flight=False, refetched=None)

        async def wait(owners: Any, parent_id: str) -> bool:  # noqa: ARG001
            raise RuntimeError("segment run failed")

        gen.wait_for_parent_generators = wait

        with pytest.raises(RuntimeError, match="segment run failed"):
            await gen._await_segment_addressing(self._app(2))
