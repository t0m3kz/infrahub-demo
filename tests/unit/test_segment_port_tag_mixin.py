"""Unit tests for SegmentPortTagMixin (generators/topology/segment_ports.py): the segment owns the ports it is
tagged on and its VLAN rows, so add_vxlan_segment and add_app_component_segment run the same reconcile.

A port is tagged from the instances of the AppComponents on the segment, the edge stamped with the run's account as
its source. The reconcile removes an edge only when its source is that account and the port is no longer derived: an
edge with no source (a person's, set from the port) is never touched. The tag step and the row step run under one
segment lock, in that order.
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


ACCOUNT = "acct-gen"
ADD = "mutation EdgeAdd($data: RelationshipNodesInput!) { RelationshipAdd(data: $data) { ok } }"
REMOVE = "mutation EdgeRemove($data: RelationshipNodesInput!) { RelationshipRemove(data: $data) { ok } }"


def _wire(gen: Any) -> list[dict[str, Any]]:
    """Answer the account query and record every mutation (query text and variables) in the returned list."""
    writes: list[dict[str, Any]] = []

    async def _execute(query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        if "AccountProfile" in query:
            writes.append({"account": True})
            return {"AccountProfile": {"id": ACCOUNT}}
        writes.append({"query": query, "variables": variables})
        return {}

    gen.client.execute_graphql = AsyncMock(side_effect=_execute)
    return writes


def _port(port_id: str, source: str | None) -> dict[str, Any]:
    """A port on the segment, its edge written by ``source`` (None: a person)."""
    return {"id": port_id, "device_id": "dev", "source_id": source}


def _components(*instance_ids: str) -> list[dict[str, Any]]:
    """One AppComponent on the segment with the given physical instances."""
    return [{"id": "comp-1", "instances": [{"id": i, "typename": "DcimPhysicalDevice"} for i in instance_ids]}]


class TestEveryHostRunsTheSameReconcile:
    @pytest.mark.parametrize("host", [VxlanSegmentGenerator, AppInstanceSegmentGenerator])
    def test_the_reconcile_is_one_implementation(self, host: type[VxlanSegmentGenerator]) -> None:
        """Both generators inherit the mixin's methods: no second copy of the tag step."""
        assert issubclass(host, SegmentPortTagMixin)
        assert host.reconcile_vxlan_segment is SegmentPortTagMixin.reconcile_vxlan_segment
        assert host._reconcile_segment_interface_tags is SegmentPortTagMixin._reconcile_segment_interface_tags


