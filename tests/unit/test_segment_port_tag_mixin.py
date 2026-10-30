"""Unit tests for SegmentPortTagMixin (generators/topology/segment_ports.py): the segment owns the ports it is
tagged on and its VLAN rows, so add_vxlan_segment and add_app_component_segment run the same reconcile.

A port is tagged from the instances of the AppComponents on the segment; a port assigned by hand is removed with
them, unless the segment has no component at all (nothing to compare with). The tag step and the row step run under
one segment lock, in that order.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from generators.topology.app_instance_segment import AppInstanceSegmentGenerator
from generators.topology.segment import VxlanSegmentGenerator
from generators.topology.segment_ports import SegmentPortTagMixin
from generators.vlan_domain import segment_lock_key


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


def _segment_obj() -> MagicMock:
    """The segment node the tag step writes its RelationshipAdd/Remove through."""
    obj = MagicMock(id="seg-1")
    obj.add_relationships = AsyncMock()
    obj.remove_relationships = AsyncMock()
    return obj


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


class TestHandAssignedPorts:
    """A port put on the segment by hand (its `switched_interfaces`) is checked against the components' instances."""

    @staticmethod
    def _reconcile(gen: Any, current: set[str], segment_obj: MagicMock) -> tuple[int, int]:
        gen.client.get = AsyncMock(return_value=segment_obj)
        return asyncio.run(
            gen._reconcile_segment_interface_tags(segment_id="seg-1", segment_name="seg", current_ids=current)
        )

    def test_port_no_instance_lands_on_is_untagged(self) -> None:
        """The segment has a component whose instance lands on if-a: a hand-added if-x is removed."""
        gen = _gen()
        gen._segment_components = AsyncMock(return_value=_components("dev-1"))
        gen._customer_facing_target_ids = AsyncMock(return_value={"if-a"})
        segment_obj = _segment_obj()

        result = self._reconcile(gen, {"if-a", "if-x"}, segment_obj)

        assert result == (0, 1)
        segment_obj.remove_relationships.assert_awaited_once_with(
            relation_to_update="switched_interfaces", related_nodes=["if-x"]
        )
        segment_obj.add_relationships.assert_not_awaited()

    def test_a_desired_untagged_port_is_tagged_through_an_edge_add(self) -> None:
        """The instance lands on if-a, which the segment is not tagged on yet: one add_relationships, no remove."""
        gen = _gen()
        gen._segment_components = AsyncMock(return_value=_components("dev-1"))
        gen._customer_facing_target_ids = AsyncMock(return_value={"if-a"})
        segment_obj = _segment_obj()

        result = self._reconcile(gen, set(), segment_obj)

        assert result == (1, 0)
        segment_obj.add_relationships.assert_awaited_once_with(
            relation_to_update="switched_interfaces", related_nodes=["if-a"]
        )
        segment_obj.remove_relationships.assert_not_awaited()

    def test_a_segment_with_no_component_keeps_its_ports(self) -> None:
        """Nothing names where the segment is wanted: an authored port is not a stray one."""
        gen = _gen()
        gen._segment_components = AsyncMock(return_value=[])
        gen._customer_facing_target_ids = AsyncMock(return_value=set())
        segment_obj = _segment_obj()

        result = self._reconcile(gen, {"if-authored"}, segment_obj)

        assert result == (0, 0)
        segment_obj.remove_relationships.assert_not_awaited()
        segment_obj.add_relationships.assert_not_awaited()
        gen._customer_facing_target_ids.assert_not_awaited()

    def test_a_component_without_instances_untags_every_port(self) -> None:
        """A component that exists but has no instance left is a desired set of nothing: its ports go."""
        gen = _gen()
        gen._segment_components = AsyncMock(return_value=_components())
        gen._customer_facing_target_ids = AsyncMock(return_value=set())
        segment_obj = _segment_obj()

        assert self._reconcile(gen, {"if-old"}, segment_obj) == (0, 1)

    def test_a_matching_set_writes_nothing(self) -> None:
        """Idempotent: the tags already are the instances' ports."""
        gen = _gen()
        gen._segment_components = AsyncMock(return_value=_components("dev-1"))
        gen._customer_facing_target_ids = AsyncMock(return_value={"if-a"})
        segment_obj = _segment_obj()

        assert self._reconcile(gen, {"if-a"}, segment_obj) == (0, 0)
        segment_obj.add_relationships.assert_not_awaited()
        segment_obj.remove_relationships.assert_not_awaited()


class TestReconcileVxlanSegment:
    @staticmethod
    def _ready(*, changed: bool) -> Any:
        gen = _gen()
        gen._fetch_segment_vlan_state = AsyncMock(
            return_value={
                "segment": {"id": "seg-1", "switched_interfaces": [{"id": "if-a", "typename": "DcimPhysicalInterface"}]}
            }
        )
        gen._reconcile_segment_interface_tags = AsyncMock(
            side_effect=lambda **_: gen.events.append("tags") or ((1, 0) if changed else (0, 0))
        )
        gen._reconcile_segment_vlan_domains_locked = AsyncMock(
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
        """current_ids are the segment's switch ports as the state query returned them."""
        gen = self._ready(changed=False)

        asyncio.run(gen.reconcile_vxlan_segment("seg-1", "seg"))

        assert gen._reconcile_segment_interface_tags.await_args.kwargs["current_ids"] == {"if-a"}

    def test_a_segment_that_is_no_vxlan_segment_is_left_alone(self) -> None:
        """No state (a cloud segment id, or a deleted segment): None, no tag, no row."""
        gen = _gen()
        gen._fetch_segment_vlan_state = AsyncMock(return_value=None)
        gen._reconcile_segment_interface_tags = AsyncMock()
        gen._reconcile_segment_vlan_domains_locked = AsyncMock()

        assert asyncio.run(gen.reconcile_vxlan_segment("seg-1", "seg")) is None
        gen._reconcile_segment_interface_tags.assert_not_awaited()
        gen._reconcile_segment_vlan_domains_locked.assert_not_awaited()
