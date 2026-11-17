"""The security and routing tags AppApplicationGenerator plans with its rules (generators/applications/application_security.py),
applied on the HFID fake."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from generators.model.errors import GeneratorError
from tests.unit.app_application_generator_helpers import _app, _backend, _dep, _frontend, _segment
from tests.unit.app_plan_helpers import AppClient, make_app_generator

UNTRACKED = {"allow_upsert": True, "update_group_context": False}


def run(app: dict[str, Any], client: AppClient | None = None) -> tuple[Any, AppClient]:
    """The application reconciled on ``client`` (a fresh one by default): (generator, client)."""
    client = client or AppClient()
    gen = make_app_generator(client)
    asyncio.run(gen._reconcile_application_rules(app))
    return gen, client


def tag_id(client: AppClient, name: str) -> str:
    """The id of the SecurityTag ``name`` the run saved."""
    (node,) = [node for node in client.store.values() if node.kind == "SecurityTag" and node.data["name"] == name]
    return node.id


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
    """Every component of a microsegmented segment gets its tag in the plan that writes the rules."""

    def test_components_and_edge_target_dicts_get_the_tags_and_the_rule_carries_them(self) -> None:
        """One tag per component, drawn from the pool when created, untracked; both dicts and the rule name them."""
        app = _c013_app()

        _, client = run(app)

        scoring, cache = app["children"]
        assert sorted(data["name"] for data in client.saved("SecurityTag")) == [
            "cache.fraud-detection.c013.demo.local",
            "scoring.fraud-detection.c013.demo.local",
        ]
        assert all(data["group_id"] == {"from_pool": {"id": "pool-tags"}} for data in client.saved("SecurityTag"))
        assert client.save_kwargs("SecurityTag") == [UNTRACKED] * 2
        scoring_tag = tag_id(client, "scoring.fraud-detection.c013.demo.local")
        cache_tag = tag_id(client, "cache.fraud-detection.c013.demo.local")
        assert sorted(client.saved("AppComponent"), key=lambda data: data["id"]) == [
            {"id": "comp-cache", "security_tag": {"id": cache_tag}},
            {"id": "comp-scoring", "security_tag": {"id": scoring_tag}},
        ]
        assert client.save_kwargs("AppComponent") == [UNTRACKED] * 2
        assert scoring["security_tag"] == {"id": scoring["security_tag"]["id"]}
        assert scoring["depends_on"][0]["target"]["security_tag"] == cache["security_tag"]
        (rule,) = client.saved("SecurityPolicyRule")
        assert rule["source_security_tag"] == {"id": scoring_tag}
        assert rule["destination_security_tag"] == {"id": cache_tag}
        assert rule["apply_on_switch"] is True

    def test_a_second_run_over_stale_data_finds_the_tags_and_draws_no_group_id(self) -> None:
        """The tags exist when the run reads components that still show none: found by name, never drawn again."""
        _, client = run(_c013_app())
        client.saves.clear()

        run(_c013_app(), client)

        assert len([node for node in client.store.values() if node.kind == "SecurityTag"]) == 2
        assert client.saved("SecurityTag") == []

    def test_components_that_carry_their_tags_cause_no_tag_write_and_no_pool_read(self) -> None:
        """Second pass over fresh data: only the rule is written."""
        _, client = run(_c013_app(tagged=True))

        assert {kind for kind, *_ in client.saves} <= {"SecurityPolicy", "SecurityPolicyRule"}
        (rule,) = client.saved("SecurityPolicyRule")
        assert rule["source_security_tag"] == {"id": "tag-scoring"}
        assert rule["destination_security_tag"] == {"id": "tag-cache"}

    def test_components_of_a_normal_segment_get_no_tag(self) -> None:
        _, client = run(_app(_frontend(_dep(target=_backend()))))

        assert client.saved("SecurityTag") == []
        assert client.saved("AppComponent") == []

    def test_a_component_that_left_its_microsegmented_segment_loses_its_tag(self) -> None:
        component = {"id": "comp-1", "network_segment": _segment("seg-n"), "security_tag": {"id": "tag-old"}}

        _, client = run(_app(component))

        assert client.saved("AppComponent") == [{"id": "comp-1", "security_tag": None}]
        assert client.save_kwargs("AppComponent") == [UNTRACKED]
        assert component["security_tag"] is None

    def test_a_target_on_another_segment_is_not_tagged_here(self) -> None:
        """A target outside this application's microsegmented segments is its own application's to tag."""
        child = {
            "id": "comp-1",
            "fqdn": "c1.example",
            "network_segment": _micro_segment("seg-m"),
            "depends_on": [
                _dep(
                    target={
                        "id": "comp-x",
                        "fqdn": "x.example",
                        "ports": ["tcp/1"],
                        "network_segment": _micro_segment("seg-other"),
                    }
                )
            ],
        }
        target = child["depends_on"][0]["target"]

        _, client = run(_app(child))

        assert [data["id"] for data in client.saved("AppComponent")] == ["comp-1"]
        assert "security_tag" not in target

    def test_a_component_without_a_name_fails_the_run_before_any_write(self) -> None:
        child = {"id": "comp-1", "network_segment": _micro_segment("seg-m")}
        client = AppClient()

        with pytest.raises(ValueError, match="needs an id and a name"):
            run(_app(child), client)

        assert client.saves == []

    def test_a_missing_tag_pool_fails_the_run_before_any_write(self) -> None:
        client = AppClient()
        client.tag_profiles = []

        with pytest.raises(LookupError, match="No group tag pool"):
            run(_c013_app(), client)

        assert client.saves == []


def _tenant(segment: dict[str, Any], tag_id: str | None = None) -> dict[str, Any]:
    """``segment`` as a routed PROD tenant segment (gateway namespace type prod), tagged."""
    return {
        **segment,
        "security_zone": {"name": "PROD", "vrf_type": "prod", "l3_vni": 60000},
        "gateway": {
            "ip_prefix": {
                "security_zone": {"name": "PROD", "vrf_type": "prod"},
                "prefix": "10.60.0.0/24",
                "ip_namespace": {"node": {"name": {"value": "default"}}},
            }
        },
        "security_tag": {"id": tag_id} if tag_id else None,
        "routing_tag": 7 if tag_id else None,
    }


def _app_on_tenant_segments(tag_id: str | None = None) -> dict[str, Any]:
    backend = _backend(ports=["tcp/8443"])
    backend["network_segment"] = _tenant(backend["network_segment"], tag_id)
    frontend = _frontend(_dep(target=backend))
    frontend["network_segment"] = _tenant(frontend["network_segment"], tag_id)
    return _app(frontend)


class TestSegmentTags:
    """Every routed tenant segment the application touches gets its group tag and its routing tag, before any rule."""

    def test_the_segments_are_tagged_and_numbered_once_each_before_the_rules(self) -> None:
        _, client = run(_app_on_tenant_segments())

        names = [data["name"] for data in client.saved("SecurityTag")]
        assert names == ["seg-src-name", "seg-dst-name"]
        assert {data["id"] for data in client.saved("ManagedVxlanSegment")} == {"seg-dst", "seg-src"}
        kinds = [kind for kind, *_ in client.saves]
        assert kinds.index("SecurityPolicyRule") > max(
            index for index, kind in enumerate(kinds) if kind in {"SecurityTag", "ManagedVxlanSegment"}
        )
        routing = [data for data in client.saved("ManagedVxlanSegment") if "routing_tag" in data]
        assert sorted(data["id"] for data in routing) == ["seg-dst", "seg-src"]
        tagged = [data for data in client.saved("ManagedVxlanSegment") if "security_tag" in data]
        assert sorted(data["id"] for data in tagged) == ["seg-dst", "seg-src"]

    def test_segments_that_carry_both_tags_cause_no_tag_write(self) -> None:
        _, client = run(_app_on_tenant_segments(tag_id="tag-seg"))

        assert {kind for kind, *_ in client.saves} <= {"SecurityPolicy", "SecurityPolicyRule"}

    def test_a_segment_dict_met_twice_is_tagged_once(self) -> None:
        """Two components on one segment: one tag, one write of the segment."""
        first = {"id": "c-1", "network_segment": _tenant(_segment("seg-1"))}
        second = {"id": "c-2", "network_segment": _tenant(_segment("seg-1"))}

        _, client = run(_app(first, second))

        assert [data["name"] for data in client.saved("SecurityTag")] == ["seg-1-name"]
        assert len([data for data in client.saved("ManagedVxlanSegment") if "security_tag" in data]) == 1

    def test_a_missing_routing_tag_pool_fails_the_run_before_any_write(self) -> None:
        """An untagged routed segment and no Segment-Routing-Tag pool: the draw comes first, nothing else is written."""
        client = AppClient()
        client.missing_number_pools.add("Segment-Routing-Tag")

        with pytest.raises(GeneratorError, match="Segment-Routing-Tag"):
            run(_app_on_tenant_segments(), client)

        assert client.saves == []


class TestCrossSegmentComponentTags:
    """A dependency into or out of a microsegmented segment names its component's tag on that side."""

    def test_the_target_dict_of_an_own_segment_reads_the_mode_derived_in_this_run(self) -> None:
        """web -> backend, both this application's, fintech_strict: the target's segment dict was read as normal, the
        rule is on the switch and names both tags."""
        backend = {**_backend(ports=["tcp/8443"]), "fqdn": "backend.example"}
        frontend = {
            **_frontend(_dep(target={**backend, "network_segment": _segment("seg-dst")})),
            "fqdn": "front.example",
        }

        _, client = run(_app(frontend, backend, profile="fintech_strict"))

        (rule,) = client.saved("SecurityPolicyRule")
        assert rule["apply_on_switch"] is True
        assert rule["destination_security_tag"] == {"id": tag_id(client, "backend.example")}
        assert rule["source_security_tag"] == {"id": tag_id(client, "front.example")}

    def test_a_newly_tagged_component_re_runs_its_calling_applications(self) -> None:
        """C005 tags web for the first time: C008's rule into it must name the tag, so C008 runs again, after the write."""
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

        gen, client = run(_app(web))

        gen.run_generator.assert_awaited_once_with("add_app_application", ["app-c008"], wait=False)
        assert [data["name"] for data in client.saved("SecurityTag")] == ["api.payment-core.c005.demo.local"]

    def test_a_component_that_had_its_tag_re_runs_nothing(self) -> None:
        """Second pass: the tag was read from the query, the callers already name it."""
        web = {
            "id": "comp-web",
            "fqdn": "api.payment-core.c005.demo.local",
            "network_segment": _micro_segment("seg-web"),
            "security_tag": {"id": "tag-web"},
            "dependents": [{"source": {"id": "comp-portal", "parent": {"id": "app-c008"}}}],
            "parent": {"id": "app-c005"},
        }

        gen, _ = run(_app(web))

        gen.run_generator.assert_not_awaited()
