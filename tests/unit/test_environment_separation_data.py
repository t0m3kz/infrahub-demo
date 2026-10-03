"""Prod and non-prod never meet in the 30_all data.

A segment, a VM and the application component running on it each carry an
environment, directly or through the customer footprint they sit on. The
loader accepts a prod segment on a dev footprint or a prod component on dev
VMs without complaint, and the security rules generated from them then
connect the two environments. These checks read the YAML alone.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

_DEMO = Path(__file__).parents[2] / "data" / "demos" / "30_all"


def _walk(node: Any, kind: str, found: list[tuple[str, dict[str, Any]]]) -> None:
    """Collect (kind, entry) for every object, following nested kind/data blocks."""
    if isinstance(node, dict):
        if "kind" in node and "data" in node:
            _walk(node["data"], node["kind"], found)
            return
        found.append((kind, node))
        for value in node.values():
            _walk(value, kind, found)
    elif isinstance(node, list):
        for item in node:
            _walk(item, kind, found)


def _objects() -> list[tuple[str, dict[str, Any]]]:
    found: list[tuple[str, dict[str, Any]]] = []
    for path in sorted(_DEMO.rglob("*.yml")):
        for doc in yaml.safe_load_all(path.read_text()):
            if doc and "spec" in doc:
                _walk(doc["spec"]["data"], doc["spec"]["kind"], found)
    return found


_OBJECTS = _objects()

# Footprint name is computed as {owner_org_id}-{environment}-{parent}.
_FOOTPRINTS: dict[str, str] = {
    f"{entry['owner']}-{entry['environment'].upper()}-{entry['parent']}": entry["environment"]
    for kind, entry in _OBJECTS
    if kind.startswith("TopologyCustomer") and {"owner", "environment", "parent"} <= set(entry)
}
_SEGMENTS = [
    entry
    for kind, entry in _OBJECTS
    if kind in ("ManagedVxlanSegment", "ManagedVlanSegment") and "customer_deployments" in entry
]
_VMS: dict[str, str] = {
    entry["name"]: entry["deployment"]
    for kind, entry in _OBJECTS
    if kind == "DcimVirtualDevice" and "deployment" in entry
}
_COMPONENTS = [
    (app["environment"], f"{app['owner'].lower()}-{app['label']}-{app['environment']}-{component['name']}", component)
    for kind, app in _OBJECTS
    if kind == "AppApplication"
    for component in app["children"]["data"]
]


def test_the_scan_finds_the_demo_data() -> None:
    """Guard against the walker silently finding nothing."""
    assert _FOOTPRINTS and _SEGMENTS and _VMS and _COMPONENTS


@pytest.mark.parametrize("segment", _SEGMENTS, ids=[s["customer_name"] for s in _SEGMENTS])
def test_segment_sits_on_footprints_of_its_own_environment(segment: dict[str, Any]) -> None:
    """A prod segment is activated only on prod footprints, a dev one only on dev."""
    for deployment in segment["customer_deployments"]:
        assert deployment in _FOOTPRINTS, f"{deployment} is not declared"
        assert _FOOTPRINTS[deployment] == segment["environment"], deployment


@pytest.mark.parametrize("vm", sorted(_VMS), ids=sorted(_VMS))
def test_vm_deployment_is_declared(vm: str) -> None:
    """Every VM pins to a footprint that the demo actually declares."""
    assert _VMS[vm] in _FOOTPRINTS


@pytest.mark.parametrize(("environment", "slug", "component"), _COMPONENTS, ids=[slug for _, slug, _ in _COMPONENTS])
def test_component_runs_on_its_application_environment(environment: str, slug: str, component: dict[str, Any]) -> None:
    """A prod component's VMs sit on prod footprints, a dev one's on dev."""
    for instance in component.get("instances") or []:
        if instance not in _VMS:
            continue  # cloud instances carry no customer footprint
        assert _FOOTPRINTS[_VMS[instance]] == environment, f"{slug}: {instance} on {_VMS[instance]}"
