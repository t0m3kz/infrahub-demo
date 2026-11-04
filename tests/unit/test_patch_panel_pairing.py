"""Patch panels by type: the audit, add_patch_panel on a fake client, the check, and the registration.

A panel names its `panel_type`; add_patch_panel builds the ports the type holds, each front port on its rear port,
and checks/patch_panel_pairing.py reports what still differs. Both read generators/helpers/patch_panel.py audit().
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import asynccontextmanager
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
import yaml

from checks.patch_panel_pairing import CheckPatchPanelPairing
from generators.helpers.patch_panel import LAYOUT_CASSETTE, LAYOUT_JACK, PANEL_TYPES, audit
from generators.logger import GeneratorError
from generators.topology.patch_panel import FRONT_PORT_KIND, REAR_PORT_KIND, PatchPanelGenerator
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


def _jack(count: int) -> dict[int, str]:
    """Front N on rear N."""
    return {n: f"r{n}" for n in range(1, count + 1)}


class TestPanelTypes:
    """The table of types is the layout."""

    @pytest.mark.parametrize("name", sorted(PANEL_TYPES))
    def test_every_type_is_a_jack_or_a_cassette_on_mpo_rear_ports(self, name: str) -> None:
        """A jack has one front port per rear port; a cassette several, on an MPO rear port."""
        panel_type = PANEL_TYPES[name]

        assert panel_type.rears >= 1 and panel_type.fronts_per_rear >= 1
        if panel_type.layout == LAYOUT_CASSETTE:
            assert panel_type.rear_connector == "mpo-12"
        else:
            assert panel_type.rear_connector == panel_type.front_connector

    def test_a_cassette_gives_rear_k_the_fronts_k_minus_1_times_f_plus_1_to_k_times_f(self) -> None:
        cassette = PANEL_TYPES["mpo12-cassette-48"]

        assert (cassette.rears, cassette.fronts, cassette.layout) == (4, 48, LAYOUT_CASSETTE)
        assert [cassette.rear_of(n) for n in (1, 12, 13, 24, 25, 48)] == [1, 1, 2, 2, 3, 4]

    def test_a_jack_puts_front_n_on_rear_n(self) -> None:
        jack = PANEL_TYPES["lc-24"]

        assert jack.layout == LAYOUT_JACK and [jack.rear_of(n) for n in (1, 7, 24)] == [1, 7, 24]


class TestAudit:
    """What differs between a panel's ports and its type."""

    def test_an_empty_panel_misses_every_port(self) -> None:
        result = audit("lc-24", [], [])

        assert result.missing_rears == list(range(1, 25)) and result.missing_fronts == list(range(1, 25))
        assert not (result.to_pair or result.extra or result.misplaced or result.problem)

    def test_a_complete_panel_has_nothing_to_do(self) -> None:
        result = audit("lc-24", _rears(24), _fronts(24, _jack(24)))

        assert not (result.missing_rears or result.missing_fronts or result.to_pair or result.misplaced)
        assert not (result.extra or result.wrong_connector or result.problem)

    def test_unpaired_fronts_are_paired_and_a_front_on_another_rear_is_misplaced(self) -> None:
        result = audit("lc-24", _rears(24), _fronts(24, {1: "r2", 2: "r2"}))

        assert result.misplaced == ["1"]
        assert result.to_pair == {f"f{n}": n for n in range(3, 25)}

    def test_a_type_shrunk_under_its_ports_reports_the_extra_ports(self) -> None:
        result = audit("sc-12", _rears(13, "sc-upc"), [])

        assert result.extra == ["rear 13"] and result.missing_fronts == list(range(1, 13))

    def test_a_port_with_another_connector_is_reported(self) -> None:
        result = audit("mpo12-cassette-24", _rears(2), [])

        assert result.wrong_connector == ["rear 1", "rear 2"]

    @pytest.mark.parametrize(
        ("type_name", "rears", "problem"),
        [
            (None, [], "is not one of"),
            ("lc-1000", [], "is not one of"),
            ("lc-24", [{"id": "x", "name": "A", "connector": "lc-upc"}], "is not a number"),
            ("lc-24", [*_rears(1), {"id": "x", "name": "1", "connector": "lc-upc"}], "share the number"),
        ],
    )
    def test_an_unknown_type_or_unnumbered_ports_have_no_audit(
        self, type_name: str | None, rears: list[dict[str, Any]], problem: str
    ) -> None:
        assert problem in str(audit(type_name, rears, []).problem)


