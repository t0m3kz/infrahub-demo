"""The real CheckSuiteCapacity rules over payloads built from the shipped demo object files.

``tests/unit/simulators/suite_capacity_payloads.py`` shapes the YAML a demo loads like the response of
``queries/validation/suite_capacity.gql`` (every rack with its suite capacity and its pod's design envelope). The
check then runs unchanged: this is the offline proof that the suites, the racks and the pod designs of the data agree.
The tests that break the data on purpose show the proof is not vacuous.

The demo roots below are the sets that load together (one stack at a time): the DC demo, the DC6 build-up on top of
it (switch, rack, pod and server stages), the colocation demo, the 30_all demo and the Arista MSS-G fabric.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, cast

import pytest

from checks.suite_capacity import CheckSuiteCapacity
from tests.integration.demo_data import ROOT, declared
from tests.unit.simulators.suite_capacity_payloads import DEMOS, DESIGN_FIELDS, SUITE, build_payloads
from tests.unit.test_circuit_query_shapes import _QUERIES, _payload_paths, _selected_paths
from utils.data_cleaning import clean_data

DEMO_SETS: dict[str, tuple[str, ...]] = {
    "01_data_center": ("01_data_center",),
    "dc6_build_up": ("01_data_center", "02_switch_dc6", "03_rack_dc6", "04_pod_dc6", "06_servers"),
    "10_colocation": ("10_colocation",),
    "30_all": ("30_all",),
    "32_arista_mssg": ("32_arista_mssg",),
}
QUERY = _QUERIES / "validation" / "suite_capacity.gql"


def _roots(label: str) -> tuple[Path, ...]:
    return tuple(DEMOS / directory for directory in DEMO_SETS[label])


def _findings(payloads: dict[str, Any]) -> list[dict[str, Any]]:
    """Run the check over the payloads and return what it logged (message, object id and kind)."""
    check = cast(Any, CheckSuiteCapacity.__new__(CheckSuiteCapacity))
    findings: list[dict[str, Any]] = []
    check.log_error = lambda **kwargs: findings.append(kwargs)
    check.validate(payloads)
    return findings


def _lower(payloads: dict[str, Any], suite_name: str, field: str) -> dict[str, Any]:
    """A copy of the payloads where every rack of suite ``suite_name`` sees ``field`` of its suite one lower."""
    lowered = copy.deepcopy(payloads)
    for edge in lowered["LocationRack"]["edges"]:
        suite = edge["node"]["parent"]["node"]
        if suite["name"]["value"] == suite_name:
            suite[field]["value"] -= 1
    return lowered


@pytest.mark.parametrize("label", sorted(DEMO_SETS))
def test_the_shipped_data_fits_its_suites(label: str) -> None:
    """Zero findings: every rack lies in its suite's grid and every pod's design fits the suites hosting its racks."""
    assert [finding["message"] for finding in _findings(build_payloads(*_roots(label)))] == []


@pytest.mark.parametrize("label", sorted(DEMO_SETS))
def test_the_payloads_hold_the_racks_the_data_declares(label: str) -> None:
    """No rack is silently missing from the simulation."""
    declared_racks = {obj.item["shortname"] for obj in declared("LocationRack", *_roots(label))}
    simulated = {rack["name"] for rack in clean_data(build_payloads(*_roots(label)))["LocationRack"]}
    assert declared_racks and simulated == declared_racks


def test_the_dc_demos_have_pods_with_a_design_to_judge() -> None:
    """The DC payloads are not vacuous: pods with their designs host racks in suites (colocation racks have none)."""
    racks = clean_data(build_payloads(*_roots("01_data_center")))["LocationRack"]
    designs = {
        rack["deployment"]["design"]["name"]
        for rack in racks
        if rack["deployment"] and rack["deployment"].get("design")
    }
    assert {"S_MIDDLE", "M_MIDDLE", "S_TOR", "S_MIXED", "M_MIXED", "S_BORDER_SPINE_POD"} <= designs
    colocation = clean_data(build_payloads(*_roots("30_all")))["LocationRack"]
    assert any(rack["deployment"] and not rack["deployment"].get("design") for rack in colocation)


@pytest.mark.parametrize(
    ("label", "suite_name", "field"),
    [
        ("01_data_center", "muc-1-s-1", "rows"),
        ("01_data_center", "muc-1-s-1", "racks_per_row"),
        ("dc6_build_up", "ktw-1-s-1", "rows"),
        ("dc6_build_up", "ktw-1-s-4", "racks_per_row"),
        ("30_all", "fra-1-s-1", "rows"),
        ("30_all", "fra-1-s-1", "racks_per_row"),
        ("30_all", "fr2-s1", "racks_per_row"),
        ("32_arista_mssg", "rom-1-s-1", "rows"),
        ("10_colocation", "mcr-ams1-s1", "rows"),
    ],
)
def test_lowering_a_suite_by_one_is_reported(label: str, suite_name: str, field: str) -> None:
    """Negative control: a suite one row (or one slot) smaller than the data needs makes the check fail on it."""
    findings = _findings(_lower(build_payloads(*_roots(label)), suite_name, field))

    assert findings, f"{suite_name}: {field} lowered by one but nothing was reported"
    assert all(suite_name in finding["message"] for finding in findings)


def test_lowering_a_dc_suite_is_reported_for_the_pod_and_the_rack_that_no_longer_fit() -> None:
    """DC10's suite is exactly the S_MIXED-sized grid: one row fewer leaves the pod and its row-2 racks outside it."""
    findings = _findings(_lower(build_payloads(*_roots("30_all")), "fra-1-s-1", "rows"))

    assert {f["object_type"] for f in findings} == {"TopologyPod", "LocationRack"}
    assert len([f for f in findings if f["object_type"] == "LocationRack"]) == 2


