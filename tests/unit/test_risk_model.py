"""Unit tests for the change-risk model (checks/risk_model.py).

The traversal itself is Infrahub's, so what is worth testing here is everything
around it: that the declared edge set matches the schemas on disk, that the
shortlisting and confirming passes are folded into the graph correctly, that the
before/after branch comparison produces the severity it should, and that the
scoring turns that into the documented verdict.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

from checks.risk_model import (
    CONFIRM_KINDS,
    EXCLUDED_EDGES,
    MIN_CONFIDENCE,
    REVIEW_SCORE,
    SHARED_FATE_KINDS,
    SHORTLIST_KINDS,
    TRAVERSAL_EDGES,
    ChangedNode,
    ReachedNode,
    ReachSet,
    build_report,
    changes_from_diff,
    component_severity,
    index_exposure,
    resolve_relationship_filter,
    severity_of,
    shortlist_from_reachable,
)

REPO = Path(__file__).resolve().parents[2]


def _load_schemas() -> dict[str, Any]:
    """The YAML schema reader from `scripts/validate_query_fields.py`.

    Loaded by path because `scripts/` is a directory of standalone entry points,
    not an importable package, and putting it on `sys.path` for one helper would
    make every module in it importable from every test.
    """
    name = "validate_query_fields"
    if (cached := sys.modules.get(name)) is not None:
        return cached.load_schemas()
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered before execution because the script's `@dataclass`es use
    # postponed annotations, and `dataclasses` resolves those through
    # `sys.modules[cls.__module__]`.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module.load_schemas()


# ---------------------------------------------------------------------------
# Helpers — payloads in the shape the SDK's `model_dump()` produces
# ---------------------------------------------------------------------------

SOURCE = ChangedNode(id="src", kind="DcimPhysicalInterface", action="updated", label="leaf-01 Ethernet1")


def _path_node(node_id: str, kind: str) -> dict[str, Any]:
    return {"id": node_id, "kind": kind, "label": kind, "display_label": node_id, "hfid": []}


def _reachable_payload(dependencies: list[tuple[str, str]], source: ChangedNode = SOURCE) -> dict[str, Any]:
    """An `InfrahubReachableNodes` result, from [(id, kind), …]."""
    return {
        "source": _path_node(source.id, source.kind),
        "dependencies": [
            {"node": _path_node(node_id, kind), "depth": 1, "path": {"hops": [], "depth": 1}}
            for node_id, kind in dependencies
        ],
        "count": len(dependencies),
    }


def _paths_payload(
    hops: list[tuple[str, str]] | None,
    source: ChangedNode = SOURCE,
    destination: tuple[str, str] = ("comp1", "AppComponent"),
    truncated_at_depth: int | None = None,
) -> dict[str, Any]:
    """An `InfrahubPathTraversal` result. `hops` is the path, source included."""
    paths = []
    if hops:
        paths.append(
            {
                "hops": [
                    {
                        "node": _path_node(node_id, kind),
                        "relationship": None
                        if index == 0
                        else {
                            "from_rel": "out",
                            "from_label": "out",
                            "to_rel": "in",
                            "to_label": "in",
                            "kind": "edge",
                        },
                    }
                    for index, (node_id, kind) in enumerate(hops)
                ],
                "depth": len(hops) - 1,
            }
        )
    return {
        "paths": paths,
        "source": _path_node(source.id, source.kind),
        "destination": _path_node(*destination),
        "count": len(paths),
        "excluded_kinds": [],
        "truncated_at_depth": truncated_at_depth,
    }


def _reach(nodes: dict[str, int], kinds: dict[str, str] | None = None) -> ReachSet:
    """A ReachSet holding the given node ids at the given confirmed depths."""
    reach = ReachSet(sources={SOURCE.id: SOURCE.label}, kinds={SOURCE.id: SOURCE.kind})
    for node_id, depth in nodes.items():
        kind = (kinds or {}).get(node_id, "AppComponent")
        reach.nodes[node_id] = ReachedNode(id=node_id, kind=kind, label=node_id, depth=depth, path=[SOURCE.id, node_id])
        reach.kinds[node_id] = kind
    return reach


def _component(
    component_id: str,
    name: str,
    app_id: str,
    app_name: str,
    criticality: str,
    instances: list[str],
    segment_id: str | None = None,
) -> dict[str, Any]:
    return {
        "id": component_id,
        "__typename": "AppComponent",
        "name": {"value": name},
        "component_type": {"value": "backend"},
        "parent": {
            "node": {
                "id": app_id,
                "__typename": "AppApplication",
                "name": {"value": app_name},
                "criticality": {"value": criticality},
                "environment": {"value": "production"},
            }
        },
        "network_segment": (
            {"node": {"id": segment_id, "__typename": "ManagedVlanSegment", "name": {"value": "seg"}}}
            if segment_id
            else None
        ),
        "instances": {
            "edges": [{"node": {"id": instance, "__typename": "DcimPhysicalDevice"}} for instance in instances]
        },
    }


def _exposure_payload(components: list[dict[str, Any]], **roots: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"AppComponent": {"edges": [{"node": component} for component in components]}}
    payload.update(roots)
    return payload


# ---------------------------------------------------------------------------
# The edge set
# ---------------------------------------------------------------------------


class TestEdgeSet:
    """`TRAVERSAL_EDGES` is the whole risk model, so it has to match the schema.

    A renamed relationship would otherwise leave the traversal silently walking
    one edge fewer, which reports a smaller blast radius rather than an error.
    """

    @staticmethod
    def _schemas() -> dict[str, Any]:
        return _load_schemas()

    def test_every_traversal_edge_exists_in_the_schemas(self) -> None:
        kinds = self._schemas()
        missing = [
            (kind_name, rel_name)
            for kind_name, rel_name in TRAVERSAL_EDGES
            if kind_name not in kinds or rel_name not in kinds[kind_name].relationships
        ]
        assert missing == []

    def test_every_excluded_edge_exists_in_the_schemas(self) -> None:
        """An omission only documents a decision while the edge is still real."""
        kinds = self._schemas()
        missing = [
            (kind_name, rel_name)
            for kind_name, rel_name, _ in EXCLUDED_EDGES
            if kind_name not in kinds or rel_name not in kinds[kind_name].relationships
        ]
        assert missing == []

    def test_excluded_edges_are_not_also_traversed(self) -> None:
        traversed = set(TRAVERSAL_EDGES)
        assert [(kind, name) for kind, name, _ in EXCLUDED_EDGES if (kind, name) in traversed] == []

    def test_route_table_association_is_excluded_and_propagation_is_not(self) -> None:
        """The one place adjacency is not reachability — see docs/change_risk.md."""
        excluded = {(kind, name) for kind, name, _ in EXCLUDED_EDGES}
        assert ("CloudTransitGatewayRouteTable", "associated_attachments") in excluded
        assert ("CloudTransitGatewayAttachment", "associated_route_table") in excluded
        assert ("CloudTransitGatewayRouteTable", "propagated_attachments") in TRAVERSAL_EDGES
        assert ("CloudTransitGatewayAttachment", "propagates_to_route_tables") in TRAVERSAL_EDGES

    def test_shortlist_and_shared_fate_kinds_exist_in_the_schemas(self) -> None:
        kinds = self._schemas()
        wanted = sorted(set(SHORTLIST_KINDS) | set(CONFIRM_KINDS) | SHARED_FATE_KINDS)
        assert [kind for kind in wanted if kind not in kinds] == []

    def test_confirm_kinds_are_a_subset_of_the_shortlist(self) -> None:
        """Nothing can be confirmed that the shortlisting pass never asked for."""
        assert CONFIRM_KINDS <= set(SHORTLIST_KINDS)


class TestResolveRelationshipFilter:
    """The edge set is declared by name and matched by identifier."""

    class _Rel:
        def __init__(self, name: str, identifier: str | None) -> None:
            self.name = name
            self.identifier = identifier

    class _Kind:
        def __init__(self, *relationships: Any) -> None:
            self.relationships = list(relationships)

    def test_resolves_names_to_identifiers(self) -> None:
        schema = {"DcimDevice": self._Kind(self._Rel("interfaces", "dcimdevice__dciminterface"))}
        identifiers, missing = resolve_relationship_filter(schema, [("DcimDevice", "interfaces")])
        assert identifiers == ["dcimdevice__dciminterface"]
        assert missing == []

    def test_both_ends_of_an_edge_collapse_to_one_identifier(self) -> None:
        """Identifiers are shared by both ends, which is why the filter is
        direction-agnostic and one entry per logical edge is enough."""
        shared = "dcimdevice__dciminterface"
        schema = {
            "DcimDevice": self._Kind(self._Rel("interfaces", shared)),
            "DcimInterface": self._Kind(self._Rel("device", shared)),
        }
        identifiers, _ = resolve_relationship_filter(
            schema, [("DcimDevice", "interfaces"), ("DcimInterface", "device")]
        )
        assert identifiers == [shared]

    def test_reports_a_renamed_relationship_rather_than_dropping_it(self) -> None:
        schema = {"DcimDevice": self._Kind(self._Rel("ports", "dcimdevice__dcimport"))}
        identifiers, missing = resolve_relationship_filter(schema, [("DcimDevice", "interfaces")])
        assert identifiers == []
        assert missing == [("DcimDevice", "interfaces")]

    def test_reports_a_missing_kind(self) -> None:
        identifiers, missing = resolve_relationship_filter({}, [("CloudTransitGateway", "route_tables")])
        assert identifiers == []
        assert missing == [("CloudTransitGateway", "route_tables")]

    def test_relationship_without_an_identifier_is_reported_missing(self) -> None:
        """An unnamed edge cannot be passed to `relationship_filter` at all."""
        schema = {"DcimDevice": self._Kind(self._Rel("interfaces", None))}
        identifiers, missing = resolve_relationship_filter(schema, [("DcimDevice", "interfaces")])
        assert identifiers == []
        assert missing == [("DcimDevice", "interfaces")]


# ---------------------------------------------------------------------------
# Pass 1: the shortlist
# ---------------------------------------------------------------------------


class TestShortlist:
    def test_collects_candidates_with_their_kinds(self) -> None:
        shortlist = shortlist_from_reachable(
            SOURCE, _reachable_payload([("comp1", "AppComponent"), ("seg1", "ManagedVlanSegment")])
        )
        assert shortlist.candidates == {"comp1": "AppComponent", "seg1": "ManagedVlanSegment"}
        assert not shortlist.missing
        assert not shortlist.truncated

    def test_selects_by_kind_for_the_confirming_pass(self) -> None:
        shortlist = shortlist_from_reachable(
            SOURCE, _reachable_payload([("comp1", "AppComponent"), ("seg1", "ManagedVlanSegment")])
        )
        assert shortlist.of_kinds(["AppComponent"]) == {"comp1"}

    def test_no_payload_means_the_source_is_not_on_this_branch(self) -> None:
        shortlist = shortlist_from_reachable(SOURCE, None)
        assert shortlist.missing
        assert shortlist.source_id == SOURCE.id
        assert shortlist.candidates == {}

    def test_result_at_the_cap_is_truncated(self) -> None:
        payload = _reachable_payload([(f"comp{i}", "AppComponent") for i in range(3)])
        assert shortlist_from_reachable(SOURCE, payload, max_results=3).truncated

    def test_result_below_the_cap_is_not_truncated(self) -> None:
        payload = _reachable_payload([("comp1", "AppComponent")])
        assert not shortlist_from_reachable(SOURCE, payload, max_results=3).truncated


class TestNoteShortlist:
    def test_records_the_source_and_candidate_kinds(self) -> None:
        reach = ReachSet()
        reach.note_shortlist(shortlist_from_reachable(SOURCE, _reachable_payload([("comp1", "AppComponent")])))
        assert set(reach.sources) == {"src"}
        assert reach.kinds["comp1"] == "AppComponent"

    def test_a_shortlisted_candidate_is_not_thereby_reachable(self) -> None:
        """The shortlist is a superset. Until a path is confirmed, a candidate is
        a name the check knows, not an object the change reaches."""
        reach = ReachSet()
        reach.note_shortlist(shortlist_from_reachable(SOURCE, _reachable_payload([("comp1", "AppComponent")])))
        assert reach.nodes == {}

    def test_missing_source_is_recorded_without_candidates(self) -> None:
        reach = ReachSet()
        reach.note_shortlist(shortlist_from_reachable(SOURCE, None))
        assert reach.missing_sources == {"src"}
        assert reach.sources == {}

    def test_truncated_shortlist_marks_the_source(self) -> None:
        reach = ReachSet()
        payload = _reachable_payload([(f"comp{i}", "AppComponent") for i in range(3)])
        reach.note_shortlist(shortlist_from_reachable(SOURCE, payload, max_results=3))
        assert reach.truncated_sources == {"src"}


# ---------------------------------------------------------------------------
# Pass 2: confirmed paths
# ---------------------------------------------------------------------------


class TestAbsorbPaths:
    def test_records_the_destination_at_its_path_depth(self) -> None:
        reach = ReachSet()
        reach.absorb_paths(
            _paths_payload(
                [("src", "DcimPhysicalInterface"), ("circ1", "TopologyVirtualCircuit"), ("comp1", "AppComponent")]
            )
        )
        assert reach.nodes["comp1"].depth == 2
        assert reach.nodes["comp1"].path == ["src", "circ1", "comp1"]

    def test_records_every_node_along_the_path_not_only_the_destination(self) -> None:
        """A circuit the change reaches an application through is as impacted as
        the application, and is what shared fate is computed from."""
        reach = ReachSet()
        reach.absorb_paths(
            _paths_payload(
                [("src", "DcimPhysicalInterface"), ("circ1", "TopologyVirtualCircuit"), ("comp1", "AppComponent")]
            )
        )
        assert reach.nodes["circ1"].depth == 1
        assert reach.kinds["circ1"] == "TopologyVirtualCircuit"

    def test_does_not_record_the_source_as_impacted_by_itself(self) -> None:
        reach = ReachSet()
        reach.absorb_paths(_paths_payload([("src", "DcimPhysicalInterface"), ("comp1", "AppComponent")]))
        assert "src" not in reach.nodes
        assert set(reach.sources) == {"src"}

    def test_records_the_first_hop_as_the_ring(self) -> None:
        reach = ReachSet()
        reach.absorb_paths(
            _paths_payload(
                [("src", "DcimPhysicalInterface"), ("circ1", "TopologyVirtualCircuit"), ("comp1", "AppComponent")]
            )
        )
        assert reach.ring("src") == {"circ1"}

    def test_no_path_means_not_reachable_over_the_curated_edges(self) -> None:
        reach = ReachSet()
        reach.absorb_paths(_paths_payload(None))
        assert reach.nodes == {}
        assert reach.truncated_sources == set()

    def test_no_path_after_a_truncated_search_means_nothing_and_says_so(self) -> None:
        """Out of depth budget is not the same answer as unreachable."""
        reach = ReachSet()
        reach.absorb_paths(_paths_payload(None, truncated_at_depth=9))
        assert reach.nodes == {}
        assert reach.truncated_sources == {"src"}

    def test_keeps_the_shallowest_depth_across_sources(self) -> None:
        """Two changed objects can reach the same node; the near one wins, so a
        later source cannot make an impacted object look further away."""
        reach = ReachSet()
        far = ChangedNode(id="src2", kind="DcimPhysicalInterface", action="updated", label="leaf-02")
        reach.absorb_paths(
            _paths_payload([("src", "DcimPhysicalInterface"), ("x", "AppComponent"), ("comp1", "AppComponent")])
        )
        reach.absorb_paths(_paths_payload([("src2", "DcimPhysicalInterface"), ("comp1", "AppComponent")], source=far))
        assert reach.nodes["comp1"].depth == 1
        assert reach.nodes["comp1"].path == ["src2", "comp1"]

    def test_kind_counts_summarise_the_confirmed_radius(self) -> None:
        reach = ReachSet()
        reach.absorb_paths(
            _paths_payload(
                [("src", "DcimPhysicalInterface"), ("circ1", "TopologyVirtualCircuit"), ("comp1", "AppComponent")]
            )
        )
        assert reach.kind_counts() == {"TopologyVirtualCircuit": 1, "AppComponent": 1}


# ---------------------------------------------------------------------------
# Severity from the branch comparison
# ---------------------------------------------------------------------------


class TestSeverity:
    def test_reachable_before_and_not_after_is_an_outage(self) -> None:
        assert severity_of("x", before=_reach({"x": 2}), after=_reach({})) == "outage"

    def test_reachable_further_away_after_is_degraded(self) -> None:
        assert severity_of("x", before=_reach({"x": 2}), after=_reach({"x": 5})) == "degraded"

    def test_reachable_at_the_same_depth_is_ok(self) -> None:
        assert severity_of("x", before=_reach({"x": 2}), after=_reach({"x": 2})) == "ok"

    def test_a_shorter_path_after_is_ok_not_degraded(self) -> None:
        assert severity_of("x", before=_reach({"x": 5}), after=_reach({"x": 2})) == "ok"

    def test_newly_reachable_is_ok(self) -> None:
        """The change opened a path rather than closing one: exposure to report,
        not damage to score."""
        assert severity_of("x", before=_reach({}), after=_reach({"x": 3})) == "ok"

    def test_reachable_on_neither_branch_is_unknown(self) -> None:
        """Never reached by either walk, so there is nothing to compare — and
        reporting it as surviving would be a guess dressed up as a result."""
        assert severity_of("x", before=_reach({}), after=_reach({})) == "unknown"


class TestComponentSeverity:
    def test_surviving_instance_caps_an_outage_at_degraded(self) -> None:
        before = _reach({"comp": 2, "vm1": 3, "vm2": 3})
        after = _reach({"vm2": 3})
        component = {"id": "comp", "name": "api", "instances": ["vm1", "vm2"]}
        assert component_severity(component, before, after) == "degraded"

    def test_every_instance_lost_stays_an_outage(self) -> None:
        before = _reach({"comp": 2, "vm1": 3, "vm2": 3})
        after = _reach({})
        component = {"id": "comp", "name": "api", "instances": ["vm1", "vm2"]}
        assert component_severity(component, before, after) == "outage"

    def test_losing_one_of_two_instances_is_degraded_even_with_the_path_intact(self) -> None:
        """Half the capacity is gone. The component node's own path says nothing
        about that, which is what `instances` is consulted for."""
        before = _reach({"comp": 2, "vm1": 3, "vm2": 3})
        after = _reach({"comp": 2, "vm2": 3})
        component = {"id": "comp", "name": "api", "instances": ["vm1", "vm2"]}
        assert component_severity(component, before, after) == "degraded"

    def test_losing_every_instance_is_an_outage_even_with_the_path_intact(self) -> None:
        before = _reach({"comp": 2, "vm1": 3})
        after = _reach({"comp": 2})
        component = {"id": "comp", "name": "api", "instances": ["vm1"]}
        assert component_severity(component, before, after) == "outage"

    def test_instances_decided_on_neither_branch_are_unknowable(self) -> None:
        """Recorded instances that no traversal reached tell us nothing, so the
        component is not called safe on the strength of them."""
        before = _reach({"comp": 2})
        after = _reach({"comp": 2})
        component = {"id": "comp", "name": "api", "instances": ["vm1", "vm2"]}
        assert component_severity(component, before, after) == "unknown"

    def test_no_instance_recorded_is_unknown_not_ok(self) -> None:
        """Redundancy cannot be confirmed, so the component is not called safe."""
        before = _reach({"comp": 2})
        after = _reach({"comp": 2})
        assert component_severity({"id": "comp", "name": "api", "instances": []}, before, after) == "unknown"

    def test_no_instance_recorded_does_not_soften_an_outage(self) -> None:
        component = {"id": "comp", "name": "api", "instances": []}
        assert component_severity(component, _reach({"comp": 2}), _reach({})) == "outage"


# ---------------------------------------------------------------------------
# Exposure indexing
# ---------------------------------------------------------------------------


class TestIndexExposure:
    def test_indexes_component_application_and_instances(self) -> None:
        exposure = index_exposure(
            [_exposure_payload([_component("comp1", "api", "app1", "Payments", "critical", ["vm1", "vm2"], "seg1")])]
        )
        assert exposure.components["comp1"]["instances"] == ["vm1", "vm2"]
        assert exposure.components["comp1"]["app_id"] == "app1"
        assert exposure.applications["app1"].criticality == "critical"
        assert exposure.applications["app1"].components == ["api"]

    def test_merges_the_same_component_from_two_payloads(self) -> None:
        """The query runs on both branches, so an id comes back twice."""
        payload = _exposure_payload([_component("comp1", "api", "app1", "Payments", "critical", ["vm1"], "seg1")])
        bare = _exposure_payload([_component("comp1", "api", "app1", "Payments", "", [], None)])
        exposure = index_exposure([bare, payload])
        assert exposure.components["comp1"]["instances"] == ["vm1"]
        assert exposure.components["comp1"]["segment_id"] == "seg1"
        assert exposure.applications["app1"].components == ["api"]

    def test_segment_with_a_component_is_not_an_orphan(self) -> None:
        exposure = index_exposure(
            [
                _exposure_payload(
                    [_component("comp1", "api", "app1", "Payments", "high", ["vm1"], "seg1")],
                    ManagedNetworkSegment={
                        "edges": [
                            {"node": {"id": "seg1", "__typename": "ManagedVlanSegment", "name": {"value": "seg"}}}
                        ]
                    },
                )
            ]
        )
        assert exposure.orphan_segments == {}

    def test_segment_with_no_component_is_an_orphan(self) -> None:
        """Unknown exposure, not zero exposure."""
        exposure = index_exposure(
            [
                _exposure_payload(
                    [],
                    ManagedNetworkSegment={
                        "edges": [
                            {"node": {"id": "seg9", "__typename": "ManagedVlanSegment", "name": {"value": "lonely"}}}
                        ]
                    },
                )
            ]
        )
        assert exposure.orphan_segments == {"seg9": "lonely"}

    def test_network_account_is_flagged_and_workload_account_is_not(self) -> None:
        exposure = index_exposure(
            [
                _exposure_payload(
                    [],
                    CloudAccount={
                        "edges": [
                            {
                                "node": {
                                    "id": "acc-net",
                                    "__typename": "CloudAccount",
                                    "name": {"value": "30all-network-aws"},
                                    "account_role": {"value": "network"},
                                }
                            },
                            {
                                "node": {
                                    "id": "acc-wl",
                                    "__typename": "CloudAccount",
                                    "name": {"value": "c003-aws"},
                                    "account_role": {"value": "workload"},
                                }
                            },
                        ]
                    },
                )
            ]
        )
        assert exposure.network_accounts == {"acc-net": "30all-network-aws"}


# ---------------------------------------------------------------------------
# Diff seeding
# ---------------------------------------------------------------------------


class TestChangesFromDiff:
    def test_reads_id_kind_action_and_element_names(self) -> None:
        changes = changes_from_diff(
            [
                {
                    "id": "dev1",
                    "kind": "DcimPhysicalDevice",
                    "action": "UPDATED",
                    "display_label": "leaf-01",
                    "elements": [{"name": "status", "element_type": "ATTRIBUTE"}],
                }
            ]
        )
        assert changes == [
            ChangedNode(
                id="dev1",
                kind="DcimPhysicalDevice",
                action="updated",
                label="leaf-01",
                elements=frozenset({"status"}),
            )
        ]

    def test_skips_entries_without_an_id(self) -> None:
        assert changes_from_diff([{"kind": "DcimPhysicalDevice", "action": "added"}]) == []

    def test_handles_no_diff(self) -> None:
        assert changes_from_diff(None) == []


# ---------------------------------------------------------------------------
# Scoring and verdict
# ---------------------------------------------------------------------------


class TestBuildReport:
    @staticmethod
    def _report(
        components: list[dict[str, Any]],
        before: ReachSet,
        after: ReachSet,
        changes: list[ChangedNode] | None = None,
        **roots: Any,
    ) -> Any:
        return build_report(
            changes=changes if changes is not None else [SOURCE],
            before=before,
            after=after,
            exposure=index_exposure([_exposure_payload(components, **roots)]),
        )

    def test_critical_application_outage_blocks(self) -> None:
        report = self._report(
            [_component("comp1", "api", "app1", "Payments", "critical", ["vm1"])],
            before=_reach({"comp1": 2, "vm1": 3}),
            after=_reach({}),
        )
        assert report.verdict == "BLOCK"
        assert report.applications[0].severity == "outage"
        assert report.applications[0].score == pytest.approx(8.0)
        assert [f.code for f in report.findings if f.level == "error"] == ["critical_outage"]

    def test_non_critical_outage_is_review_even_below_the_score_threshold(self) -> None:
        """A medium application scores 2.0, under the threshold — but an outage
        is never a PASS."""
        report = self._report(
            [_component("comp1", "api", "app1", "Reporting", "medium", ["vm1"])],
            before=_reach({"comp1": 2, "vm1": 3}),
            after=_reach({}),
        )
        assert report.score < REVIEW_SCORE
        assert report.verdict == "REVIEW"
        assert "outage" in {f.code for f in report.findings if f.level == "error"}

    def test_unaffected_application_passes(self) -> None:
        report = self._report(
            [_component("comp1", "api", "app1", "Reporting", "medium", ["vm1", "vm2"])],
            before=_reach({"comp1": 2, "vm1": 3, "vm2": 3}),
            after=_reach({"comp1": 2, "vm1": 3, "vm2": 3}),
        )
        assert report.verdict == "PASS"
        assert report.score == pytest.approx(0.0)
        assert [f.code for f in report.findings if f.level == "error"] == []

    def test_degraded_application_scores_the_partial_weight(self) -> None:
        report = self._report(
            [_component("comp1", "api", "app1", "Payments", "critical", ["vm1", "vm2"])],
            before=_reach({"comp1": 2, "vm1": 3, "vm2": 3}),
            after=_reach({"comp1": 2, "vm2": 3}),
        )
        assert report.applications[0].severity == "degraded"
        assert report.applications[0].score == pytest.approx(8.0 * 0.4)

    def test_application_severity_is_the_worst_of_its_components(self) -> None:
        report = self._report(
            [
                _component("comp1", "api", "app1", "Payments", "high", ["vm1", "vm2"]),
                _component("comp2", "worker", "app1", "Payments", "high", ["vm3"]),
            ],
            before=_reach({"comp1": 2, "comp2": 2, "vm1": 3, "vm2": 3, "vm3": 3}),
            after=_reach({"comp1": 2, "vm1": 3, "vm2": 3}),
        )
        assert report.applications[0].severity == "outage"

    def test_low_confidence_forces_review_without_changing_the_score(self) -> None:
        """A component whose fate is undetermined contributes no score and still
        has to be looked at — docs/change_risk.md, "Confidence"."""
        report = self._report(
            [_component("comp1", "api", "app1", "Reporting", "low", [])],
            before=_reach({"comp1": 2}),
            after=_reach({"comp1": 2}),
        )
        assert report.applications[0].severity == "unknown"
        assert report.score == pytest.approx(0.0)
        assert report.confidence < MIN_CONFIDENCE
        assert report.verdict == "REVIEW"
        assert "low_confidence" in {f.code for f in report.findings if f.level == "error"}

    def test_single_homed_is_reported_even_when_the_verdict_passes(self) -> None:
        report = self._report(
            [_component("comp1", "api", "app1", "Reporting", "low", ["vm1"])],
            before=_reach({"comp1": 2, "vm1": 3}),
            after=_reach({"comp1": 2, "vm1": 3}),
        )
        assert report.verdict == "PASS"
        assert "single_homed" in {f.code for f in report.findings}

    def test_shared_fate_fires_for_a_circuit_on_two_application_paths(self) -> None:
        reach = ReachSet()
        for component_id in ("comp1", "comp2"):
            reach.absorb_paths(
                _paths_payload(
                    [
                        ("src", "DcimPhysicalInterface"),
                        ("circ1", "TopologyVirtualCircuit"),
                        (component_id, "AppComponent"),
                    ],
                    destination=(component_id, "AppComponent"),
                )
            )
        for instance_id in ("vm1", "vm2"):
            reach.absorb_paths(
                _paths_payload(
                    [("src", "DcimPhysicalInterface"), (instance_id, "DcimVirtualDevice")],
                    destination=(instance_id, "DcimVirtualDevice"),
                )
            )
        report = self._report(
            [
                _component("comp1", "api", "app1", "Payments", "high", ["vm1"]),
                _component("comp2", "web", "app2", "Portal", "medium", ["vm2"]),
            ],
            before=reach,
            after=reach,
        )
        shared = [f for f in report.findings if f.code == "shared_fate"]
        assert [f.object_id for f in shared] == ["circ1"]
        assert "Payments" in shared[0].message and "Portal" in shared[0].message

    def test_shared_fate_does_not_fire_for_a_single_application(self) -> None:
        reach = ReachSet()
        reach.absorb_paths(
            _paths_payload(
                [("src", "DcimPhysicalInterface"), ("circ1", "TopologyVirtualCircuit"), ("comp1", "AppComponent")]
            )
        )
        reach.absorb_paths(
            _paths_payload(
                [("src", "DcimPhysicalInterface"), ("vm1", "DcimVirtualDevice")],
                destination=("vm1", "DcimVirtualDevice"),
            )
        )
        report = self._report(
            [_component("comp1", "api", "app1", "Payments", "high", ["vm1"])],
            before=reach,
            after=reach,
        )
        assert [f for f in report.findings if f.code == "shared_fate"] == []

    def test_network_account_is_shared_fate_by_construction(self) -> None:
        report = self._report(
            [_component("comp1", "api", "app1", "Payments", "high", ["vm1", "vm2"])],
            before=_reach({"comp1": 2, "vm1": 3, "vm2": 3}),
            after=_reach({"comp1": 2, "vm1": 3, "vm2": 3}),
            CloudAccount={
                "edges": [
                    {
                        "node": {
                            "id": "acc-net",
                            "__typename": "CloudAccount",
                            "name": {"value": "30all-network-aws"},
                            "account_role": {"value": "network"},
                        }
                    }
                ]
            },
        )
        shared = [f for f in report.findings if f.code == "shared_fate"]
        assert [f.object_id for f in shared] == ["acc-net"]

    def test_unresolved_edge_is_an_error_because_the_radius_was_not_walked(self) -> None:
        report = build_report(
            changes=[SOURCE],
            before=_reach({"comp1": 2}),
            after=_reach({"comp1": 2}),
            exposure=index_exposure([_exposure_payload([])]),
            unresolved_edges=[("CloudTransitGateway", "route_tables")],
        )
        assert "unresolved_edge" in {f.code for f in report.findings if f.level == "error"}

    def test_truncated_source_lowers_confidence(self) -> None:
        after = _reach({"comp1": 2, "vm1": 3})
        after.truncated_sources.add("src")
        report = self._report(
            [_component("comp1", "api", "app1", "Reporting", "low", ["vm1"])],
            before=_reach({"comp1": 2, "vm1": 3}),
            after=after,
        )
        assert "truncated" in {f.code for f in report.findings}
        assert report.confidence < 1.0

    def test_reaching_nothing_says_so_rather_than_passing_quietly(self) -> None:
        report = self._report([], before=_reach({}), after=_reach({}))
        assert report.verdict == "PASS"
        assert "no_exposure" in {f.code for f in report.findings}

    def test_report_serialises_for_an_artifact(self) -> None:
        report = self._report(
            [_component("comp1", "api", "app1", "Payments", "critical", ["vm1"])],
            before=_reach({"comp1": 2, "vm1": 3}),
            after=_reach({}),
        )
        payload = report.as_dict()
        assert payload["verdict"] == "BLOCK"
        assert payload["applications"][0]["name"] == "Payments"
        assert payload["applications"][0]["severity"] == "outage"
        assert any(finding["code"] == "critical_outage" for finding in payload["findings"])