class _Port:
    """A node the fake client creates; ``save`` is recorded and gives a created node its id."""

    def __init__(self, client: _Client, kind: str, data: dict[str, Any]) -> None:
        self.client, self.kind, self.data = client, kind, data
        self.id = data.get("id") or f"new-{kind}-{data.get('name')}"

    async def save(self, **kwargs: Any) -> None:
        self.client.saved.append((self.kind, self.data, kwargs))


class _Client:
    """Records every port the generator saves."""

    def __init__(self) -> None:
        self.saved: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

    async def create(self, kind: str, data: dict[str, Any]) -> _Port:
        assert kind in (REAR_PORT_KIND, FRONT_PORT_KIND)
        return _Port(self, kind, data)


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


def _panel(panel_type: str | None, rears: list[dict[str, Any]], fronts: list[dict[str, Any]], **extra: Any) -> dict:
    """A DcimPatchPanel node of the patch_panel_data / patch_panel_pairing responses."""
    return {
        "id": "p1",
        "name": {"value": "PP-01"},
        "panel_id": {"value": "r1:PP-01"},
        "panel_type": {"value": panel_type},
        "rear_ports": _edges(rears),
        "front_ports": _edges(fronts),
        **extra,
    }


async def _run(
    panel_type: str | None, rears: list[dict[str, Any]], fronts: list[dict[str, Any]]
) -> tuple[_Client, MagicMock, list[str]]:
    """Run the generator once; return the client, the logger and the locks it took."""
    client = _Client()
    locks: list[str] = []
    generator = cast(Any, PatchPanelGenerator.__new__(PatchPanelGenerator))
    generator.client = client
    generator.logger = MagicMock()
    generator.logger.error.side_effect = GeneratorError

    @asynccontextmanager
    async def lock(key: str) -> Any:
        locks.append(key)
        yield

    generator.resource_lock = lock
    await generator.generate({"DcimPatchPanel": {"edges": [{"node": _panel(panel_type, rears, fronts)}]}})
    return client, generator.logger, locks


class TestGenerator:
    """add_patch_panel on a fake client."""

    @pytest.mark.asyncio
    async def test_an_empty_jack_panel_gets_every_port_each_front_on_its_rear(self) -> None:
        client, _, locks = await _run("lc-24", [], [])

        rears = [data for kind, data, _ in client.saved if kind == REAR_PORT_KIND]
        fronts = [data for kind, data, _ in client.saved if kind == FRONT_PORT_KIND]
        assert locks == ["patch-panel-p1"]
        assert [r["name"] for r in rears] == [str(n) for n in range(1, 25)]
        assert rears[0] == {"name": "1", "connector": "lc-upc", "panel": {"id": "p1"}}
        assert fronts[6] == {
            "name": "7",
            "connector": "lc-upc",
            "panel": {"id": "p1"},
            "rear_port": {"id": f"new-{REAR_PORT_KIND}-7"},
        }
        assert all(kwargs == {"allow_upsert": True, "update_group_context": False} for _, _, kwargs in client.saved)

    @pytest.mark.asyncio
    async def test_a_cassette_puts_twelve_fronts_on_each_mpo_rear(self) -> None:
        client, _, _ = await _run("mpo12-cassette-48", [], [])

        rears = [data for kind, data, _ in client.saved if kind == REAR_PORT_KIND]
        fronts = {data["name"]: data["rear_port"]["id"] for kind, data, _ in client.saved if kind == FRONT_PORT_KIND}
        assert [(r["name"], r["connector"]) for r in rears] == [(str(n), "mpo-12") for n in range(1, 5)]
        assert fronts["12"] == f"new-{REAR_PORT_KIND}-1" and fronts["13"] == f"new-{REAR_PORT_KIND}-2"
        assert len(fronts) == 48

    @pytest.mark.asyncio
    async def test_a_rerun_over_a_complete_panel_writes_nothing(self) -> None:
        client, _, _ = await _run("lc-24", _rears(24), _fronts(24, _jack(24)))

        assert client.saved == []

    @pytest.mark.asyncio
    async def test_existing_unpaired_fronts_are_paired_by_upsert(self) -> None:
        client, _, _ = await _run("lc-24", _rears(24), _fronts(24, {n: f"r{n}" for n in range(2, 25)}))

        assert client.saved == [
            (
                FRONT_PORT_KIND,
                {"id": "f1", "rear_port": {"id": "r1"}},
                {"allow_upsert": True, "update_group_context": False},
            )
        ]

    @pytest.mark.asyncio
    async def test_a_misplaced_or_extra_port_is_warned_about_and_left(self) -> None:
        paired = {**_jack(12), 1: "r2"}
        client, logger, _ = await _run("sc-12", _rears(13, "sc-upc"), _fronts(12, paired))

        assert client.saved == []
        warnings = " ".join(str(call.args[0]) for call in logger.warning.call_args_list)
        assert "rear 13" in warnings and "front 1" in warnings

    @pytest.mark.asyncio
    async def test_an_unknown_type_fails_the_run_and_writes_nothing(self) -> None:
        with pytest.raises(GeneratorError):
            await _run("lc-1000", [], [])