def test_every_demo_suite_states_a_positive_row_and_slot_count() -> None:
    """LocationSuite.rows and racks_per_row are required: every suite under data/ sets both as positive integers."""
    suites = declared(SUITE, ROOT / "data")
    assert suites
    for suite in suites:
        for attribute in ("rows", "racks_per_row"):
            value = suite.item.get(attribute)
            assert isinstance(value, int) and not isinstance(value, bool) and value >= 1, (
                f"{suite.item.get('shortname')} in {suite.path.relative_to(ROOT)}: {attribute}={value!r}"
            )


def test_the_demo_sets_cover_every_suite_and_rack_under_data() -> None:
    """A suite or rack declared somewhere under data/ that no demo set simulates would escape the proof above."""
    in_sets = {DEMOS / directory for directories in DEMO_SETS.values() for directory in directories}
    for kind in (SUITE, "LocationRack"):
        everywhere = {obj.item["shortname"] for obj in declared(kind, ROOT / "data")}
        simulated = {obj.item["shortname"] for root in in_sets for obj in declared(kind, root)}
        assert everywhere == simulated, f"{kind} outside the demo sets: {sorted(everywhere - simulated)}"


def test_the_query_selects_what_the_check_reads_and_nothing_else() -> None:
    """The fields the check reads from a rack are exactly the ones suite_capacity.gql selects."""
    reads = {
        "id",
        "name",
        "row_index",
        "index",
        "parent.id",
        "parent.name",
        "parent.rows",
        "parent.racks_per_row",
        "deployment.id",
        "deployment.name",
        "deployment.design.id",
        "deployment.design.name",
        *(f"deployment.design.{field}" for field in DESIGN_FIELDS),
    }
    selected = _selected_paths(QUERY, "LocationRack")
    leaves = {path for path in selected if not any(other.startswith(f"{path}.") for other in selected)}
    assert leaves == reads


def test_the_simulated_payload_carries_only_fields_the_query_selects() -> None:
    """A payload field the query does not select is a field no real response carries."""
    payloads = build_payloads(*_roots("30_all"))
    selected = _selected_paths(QUERY, "LocationRack")
    for edge in payloads["LocationRack"]["edges"]:
        unselected = _payload_paths(edge["node"]) - selected
        assert not unselected, f"fields the query does not select: {sorted(unselected)}"
