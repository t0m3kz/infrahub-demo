"""Unit tests for SegmentPortTagMixin (generators/topology/segment_ports.py): the segment owns its VLAN rows and the
ports tagged with them, so add_vxlan_segment and add_app_component_segment run the same reconcile.

The desired ports are the ones derived from the instances of the AppComponents on the segment (and the border service
ports of a terminating firewall context) plus the ones a deployment requests (``requested_ports``). The rows come
FIRST, for the devices of the desired ports; then each desired port joins its switch's ROW of the segment (an edge of
the port's ``capabilities``). Every port edge of the segment is the reconcile's: a missing one is added, one no longer
desired is removed. Both steps run under one segment lock, rows then tags.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from generators.segment_vlan import segment_lock_key
from generators.topology.app_instance_segment import AppInstanceSegmentGenerator
from generators.topology.segment import VxlanSegmentGenerator
from generators.topology.segment_ports import SegmentPortTagMixin


def _gen() -> Any:
    """A segment generator with a recording resource_lock and a mocked client."""
    gen: Any = VxlanSegmentGenerator.__new__(VxlanSegmentGenerator)
    gen.client = AsyncMock()
    gen.logger = MagicMock()
    gen.events = []

    @asynccontextmanager
    async def _lock(key: str) -> AsyncGenerator[None]:
        gen.events.append(f"lock:{key}")
        yield
        gen.events.append(f"unlock:{key}")

    gen.resource_lock = _lock
    return gen


ADD = "mutation EdgeAdd($data: RelationshipNodesInput!) { RelationshipAdd(data: $data) { ok } }"
REMOVE = "mutation EdgeRemove($data: RelationshipNodesInput!) { RelationshipRemove(data: $data) { ok } }"


def _wire(gen: Any) -> list[dict[str, Any]]:
    """Record every mutation (query text and variables) in the returned list."""
    writes: list[dict[str, Any]] = []

    async def _execute(query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        writes.append({"query": query, "variables": variables})
        return {}

    gen.client.execute_graphql = AsyncMock(side_effect=_execute)
    return writes


ROW = "row-dev"
"""The segment's row on ``dev``, the device every test port sits on (unless a test names another)."""


def _port(port_id: str, row: str = ROW, device: str = "dev") -> dict[str, Any]:
    """A port on the segment, its edge to ``row``."""
    return {"id": port_id, "device_id": device, "row_id": row}


def _state(
    ports: list[dict[str, Any]],
    rows: dict[str, str] | None = None,
    requested: list[str] | None = None,
    **segment: Any,
) -> dict[str, Any]:
    """A segment state with ``ports``, the rows ``{device id: row id}`` (default: ``dev`` has ROW) under one
    deployment ``sd-1``, which requests ``requested``."""
    rows = {"dev": ROW} if rows is None else rows
    return {
        "segment": {"id": "seg-1", "ports": ports, **segment},
        "deployments": [
            {
                "id": "sd-1",
                "deployment_id": "dc-1",
                "rows": {device: {"id": row, "vlan_id": 100} for device, row in rows.items()},
                "requested": requested or [],
            }
        ],
    }


class TestEveryHostRunsTheSameReconcile:
    @pytest.mark.parametrize("host", [VxlanSegmentGenerator, AppInstanceSegmentGenerator])
    def test_the_reconcile_is_one_implementation(self, host: type[VxlanSegmentGenerator]) -> None:
        """Both generators inherit the mixin's methods: no second copy of the tag step."""
        assert issubclass(host, SegmentPortTagMixin)
        assert host.reconcile_vxlan_segment is SegmentPortTagMixin.reconcile_vxlan_segment
        assert host._reconcile_segment_port_rows is SegmentPortTagMixin._reconcile_segment_port_rows
        assert host._derived_segment_port_ids is SegmentPortTagMixin._derived_segment_port_ids


