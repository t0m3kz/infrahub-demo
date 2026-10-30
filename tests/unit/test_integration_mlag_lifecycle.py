"""Unit tests for the pure readers and judges of test_66's MLAG scenario (tests/integration/mlag_lifecycle_helpers.py)."""

from __future__ import annotations

from typing import Any

import pytest

from tests.integration.mlag_lifecycle_helpers import (
    artifact_errors,
    candidate_errors,
    mlag_check_findings,
    new_findings,
    parse_candidates,
    parse_rows,
    pick_candidate,
    snapshot_errors,
    vlan_lines,
)

NAMES = {"dev-a": "leaf-a", "dev-b": "leaf-b"}


def _member(device_id: str, name: str, role: str = "access-leaf", rack: str | None = "rack-1") -> dict[str, Any]:
    node: dict[str, Any] = {"id": device_id, "name": {"value": name}, "role": {"value": role}}
    node["rack"] = {"node": {"id": rack}} if rack else {"node": None}
    return {"node": node}


def _mlag(name: str, *members: dict[str, Any]) -> dict[str, Any]:
    return {"node": {"id": f"id-{name}", "name": {"value": name}, "capabilities": {"edges": list(members)}}}


def _row(row_id: str, device: str, deployment: str, vlan: int, svi: str | None) -> dict[str, Any]:
    return {
        "node": {
            "id": row_id,
            "vlan_id": {"value": vlan},
            "device": {"node": {"id": device}},
            "segment_deployment": {"node": {"id": deployment}},
            "svi": {"node": {"id": svi}} if svi else {"node": None},
        }
    }


def _result(rows: list[dict[str, Any]], svis: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"ManagedVlanDomainSegment": {"edges": rows}, "DcimVirtualInterface": {"edges": svis or []}}


def _svi(iface_id: str, device: str, name: str) -> dict[str, Any]:
    return {"node": {"id": iface_id, "name": {"value": name}, "device": {"node": {"id": device}}}}


def _healthy_rows() -> Any:
    return parse_rows(
        _result(
            [
                _row("r1", "dev-a", "dep-1", 100, "svi-a1"),
                _row("r2", "dev-b", "dep-1", 100, "svi-b1"),
            ],
            [_svi("svi-a1", "dev-a", "Vlan100"), _svi("svi-b1", "dev-b", "Vlan100")],
        )
    )


def test_candidates_are_parsed_with_role_and_rack_sorted_by_name() -> None:
    """A member without rack parses to rack_id None; the list is sorted by MLAG name."""
    result = {
        "ManagedMLAG": {
            "edges": [
                _mlag("z-mlag", _member("dev-a", "leaf-a"), _member("dev-b", "leaf-b", rack=None)),
                _mlag("a-mlag", _member("dev-c", "leaf-c")),
            ]
        }
    }

    candidates = parse_candidates(result)

    assert [c["name"] for c in candidates] == ["a-mlag", "z-mlag"]
    assert candidates[1]["members"][1] == {"id": "dev-b", "name": "leaf-b", "role": "access-leaf", "rack_id": None}


def test_rows_are_grouped_by_device_and_deployment_with_their_svi() -> None:
    rows, svis = _healthy_rows()

    assert rows["dev-a"]["dep-1"] == {"id": "r1", "vlan_id": 100, "svi_id": "svi-a1"}
    assert svis == {"dev-a": {"Vlan100": "svi-a1"}, "dev-b": {"Vlan100": "svi-b1"}}


def test_empty_results_parse_to_nothing() -> None:
    assert parse_rows({}) == ({}, {})
    assert parse_candidates({}) == []


def test_a_healthy_access_leaf_pair_with_rows_passes_the_precondition() -> None:
    rows, _ = _healthy_rows()
    candidate = parse_candidates(
        {"ManagedMLAG": {"edges": [_mlag("m", _member("dev-a", "leaf-a"), _member("dev-b", "leaf-b"))]}}
    )[0]

    assert candidate_errors(candidate, rows) == []


@pytest.mark.parametrize(
    ("members", "expected"),
    [
        ([_member("dev-a", "leaf-a"), _member("dev-b", "leaf-b", role="leaf")], "leaf-b is a leaf"),
        ([_member("dev-a", "leaf-a"), _member("dev-b", "leaf-b", rack=None)], "leaf-b is in no rack"),
        ([_member("dev-a", "leaf-a"), _member("dev-c", "leaf-c")], "leaf-c has no ManagedVlanDomainSegment row"),
        ([_member("dev-a", "leaf-a")], "has 1 member(s)"),
    ],
)
def test_the_precondition_names_why_a_pair_cannot_show_the_property(
    members: list[dict[str, Any]], expected: str
) -> None:
    rows, _ = _healthy_rows()
    candidate = parse_candidates({"ManagedMLAG": {"edges": [_mlag("m", *members)]}})[0]

    assert any(expected in error for error in candidate_errors(candidate, rows))


def test_pick_candidate_takes_the_first_valid_one_and_reports_all_failures_otherwise() -> None:
    rows, _ = _healthy_rows()
    bad = _mlag("a-bad", _member("dev-a", "leaf-a", role="leaf"), _member("dev-b", "leaf-b"))
    good = _mlag("b-good", _member("dev-a", "leaf-a"), _member("dev-b", "leaf-b"))

    picked, errors = pick_candidate(parse_candidates({"ManagedMLAG": {"edges": [good, bad]}}), rows)
    nothing, reasons = pick_candidate(parse_candidates({"ManagedMLAG": {"edges": [bad]}}), rows)
    empty, empty_reasons = pick_candidate([], rows)

    assert picked is not None
    assert picked["name"] == "b-good"
    assert errors == []
    assert nothing is None
    assert reasons[0].startswith("a-bad:")
    assert empty is None
    assert empty_reasons == ["no ManagedMLAG exists"]


