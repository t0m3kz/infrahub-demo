"""checks/devices/software_lifecycle.py: what runs an image that is not approved, and the registration of the check."""

from __future__ import annotations

from typing import Any, cast

import pytest
import yaml

from checks.devices.software_lifecycle import CheckSoftwareLifecycle
from tests.integration.demo_data import ROOT
from tests.unit.query_shape_helpers import query_tree, unselected

QUERY = ROOT / "queries" / "validation" / "software_lifecycle.gql"


def _v(value: Any) -> dict[str, Any]:
    return {"value": value}


def _runner(kind: str, name: str) -> dict[str, Any]:
    return {"node": {"id": f"id-{name}", "__typename": kind, "display_label": name}}


def _image(
    lifecycle: str = "approved",
    *runners: dict[str, Any],
    vulnerable: bool = False,
    reason: str | None = None,
    name: str = "arista_eos-4.28.3M",
) -> dict[str, Any]:
    return {
        "id": f"img-{name}",
        "name": _v(name),
        "version": _v("4.28.3M"),
        "lifecycle": _v(lifecycle),
        "vulnerable": _v(vulnerable),
        "vulnerability_reason": _v(reason),
        "platform": {"node": {"name": _v("arista_eos")}},
        "runners": {"edges": list(runners)},
    }


def _edges(*images: dict[str, Any]) -> dict[str, Any]:
    return {"edges": [{"node": image} for image in images]}


def _run(by_lifecycle: tuple[dict[str, Any], ...] = (), by_vulnerability: tuple[dict[str, Any], ...] = ()) -> list:
    """(severity, finding) for the two sets of one query answer."""
    check = cast(Any, CheckSoftwareLifecycle.__new__(CheckSoftwareLifecycle))
    findings: list[tuple[str, dict[str, Any]]] = []
    check.log_error = lambda **kwargs: findings.append(("error", kwargs))
    check.log_warning = lambda message: findings.append(("warning", {"message": message}))
    check.validate({"by_lifecycle": _edges(*by_lifecycle), "by_vulnerability": _edges(*by_vulnerability)})
    return findings


def test_every_runner_of_an_end_of_life_image_is_an_error_whatever_its_kind() -> None:
    """A device, a cluster and a cloud instance share one edge: all three are reported, by their own kind."""
    runners = [
        _runner("DcimPhysicalDevice", "ktw1-leaf-01"),
        _runner("ManagedHypervisorCluster", "ktw1-vsphere-cl01"),
        _runner("CloudInstance", "web-1"),
    ]
    findings = _run(by_lifecycle=(_image("end_of_life", *runners),))

    assert [severity for severity, _ in findings] == ["error"] * 3
    assert [f["object_type"] for _, f in findings] == [
        "DcimPhysicalDevice",
        "ManagedHypervisorCluster",
        "CloudInstance",
    ]
    assert "which is end of life." in findings[0][1]["message"]
    assert "arista_eos-4.28.3M (arista_eos 4.28.3M)" in findings[0][1]["message"]


def test_a_deprecated_image_is_a_warning() -> None:
    """Still supported: the runner is moved at the next window, so it does not fail the proposed change."""
    findings = _run(by_lifecycle=(_image("deprecated", _runner("DcimPhysicalDevice", "ktw1-leaf-01")),))

    assert [severity for severity, _ in findings] == ["warning"]
    assert "ktw1-leaf-01 (DcimPhysicalDevice) runs" in findings[0][1]["message"]


def test_a_vulnerable_image_is_an_error_even_when_it_is_approved_and_the_reason_is_in_the_message() -> None:
    image = _image("approved", _runner("CloudInstance", "web-1"), vulnerable=True, reason="CVE-2025-1234, fix 4.32.1F")
    findings = _run(by_vulnerability=(image,))

    assert [severity for severity, _ in findings] == ["error"]
    assert "which is vulnerable (CVE-2025-1234, fix 4.32.1F)." in findings[0][1]["message"]


def test_a_deprecated_and_vulnerable_image_is_one_error_that_says_both() -> None:
    """The image is in both sets of the query: it is reported once per runner, and the vulnerability makes it an error."""
    image = _image("deprecated", _runner("DcimPhysicalDevice", "x"), vulnerable=True)
    findings = _run(by_lifecycle=(image,), by_vulnerability=(image,))

    assert [severity for severity, _ in findings] == ["error"]
    assert "vulnerable and deprecated" in findings[0][1]["message"]


def test_an_approved_image_and_an_image_without_runners_report_nothing() -> None:
    assert _run(by_lifecycle=(_image("approved", _runner("DcimPhysicalDevice", "x")), _image("end_of_life"))) == []


def test_the_fixtures_emit_only_fields_the_query_selects() -> None:
    """The canned rows match software_lifecycle.gql, which must select the runners with their kind and label."""
    tree = query_tree(QUERY)
    for root in ("by_lifecycle", "by_vulnerability"):
        node = tree[root]["edges"]["node"]
        assert unselected([_image("end_of_life", _runner("CloudInstance", "web-1"))], node) == []
        assert {"lifecycle", "vulnerable", "vulnerability_reason", "runners", "platform"} <= set(node)


def test_the_check_and_its_query_are_registered() -> None:
    config = yaml.safe_load((ROOT / ".infrahub.yml").read_text())
    checks = {c["name"]: c for c in config["check_definitions"]}
    queries = {q["name"]: q["file_path"] for q in config["queries"]}

    assert checks["validate_software_lifecycle"]["class_name"] == "CheckSoftwareLifecycle"
    assert CheckSoftwareLifecycle.query == "software_lifecycle"
    assert (ROOT / queries["software_lifecycle"]).is_file()


@pytest.mark.parametrize(
    ("attribute", "values"),
    [("lifecycle", {"approved", "deprecated", "end_of_life"}), ("vulnerable", None), ("vulnerability_reason", None)],
)
def test_the_image_schema_has_the_fields_the_check_reads(attribute: str, values: set[str] | None) -> None:
    """A new lifecycle choice must be classified by the check, or it is silently ignored."""
    schema = yaml.safe_load((ROOT / "schemas" / "extensions" / "dcim" / "software_image.yml").read_text())
    image = next(n for n in schema["nodes"] if n["name"] == "SoftwareImage")
    field = next(a for a in image["attributes"] if a["name"] == attribute)

    if values is not None:
        assert {c["name"] for c in field["choices"]} == values
