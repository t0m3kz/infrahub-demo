"""Unit tests for the security tags AppApplicationGenerator derives
(generators/topology/application_security.py): component tags, segment tags and
cross-segment component tags.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from generators.protocols import AppComponent
from tests.unit.app_application_generator_helpers import _app, _backend, _dep, _frontend, _make_gen, _segment

# ===========================================================================
# TestComponentTags
# ===========================================================================


def _micro_segment(seg_id: str = "seg-m") -> dict[str, Any]:
    return {**_segment(seg_id, "ManagedVxlanSegment"), "isolation_mode": "microsegmented"}


def _c013_app(*, tagged: bool = False) -> dict[str, Any]:
    """fraud-detection: scoring-api -> feature-cache on one microsegmented segment."""
    cache = {
        "id": "comp-cache",
        "name": "feature-cache",
        "fqdn": "cache.fraud-detection.c013.demo.local",
        "ports": ["tcp/6379"],
        "component_type": "cache",
        "network_segment": _micro_segment(),
        "security_tag": {"id": "tag-cache"} if tagged else None,
    }
    scoring = {
        "id": "comp-scoring",
        "name": "scoring-api",
        "fqdn": "scoring.fraud-detection.c013.demo.local",
        "ports": ["tcp/8443"],
        "component_type": "backend",
        "network_segment": _micro_segment(),
        "security_tag": {"id": "tag-scoring"} if tagged else None,
        # The target is a second dict of the cache component, as application.gql returns it.
        "depends_on": [_dep("scoring-to-cache", target={**cache, "network_segment": _micro_segment()})],
    }
    return _app(scoring, cache, profile="fintech_strict")


class TestComponentTags:
    """Every component of a microsegmented segment gets its tag before the rules are planned."""

    @staticmethod
    def _gen() -> Any:
        gen = _make_gen()
        gen._ensure_segment_isolation_mode = AsyncMock()
        gen._reconcile_private_access_components = AsyncMock(return_value=(0, 0))
        gen._reconcile_segment_rule = AsyncMock(return_value=(True, False))
        gen.resource_lock = MagicMock()
        return gen

    def test_components_and_edge_target_dicts_get_the_tag_ids(self) -> None:
        """The child and the edge-target dict of the same component carry the same tag id."""
        gen = self._gen()
        app = _c013_app()
        ensure = AsyncMock(side_effect=lambda client, lock, *, kind, node, name: f"tag-{node['id']}")

        with patch("generators.topology.application_security.ensure_security_tag", ensure):
            asyncio.run(gen._reconcile_application_rules(app))

        assert sorted(call.kwargs["node"]["id"] for call in ensure.await_args_list) == [
            "comp-cache",
            "comp-scoring",
        ]
        assert {call.kwargs["kind"] for call in ensure.await_args_list} == {AppComponent}
        assert sorted(call.kwargs["name"] for call in ensure.await_args_list) == [
            "cache.fraud-detection.c013.demo.local",
            "scoring.fraud-detection.c013.demo.local",
        ]
        scoring, cache = app["children"]
        assert scoring["security_tag"] == {"id": "tag-comp-scoring"}
        assert cache["security_tag"] == {"id": "tag-comp-cache"}
        assert scoring["depends_on"][0]["target"]["security_tag"] == {"id": "tag-comp-cache"}
        rule_call = gen._reconcile_segment_rule.await_args
        assert rule_call.kwargs["dst_comp"]["security_tag"] == {"id": "tag-comp-cache"}

    def test_components_of_a_normal_segment_get_no_tag(self) -> None:
        gen = self._gen()
        ensure = AsyncMock()

        with patch("generators.topology.application_security.ensure_security_tag", ensure):
            asyncio.run(gen._reconcile_application_rules(_app(_frontend(_dep(target=_backend())))))

        ensure.assert_not_awaited()

    def test_component_that_left_its_microsegmented_segment_loses_its_tag(self) -> None:
        gen = self._gen()
        component = {"id": "comp-1", "network_segment": _segment("seg-n"), "security_tag": {"id": "tag-old"}}
        clear = AsyncMock()

        with patch("generators.topology.application_security.clear_component_tag", clear):
            asyncio.run(gen._ensure_component_tags([component], []))

        clear.assert_awaited_once_with(gen.client, component_id="comp-1")
        assert component["security_tag"] is None

    def test_target_on_another_segment_is_not_tagged_here(self) -> None:
        """A target outside this application's microsegmented segments is its own application's to tag."""
        gen = self._gen()
        child = {"id": "comp-1", "network_segment": _micro_segment("seg-m")}
        target = {"id": "comp-x", "network_segment": _micro_segment("seg-other")}
        ensure = AsyncMock(return_value="tag-1")

        with patch("generators.topology.application_security.ensure_security_tag", ensure):
            asyncio.run(gen._ensure_component_tags([child], [(child, {}, target)]))

        assert [call.kwargs["node"]["id"] for call in ensure.await_args_list] == ["comp-1"]
        assert "security_tag" not in target

    def test_tag_failure_stops_the_application_before_any_rule(self) -> None:
        """Errors propagate: no rule is planned without its tags."""
        gen = self._gen()
        ensure = AsyncMock(side_effect=RuntimeError("SGT-POOL: no group_id allocated"))

        with (
            patch("generators.topology.application_security.ensure_security_tag", ensure),
            pytest.raises(RuntimeError, match="SGT-POOL"),
        ):
            asyncio.run(gen._reconcile_application_rules(_c013_app()))

        gen._reconcile_segment_rule.assert_not_awaited()

    def test_second_run_over_tagged_components_writes_no_tag(self) -> None:
        """AC1: with the real helper, components that carry their tags cause no lookup, lock or write."""
        gen = self._gen()
        gen.client = MagicMock()
        gen.client.create = AsyncMock()
        gen.client.filters = AsyncMock()

        asyncio.run(gen._reconcile_application_rules(_c013_app(tagged=True)))

        gen.client.create.assert_not_awaited()
        gen.client.filters.assert_not_awaited()
        gen.resource_lock.assert_not_called()
        rule_kwargs = gen._reconcile_segment_rule.await_args.kwargs
        assert rule_kwargs["src_comp"]["security_tag"] == {"id": "tag-scoring"}
        assert rule_kwargs["dst_comp"]["security_tag"] == {"id": "tag-cache"}

    def test_application_query_reads_the_tag_of_components_edge_targets_and_segments(self, root_dir: Path) -> None:
        """The fast path needs the tag of every child, every dependency target and every segment."""
        query = (root_dir / "queries/topology/add/application.gql").read_text()
        assert query.count("security_tag { node { id } }") == 3
        fragment = query.split("fragment AppComponentSecurityFields")[0]
        assert fragment.count("security_tag { node { id } }") == 1
        assert "namespace_type { value }" in fragment


# ===========================================================================
# TestSegmentTags
# ===========================================================================


def _tenant(segment: dict[str, Any], tag_id: str | None = None) -> dict[str, Any]:
    """``segment`` as a routed PROD tenant segment (gateway namespace type prod), tagged (group tag and routing
    tag) or not."""
    return {
        **segment,
        "gateway": {
            "ip_prefix": {"prefix": "10.60.0.0/24", "ip_namespace": {"name": "PROD", "namespace_type": "prod"}}
        },
        "security_tag": {"id": tag_id} if tag_id else None,
        "routing_tag": 7 if tag_id else None,
    }


class TestSegmentTags:
    """Every routed tenant segment the application touches gets its group tag, after its zone, before any rule."""

    @staticmethod
    def _gen() -> Any:
        gen = _make_gen()
        gen._ensure_segment_isolation_mode = AsyncMock()
        gen._reconcile_private_access_components = AsyncMock(return_value=(0, 0))
        gen.resource_lock = MagicMock()
        return gen

    @staticmethod
    def _app_on_tenant_segments(tag_id: str | None = None) -> dict[str, Any]:
        backend = _backend(ports=["tcp/8443"])
        backend["network_segment"] = _tenant(backend["network_segment"], tag_id)
        frontend = _frontend(_dep(target=backend))
        frontend["network_segment"] = _tenant(frontend["network_segment"], tag_id)
        return _app(frontend)

    def test_component_and_edge_target_segments_are_tagged_after_zones_before_rules(self) -> None:
        """The application run gives each segment it touches its group tag and its routing tag, the edge
        target's segment included."""
        gen = self._gen()
        order: list[str] = []
        gen._ensure_segment_zones = AsyncMock(side_effect=lambda segments: order.append("zones"))
        gen._reconcile_segment_rule = AsyncMock(side_effect=lambda **kw: order.append("rule") or (True, False))
        ensure = AsyncMock(side_effect=lambda client, lock, *, segment: order.append(f"tag {segment['id']}"))
        routing = AsyncMock(side_effect=lambda client, *, segment: order.append(f"routing {segment['id']}"))

        with (
            patch("generators.topology.application_security.ensure_segment_tag", ensure),
            patch("generators.topology.application_security.ensure_segment_routing_tag", routing),
        ):
            asyncio.run(gen._reconcile_application_rules(self._app_on_tenant_segments()))

        assert order == ["zones", "tag seg-src", "routing seg-src", "tag seg-dst", "routing seg-dst", "rule"]
        assert all(call.args == (gen.client, gen.resource_lock) for call in ensure.await_args_list)
        assert all(call.args == (gen.client,) for call in routing.await_args_list)

    def test_second_run_over_tagged_segments_writes_nothing(self) -> None:
        """With the real helpers, segments that carry both tags cause no lookup, lock or write."""
        gen = self._gen()
        gen.client = MagicMock()
        gen.client.create = AsyncMock()
        gen.client.filters = AsyncMock()
        gen.client.get = AsyncMock()
        gen._reconcile_segment_rule = AsyncMock(return_value=(False, True))

        asyncio.run(gen._reconcile_application_rules(self._app_on_tenant_segments(tag_id="tag-seg")))

        gen.client.create.assert_not_awaited()
        gen.client.filters.assert_not_awaited()
        gen.client.get.assert_not_awaited()
        gen.resource_lock.assert_not_called()

    def test_segment_tag_failure_stops_the_application_before_any_rule(self) -> None:
        """A pool that cannot be drawn from fails the run; no rule is planned."""
        gen = self._gen()
        gen._reconcile_segment_rule = AsyncMock()
        ensure = AsyncMock(side_effect=LookupError("No group tag pool"))

        with (
            patch("generators.topology.application_security.ensure_segment_tag", ensure),
            pytest.raises(LookupError, match="No group tag pool"),
        ):
            asyncio.run(gen._reconcile_application_rules(self._app_on_tenant_segments()))

        gen._reconcile_segment_rule.assert_not_awaited()

    def test_routing_tag_pool_missing_stops_the_application_before_any_rule(self) -> None:
        """An untagged routed segment and no Segment-Routing-Tag pool: the run fails, no rule is planned."""
        gen = self._gen()
        gen.client = MagicMock()
        gen.client.get = AsyncMock(return_value=None)
        gen._reconcile_segment_rule = AsyncMock()
        group_tag = AsyncMock()

        with (
            patch("generators.topology.application_security.ensure_segment_tag", group_tag),
            pytest.raises(LookupError, match="Segment-Routing-Tag"),
        ):
            asyncio.run(gen._reconcile_application_rules(self._app_on_tenant_segments()))

        gen._reconcile_segment_rule.assert_not_awaited()