def _validate(*panels: dict[str, Any]) -> list[str]:
    """The errors of the check over the given panels."""
    check = cast(Any, CheckPatchPanelPairing.__new__(CheckPatchPanelPairing))
    errors: list[str] = []
    check.log_error = lambda message: errors.append(message)
    check.validate({"DcimPatchPanel": {"edges": [{"node": panel} for panel in panels]}})
    return errors


class TestCheck:
    """What the check accepts and refuses."""

    def test_a_complete_jack_panel_and_cassette_panel_pass(self) -> None:
        cassette = {n: f"r{(n - 1) // 12 + 1}" for n in range(1, 49)}

        assert (
            _validate(
                _panel("lc-24", _rears(24), _fronts(24, _jack(24))),
                _panel("mpo12-cassette-48", _rears(4, "mpo-12"), _fronts(48, cassette)),
            )
            == []
        )

    def test_missing_ports_are_one_error_per_side_in_ranges(self) -> None:
        errors = _validate(_panel("lc-24", _rears(20), _fronts(20, _jack(20))))

        assert errors == [
            "DcimPatchPanel 'r1:PP-01': rear port(s) 21-24 of its type are missing (rule: the panel holds every port "
            "of its type; run add_patch_panel)",
            "DcimPatchPanel 'r1:PP-01': front port(s) 21-24 of its type are missing (rule: the panel holds every port "
            "of its type; run add_patch_panel)",
        ]

    def test_unpaired_and_misplaced_fronts_are_errors(self) -> None:
        errors = _validate(_panel("lc-24", _rears(24), _fronts(24, {**_jack(24), 1: "r2", 5: None, 6: None})))

        assert any("front port(s) 5-6 have no rear port" in e for e in errors)
        assert any("front port(s) 1 are on another rear port" in e for e in errors)

    def test_extra_ports_and_another_connector_are_errors(self) -> None:
        errors = _validate(_panel("sc-12", _rears(13), _fronts(12, _jack(12))))

        assert any("rear 13 are not in its type" in e for e in errors)
        assert any("another connector" in e for e in errors)

    def test_an_unknown_type_is_one_error(self) -> None:
        (error,) = _validate(_panel(None, [], []))

        assert "panel_type None is not one of" in error


class TestRegistration:
    """The rules, the group, the queries and the definition agree."""

    def test_the_group_rule_and_the_type_rule_run_add_patch_panel_on_panels_only(self) -> None:
        rules = {r["name"]: r for r in trigger_rules() if r["action"] == "run-patch-panel-generator"}
        joined = [r for r in group_trigger_rules() if r["action"] == "run-patch-panel-generator"]

        assert sorted(rules) == ["trigger-patch-panel-on-type-updated"]
        rule = rules["trigger-patch-panel-on-type-updated"]
        assert rule["node_kind"] == "DcimPatchPanel"
        assert [m["attribute_name"] for m in rule["matches"]["data"]] == ["panel_type"]
        assert [(r["name"], r["group"]) for r in joined] == [("trigger-patch-panel-on-added", GROUP_PATCH_PANELS)]
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
        for name in ("patch_panel_data", "patch_panel_pairing"):
            assert "panel_type { value }" in (ROOT / queries[name]).read_text(), name
        assert checks["validate_patch_panel_pairing"]["class_name"] == "CheckPatchPanelPairing"
        assert CheckPatchPanelPairing.query == "patch_panel_pairing"

    def test_every_declared_panel_is_a_member_of_the_group_and_names_a_known_type(self) -> None:
        """The generator starts when a panel joins patch_panels and builds the ports of its type."""
        panels = declared("DcimPatchPanel", ROOT / "data" / "demos")
        outside = sorted({str(obj.path.relative_to(ROOT)) for obj in panels if GROUP_PATCH_PANELS not in obj.groups()})
        unknown = sorted(
            f"{obj.path.relative_to(ROOT)}: {obj.item.get('panel_type')}"
            for obj in panels
            if obj.item.get("panel_type") not in PANEL_TYPES
        )

        assert panels
        assert not outside, f"panels not in '{GROUP_PATCH_PANELS}': {outside}"
        assert not unknown, f"panels without a known panel_type: {unknown}"