class TestPortEdges:
    """End to end through reconcile_vxlan_segment; the row step is stubbed and records the devices it was given."""

    @staticmethod
    def _gen(derived: set[str], devices: dict[str, str | None] | None = None) -> tuple[Any, list[dict[str, Any]]]:
        """``derived`` are the component ports; ``devices`` overrides the device of a port (default: ``dev``)."""
        gen = _gen()
        gen._derived_segment_port_ids = AsyncMock(return_value=derived)
        gen._port_devices = AsyncMock(
            side_effect=lambda ids: {port_id: (devices or {}).get(port_id, "dev") for port_id in ids}
        )
        gen._reconcile_segment_vlans_locked = AsyncMock(return_value=False)
        gen.reconcile_vrfs = AsyncMock()
        return gen, _wire(gen)

    @staticmethod
    def _reconcile(gen: Any, state: dict[str, Any]) -> tuple[int, int]:
        gen._fetch_segment_vlan_state = AsyncMock(return_value=state)
        result = asyncio.run(gen.reconcile_vxlan_segment("seg-1", "seg"))
        assert result is not None
        return result

    @staticmethod
    def _row_devices(gen: Any) -> set[str]:
        """The devices the row step was asked to give a row (the desired ports')."""
        return gen._reconcile_segment_vlans_locked.await_args.args[3]

    @staticmethod
    def _mutations(writes: list[dict[str, Any]]) -> list[tuple[str, str, str]]:
        return [
            (w["query"], w["variables"]["data"]["id"], w["variables"]["data"]["nodes"][0]["id"])
            for w in writes
            if "query" in w
        ]

    def test_a_derived_port_is_added_on_the_port(self) -> None:
        """One RelationshipAdd on the PORT: name `capabilities`, its switch's row as the node."""
        gen, writes = self._gen({"if-a"})

        assert self._reconcile(gen, _state([])) == (1, 0)

        assert writes == [
            {"query": ADD, "variables": {"data": {"id": "if-a", "name": "capabilities", "nodes": [{"id": ROW}]}}}
        ]
        assert self._row_devices(gen) == {"dev"}

    def test_an_edge_no_longer_desired_is_removed(self) -> None:
        """No component and no request: every port edge of the segment goes, and no device needs a row."""
        gen, writes = self._gen(set())

        assert self._reconcile(gen, _state([_port("if-old")])) == (0, 1)

        assert writes == [
            {"query": REMOVE, "variables": {"data": {"id": "if-old", "name": "capabilities", "nodes": [{"id": ROW}]}}}
        ]
        assert self._row_devices(gen) == set()

    def test_a_moved_instance_swaps_its_port(self) -> None:
        """The instance now lands on if-b: if-b is added, the old if-a removed."""
        gen, writes = self._gen({"if-b"})

        assert self._reconcile(gen, _state([_port("if-a")])) == (1, 1)
        assert [w["query"] for w in writes] == [ADD, REMOVE]

    def test_a_matching_set_writes_nothing(self) -> None:
        """Idempotent: the edges already are the desired ports."""
        gen, writes = self._gen({"if-a"})

        assert self._reconcile(gen, _state([_port("if-a")])) == (0, 0)
        assert writes == []

    def test_a_port_whose_device_got_no_row_is_skipped(self) -> None:
        """No row of the segment on the port's device after the row pass (no deployment there): a WARNING, no add."""
        gen, writes = self._gen({"if-a"}, devices={"if-a": "dev-out"})

        assert self._reconcile(gen, _state([])) == (0, 0)
        assert writes == []
        gen.logger.warning.assert_called_once()
        assert self._row_devices(gen) == {"dev-out"}

    def test_a_port_joins_the_row_of_its_own_device(self) -> None:
        """Two derived ports on two switches: each joins its own switch's row."""
        gen, writes = self._gen({"if-a", "if-b"}, devices={"if-b": "dev-2"})

        assert self._reconcile(gen, _state([], rows={"dev": ROW, "dev-2": "row-dev-2"})) == (2, 0)
        assert self._mutations(writes) == [(ADD, "if-a", ROW), (ADD, "if-b", "row-dev-2")]

    def test_a_requested_port_gets_its_switch_row_and_edge(self) -> None:
        """A port the deployment requests (no component derives it): its device is handed to the row step and the
        port joins the row."""
        gen, writes = self._gen(set())

        assert self._reconcile(gen, _state([], requested=["if-r"])) == (1, 0)
        assert self._row_devices(gen) == {"dev"}
        assert self._mutations(writes) == [(ADD, "if-r", ROW)]

    def test_a_withdrawn_request_has_its_edge_removed(self) -> None:
        """The port was requested and tagged; the request is gone: its edge is removed."""
        gen, writes = self._gen(set())

        assert self._reconcile(gen, _state([_port("if-r")])) == (0, 1)
        assert self._mutations(writes) == [(REMOVE, "if-r", ROW)]

    def test_a_requested_port_whose_row_is_under_another_deployment_is_skipped(self) -> None:
        """The switch's row is under another deployment than the requesting one: a WARNING, no edge."""
        gen, writes = self._gen(set())
        state = _state([], rows={})
        state["deployments"].append(
            {"id": "sd-2", "deployment_id": "dc-2", "rows": {"dev": {"id": ROW, "vlan_id": 100}}, "requested": []}
        )
        state["deployments"][0]["requested"] = ["if-r"]

        assert self._reconcile(gen, state) == (0, 0)
        assert writes == []
        gen.logger.warning.assert_called_once()

    def test_a_derived_port_that_is_also_requested_follows_the_derivation(self) -> None:
        """Derived and requested: the request's deployment check does not apply, the port joins its switch's row."""
        gen, writes = self._gen({"if-r"})
        state = _state([], rows={})
        state["deployments"].append(
            {"id": "sd-2", "deployment_id": "dc-2", "rows": {"dev": {"id": ROW, "vlan_id": 100}}, "requested": []}
        )
        state["deployments"][0]["requested"] = ["if-r"]

        assert self._reconcile(gen, state) == (1, 0)
        assert self._mutations(writes) == [(ADD, "if-r", ROW)]


