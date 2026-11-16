"""The envelope a zone or virtual colocation design sets on its fabric_templates entries
(generators/model/colocation_model.py through ``_valid_fabric_templates``): what is warned about and what is skipped.
"""

from __future__ import annotations

import itertools
from typing import Any

import pytest

from tests.unit.colocation_zone_generator_helpers import (
    _EDGE_TEMPLATE,
    _EDGE_TEMPLATE_WITH_ALL_SERVICE_PORTS,
    _VIRTUAL_EDGE_TEMPLATE,
    _made_virtual,
    _made_zone,
    _zone,
    seed_nodes,
    zone_env,
)
from tests.unit.simulators.fabric_designs import colo_metro_design, colo_zone_design

_NODE_KINDS = pytest.mark.parametrize(
    ("node", "metro"),
    [(_made_zone, "fr"), (_made_virtual, "am")],
    ids=["zone", "virtual-colocation"],
)


def _sized(node: Any, *, design: str | None = None, metro_design: str | None = None) -> Any:
    """A zone or virtual colocation generator with the designs set."""
    gen = node()
    if design:
        gen.data["design"] = colo_zone_design(design)
    if metro_design:
        gen.data["parent"]["design"] = colo_metro_design(metro_design)
    return gen


def _entry(role: str, quantity: int, template_id: str, template: dict[str, Any] | None = None) -> dict[str, Any]:
    """A cleaned fabric_templates row; the default edge template exposes every service port role."""
    base = template or _EDGE_TEMPLATE_WITH_ALL_SERVICE_PORTS
    return {"role": role, "quantity": quantity, "template": {**base, "id": template_id}}


class TestDesignWarnings:
    """Findings against the envelope are warnings (an error would fail the task), once per finding."""

    @_NODE_KINDS
    def test_design_not_among_the_metro_designs_zone_templates_warns_and_keeps_the_entries(
        self, node: Any, metro: str
    ) -> None:
        """The metro's supernets were not sized for it; the check errors, the generator only warns."""
        gen = _sized(node, metro_design="S")
        gen.data["design"] = {**colo_zone_design("EDGE"), "name": "ODD"}
        entries = gen.data["fabric_templates"]

        assert gen._valid_fabric_templates() == entries

        warnings = [c.args[0] for c in gen.logger.warning.call_args_list]
        assert len(warnings) == 1
        assert "'ODD'" in warnings[0]
        assert "zone_templates" in warnings[0]
        assert "['EDGE', 'EDGE_FW', 'EDGE_FW_LB']" in warnings[0]
        gen.logger.error.assert_not_called()

    @_NODE_KINDS
    def test_design_among_the_metro_designs_zone_templates_does_not_warn(self, node: Any, metro: str) -> None:
        gen = _sized(node, design="EDGE_FW_LB", metro_design="S")

        gen._valid_fabric_templates()

        gen.logger.warning.assert_not_called()

    @_NODE_KINDS
    def test_a_design_with_no_metro_design_is_never_compared(self, node: Any, metro: str) -> None:
        gen = _sized(node)
        gen.data["design"] = {**colo_zone_design("EDGE"), "name": "ODD"}

        gen._valid_fabric_templates()

        gen.logger.warning.assert_not_called()