class TestPortProvenance:
    """A port is added with the run's account as the edge source and removed only by that account."""

    @staticmethod
    def _reconcile(gen: Any, ports: list[dict[str, Any]], terminated: set[str] | None = None) -> tuple[int, int]:
        return asyncio.run(
            gen._reconcile_segment_interface_tags(
                segment_id="seg-1", segment_name="seg", ports=ports, terminated_port_ids=terminated or set()
            )
        )

    @staticmethod
    def _gen(desired: set[str], components: list[dict[str, Any]] | None = None) -> tuple[Any, list[dict[str, Any]]]:
        gen = _gen()
        gen._segment_components = AsyncMock(return_value=_components("dev-1") if components is None else components)
        gen._customer_facing_target_ids = AsyncMock(return_value=desired)
        return gen, _wire(gen)

    def test_a_derived_port_is_added_on_the_port_with_the_account_as_source(self) -> None:
        """One RelationshipAdd on the PORT: name `segments`, the segment as the node, `_relation__source` the account."""
        gen, writes = self._gen({"if-a"})

        assert self._reconcile(gen, []) == (1, 0)

        assert writes == [
            {"account": True},
            {
                "query": ADD,
                "variables": {
                    "data": {"id": "if-a", "name": "segments", "nodes": [{"id": "seg-1", "_relation__source": ACCOUNT}]}
                },
            },
        ]

    def test_a_person_edge_is_never_removed(self) -> None:
        """A sourceless edge on a port no instance lands on stays, with components or without any."""
        for components in (None, []):
            gen, writes = self._gen({"if-a"} if components is None else set(), components)
            ports = [_port("if-a", ACCOUNT), _port("if-x", None)]

            assert self._reconcile(gen, ports if components is None else ports[1:]) == (0, 0)
            assert writes == []

    def test_a_generated_edge_is_removed_when_the_component_leaves(self) -> None:
        """No component left: the edge this account wrote goes, one RelationshipRemove on the port."""
        gen, writes = self._gen(set(), components=[])

        assert self._reconcile(gen, [_port("if-old", ACCOUNT)]) == (0, 1)

        assert writes[-1] == {
            "query": REMOVE,
            "variables": {"data": {"id": "if-old", "name": "segments", "nodes": [{"id": "seg-1"}]}},
        }
        gen._customer_facing_target_ids.assert_awaited_once_with([])

    def test_a_moved_instance_swaps_its_port(self) -> None:
        """The instance now lands on if-b: if-b is added with the source, the old generated if-a removed."""
        gen, writes = self._gen({"if-b"})

        assert self._reconcile(gen, [_port("if-a", ACCOUNT)]) == (1, 1)

        assert [w["query"] for w in writes if "query" in w] == [ADD, REMOVE]

    def test_an_edge_of_another_account_is_not_removed(self) -> None:
        """Only the generator's own account's edges are its to remove."""
        gen, writes = self._gen(set(), components=[])

        assert self._reconcile(gen, [_port("if-other", "acct-other")]) == (0, 0)
        assert [w for w in writes if "query" in w] == []

    def test_a_person_edge_on_a_derived_port_is_kept_in_place(self) -> None:
        """The port is derived and already on the segment: nothing is written, the edge keeps its (empty) source,
        so it survives the component leaving later."""
        gen, writes = self._gen({"if-a"})

        assert self._reconcile(gen, [_port("if-a", None)]) == (0, 0)
        assert writes == []

    def test_a_matching_set_writes_nothing_and_asks_no_account(self) -> None:
        """Idempotent: the generated edges already are the instances' ports, and the account is not even read."""
        gen, writes = self._gen({"if-a"})

        assert self._reconcile(gen, [_port("if-a", None)]) == (0, 0)
        assert self._reconcile(gen, []) == (1, 0)
        assert self._reconcile(gen, [_port("if-a", ACCOUNT)]) == (0, 0)
        assert [w.get("query") for w in writes if "query" in w] == [ADD]

    def test_a_component_without_instances_removes_every_generated_port(self) -> None:
        """A component that exists but has no instance left is a desired set of nothing."""
        gen, _writes = self._gen(set(), components=_components())

        assert self._reconcile(gen, [_port("if-old", ACCOUNT)]) == (0, 1)

    def test_the_account_is_resolved_once_per_run(self) -> None:
        """Two reconciles on one generator ask AccountProfile once."""
        gen, writes = self._gen({"if-a"})

        self._reconcile(gen, [])
        self._reconcile(gen, [_port("if-gone", ACCOUNT)])

        assert [w for w in writes if w.get("account")] == [{"account": True}]


class TestTerminatedServicePorts:
    """A segment terminated on a firewall context is tagged on the border service ports facing its pair, in the same
    reconcile as the component ports and with the same provenance."""

    @staticmethod
    def _context(*caps: dict[str, Any]) -> dict[str, Any]:
        """The cleaned ``terminated_on`` of a state segment, its capabilities being ``caps``."""
        return {"terminated_on": {"id": "ctx-1", "capabilities": list(caps)}}

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
        """No ``terminated_on`` (or one with no capabilities): nothing to tag."""
        assert SegmentPortTagMixin._terminated_service_port_ids({}) == set()
        assert SegmentPortTagMixin._terminated_service_port_ids({"terminated_on": None}) == set()
        assert SegmentPortTagMixin._terminated_service_port_ids(self._context()) == set()

    def test_the_service_port_is_added_next_to_the_component_ports_with_the_account_as_source(self) -> None:
        """The border port is a desired port like a host-facing one: one RelationshipAdd on the PORT, source set."""
        gen, writes = TestPortProvenance._gen({"if-a"})

        assert TestPortProvenance._reconcile(gen, [], {"bl-1"}) == (2, 0)

        added = [w["variables"]["data"] for w in writes if w.get("query") == ADD]
        assert {data["id"] for data in added} == {"if-a", "bl-1"}
        assert all(data["nodes"] == [{"id": "seg-1", "_relation__source": ACCOUNT}] for data in added)

    def test_a_segment_with_no_component_keeps_its_service_ports(self) -> None:
        """No instance lands anywhere, yet the generated service port is still desired: nothing is removed."""
        gen, writes = TestPortProvenance._gen(set(), components=[])

        assert TestPortProvenance._reconcile(gen, [_port("bl-1", ACCOUNT)], {"bl-1"}) == (0, 0)
        assert [w for w in writes if "query" in w] == []

    def test_a_service_port_is_removed_when_the_segment_leaves_the_context(self) -> None:
        """terminated_on cleared or moved: the port this account wrote and no context derives any more goes."""
        gen, writes = TestPortProvenance._gen(set(), components=[])

        assert TestPortProvenance._reconcile(gen, [_port("bl-old", ACCOUNT)], set()) == (0, 1)
        assert writes[-1]["query"] == REMOVE

    def test_a_person_edge_on_a_service_port_is_never_removed(self) -> None:
        """A sourceless edge on a border port stays when the segment is not (or no longer) terminated."""
        gen, writes = TestPortProvenance._gen(set(), components=[])

        assert TestPortProvenance._reconcile(gen, [_port("bl-1", None)], set()) == (0, 0)
        assert [w for w in writes if "query" in w] == []

    def test_reconcile_hands_the_context_ports_to_the_tag_step(self) -> None:
        """reconcile_vxlan_segment derives the ids from the state's segment, in the same call as the ports."""
        gen = TestReconcileVxlanSegment._ready(changed=False)
        gen._fetch_segment_vlan_state.return_value = {
            "segment": {"id": "seg-1", "ports": [], **self._context(self._cap("bl-1"))}
        }

        asyncio.run(gen.reconcile_vxlan_segment("seg-1", "seg"))

        assert gen._reconcile_segment_interface_tags.await_args.kwargs["terminated_port_ids"] == {"bl-1"}


