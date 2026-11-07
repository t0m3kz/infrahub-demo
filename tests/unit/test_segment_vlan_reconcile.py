"""Unit tests for SegmentVlanMixin's per-segment VLAN domain reconciliation
(generators/segment_vlan.py).

ManagedSegmentVlan rows are reached by add_vxlan_segment and by
every add_app_component_segment run of a component on the segment, so no run
owns them: they are reconciled per SEGMENT from desired state (every
switch with a port tagged with the segment, plus border gateways of
a stretched segment), each under the segment deployment its switch sits in,
written untracked, and stale ones are deleted explicitly. Covers state
parsing, the desired device set, create/keep/delete/move, the lock, and that a
device with no deployment of the segment is reported and leaves the rows alone.
MLAG pairs: tests/unit/test_segment_vlan_pairs.py.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

from generators.segment_vlan import SegmentVlanMixin
from tests.unit.segment_vlan_helpers import make_gen, raw_state


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


# ===========================================================================
# State parsing
# ===========================================================================


class TestFetchSegmentVlanState:
    def test_parses_segment_and_the_rows_per_deployment(self) -> None:
        """Each segment deployment carries its hosting deployment and its rows keyed by device id."""
        gen = make_gen()
        gen.client.execute_graphql = AsyncMock(
            return_value=raw_state(
                interfaces=[("if-1", "DcimPhysicalInterface", "sw-1")],
                deployments=[("sd-a", "dc-1"), ("sd-b", "dc-2")],
                rows=[("row-1", "sw-1", 120, "sd-a")],
            )
        )

        state = asyncio.run(gen._fetch_segment_vlan_state("seg-1"))

        assert state is not None
        assert state["deployments"] == [
            {"id": "sd-a", "deployment_id": "dc-1", "rows": {"sw-1": {"id": "row-1", "vlan_id": 120}}},
            {"id": "sd-b", "deployment_id": "dc-2", "rows": {}},
        ]
        assert state["segment"]["id"] == "seg-1"
        assert gen.client.execute_graphql.call_args.kwargs["variables"] == {"segment_id": "seg-1"}

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
        for kind in ("DcimPhysicalInterface", "DcimLAGInterface"):
            assert (
                f"{kind}(segments__ids: [$segment_id]) {{ edges {{ node {{ id device {{ node {{ id }} }} "
                "segments(ids: [$segment_id]) { edges { properties { source { id } } node { id } } } } } }" in compact
            )
        assert "ManagedSegmentDeployment(segment__ids: [$segment_id])" in compact
        assert "vlans { edges { node { id vlan_id { value } device { node { id } } } } }" in compact
        assert "ManagedMLAG" not in compact


class TestSegmentPorts:
    def test_ports_are_read_from_the_port_side_with_the_source_of_their_edge(self) -> None:
        """Each port carrying the segment is {id, device_id, source_id}; a person's edge has no source."""
        gen = make_gen()
        gen.client.execute_graphql = AsyncMock(
            return_value=raw_state(
                interfaces=[("if-1", "DcimPhysicalInterface", "sw-1"), ("po-1", "DcimLAGInterface", "sw-2")],
                sources={"if-1": "acct-1"},
            )
        )

        state = asyncio.run(gen._fetch_segment_vlan_state("seg-1"))

        assert state is not None
        assert state["segment"]["ports"] == [
            {"id": "if-1", "device_id": "sw-1", "source_id": "acct-1"},
            {"id": "po-1", "device_id": "sw-2", "source_id": None},
        ]

    def test_an_edge_to_another_segment_is_not_this_segments_port(self) -> None:
        """A port on several segments: only the edge whose peer is this segment counts."""
        raw = raw_state(interfaces=[("if-1", "DcimPhysicalInterface", "sw-1")])
        raw["DcimPhysicalInterface"]["edges"][0]["node"]["segments"]["edges"][0]["node"]["id"] = "seg-other"

        assert SegmentVlanMixin._segment_ports(raw, "seg-1") == []
