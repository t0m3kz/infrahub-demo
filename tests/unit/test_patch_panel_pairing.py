"""Patch panel pairing: the planner, add_patch_panel on a fake client, the check, and the registration.

An object template cannot pair a front port with its rear port, so add_patch_panel does it from the ports alone and
checks/patch_panel_pairing.py guards the result.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
import yaml

from checks.patch_panel_pairing import CheckPatchPanelPairing
from generators.helpers.patch_panel import (
    LAYOUT_CASSETTE,
    LAYOUT_EMPTY,
    LAYOUT_JACK,
    LAYOUT_UNKNOWN,
    pairing_changes,
    plan_pairing,
)
from generators.logger import GeneratorError
from generators.topology.patch_panel import PatchPanelGenerator
from tests.integration.demo_data import (
    ROOT,
    action_generators,
    declared,
    definition_targets,
    group_trigger_rules,
    trigger_rules,
)
from utils.limits import GROUP_PATCH_PANELS


def _rears(count: int, connector: str = "lc-upc") -> list[dict[str, Any]]:
    """Rear ports named 1..count."""
    return [{"id": f"r{n}", "name": str(n), "connector": connector} for n in range(1, count + 1)]


def _fronts(count: int, paired: Mapping[int, str | None] | None = None) -> list[dict[str, Any]]:
    """Front ports named 1..count, paired as given (number -> rear port id)."""
    paired = paired or {}
    return [
        {"id": f"f{n}", "name": str(n), "connector": "lc-upc", "rear_port": paired.get(n)} for n in range(1, count + 1)
    ]


class TestPlanner:
    """The layout is what the ports say."""

    @pytest.mark.parametrize("count", [24, 48])
    def test_a_jack_panel_pairs_front_n_with_rear_n(self, count: int) -> None:
        pairing = plan_pairing(_rears(count), _fronts(count))

        assert pairing.layout == LAYOUT_JACK
        assert pairing.wanted == {f"f{n}": f"r{n}" for n in range(1, count + 1)}

    def test_numbers_are_compared_as_integers_not_as_text(self) -> None:
        rears = [{"id": f"r{n}", "name": str(n), "connector": "lc-upc"} for n in (10, 2, 1)]
        fronts = [{"id": f"f{n}", "name": str(n), "connector": "lc-upc"} for n in (1, 10, 2)]

        assert plan_pairing(rears, fronts).wanted == {"f1": "r1", "f2": "r2", "f10": "r10"}

    def test_a_cassette_panel_gives_rear_k_the_fronts_k_minus_1_times_f_plus_1_to_k_times_f(self) -> None:
        pairing = plan_pairing(_rears(4, "mpo-12"), _fronts(48))

        assert pairing.layout == LAYOUT_CASSETTE
        assert pairing.wanted["f1"] == pairing.wanted["f12"] == "r1"
        assert pairing.wanted["f13"] == "r2"
        assert pairing.wanted["f48"] == "r4"
        assert sum(1 for rear in pairing.wanted.values() if rear == "r3") == 12

    def test_fronts_a_multiple_of_non_mpo_rears_have_no_layout(self) -> None:
        assert plan_pairing(_rears(4), _fronts(8)).layout == LAYOUT_UNKNOWN

    def test_unequal_counts_have_no_layout(self) -> None:
        pairing = plan_pairing(_rears(24), _fronts(23))

        assert pairing.layout == LAYOUT_UNKNOWN and pairing.wanted == {} and pairing.problem

    def test_fronts_that_are_no_multiple_of_the_mpo_rears_have_no_layout(self) -> None:
        assert plan_pairing(_rears(4, "mpo-12"), _fronts(50)).layout == LAYOUT_UNKNOWN

    def test_a_non_numeric_name_leaves_the_panel_unpaired(self) -> None:
        rears = _rears(2)
        rears[1]["name"] = "A"

        pairing = plan_pairing(rears, _fronts(2))

        assert pairing.layout == LAYOUT_UNKNOWN and "'A'" in str(pairing.problem)

    def test_a_repeated_number_leaves_the_panel_unpaired(self) -> None:
        rears = _rears(2)
        rears[1]["name"] = "01"
        rears[0]["name"] = "1"

        assert plan_pairing(rears, _fronts(2)).layout == LAYOUT_UNKNOWN

    def test_a_panel_without_ports_is_empty_and_one_with_one_side_is_not(self) -> None:
        assert plan_pairing([], []).layout == LAYOUT_EMPTY
        assert plan_pairing(_rears(2), []).layout == LAYOUT_UNKNOWN

    def test_only_unpaired_fronts_are_written_and_a_front_paired_differently_is_a_conflict(self) -> None:
        fronts = _fronts(3, {1: "r1", 2: "r3"})

        to_write, conflicts = pairing_changes(plan_pairing(_rears(3), fronts), fronts)

        assert to_write == {"f3": "r3"}
        assert conflicts == {"f2": "r2"}

    def test_a_paired_panel_has_nothing_to_write(self) -> None:
        fronts = _fronts(3, {1: "r1", 2: "r2", 3: "r3"})

        assert pairing_changes(plan_pairing(_rears(3), fronts), fronts) == ({}, {})


class _Port:
    """A node the fake client creates; ``save`` is recorded."""

    def __init__(self, client: _Client, data: dict[str, Any]) -> None:
        self.client = client
        self.data = data

    async def save(self, **kwargs: Any) -> None:
        self.client.saved.append((self.data, kwargs))


class _Client:
    """Records every port the generator saves."""

    def __init__(self) -> None:
        self.saved: list[tuple[dict[str, Any], dict[str, Any]]] = []

    async def create(self, kind: str, data: dict[str, Any]) -> _Port:
        assert kind == "DcimFrontPatchPanelPort"
        return _Port(self, data)


def _edges(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    """The GraphQL shape of a list of nodes (attributes wrapped as {"value": x})."""
    out = []
    for node in nodes:
        item: dict[str, Any] = {
            "id": node["id"],
            "name": {"value": node["name"]},
            "connector": {"value": node["connector"]},
        }
        if "rear_port" in node:
            item["rear_port"] = {"node": {"id": node["rear_port"]} if node["rear_port"] else None}
        out.append({"node": item})
    return {"edges": out}


def _payload(rears: list[dict[str, Any]], fronts: list[dict[str, Any]]) -> dict[str, Any]:
    """The patch_panel_data response for one panel."""
    node = {
        "id": "p1",
        "name": {"value": "PP-01"},
        "panel_id": {"value": "r1:PP-01"},
        "rear_ports": _edges(rears),
        "front_ports": _edges(fronts),
    }
    return {"DcimPatchPanel": {"edges": [{"node": node}]}}


async def _run(rears: list[dict[str, Any]], fronts: list[dict[str, Any]]) -> tuple[_Client, MagicMock]:
    """Run the generator once; return the client and the logger."""
    client = _Client()
    generator = cast(Any, PatchPanelGenerator.__new__(PatchPanelGenerator))
    generator.client = client
    generator.logger = MagicMock()
    generator.logger.error.side_effect = GeneratorError
    await generator.generate(_payload(rears, fronts))
    return client, generator.logger


class TestGenerator:
    """add_patch_panel on a fake client."""

    @pytest.mark.asyncio
    async def test_one_untracked_write_per_unpaired_front_port(self) -> None:
        client, _ = await _run(_rears(24), _fronts(24))

        assert len(client.saved) == 24
        data, kwargs = client.saved[0]
        assert data == {"id": "f1", "rear_port": {"id": "r1"}}
        assert kwargs == {"allow_upsert": True, "update_group_context": False}

    @pytest.mark.asyncio
    async def test_a_rerun_over_a_paired_panel_writes_nothing(self) -> None:
        client, _ = await _run(_rears(24), _fronts(24, {n: f"r{n}" for n in range(1, 25)}))

        assert client.saved == []

    @pytest.mark.asyncio
    async def test_only_the_differing_ports_are_written(self) -> None:
        client, _ = await _run(_rears(3), _fronts(3, {1: "r1"}))

        assert [data["id"] for data, _ in client.saved] == ["f2", "f3"]

    @pytest.mark.asyncio
    async def test_a_front_paired_differently_is_warned_about_and_not_overwritten(self) -> None:
        client, logger = await _run(_rears(2), _fronts(2, {1: "r2"}))

        assert [data["id"] for data, _ in client.saved] == ["f2"]
        logger.warning.assert_called_once()

    @pytest.mark.asyncio
    async def test_an_empty_panel_is_logged_and_left_without_an_error(self) -> None:
        client, logger = await _run([], [])

        assert client.saved == []
        logger.info.assert_called_once()
        logger.error.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_underivable_layout_fails_the_run_and_writes_nothing(self) -> None:
        with pytest.raises(GeneratorError):
            await _run(_rears(3), _fronts(2))


def _check_panel(panel_id: str, rears: list[dict[str, Any]], fronts: list[dict[str, Any]]) -> dict[str, Any]:
    """One DcimPatchPanel edge of the patch_panel_pairing response."""
    node = {"id": panel_id, "panel_id": {"value": panel_id}, "rear_ports": _edges(rears), "front_ports": _edges(fronts)}
    return {"node": node}


def _validate(*panels: dict[str, Any]) -> list[str]:
    """The errors of the check over the given panels."""
    check = cast(Any, CheckPatchPanelPairing.__new__(CheckPatchPanelPairing))
    errors: list[str] = []
    check.log_error = lambda message: errors.append(message)
    check.validate({"DcimPatchPanel": {"edges": list(panels)}})
    return errors


class TestCheck:
    """What the check accepts and refuses."""

    def test_a_fully_paired_jack_panel_and_cassette_panel_pass(self) -> None:
        jack = _check_panel("a:PP", _rears(2), _fronts(2, {1: "r1", 2: "r2"}))
        paired = {n: f"r{(n - 1) // 12 + 1}" for n in range(1, 49)}
        cassette = _check_panel("a:CS", _rears(4, "mpo-12"), _fronts(48, paired))

        assert _validate(jack, cassette) == []

    def test_an_unpaired_front_port_is_an_error(self) -> None:
        (error,) = _validate(_check_panel("a:PP", _rears(2), _fronts(2, {1: "r1"})))

        assert "front port 2 has no rear port" in error

    def test_a_front_port_on_the_wrong_rear_port_is_an_error(self) -> None:
        (error,) = _validate(_check_panel("a:PP", _rears(2), _fronts(2, {1: "r2", 2: "r2"})))

        assert "front port 1 is on rear port 2" in error and "rear port 1" in error

    def test_a_cassette_rear_port_with_too_few_fronts_is_an_error(self) -> None:
        paired = {n: "r1" for n in range(1, 49)}

        errors = _validate(_check_panel("a:CS", _rears(4, "mpo-12"), _fronts(48, paired)))

        assert len(errors) == 36

    def test_an_underivable_layout_and_an_empty_panel_are_errors(self) -> None:
        errors = _validate(_check_panel("a:X", _rears(3), _fronts(2)), _check_panel("a:E", [], []))

        assert any("a:X" in e and "layout not derivable" in e for e in errors)
        assert any("a:E" in e and "no ports" in e for e in errors)


class TestRegistration:
    """The rules, the group, the queries and the definition agree."""

    def test_the_group_rule_and_the_two_port_edge_rules_run_add_patch_panel_on_panels_only(self) -> None:
        rules = {r["name"]: r for r in trigger_rules() if r["action"] == "run-patch-panel-generator"}
        joined = [r for r in group_trigger_rules() if r["action"] == "run-patch-panel-generator"]

        assert sorted(rules) == [
            "trigger-patch-panel-on-front-ports-updated",
            "trigger-patch-panel-on-rear-ports-updated",
        ]
        assert {r["node_kind"] for r in rules.values()} == {"DcimPatchPanel"}
        assert [(r["name"], r["group"]) for r in joined] == [("trigger-patch-panel-on-added", GROUP_PATCH_PANELS)]
        edges = {m["relationship_name"] for r in rules.values() if r.get("matches") for m in r["matches"]["data"]}
        assert edges == {"rear_ports", "front_ports"}
        assert action_generators()["run-patch-panel-generator"] == "add_patch_panel"
        assert definition_targets()["add_patch_panel"] == GROUP_PATCH_PANELS

    def test_the_definition_names_its_class_file_and_queries(self) -> None:
        config = yaml.safe_load((ROOT / ".infrahub.yml").read_text())
        definition = {d["name"]: d for d in config["generator_definitions"]}["add_patch_panel"]
        queries = {q["name"]: q["file_path"] for q in config["queries"]}
        checks = {c["name"]: c for c in config["check_definitions"]}

        assert definition["class_name"] == "PatchPanelGenerator"
        assert definition["query"] == "patch_panel_data"
        assert definition["parameters"] == {"panel_id": "panel_id__value"}
        assert (ROOT / queries["patch_panel_data"]).is_file()
        assert (ROOT / queries["patch_panel_pairing"]).is_file()
        assert checks["validate_patch_panel_pairing"]["class_name"] == "CheckPatchPanelPairing"
        assert CheckPatchPanelPairing.query == "patch_panel_pairing"

    def test_the_panel_templates_are_in_no_group(self) -> None:
        """A template does not pass its member_of_groups on to the panel it creates (measured), so a template in the
        group only made the template a target of the generator."""
        text = (ROOT / "data/bootstrap/10_physical_devices_templates_patch_panel_rear.yaml").read_text()
        templates = yaml.safe_load(text)["spec"]["data"]

        assert templates
        for template in templates:
            assert not template.get("member_of_groups"), template["template_name"]

    def test_every_declared_panel_is_a_member_of_the_group(self) -> None:
        """The generator starts when a panel joins patch_panels, so every panel in data/ declares the group itself."""
        outside = sorted(
            {
                str(obj.path.relative_to(ROOT))
                for obj in declared("DcimPatchPanel", ROOT / "data" / "demos")
                if GROUP_PATCH_PANELS not in obj.groups()
            }
        )

        assert declared("DcimPatchPanel", ROOT / "data" / "demos")
        assert not outside, f"panels not in '{GROUP_PATCH_PANELS}': {outside}"
