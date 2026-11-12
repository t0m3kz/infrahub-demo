"""Unit tests for SegmentVlanMixin's per-segment VLAN domain reconciliation (generators/segment_vlan.py)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

from generators.segment_vlan import SEGMENT_PORTS_QUERY, SegmentVlanMixin
from tests.unit.segment_vlan_helpers import VlanWorld, make_device, make_gen, raw_state


def _customer_dc(parent_id: str, pod_ids: list[str]) -> dict[str, Any]:
    """A raw TopologyCustomerDC edge whose hosting DC has the given pods."""
    return {
        "node": {
            "id": f"cust-{parent_id}",
            "parent": {
                "node": {
                    "id": parent_id,
                    "children": {"edges": [{"node": {"id": pod}} for pod in pod_ids] + [{"node": {}}]},
                }
            },
        }
    }


# State parsing


class TestFetchSegmentVlanState:
    def test_parses_segment_and_the_rows_per_deployment(self) -> None:
        """Each segment deployment carries its hosting deployment, its rows keyed by device id and the switch ports
        assigned to it (its ``capabilities``: a virtual interface is no request)."""
        gen = make_gen()
        gen.client.execute_graphql = AsyncMock(
            return_value=raw_state(
                interfaces=[("if-1", "DcimPhysicalInterface", "sw-1")],
                deployments=[("sd-a", "dc-1"), ("sd-b", "dc-2")],
                rows=[("row-1", "sw-1", 120, "sd-a")],
                requested={"sd-a": [("if-9", "DcimPhysicalInterface"), ("vif-1", "DcimVirtualInterface")]},
            )
        )

        state = asyncio.run(gen._fetch_segment_vlan_state("seg-1"))

        assert state is not None
        assert state["deployments"] == [
            {
                "id": "sd-a",
                "deployment_id": "dc-1",
                "vni": None,
                "rows": {"sw-1": {"id": "row-1", "vlan_id": 120}},
                "requested": ["if-9"],
            },
            {"id": "sd-b", "deployment_id": "dc-2", "vni": None, "rows": {}, "requested": []},
        ]
        assert state["segment"]["id"] == "seg-1"
        calls = gen.client.execute_graphql.call_args_list
        assert [call.kwargs["variables"] for call in calls] == [
            {"segment_id": "seg-1"},
            {"row_ids": ["row-1"]},
        ]

    def test_non_vxlan_segment_has_no_state(self) -> None:
        """An id that matches no ManagedVxlanSegment (a cloud segment, a deleted one): None, nothing to reconcile."""
        gen = make_gen()
        gen.client.execute_graphql = AsyncMock(
            return_value={"ManagedVxlanSegment": {"edges": []}, "ManagedSegmentDeployment": {"edges": []}}
        )

        assert asyncio.run(gen._fetch_segment_vlan_state("vlan-seg-1")) is None

    def test_query_reads_pod_children_interface_devices_and_the_deployments_rows(self, root_dir: Path) -> None:
        """The query carries the hosting parents' pods, the tagged ports' devices and the rows under each deployment."""
        query = (root_dir / "queries" / "topology" / "add" / "segment_vlans.gql").read_text()
        compact = " ".join(query.split())
        assert "... on TopologyDataCenter { children { edges { node { ... on TopologyPod { id } } } } }" in compact
        assert "DcimPhysicalInterface" not in compact and "segments" not in compact
        ports = " ".join(SEGMENT_PORTS_QUERY.split())
        for kind in ("DcimPhysicalInterface", "DcimLAGInterface"):
            assert (
                f"{kind}(capabilities__ids: $row_ids) {{ edges {{ node {{ id device {{ node {{ id }} }} "
                "capabilities(ids: $row_ids) { edges { node { id __typename } } }" in ports
            )
        assert "ManagedSegmentDeployment(segment__ids: [$segment_id])" in compact
        assert "vlans { edges { node { id vlan_id { value } device { node { id } } } } }" in compact
        assert "capabilities { edges { node { id __typename } } }" in compact
        assert "ManagedMLAG" not in compact


class TestSegmentPorts:
    def test_ports_are_read_from_the_port_side(self) -> None:
        """Each port edge to a row of the segment is {id, device_id, row_id}."""
        gen = make_gen()
        gen.client.execute_graphql = AsyncMock(
            return_value=raw_state(
                interfaces=[("if-1", "DcimPhysicalInterface", "sw-1"), ("po-1", "DcimLAGInterface", "sw-2")],
                deployments=[("sd-a", "dc-1")],
                rows=[("row-1", "sw-1", 100, "sd-a"), ("row-2", "sw-2", 100, "sd-a")],
            )
        )

        state = asyncio.run(gen._fetch_segment_vlan_state("seg-1"))

        assert state is not None
        assert state["segment"]["ports"] == [
            {"id": "if-1", "device_id": "sw-1", "row_id": "row-1"},
            {"id": "po-1", "device_id": "sw-2", "row_id": "row-2"},
        ]

    def test_an_edge_to_another_capability_is_not_this_segments_port(self) -> None:
        """A port with other capabilities: only an edge to a row of this segment counts."""
        raw = raw_state(
            interfaces=[("if-1", "DcimPhysicalInterface", "sw-1")],
            deployments=[("sd-a", "dc-1")],
            rows=[("row-1", "sw-1", 100, "sd-a")],
        )
        edge = raw["DcimPhysicalInterface"]["edges"][0]["node"]["capabilities"]["edges"][0]
        assert len(SegmentVlanMixin._segment_ports(raw, {"row-1"})) == 1
        assert SegmentVlanMixin._segment_ports(raw, {"row-other"}) == []
        edge["node"]["__typename"] = "ManagedVRF"
        assert SegmentVlanMixin._segment_ports(raw, {"row-1"}) == []

    def test_no_deployment_reads_no_port(self) -> None:
        """A port joins its switch's row, never the segment: without a row there is nothing to read."""
        gen = make_gen()
        gen.client.execute_graphql = AsyncMock()

        assert asyncio.run(gen._fetch_segment_ports([])) == []
        gen.client.execute_graphql.assert_not_awaited()


