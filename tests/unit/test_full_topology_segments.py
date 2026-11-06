"""The realization of a segment in a DC is data of its own, so a second load of the sample updates it.

``ManagedSegmentDeployment`` is named by its segment and its deployment and ``ManagedVlanDomainSegment`` by its device and
VLAN id (schemas/extensions/service/segment.yml). A deployment nested under its segment (``segment_deployments``) has no
name to match an existing one by, so a second load would create it again and break its uniqueness: the sample declares
it as a row of its own.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_DATA = Path(__file__).parents[2] / "data" / "demos" / "00_full_topology"
_SCHEMA = Path(__file__).parents[2] / "schemas" / "extensions" / "service" / "segment.yml"


def _documents() -> list[tuple[Path, dict[str, Any]]]:
    return [(path, doc) for path in sorted(_DATA.rglob("*.yml")) for doc in yaml.safe_load_all(path.read_text()) if doc]


def _nodes() -> dict[str, dict[str, Any]]:
    return {node["name"]: node for node in yaml.safe_load(_SCHEMA.read_text()).get("nodes") or []}


def test_the_realization_and_the_vlan_row_are_named_by_their_parents() -> None:
    """The names an object file upserts by: segment + deployment, and device + VLAN id (each backed by a uniqueness)."""
    nodes = _nodes()
    assert nodes["SegmentDeployment"]["human_friendly_id"] == ["segment__name__value", "deployment__name__value"]
    assert nodes["VlanDomainSegment"]["human_friendly_id"] == ["device__name__value", "vlan_id__value"]


def test_no_segment_nests_its_deployment() -> None:
    """A nested ``segment_deployments`` can only create: the second load of the sample would violate its uniqueness."""
    nesting = [
        path.relative_to(_DATA)
        for path, doc in _documents()
        if "segment_deployments" in str((doc.get("spec") or {}).get("data"))
    ]
    assert nesting == []


def test_each_dc_declares_its_deployment_with_the_same_vlan_on_both_leaves() -> None:
    """One deployment row per DC, with a VLAN row for each leaf of the MLAG pair under one VLAN id."""
    rows = [
        (path, row)
        for path, doc in _documents()
        if (doc.get("spec") or {}).get("kind") == "ManagedSegmentDeployment"
        for row in doc["spec"]["data"]
    ]
    assert sorted(row["deployment"] for _, row in rows) == ["DC-GDN1", "DC-KTW1", "DC-WAW1"]
    for _, row in rows:
        vlans = row["vlan_domain_segments"]["data"]
        assert len(vlans) == 2 and len({vlan["vlan_id"] for vlan in vlans}) == 1
        assert {vlan["device"].split("-")[0] for vlan in vlans} == {row["deployment"].split("-")[1].lower()}
