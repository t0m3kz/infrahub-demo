"""The realization of a segment in a DC is data of its own, so a second load of the sample updates it.

``ManagedSegmentDeployment`` is named by its segment and its deployment and ``ManagedSegmentVlan`` by its device and
VLAN id (schemas/extensions/service/segment.yml). A deployment nested under its segment (``segment_deployments``) has no
name to match an existing one by, so a second load would create it again and break its uniqueness: the sample declares
it as a row of its own.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from utils.segment_vlan import shared_vlan_id

_DATA = Path(__file__).parents[2] / "data" / "demos" / "00_full_topology"
_SCHEMA = Path(__file__).parents[2] / "schemas" / "extensions" / "service" / "segment.yml"


def _documents() -> list[tuple[Path, dict[str, Any]]]:
    return [(path, doc) for path in sorted(_DATA.rglob("*.yml")) for doc in yaml.safe_load_all(path.read_text()) if doc]


_STRETCHED = "c900-sync-p"


def _nodes() -> dict[str, dict[str, Any]]:
    return {node["name"]: node for node in yaml.safe_load(_SCHEMA.read_text()).get("nodes") or []}


def test_the_realization_and_the_vlan_row_are_named_by_their_parents() -> None:
    """The names an object file upserts by: segment + deployment, and device + VLAN id (each backed by a uniqueness)."""
    nodes = _nodes()
    assert nodes["SegmentDeployment"]["human_friendly_id"] == ["segment__name__value", "deployment__name__value"]
    assert nodes["SegmentVlan"]["human_friendly_id"] == ["device__name__value", "vlan_id__value"]


def test_no_segment_nests_its_deployment() -> None:
    """A nested ``segment_deployments`` can only create: the second load of the sample would violate its uniqueness."""
    nesting = [
        path.relative_to(_DATA)
        for path, doc in _documents()
        if "segment_deployments" in str((doc.get("spec") or {}).get("data"))
    ]
    assert nesting == []


def _deployment_rows() -> list[dict]:
    return [
        row
        for _, doc in _documents()
        if (doc.get("spec") or {}).get("kind") == "ManagedSegmentDeployment"
        for row in doc["spec"]["data"]
    ]


def test_each_dc_declares_its_deployment_with_the_same_vlan_on_both_leaves() -> None:
    """One deployment row per DC for its own (local) segment, a VLAN row for each leaf of the MLAG pair under one id."""
    rows = [row for row in _deployment_rows() if row["segment"] != _STRETCHED]
    assert sorted(row["deployment"] for row in rows) == ["DC-GDN1", "DC-KTW1", "DC-WAW1"]
    for row in rows:
        vlans = row["vlans"]["data"]
        assert len(vlans) == 2 and len({vlan["vlan_id"] for vlan in vlans}) == 1
        assert {vlan["device"].split("-")[0] for vlan in vlans} == {row["deployment"].split("-")[1].lower()}


def test_the_stretched_segment_is_shared_one_vni_and_the_vlan_it_gives_on_every_leaf_of_both_dcs() -> None:
    """c900-sync-p: two realizations (KTW1, WAW1) with ONE VNI, and on all eight switches (leaves and border leaves) the VLAN id VNI - 10000."""
    rows = [row for row in _deployment_rows() if row["segment"] == _STRETCHED]
    assert sorted(row["deployment"] for row in rows) == ["DC-KTW1", "DC-WAW1"]
    vnis = {row["vni"] for row in rows}
    assert len(vnis) == 1 and shared_vlan_id(vnis.pop()) is not None
    assert {vlan["vlan_id"] for row in rows for vlan in row["vlans"]["data"]} == {shared_vlan_id(rows[0]["vni"])}
    assert len([vlan for row in rows for vlan in row["vlans"]["data"]]) == 8
    segment = next(
        item
        for _, doc in _documents()
        if (doc.get("spec") or {}).get("kind") == "ManagedVxlanSegment"
        for item in doc["spec"]["data"]
        if item.get("customer_name") == "sync"
    )
    assert segment["vlan_scope"] == "shared" and segment["stretch_scope"] == "dc_pair"
    assert len(segment["customer_deployments"]) == 2
