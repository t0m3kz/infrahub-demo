"""Unit tests for utils/cable_path.py: following a cable through passive patch panels."""

from __future__ import annotations

from utils.cable_path import DANGLING, LOOP, MISSING_PAIR, OK, trace_through_panels


def _jack(panel: str, name: str) -> dict[str, tuple[str, str, str]]:
    """The front and rear port of one jack, ids ``<panel>-f<name>`` / ``<panel>-r<name>``."""
    return {f"{panel}-f{name}": (panel, "front", name), f"{panel}-r{name}": (panel, "rear", name)}


def _cable(cables: dict[str, dict[str, str]], cable_id: str, end_a: str, end_b: str) -> None:
    """Register one cable on both of its ends."""
    cables[end_a] = {"cable_id": cable_id, "far_id": end_b}
    cables[end_b] = {"cable_id": cable_id, "far_id": end_a}


def test_a_plain_cable_without_a_panel_ends_at_the_far_port() -> None:
    """No panel on the way: one hop, the far port is the terminal."""
    cables: dict[str, dict[str, str]] = {}
    _cable(cables, "c1", "A", "B")

    result = trace_through_panels("A", cables, {})

    assert (result.status, result.terminal, result.hops) == (OK, "B", ("c1",))


def test_a_cross_connect_straight_through_one_panel() -> None:
    """device A -- front 7 | rear 7 -- device B."""
    cables: dict[str, dict[str, str]] = {}
    _cable(cables, "c1", "A", "pp-f7")
    _cable(cables, "c2", "pp-r7", "B")

    result = trace_through_panels("A", cables, _jack("pp", "7"))

    assert (result.status, result.terminal, result.hops) == (OK, "B", ("c1", "c2"))


def test_the_walk_is_the_same_from_the_other_end() -> None:
    """The pairing is symmetric: B reaches A through the same jack."""
    cables: dict[str, dict[str, str]] = {}
    _cable(cables, "c1", "A", "pp-f7")
    _cable(cables, "c2", "pp-r7", "B")

    result = trace_through_panels("B", cables, _jack("pp", "7"))

    assert (result.status, result.terminal, result.hops) == (OK, "A", ("c2", "c1"))


def test_two_panels_in_series() -> None:
    """A -- pp1 jack 1 -- pp2 jack 3 -- B: each panel pairs by its own name."""
    cables: dict[str, dict[str, str]] = {}
    ports = {**_jack("pp1", "1"), **_jack("pp2", "3")}
    _cable(cables, "c1", "A", "pp1-f1")
    _cable(cables, "c2", "pp1-r1", "pp2-f3")
    _cable(cables, "c3", "pp2-r3", "B")

    result = trace_through_panels("A", cables, ports)

    assert (result.status, result.terminal, result.hops) == (OK, "B", ("c1", "c2", "c3"))


def test_the_pair_is_the_same_name_in_the_same_panel_only() -> None:
    """Rear 1 of ANOTHER panel is not the pair of front 1."""
    cables: dict[str, dict[str, str]] = {}
    ports = {"pp1-f1": ("pp1", "front", "1"), "pp2-r1": ("pp2", "rear", "1")}
    _cable(cables, "c1", "A", "pp1-f1")
    _cable(cables, "c2", "pp2-r1", "B")

    result = trace_through_panels("A", cables, ports)

    assert (result.status, result.last_port) == (MISSING_PAIR, "pp1-f1")


def test_a_pair_without_a_cable_dangles() -> None:
    """The cable lands on front 7 and rear 7 has no cable: dangling, stopped at the uncabled pair."""
    cables: dict[str, dict[str, str]] = {}
    _cable(cables, "c1", "A", "pp-f7")

    result = trace_through_panels("A", cables, _jack("pp", "7"))

    assert (result.status, result.terminal, result.last_port, result.hops) == (DANGLING, None, "pp-r7", ("c1",))


def test_a_missing_pair_port_is_reported_as_such() -> None:
    """Rear 7 does not exist at all (the P1 defect): missing_pair, not dangling."""
    cables: dict[str, dict[str, str]] = {}
    _cable(cables, "c1", "A", "pp-f7")

    result = trace_through_panels("A", cables, {"pp-f7": ("pp", "front", "7")})

    assert (result.status, result.terminal, result.last_port) == (MISSING_PAIR, None, "pp-f7")


def test_front_cabled_to_the_rear_of_the_same_jack_is_a_loop() -> None:
    """Front 1 patched to rear 1 of one panel: the walk comes back to where it started."""
    cables: dict[str, dict[str, str]] = {}
    _cable(cables, "c1", "pp-f1", "pp-r1")

    result = trace_through_panels("pp-f1", cables, _jack("pp", "1"))

    assert result.status == LOOP and result.terminal is None


def test_a_loop_through_two_panels() -> None:
    """pp1 rear 1 -- pp2 front 1, pp2 rear 1 -- pp1 front 1: a ring with no end."""
    cables: dict[str, dict[str, str]] = {}
    ports = {**_jack("pp1", "1"), **_jack("pp2", "1")}
    _cable(cables, "c1", "pp1-r1", "pp2-f1")
    _cable(cables, "c2", "pp2-r1", "pp1-f1")

    result = trace_through_panels("pp1-r1", cables, ports)

    assert result.status == LOOP


def test_an_uncabled_start_dangles_with_no_hop() -> None:
    """The start port carries no cable."""
    result = trace_through_panels("A", {}, {})

    assert (result.status, result.terminal, result.hops, result.last_port) == (DANGLING, None, (), "A")
