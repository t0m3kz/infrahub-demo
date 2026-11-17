"""generators/helpers/security_tag.py: the group tags of segments and components, as plan specs from the profile's tag
pool, and the flow of a generator that tags a segment alone."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest

from generators.helpers.security_tag import (
    TagTarget,
    component_target,
    ensure_segment_tag,
    plan_clear_component_tag,
    plan_security_tags,
    read_tag_pool,
    segment_target,
    tag_lock_keys,
)
from generators.plan import Hfid, NodeRef, NodeSpec, TopologyPlan
from generators.protocols import AppComponent, SecurityTag
from tests.unit.app_plan_helpers import AppClient, make_app_generator

FQDN = "cache.fraud-detection.c013.demo.local"
SEGMENT = "c005-web-frontend-local-dc10-p"


def _component(tag_id: str | None = None) -> dict[str, Any]:
    return {"id": "comp-1", "fqdn": FQDN, "security_tag": {"id": tag_id} if tag_id else None}


def _segment(
    *,
    kind: str = "ManagedVxlanSegment",
    vrf_type: str | None = "prod",
    zone: str | None = "PROD",
    tag_id: str | None = None,
) -> dict[str, Any]:
    """A cleaned segment as vxlan_segment.gql / application.gql select it (routed PROD tenant by default)."""
    segment: dict[str, Any] = {
        "id": "seg-1",
        "typename": kind,
        "name": SEGMENT,
        "security_zone": {"name": zone, "vrf_type": vrf_type} if zone else None,
        "security_tag": {"id": tag_id} if tag_id else None,
    }
    if vrf_type:
        segment["gateway"] = {"ip_prefix": {"ip_namespace": {"name": "default"}}}
    return segment


def _plan(*targets: TagTarget, pool_id: str | None = "pool-sgt") -> tuple[TopologyPlan, dict[str, Any]]:
    plan = TopologyPlan(root_id=None)
    return plan, plan_security_tags(plan, list(targets), pool_id)


class TestPlanSecurityTags:
    def test_a_node_that_has_its_tag_writes_nothing(self) -> None:
        component = _component("tag-1")

        plan, tags = _plan(component_target(component))

        assert plan.specs == []
        assert tags == {"comp-1": "tag-1"}

    def test_a_new_tag_is_drawn_from_the_pool_when_created_and_the_node_points_at_it_untracked(self) -> None:
        component = _component()

        plan, tags = _plan(component_target(component))

        tag, tagged = plan.of(NodeSpec)
        assert tag == NodeSpec(
            f"tag-{FQDN}",
            SecurityTag,
            {"name": FQDN},
            hfid=Hfid("SecurityTag", (FQDN,)),
            ownership="shared",
            create_only={"group_id": {"from_pool": {"id": "pool-sgt"}}},
            sequential=True,
        )
        assert tagged == NodeSpec(
            "tagged-comp-1",
            AppComponent,
            {"id": "comp-1", "security_tag": {"id": NodeRef(f"tag-{FQDN}")}},
            ownership="shared",
        )
        assert tags == {"comp-1": NodeRef(f"tag-{FQDN}")}
        assert component["security_tag"] == {"id": NodeRef(f"tag-{FQDN}")}

    def test_a_node_met_twice_is_tagged_once_and_both_dicts_carry_the_tag(self) -> None:
        first, second = _component(), _component()

        plan, _ = _plan(component_target(first), component_target(second))

        assert len(plan.specs) == 2
        assert first["security_tag"] == second["security_tag"] == {"id": NodeRef(f"tag-{FQDN}")}

    def test_a_second_copy_of_a_tagged_node_takes_the_tag_the_first_has(self) -> None:
        plan, _ = _plan(component_target(_component("tag-1")), component_target(copy := _component()))

        assert plan.specs == []
        assert copy["security_tag"] == {"id": "tag-1"}

    @pytest.mark.parametrize("component", [{"id": "comp-1"}, {"fqdn": FQDN}])
    def test_a_node_without_an_id_or_a_name_is_refused(self, component: dict[str, Any]) -> None:
        with pytest.raises(ValueError, match="needs an id and a name"):
            _plan(component_target(component))

    def test_no_pool_to_draw_from_is_refused(self) -> None:
        with pytest.raises(ValueError, match="no group tag pool"):
            _plan(component_target(_component()), pool_id=None)

    def test_the_lock_of_a_tag_is_its_name(self) -> None:
        assert tag_lock_keys([FQDN, None, ""]) == [f"sgt-{FQDN}"]


class TestSegmentTarget:
    """Every routed tenant segment, and nothing else, gets the group named after it."""

    @pytest.mark.parametrize("vrf_type", ["prod", "non_prod"])
    def test_a_routed_tenant_segment_is_tagged_with_its_name_on_its_own_kind(self, vrf_type: str) -> None:
        target = segment_target(_segment(vrf_type=vrf_type))
        assert target is not None

        plan, _ = _plan(target)

        tag, tagged = plan.of(NodeSpec)
        assert tag.data == {"name": SEGMENT}
        assert (tagged.kind, tagged.data["id"]) == ("ManagedVxlanSegment", "seg-1")

    @pytest.mark.parametrize(
        "segment",
        [
            _segment(vrf_type="management", zone="MANAGEMENT"),
            _segment(vrf_type="internet", zone="INTERNET"),
            _segment(zone="MANAGEMENT"),
            _segment(vrf_type=None),
            _segment(kind="CloudNetworkSegment"),
        ],
        ids=["management-vrf", "internet-vrf", "exchange-zone", "no-gateway", "cloud"],
    )
    def test_any_other_segment_is_never_tagged(self, segment: dict[str, Any]) -> None:
        assert segment_target(segment) is None


class TestReadTagPool:
    def test_the_pool_the_profiles_name(self) -> None:
        assert asyncio.run(read_tag_pool(AppClient())) == "pool-tags"

    @staticmethod
    def _client(profiles: list[Any]) -> Any:
        client = AppClient()
        client.tag_profiles = profiles
        return client

    @pytest.mark.parametrize("profiles", [[], [MagicMock(tag_pool=MagicMock(id=None))]])
    def test_no_profile_naming_a_pool_raises_lookup_error(self, profiles: list[Any]) -> None:
        """No row, or rows without a pool: nothing to draw from, fail closed."""
        with pytest.raises(LookupError, match="name 0 tag pools"):
            asyncio.run(read_tag_pool(self._client(profiles)))

    def test_profiles_naming_two_pools_raise_lookup_error(self) -> None:
        """Group IDs are fabric-wide: two pools would let one number mean two groups."""
        profiles = [
            MagicMock(tag_pool=MagicMock(id=f"pool-{n}", display_label=name))
            for n, name in enumerate(("SGT-POOL", "OTHER-POOL"))
        ]
        with pytest.raises(LookupError, match=r"name 2 tag pools \['OTHER-POOL', 'SGT-POOL'\]"):
            asyncio.run(read_tag_pool(self._client(profiles)))


def test_the_tag_of_a_component_that_left_its_segment_is_cleared_untracked() -> None:
    plan = TopologyPlan(root_id=None)

    plan_clear_component_tag(plan, "comp-1")

    assert plan.specs == [
        NodeSpec("untagged-comp-1", AppComponent, {"id": "comp-1"}, ownership="shared", clear=("security_tag",))
    ]


class TestEnsureSegmentTag:
    """The flow of a generator that tags one segment: its own lock, the pool read, one write."""

    @staticmethod
    def _run(segment: dict[str, Any], client: AppClient | None = None) -> tuple[Any, AppClient]:
        client = client or AppClient()
        gen = make_app_generator(client)
        asyncio.run(ensure_segment_tag(gen, segment=segment))
        return gen, client

    def test_a_routed_tenant_segment_is_tagged_under_the_lock_of_its_name(self) -> None:
        gen, client = self._run(_segment())

        assert gen.locked_keys == [f"sgt-{SEGMENT}"]
        (tag,) = client.saved("SecurityTag")
        assert tag == {"name": SEGMENT, "group_id": {"from_pool": {"id": "pool-tags"}}}
        assert client.save_kwargs("SecurityTag") == [{"allow_upsert": True, "update_group_context": False}]
        tag_id = next(node.id for node in client.store.values() if node.kind == "SecurityTag")
        assert client.saved("ManagedVxlanSegment") == [{"id": "seg-1", "security_tag": {"id": tag_id}}]

    def test_a_tag_that_exists_is_found_and_not_drawn_again(self) -> None:
        client = AppClient()
        tag = client.seed("SecurityTag", name=SEGMENT, group_id=7)

        _, client = self._run(_segment(), client)

        assert client.saved("SecurityTag") == []
        assert client.saved("ManagedVxlanSegment") == [{"id": "seg-1", "security_tag": {"id": tag.id}}]

    def test_a_segment_that_has_its_tag_writes_nothing_and_takes_no_lock(self) -> None:
        gen, client = self._run(_segment(tag_id="tag-3"))

        assert client.saves == []
        assert gen.locked_keys == []

    def test_any_other_segment_is_never_tagged(self) -> None:
        gen, client = self._run(_segment(kind="CloudNetworkSegment"))

        assert client.saves == []
        assert gen.locked_keys == []

    def test_a_missing_pool_fails_before_any_write(self) -> None:
        client = AppClient()
        client.tag_profiles = []

        with pytest.raises(LookupError, match="No group tag pool"):
            self._run(_segment(), client)

        assert client.saves == []