class TestRequestedPorts:
    """The ports assigned to a deployment (its ``capabilities``) are desired like the derived ones: the reconcile gives the port's switch a
    row and joins the port to it; without a request (and no component) the port's switch needs no row."""

    @staticmethod
    def _run(
        world: VlanWorld, gen: Any, devices: dict[str, str]
    ) -> tuple[tuple[int, int] | None, list[dict[str, Any]]]:
        """reconcile_vxlan_segment with no derived port (no component), the ports on ``devices``, the VRF pass
        stubbed; returns the result and the edge mutations written."""
        gen._derived_segment_port_ids = AsyncMock(return_value=set())
        gen._port_devices = AsyncMock(side_effect=lambda ids: {port_id: devices.get(port_id) for port_id in ids})
        gen.reconcile_vrfs = AsyncMock()
        read = gen.client.execute_graphql.side_effect
        mutations: list[dict[str, Any]] = []

        async def _execute(**kwargs: Any) -> dict[str, Any]:
            data = (kwargs.get("variables") or {}).get("data")
            if data is not None:
                mutations.append(data | {"mutation": kwargs["query"].split("{")[1].split("(")[0].strip()})
                return {}
            return await read(**kwargs)

        gen.client.execute_graphql = AsyncMock(side_effect=_execute)
        return asyncio.run(gen.reconcile_vxlan_segment("seg-1", "seg")), mutations

    def test_a_requested_port_gets_its_switch_row_and_edge(self) -> None:
        """No row yet: the requested port's switch gets one, and the port joins it."""
        world = VlanWorld()
        # No port edge in the fake server yet: if-1 is only requested.
        gen = world.gen(
            [make_device("sw-1")], interfaces=(), requested={"sd-seg-1": [("if-1", "DcimPhysicalInterface")]}
        )

        result, mutations = self._run(world, gen, {"if-1": "sw-1"})

        assert [c["device"]["id"] for c in world.created] == ["sw-1"]
        assert result == (1, 0)
        assert mutations == [
            {"id": "if-1", "name": "capabilities", "nodes": [{"id": world.rows[0].id}], "mutation": "RelationshipAdd"}
        ]

    def test_a_port_on_a_switch_nobody_asks_for_creates_no_row(self) -> None:
        """No request, no component: no device needs a row and none is created."""
        world = VlanWorld()
        gen = world.gen([make_device("sw-1")], interfaces=(("if-1", "DcimPhysicalInterface", "sw-1"),))

        result, mutations = self._run(world, gen, {"if-1": "sw-1"})

        assert result == (0, 0)
        assert world.created == [] and mutations == []

    def test_a_withdrawn_request_drops_the_row(self) -> None:
        """sw-1's row exists for a port no deployment requests any more and no component derives: the row goes
        (and the port's edge with it)."""
        world = VlanWorld()
        world.add("seg-1", "sw-1", 120)
        gen = world.gen([make_device("sw-1")], interfaces=(("if-1", "DcimPhysicalInterface", "sw-1"),))

        self._run(world, gen, {"if-1": "sw-1"})

        assert world.vlan_ids("seg-1") == {}

    def test_an_assigned_non_switched_interface_is_no_request(self) -> None:
        """A virtual interface assigned to the deployment is not requested: no row and no edge for it."""
        world = VlanWorld()
        gen = world.gen(
            [make_device("sw-1")], interfaces=(), requested={"sd-seg-1": [("vif-1", "DcimVirtualInterface")]}
        )

        result, mutations = self._run(world, gen, {"vif-1": "sw-1"})

        assert result == (0, 0)
        assert world.created == [] and mutations == []
        gen._port_devices.assert_awaited_once_with(set())