# ===========================================================================
# TestCrossSegmentComponentTags
# ===========================================================================


class TestCrossSegmentComponentTags:
    """A dependency into or out of a microsegmented segment names its component's tag on that side."""

    @staticmethod
    def _gen() -> Any:
        gen = _make_gen()
        gen._reconcile_private_access_components = AsyncMock(return_value=(0, 0))
        gen._reconcile_segment_rule = AsyncMock(return_value=(True, False))
        gen.resource_lock = MagicMock()
        gen.run_generator = AsyncMock()
        return gen

    def test_target_dict_of_an_own_segment_reads_the_mode_derived_in_this_run(self) -> None:
        """web -> backend, both this application's: the target's segment dict was read as normal,
        so the rule (apply_on_switch, tags) is planned against the mode just derived for it."""
        gen = self._gen()

        async def _derive(segment: dict[str, Any], _profile: str) -> None:
            segment["isolation_mode"] = "microsegmented"

        gen._ensure_segment_isolation_mode = AsyncMock(side_effect=_derive)
        backend = _backend(ports=["tcp/8443"])
        app = _app(_frontend(_dep(target={**backend, "network_segment": _segment("seg-dst")})), backend)

        with patch("generators.topology.application_security.ensure_security_tag", AsyncMock(return_value="tag-x")):
            asyncio.run(gen._reconcile_application_rules(app))

        dst_seg = gen._reconcile_segment_rule.await_args.kwargs["dst_seg"]
        assert dst_seg["isolation_mode"] == "microsegmented"

    def test_newly_tagged_component_re_runs_its_calling_applications(self) -> None:
        """C005 tags web for the first time: C008's rule into it must name the tag, so C008 runs again."""
        gen = self._gen()
        gen._ensure_segment_isolation_mode = AsyncMock()
        web = {
            "id": "comp-web",
            "fqdn": "api.payment-core.c005.demo.local",
            "network_segment": _micro_segment("seg-web"),
            "security_tag": None,
            "dependents": [
                {"source": {"id": "comp-portal", "parent": {"id": "app-c008"}}},
                {"source": {"id": "comp-own", "parent": {"id": "app-c005"}}},
            ],
            "parent": {"id": "app-c005"},
        }

        with patch("generators.topology.application_security.ensure_security_tag", AsyncMock(return_value="tag-web")):
            asyncio.run(gen._ensure_component_tags([web], []))

        gen.run_generator.assert_awaited_once_with("add_app_application", ["app-c008"], wait=False)

    def test_component_that_had_its_tag_re_runs_nothing(self) -> None:
        """Second pass: the tag was read from the query, the callers already name it."""
        gen = self._gen()
        web = {
            "id": "comp-web",
            "fqdn": "api.payment-core.c005.demo.local",
            "network_segment": _micro_segment("seg-web"),
            "security_tag": {"id": "tag-web"},
            "dependents": [{"source": {"id": "comp-portal", "parent": {"id": "app-c008"}}}],
            "parent": {"id": "app-c005"},
        }

        with patch("generators.topology.application_security.ensure_security_tag", AsyncMock(return_value="tag-web")):
            asyncio.run(gen._ensure_component_tags([web], []))

        gen.run_generator.assert_not_awaited()

    def test_application_query_reads_the_calling_application_of_a_component(self, root_dir: Path) -> None:
        """The fan-out reads dependents' source application (AppApplicationGenerator._calling_application_ids)."""
        query = (root_dir / "queries/topology/add/application.gql").read_text()
        dependents = query.split("dependents {", 1)[1].split("depends_on {", 1)[0]
        # `__typename` is required: the live server cannot resolve a hierarchical parent without it
        # ("Unable to identify the type of the instance"), which failed every add_app_application run.
        assert "source { node { id ... on AppComponent { parent { node { id __typename } } } } }" in dependents