class TestTerminatedServicePorts:
    """A segment terminated on a firewall context is tagged on the border service ports facing its pair, in the same
    reconcile as the component ports."""

    @staticmethod
    def _context(*far_ends: dict[str, Any]) -> dict[str, Any]:
        """The cleaned ``terminated_on`` of a state segment: one member per far end, its uplink cabled to it (a context
        has no interface edge; its border ports are derived from its members' cables)."""
        members = [
            {
                "id": f"fw-{index}",
                "role": "internal-firewall",
                "interfaces": [
                    {
                        "typename": "DcimPhysicalInterface",
                        "id": f"up-{index}",
                        "name": "ethernet1/1",
                        "role": "uplink",
                        "cable": {"endpoints": [far]},
                    }
                ],
            }
            for index, far in enumerate(far_ends)
        ]
        return {"terminated_on": {"id": "ctx-1", "shared_devices": members}}

    @staticmethod
    def _cap(cap_id: str, *, typename: str = "DcimPhysicalInterface", role: str = "firewall") -> dict[str, Any]:
        return {"id": cap_id, "typename": typename, "role": role}

    def test_the_service_ports_of_the_context_are_derived(self) -> None:
        """Only the switch ports (physical, LAG) with the firewall service role count: not the pair's sub-interfaces,
        not a load-balancer port."""
        segment = self._context(
            self._cap("bl-1"),
            self._cap("bl-lag", typename="DcimLAGInterface"),
            self._cap("fw-sub", typename="DcimVirtualInterface", role="service"),
            self._cap("lb-port", role="load-balancer"),
            {"typename": "DcimPhysicalInterface", "role": "firewall"},
        )

        assert SegmentPortTagMixin._terminated_service_port_ids(segment) == {"bl-1", "bl-lag"}

    def test_a_segment_not_terminated_derives_none(self) -> None:
        """No ``terminated_on`` (or one with no members): nothing to tag."""
        assert SegmentPortTagMixin._terminated_service_port_ids({}) == set()
        assert SegmentPortTagMixin._terminated_service_port_ids({"terminated_on": None}) == set()
        assert SegmentPortTagMixin._terminated_service_port_ids(self._context()) == set()

    def test_the_service_port_is_added_next_to_the_component_ports(self) -> None:
        """The border port is a desired port like a host-facing one: one RelationshipAdd on the PORT."""
        gen = _gen()
        gen._fetch_segment_vlan_state = AsyncMock(return_value=_state([], **self._context(self._cap("bl-1"))))
        gen._segment_components = AsyncMock(return_value=[])
        gen._customer_facing_target_ids = AsyncMock(return_value={"if-a"})
        gen._port_devices = AsyncMock(side_effect=lambda ids: dict.fromkeys(ids, "dev"))
        gen._reconcile_segment_vlans_locked = AsyncMock(return_value=False)
        gen.reconcile_vrfs = AsyncMock()
        writes = _wire(gen)

        assert asyncio.run(gen.reconcile_vxlan_segment("seg-1", "seg")) == (2, 0)

        gen._port_devices.assert_awaited_once_with({"if-a", "bl-1"})
        added = [w["variables"]["data"] for w in writes if w.get("query") == ADD]
        assert {data["id"] for data in added} == {"if-a", "bl-1"}
        assert all(data["nodes"] == [{"id": ROW}] for data in added)


