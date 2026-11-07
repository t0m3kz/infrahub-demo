"""checks/shared_vlan.py: a shared segment has the VLAN id its VNI gives on every switch (SV1)."""

from __future__ import annotations

from typing import Any, cast

import yaml

from checks.shared_vlan import CheckSharedVlan, shared_vlan_errors
from tests.integration.demo_data import ROOT
from tests.unit.query_shape_helpers import query_tree, unselected

QUERY = ROOT / "queries" / "validation" / "shared_vlan.gql"


def _v(value: Any) -> dict[str, Any]:
    return {"value": value}


def _segment(scope: str = "shared", name: str = "c900-sync-p") -> dict[str, Any]:
    return {"id": f"seg-{name}", "name": _v(name), "vlan_scope": _v(scope)}


def _deployment(vni: int | None, *rows: tuple[str, int], name: str = "c900-sync-p", scope: str = "shared") -> dict:
    return {
        "id": f"sd-{name}-{vni}",
        "vni": _v(vni),
        "segment": {"node": {"id": f"seg-{name}", "vlan_scope": _v(scope)}},
        "deployment": {"node": {"id": "dc-1", "display_label": "DC-1"}},
        "vlans": {
            "edges": [
                {"node": {"id": f"row-{dev}", "vlan_id": _v(vlan), "device": {"node": {"id": dev, "name": _v(dev)}}}}
                for dev, vlan in rows
            ]
        },
    }


def _run(segments: list[dict[str, Any]], deployments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    check = cast(Any, CheckSharedVlan.__new__(CheckSharedVlan))
    findings: list[dict[str, Any]] = []
    check.log_error = lambda **kwargs: findings.append(kwargs)
    check.validate(
        {
            "ManagedVxlanSegment": {"edges": [{"node": s} for s in segments]},
            "ManagedSegmentDeployment": {"edges": [{"node": d} for d in deployments]},
        }
    )
    return findings


def test_rows_with_the_id_of_the_vni_pass_in_every_deployment() -> None:
    assert (
        _run([_segment()], [_deployment(12500, ("a", 2500)), _deployment(12500, ("b", 2500), name="c900-sync-p")]) == []
    )


def test_a_row_with_another_id_is_reported_with_the_expected_one() -> None:
    findings = _run([_segment()], [_deployment(12500, ("a", 2500), ("b", 2501))])

    assert len(findings) == 1
    assert "2501 on 'b'" in findings[0]["message"] and "gives VLAN 2500" in findings[0]["message"]
    assert findings[0]["object_id"] == "row-b"


def test_a_vni_outside_the_shared_band_or_two_vnis_are_reported_once_per_segment() -> None:
    assert len(_run([_segment()], [_deployment(10100, ("a", 100))])) == 1
    assert len(_run([_segment()], [_deployment(12500, ("a", 2500)), _deployment(12501, ("b", 2501))])) == 1
    assert len(_run([_segment()], [_deployment(None, ("a", 2500))])) == 1


def test_a_local_segment_is_not_judged() -> None:
    assert _run([_segment("local")], [_deployment(10100, ("a", 100), scope="local")]) == []


def test_the_pure_function_and_the_check_agree() -> None:
    errors = shared_vlan_errors(
        [{"id": "s", "name": "x", "vlan_scope": "shared"}],
        [{"segment": {"id": "s"}, "vni": 12600, "vlans": [{"id": "r", "vlan_id": 7, "device": {"name": "sw"}}]}],
    )
    assert [object_id for _, object_id in errors] == ["r"]


def test_the_fixtures_emit_only_fields_the_query_selects() -> None:
    tree = query_tree(QUERY)
    assert unselected([_segment()], tree["ManagedVxlanSegment"]["edges"]["node"]) == []
    assert unselected([_deployment(12500, ("a", 2500))], tree["ManagedSegmentDeployment"]["edges"]["node"]) == []


def test_the_check_and_its_query_are_registered() -> None:
    config = yaml.safe_load((ROOT / ".infrahub.yml").read_text())
    checks = {c["name"]: c for c in config["check_definitions"]}
    queries = {q["name"]: q["file_path"] for q in config["queries"]}

    assert checks["validate_shared_vlan"]["class_name"] == "CheckSharedVlan"
    assert CheckSharedVlan.query == "shared_vlan" and (ROOT / queries["shared_vlan"]).is_file()