class TestReconcileVxlanSegment:
    @staticmethod
    def _ready(*, changed: bool) -> Any:
        gen = _gen()
        gen._fetch_segment_vlan_state = AsyncMock(
            return_value={"segment": {"id": "seg-1", "ports": [{"id": "if-a", "device_id": "dev", "source_id": None}]}}
        )
        gen._reconcile_segment_interface_tags = AsyncMock(
            side_effect=lambda **_: gen.events.append("tags") or ((1, 0) if changed else (0, 0))
        )
        gen._reconcile_segment_vlans_locked = AsyncMock(
            side_effect=lambda *_a, **_k: gen.events.append("rows") or False
        )
        # The VRF pass over the domains' devices has its own tests (test_vrf_callers.py).
        gen.reconcile_vrfs = AsyncMock()
        return gen

    def test_tags_then_rows_under_one_segment_lock(self) -> None:
        """The rows are derived from the tags just written, inside the same lock."""
        gen = self._ready(changed=True)

        result = asyncio.run(gen.reconcile_vxlan_segment("seg-1", "seg"))

        assert result == (1, 0)
        assert gen.events == [
            f"lock:{segment_lock_key('seg-1')}",
            "tags",
            "rows",
            f"unlock:{segment_lock_key('seg-1')}",
        ]

    def test_the_state_is_read_again_only_when_a_tag_changed(self) -> None:
        """The rows are reconciled from a fresh read after a tag write, and from the first read otherwise."""
        changed = self._ready(changed=True)
        asyncio.run(changed.reconcile_vxlan_segment("seg-1", "seg"))
        unchanged = self._ready(changed=False)
        asyncio.run(unchanged.reconcile_vxlan_segment("seg-1", "seg"))

        assert changed._fetch_segment_vlan_state.await_count == 2
        assert unchanged._fetch_segment_vlan_state.await_count == 1

    def test_the_tag_step_starts_from_the_customer_ports_the_segment_has(self) -> None:
        """ports are the segment's switch ports (with their edge source) as the state query returned them."""
        gen = self._ready(changed=False)

        asyncio.run(gen.reconcile_vxlan_segment("seg-1", "seg"))

        assert gen._reconcile_segment_interface_tags.await_args.kwargs["ports"] == [
            {"id": "if-a", "device_id": "dev", "source_id": None}
        ]

    def test_a_segment_that_is_no_vxlan_segment_is_left_alone(self) -> None:
        """No state (a cloud segment id, or a deleted segment): None, no tag, no row."""
        gen = _gen()
        gen._fetch_segment_vlan_state = AsyncMock(return_value=None)
        gen._reconcile_segment_interface_tags = AsyncMock()
        gen._reconcile_segment_vlans_locked = AsyncMock()

        assert asyncio.run(gen.reconcile_vxlan_segment("seg-1", "seg")) is None
        gen._reconcile_segment_interface_tags.assert_not_awaited()
        gen._reconcile_segment_vlans_locked.assert_not_awaited()