class TestOverMaxEntries:
    """A role whose total passes the design maximum is skipped whole, judged in template-id order."""

    @staticmethod
    def _zone_with(entries: list[dict[str, Any]], design: dict[str, Any]) -> Any:
        gen = _made_zone(fabric_templates=entries)
        gen.data["design"] = design
        return gen

    @staticmethod
    def _ids(entries: list[dict[str, Any]]) -> list[str]:
        return [entry["template"]["id"] for entry in entries]

    def test_role_total_exactly_at_the_maximum_is_kept(self) -> None:
        entries = [_entry("edge", 2, "t-edge"), _entry("internal-firewall", 2, "t-fw", _EDGE_TEMPLATE)]
        gen = self._zone_with(entries, colo_zone_design("EDGE_FW"))

        assert gen._valid_fabric_templates() == entries
        gen.logger.warning.assert_not_called()

    def test_every_role_is_judged_against_its_own_maximum(self) -> None:
        """Edge, firewall and load-balancer totals do not share a budget."""
        entries = [
            _entry("edge", 2, "t-edge"),
            _entry("internal-firewall", 2, "t-fw", _EDGE_TEMPLATE),
            _entry("load-balancer", 2, "t-lb", _EDGE_TEMPLATE),
        ]
        gen = self._zone_with(entries, colo_zone_design("EDGE_FW_LB"))

        assert gen._valid_fabric_templates() == entries
        gen.logger.warning.assert_not_called()

    @pytest.mark.parametrize(
        ("role", "design_name", "key", "maximum"),
        [
            ("edge", "EDGE", "max_edges", 2),
            ("internal-firewall", "EDGE_FW", "max_firewalls", 2),
            ("load-balancer", "EDGE_FW_LB", "max_load_balancers", 2),
            ("load-balancer", "EDGE_FW", "max_load_balancers", 0),
            ("internal-firewall", "EDGE", "max_firewalls", 0),
        ],
    )
    def test_one_entry_past_the_maximum_is_skipped_with_a_warning_naming_the_limit(
        self, role: str, design_name: str, key: str, maximum: int
    ) -> None:
        """Beyond the envelope the pools are too small: the entry is dropped before any device exists."""
        edge = _entry("edge", 2, "t-edge")
        beyond = _entry(role, maximum + 1, "t-beyond", _EDGE_TEMPLATE)
        gen = self._zone_with([edge, beyond] if role != "edge" else [beyond], colo_zone_design(design_name))

        kept = gen._valid_fabric_templates()

        assert "t-beyond" not in self._ids(kept)
        warnings = [c.args[0] for c in gen.logger.warning.call_args_list]
        assert len(warnings) == 1
        assert f"{key}={maximum}" in warnings[0]
        assert f"role={role!r}" in warnings[0]
        assert design_name in warnings[0]
        gen.logger.error.assert_not_called()

    def test_an_entry_that_only_partly_fits_is_skipped_whole(self) -> None:
        """Two edges at max_edges 2 with one already counted: the second entry (quantity 2) goes, not half of it."""
        first, second = _entry("edge", 1, "t-a"), _entry("edge", 2, "t-b")
        gen = self._zone_with([first, second], colo_zone_design("EDGE"))

        assert gen._valid_fabric_templates() == [first]
        assert gen.logger.warning.call_count == 1

    def test_without_a_design_nothing_is_skipped(self) -> None:
        """No envelope, no cap here (the generator warns about the fallback once per run, the check errors)."""
        entries = [_entry("edge", 10, "t-edge"), _entry("edge", 10, "t-edge-2")]
        gen = _made_zone(fabric_templates=entries)

        assert gen._valid_fabric_templates() == entries
        gen.logger.warning.assert_not_called()

    @pytest.mark.parametrize("order", list(itertools.permutations(range(3))), ids=lambda o: "".join(map(str, o)))
    def test_same_entries_are_kept_whatever_order_the_query_returns_them_in(self, order: tuple[int, ...]) -> None:
        """Judged in template-id order (t-1 then t-2 then t-3) against max_edges 3: t-3 is always the one skipped."""
        design = {**colo_zone_design("EDGE_FW_LB"), "max_edges": 3}
        rows = [_entry("edge", 1, "t-3"), _entry("edge", 2, "t-1"), _entry("edge", 1, "t-2")]
        entries = [rows[i] for i in order]
        gen = self._zone_with(entries, design)

        kept = gen._valid_fabric_templates()

        assert sorted(self._ids(kept)) == ["t-1", "t-2"]
        assert self._ids(kept) == [entry["template"]["id"] for entry in entries if entry["template"]["id"] != "t-3"]
        assert gen.logger.warning.call_count == 1
        assert "(quantity 1)" in gen.logger.warning.call_args.args[0]

    @pytest.mark.parametrize("reverse", [False, True])
    def test_equal_template_entries_tie_break_on_quantity_not_on_query_order(self, reverse: bool) -> None:
        """Two entries of one role on one template: the smaller quantity is judged first, whatever the query order."""
        design = {**colo_zone_design("EDGE_FW_LB"), "max_edges": 2}
        rows = [_entry("edge", 2, "t-same"), _entry("edge", 1, "t-same")]
        gen = self._zone_with(list(reversed(rows)) if reverse else rows, design)

        kept = gen._valid_fabric_templates()

        assert [entry["quantity"] for entry in kept] == [1]

    def test_a_rerun_with_a_reordered_query_skips_the_same_entry(self) -> None:
        """Idempotency: the same skip decision on a second run, so the device set does not flip between runs."""
        design = {**colo_zone_design("EDGE_FW_LB"), "max_edges": 3}
        rows = [_entry("edge", 1, "t-3"), _entry("edge", 2, "t-1"), _entry("edge", 1, "t-2")]
        first = self._zone_with(rows, design)
        second = self._zone_with(list(reversed(rows)), design)

        assert sorted(self._ids(first._valid_fabric_templates())) == sorted(self._ids(second._valid_fabric_templates()))

    def test_virtual_colocation_is_held_to_its_design_too(self) -> None:
        gen = _made_virtual(fabric_templates=[_entry("edge", 3, "t-v", _VIRTUAL_EDGE_TEMPLATE)])
        gen.data["design"] = colo_zone_design("EDGE")

        assert gen._valid_fabric_templates() == []
        gen.logger.warning.assert_called_once()

    @pytest.mark.asyncio
    async def test_skipped_entries_never_reach_the_device_pass(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Only the kept entries are planned as devices."""
        kept, skipped = _entry("edge", 2, "t-a"), _entry("edge", 2, "t-b")
        node = _zone(fabric_templates=[kept, skipped])
        node["design"] = colo_zone_design("EDGE")
        gen, client = zone_env(monkeypatch)
        seed_nodes(client, node)

        await gen.generate({"TopologyColocationZone": [node]})

        assert sorted(n.data["name"] for n in client.store.values() if n.kind == "DcimPhysicalDevice") == [
            "eg-fr201",
            "eg-fr202",
        ]
        assert all(
            n.data["object_template"] == {"id": "t-a"} for n in client.store.values() if n.kind == "DcimPhysicalDevice"
        )

    @pytest.mark.asyncio
    async def test_when_every_entry_is_skipped_the_run_is_a_no_op(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A load balancer under a design that holds none: nothing usable, so no pools and no devices."""
        node = _zone(fabric_templates=[_entry("load-balancer", 2, "t-lb", _EDGE_TEMPLATE)])
        node["design"] = colo_zone_design("EDGE")
        gen, client = zone_env(monkeypatch)
        seed_nodes(client, node)

        await gen.generate({"TopologyColocationZone": [node]})

        assert client.saves == []
        gen.logger.error.assert_not_called()