def test_identical_snapshots_have_no_errors() -> None:
    assert snapshot_errors(_healthy_rows(), _healthy_rows(), NAMES) == []


def test_snapshot_errors_name_a_deleted_added_renumbered_and_recreated_row() -> None:
    before = _healthy_rows()
    after = parse_rows(
        _result(
            [
                _row("r1-new", "dev-a", "dep-1", 100, "svi-a1"),
                _row("r2", "dev-b", "dep-1", 101, "svi-b1"),
                _row("r3", "dev-b", "dep-2", 102, None),
            ],
            [
                _svi("svi-a1", "dev-a", "Vlan100"),
                _svi("svi-b1-new", "dev-b", "Vlan100"),
                _svi("svi-x", "dev-b", "Vlan9"),
            ],
        )
    )

    errors = snapshot_errors(before, after, NAMES)

    assert any("leaf-a: row of deployment dep-1 changed" in e and "r1-new" in e for e in errors)
    assert any("leaf-b: row of deployment dep-1 changed" in e and "101" in e for e in errors)
    assert any("leaf-b: row of deployment dep-2 appeared" in e for e in errors)
    assert any("leaf-b: SVI Vlan100 was re-created" in e for e in errors)
    assert any("leaf-b: SVI Vlan9 appeared" in e for e in errors)


def test_snapshot_errors_name_a_deleted_row_and_svi() -> None:
    before = _healthy_rows()
    after = parse_rows(_result([_row("r1", "dev-a", "dep-1", 100, "svi-a1")], [_svi("svi-a1", "dev-a", "Vlan100")]))

    errors = snapshot_errors(before, after, NAMES)

    assert errors == ["leaf-b: row of deployment dep-1 was deleted", "leaf-b: SVI Vlan100 was deleted"]


EOS = """\
vlan 100
   name a
vlan 4094
   name MLAG
mlag configuration
   domain-id d
interface Vlan100
   ip address virtual 10.0.0.1/24
interface Vlan4094
   ip address 10.9.9.1/31
interface Vxlan1
   vxlan vlan 100 vni 10100
   vxlan vlan 4094 vni 99
"""
NXOS = """\
vlan 100
  vn-segment 10100
vpc domain 1
interface Vlan100
interface nve1
  member vni 10100
"""


def test_vlan_lines_keep_the_segment_vlans_and_drop_the_mlags_own() -> None:
    assert vlan_lines(EOS, {100}) == [
        "vlan 100",
        "interface Vlan100",
        "   vxlan vlan 100 vni 10100",
    ]


def test_vlan_lines_accept_string_ids_and_keep_vni_lines_of_nxos() -> None:
    assert vlan_lines(NXOS, {"100"}) == ["vlan 100", "  vn-segment 10100", "interface Vlan100", "  member vni 10100"]


def test_vlan_lines_of_a_config_without_the_vlans_is_empty() -> None:
    assert vlan_lines(EOS, {200}) == []


def test_artifact_errors_ignore_the_mlag_block_but_see_a_lost_vlan_line() -> None:
    before = {"leaf-a": vlan_lines(EOS, {100})}
    without_mlag = EOS.replace("mlag configuration\n   domain-id d\n", "")
    lost = EOS.replace("interface Vlan100\n", "")

    assert artifact_errors(before, {"leaf-a": vlan_lines(without_mlag, {100})}) == []
    errors = artifact_errors(before, {"leaf-a": vlan_lines(lost, {100})})
    assert len(errors) == 1
    assert "lost ['interface Vlan100']" in errors[0]


def test_artifact_errors_refuse_a_render_that_proves_nothing() -> None:
    assert "proves nothing" in artifact_errors({"leaf-a": []}, {"leaf-a": []})[0]


def test_new_findings_ignore_what_the_stack_already_had() -> None:
    assert new_findings(["old"], ["old", "new b", "new a"]) == ["new a", "new b"]
    assert new_findings(["old"], []) == []


def _check_result(vlan_b: int) -> dict[str, Any]:
    members = [{"id": "dev-a", "name": {"value": "leaf-a"}}, {"id": "dev-b", "name": {"value": "leaf-b"}}]
    row_a = _row("r1", "dev-a", "dep-1", 100, None)
    row_b = _row("r2", "dev-b", "dep-1", vlan_b, None)
    for row in (row_a, row_b):
        row["node"]["segment_deployment"]["node"]["segment"] = {"node": {"name": {"value": "web"}}}
        row["node"]["device"]["node"]["name"] = {"value": "x"}
    return {
        "ManagedMLAG": {
            "edges": [
                {
                    "node": {
                        "id": "m",
                        "name": {"value": "m"},
                        "peer_devices": {"value": "leaf-a,leaf-b"},
                        "capabilities": {"edges": [{"node": m} for m in members]},
                    }
                }
            ]
        },
        "ManagedVlanDomainSegment": {"edges": [row_a, row_b]},
        "DcimLAGInterface": {"edges": []},
    }


def test_the_check_findings_are_empty_for_equal_pair_rows() -> None:
    assert mlag_check_findings(_check_result(100)) == []


def test_the_check_findings_report_differing_pair_rows() -> None:
    findings = mlag_check_findings(_check_result(101))

    assert len(findings) == 1
    assert "the two members of an MLAG hold the same VLAN id" in findings[0]