class TestReconcileVxlanSegment:
    @staticmethod
    def _ready(*, changed: bool) -> Any:
        gen = _gen()
        gen._fetch_segment_vlan_state = AsyncMock(return_value=_state([_port("if-a")], requested=["if-r"]))
        gen._derived_segment_port_ids = AsyncMock(side_effect=lambda *_a: gen.events.append("derive") or {"if-a"})
        gen._port_devices = AsyncMock(side_effect=lambda ids: dict.fromkeys(ids, "dev"))
        gen._reconcile_segment_vlans_locked = AsyncMock(
            side_effect=lambda *_a, **_k: gen.events.append("rows") or changed
        )
        gen._reconcile_segment_port_rows = AsyncMock(side_effect=lambda **_: gen.events.append("tags") or (0, 0))
        # The VRF pass over the domains' devices has its own tests (test_vrf_callers.py).
        gen.reconcile_vrfs = AsyncMock()
        return gen

    def test_rows_then_tags_under_one_segment_lock(self) -> None:
        """The rows are reconciled first, then the port tags on them, inside the same lock."""
        gen = self._ready(changed=True)

        result = asyncio.run(gen.reconcile_vxlan_segment("seg-1", "seg"))

        assert result == (0, 0)
        assert gen.events == [
            f"lock:{segment_lock_key('seg-1')}",
            "derive",
            "rows",
            "tags",
            f"unlock:{segment_lock_key('seg-1')}",
        ]

    def test_the_state_is_read_again_only_when_a_row_changed(self) -> None:
        """The tags are reconciled against a fresh read after a row write, and against the first read otherwise."""
        changed = self._ready(changed=True)
        asyncio.run(changed.reconcile_vxlan_segment("seg-1", "seg"))
        unchanged = self._ready(changed=False)
        asyncio.run(unchanged.reconcile_vxlan_segment("seg-1", "seg"))

        assert changed._fetch_segment_vlan_state.await_count == 2
        assert unchanged._fetch_segment_vlan_state.await_count == 1

    def test_the_tag_step_gets_the_ports_the_desired_ports_and_the_rows(self) -> None:
        """ports are the segment's switch ports today; port_devices the derived and requested ports; requested only
        the ports no component derives; rows by device with their deployment."""
        gen = self._ready(changed=False)

        asyncio.run(gen.reconcile_vxlan_segment("seg-1", "seg"))

        gen._port_devices.assert_awaited_once_with({"if-a", "if-r"})
        kwargs = gen._reconcile_segment_port_rows.await_args.kwargs
        assert kwargs["ports"] == [_port("if-a")]
        assert kwargs["port_devices"] == {"if-a": "dev", "if-r": "dev"}
        assert kwargs["requested"] == {"if-r": "sd-1"}
        assert kwargs["rows_by_device"] == {"dev": (ROW, "sd-1")}
        assert gen._reconcile_segment_vlans_locked.await_args.args[3] == {"dev"}

    def test_a_segment_that_is_no_vxlan_segment_is_left_alone(self) -> None:
        """No state (a cloud segment id, or a deleted segment): None, no tag, no row."""
        gen = _gen()
        gen._fetch_segment_vlan_state = AsyncMock(return_value=None)
        gen._derived_segment_port_ids = AsyncMock()
        gen._reconcile_segment_port_rows = AsyncMock()
        gen._reconcile_segment_vlans_locked = AsyncMock()

        assert asyncio.run(gen.reconcile_vxlan_segment("seg-1", "seg")) is None
        gen._derived_segment_port_ids.assert_not_awaited()
        gen._reconcile_segment_port_rows.assert_not_awaited()
        gen._reconcile_segment_vlans_locked.assert_not_awaited()
