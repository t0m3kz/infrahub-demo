"""Unit tests for CheckVirtualDevicePlacement (checks/virtual_device_placement.py).

The payloads are raw GraphQL responses shaped like queries/validation/virtual_device_placement.gql. The data test
reads the demo files: every declared DcimVirtualDevice names a data center or a customer footprint.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from checks.virtual_device_placement import ALLOWED_DEPLOYMENT_KINDS, VIRTUAL_DEVICE, CheckVirtualDevicePlacement
from tests.integration.demo_data import declared, distinct, name

_ROOT = Path(__file__).parents[2]
_DEMOS = _ROOT / "data" / "demos"


def _device(node_id: str, device_name: str, kind: str | None, deployment: str = "X") -> dict[str, Any]:
    """A raw device row; ``kind`` None is an unset deployment."""
    node = {"id": f"dep-{deployment}", "__typename": kind, "name": {"value": deployment}} if kind else None
    return {"id": node_id, "name": {"value": device_name}, "deployment": {"node": node}}


def _run(devices: list[dict[str, Any]], appliances: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Validate and return every logged finding (message, object_id, object_type)."""
    check = cast(Any, CheckVirtualDevicePlacement.__new__(CheckVirtualDevicePlacement))
    findings: list[dict[str, Any]] = []
    check.log_error = lambda **kwargs: findings.append(kwargs)
    check.validate(
        {
            "DcimVirtualDevice": {"edges": [{"node": device} for device in devices]},
            "CloudNetworkAppliance": {"edges": [{"node": device} for device in appliances or []]},
        }
    )
    return findings


@pytest.mark.parametrize("kind", ALLOWED_DEPLOYMENT_KINDS[VIRTUAL_DEVICE])
def test_allowed_deployment_kind_passes(kind: str) -> None:
    """A data center and both customer footprints are where a virtual device may be deployed."""
    assert _run([_device("vm-1", "c005-web-dc10-1", kind, "C005-P-DC10")]) == []


@pytest.mark.parametrize(
    "kind",
    [
        "TopologyColocationZone",
        "TopologyVirtualColocation",
        "TopologyColocationMetro",
        "TopologyPod",
        "TopologyCustomerCloud",
        "TopologyCustomerOffice",
    ],
)
def test_forbidden_deployment_kind_fails(kind: str) -> None:
    findings = _run([_device("vm-1", "eg-am-ne01", kind, "AM-NE")])

    assert len(findings) == 1
    assert findings[0]["object_id"] == "vm-1"
    assert findings[0]["object_type"] == "DcimVirtualDevice"


def test_finding_names_the_device_its_deployment_and_what_is_allowed() -> None:
    message = _run([_device("vm-1", "eg-am-ne01", "TopologyVirtualColocation", "AM-NE")])[0]["message"]

    assert "DcimVirtualDevice 'eg-am-ne01' is deployed to TopologyVirtualColocation 'AM-NE'" in message
    assert "TopologyDataCenter, TopologyCustomerDC, TopologyCustomerColocation" in message
    assert "CloudNetworkAppliance" in message


def test_unset_deployment_is_not_reported() -> None:
    """The field is optional: no deployment, no finding."""
    assert _run([_device("vm-1", "c005-web-dc10-1", None)]) == []


def test_empty_result_has_no_findings() -> None:
    assert _run([]) == []
    check = cast(Any, CheckVirtualDevicePlacement.__new__(CheckVirtualDevicePlacement))
    check.validate({})


def test_appliance_in_a_virtual_colocation_passes() -> None:
    assert _run([], [_device("app-1", "eg-am-ne01", "TopologyVirtualColocation", "AM-NE")]) == []


@pytest.mark.parametrize(
    "kind",
    ["TopologyColocationZone", "TopologyDataCenter", "TopologyCustomerDC", "TopologyCustomerColocation", "TopologyPod"],
)
def test_appliance_outside_a_virtual_colocation_fails(kind: str) -> None:
    """A provider-hosted function is rented in a virtual colocation; the schema's peer is the generic, so this is the rule."""
    findings = _run([], [_device("app-1", "eg-fr201", kind, "FR2")])

    assert len(findings) == 1
    assert findings[0]["object_id"] == "app-1"
    assert findings[0]["object_type"] == "CloudNetworkAppliance"
    assert f"CloudNetworkAppliance 'eg-fr201' is deployed to {kind} 'FR2'" in findings[0]["message"]
    assert "TopologyVirtualColocation" in findings[0]["message"]


def test_appliance_without_a_deployment_is_not_reported() -> None:
    assert _run([], [_device("app-1", "eg-am-ne01", None)]) == []


def test_each_wrong_device_is_its_own_finding() -> None:
    findings = _run(
        [
            _device("vm-1", "ok", "TopologyCustomerDC", "C005-P-DC10"),
            _device("vm-2", "bad-zone", "TopologyColocationZone", "FR2"),
            _device("vm-3", "bad-vc", "TopologyVirtualColocation", "AM-NE"),
        ]
    )

    assert [finding["object_id"] for finding in findings] == ["vm-2", "vm-3"]


def test_query_is_registered_and_selects_what_the_check_reads() -> None:
    """The check and its query are registered, and the generic peer is read through id/__typename/name only."""
    config = yaml.safe_load((_ROOT / ".infrahub.yml").read_text())
    queries = {query["name"]: query["file_path"] for query in config["queries"]}
    checks = {check["name"]: check for check in config["check_definitions"]}

    assert checks["validate_virtual_device_placement"]["class_name"] == "CheckVirtualDevicePlacement"
    assert "targets" not in checks["validate_virtual_device_placement"]
    text = (_ROOT / queries[CheckVirtualDevicePlacement.query]).read_text()
    assert "__typename" in text
    assert "... on" not in text


def test_every_declared_demo_virtual_device_is_deployed_to_an_allowed_kind() -> None:
    """The 30_all and demo files declare each VM for a customer footprint (DC or colocation): none for a zone or a
    virtual colocation. The deployment HFID is the footprint name, resolved here to its declared kind."""
    kinds: dict[str, set[str]] = {}
    for kind in ALLOWED_DEPLOYMENT_KINDS[VIRTUAL_DEVICE]:
        for obj in distinct(declared(kind, _DEMOS)):
            kinds.setdefault(name(obj), set()).add(kind)
    devices = declared("DcimVirtualDevice", _DEMOS)

    assert devices
    for device in devices:
        deployment = device.item.get("deployment")
        if deployment is None:
            continue
        assert kinds.get(str(deployment)), (
            f"{device.item['name']} in {device.path} is deployed to '{deployment}', "
            f"which no demo file declares as one of {ALLOWED_DEPLOYMENT_KINDS[VIRTUAL_DEVICE]}"
        )
