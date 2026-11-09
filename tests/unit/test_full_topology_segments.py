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


def _deployment_declarations() -> list[dict]:
    return [
        row
        for _, doc in _documents()
        if (doc.get("spec") or {}).get("kind") == "ManagedSegmentDeployment"
        for row in doc["spec"]["data"]
    ]


def _deployment_rows() -> list[dict]:
    """The realizations with their VLAN rows (the port bindings of 90_bindings/96_segment_ports upsert the same
    deployments with only their ``capabilities``)."""
    return [row for row in _deployment_declarations() if "vlans" in row]


_SITE_PREFIX = {"DC-KTW1": "ktw1-", "DC-WAW1": "waw1-", "DC-GDN1": "gdn1-", "C900-OFFICE": "bcn-office-"}


def test_every_port_of_a_deployment_is_a_switch_port_of_its_hosting_deployment() -> None:
    """A port is a member of the segment's deployment in its device's hosting deployment, a physical port or a LAG,
    each deployment's list declared once (an upsert of the list replaces it), and the deployment is realized."""
    realized = {(row["segment"], row["deployment"]) for row in _deployment_rows()}
    bindings = [row for row in _deployment_declarations() if "capabilities" in row]
    keys = [(row["segment"], row["deployment"]) for row in bindings]
    assert bindings and len(keys) == len(set(keys))
    for row in bindings:
        assert (row["segment"], row["deployment"]) in realized, row["segment"]
        for port in row["capabilities"]:
            assert port["kind"] in {"DcimPhysicalInterface", "DcimLAGInterface"}, port
            assert port["data"]["device"].startswith(_SITE_PREFIX[row["deployment"]]), (row["deployment"], port)


def test_no_port_names_a_segment() -> None:
    """The port-to-segment edge is gone: a port reaches a segment through the deployment only."""
    ports = [
        item
        for _, doc in _documents()
        if (doc.get("spec") or {}).get("kind") in {"DcimPhysicalInterface", "DcimLAGInterface"}
        for item in doc["spec"]["data"]
        if "segments" in item
    ]
    assert ports == []


def test_each_dc_declares_its_deployment_with_the_same_vlan_on_both_leaves() -> None:
    """Each DC's routed local segment has two leaf rows; its firewall-terminated segment also reaches both borders."""
    rows = [row for row in _deployment_rows() if row["segment"] != _STRETCHED and row["deployment"].startswith("DC-")]
    assert sorted(row["deployment"] for row in rows) == [
        "DC-GDN1",
        "DC-GDN1",
        "DC-KTW1",
        "DC-KTW1",
        "DC-WAW1",
        "DC-WAW1",
    ]
    for row in rows:
        vlans = row["vlans"]["data"]
        site = row["deployment"].split("-")[1].lower()
        expected_devices = {f"{site}-leaf-01", f"{site}-leaf-02"}
        terminated = row["segment"] == f"c900-internal-fw-{site}-p"
        if terminated:
            expected_devices |= {f"{site}-bl-01", f"{site}-bl-02"}
        assert {vlan["device"] for vlan in vlans} == expected_devices
        assert len(vlans) == len(expected_devices) and len({vlan["vlan_id"] for vlan in vlans}) == 1
        assert all(("svi" not in vlan) if terminated else ("svi" in vlan) for vlan in vlans)
        assert {vlan["device"].split("-")[0] for vlan in vlans} == {row["deployment"].split("-")[1].lower()}


def test_the_stretched_segment_is_shared_one_vni_and_the_vlan_it_gives_on_every_leaf_of_both_dcs() -> None:
    """c900-sync-p: two realizations (KTW1, WAW1) with ONE VNI, and on the eight VTEPs (leaves and border leaves, no port needed) the VLAN id 2500 + VNI - 40000."""
    rows = [row for row in _deployment_rows() if row["segment"] == _STRETCHED]
    assert sorted(row["deployment"] for row in rows) == ["DC-KTW1", "DC-WAW1"]
    vnis = {row["vni"] for row in rows}
    assert len(vnis) == 1 and shared_vlan_id(vnis.pop()) is not None
    assert {vlan["vlan_id"] for row in rows for vlan in row["vlans"]["data"]} == {shared_vlan_id(rows[0]["vni"])}
    assert len([vlan for row in rows for vlan in row["vlans"]["data"]]) == 8
    # A border leaf carries the stretched segment as VLAN and VNI only: its row has no SVI, a leaf's has one.
    for vlan in (vlan for row in rows for vlan in row["vlans"]["data"]):
        assert ("svi" in vlan) == ("-leaf-" in vlan["device"]), vlan["device"]
    segment = next(
        item
        for _, doc in _documents()
        if (doc.get("spec") or {}).get("kind") == "ManagedVxlanSegment"
        for item in doc["spec"]["data"]
        if item.get("customer_name") == "sync"
    )
    assert segment["vlan_scope"] == "shared" and segment["stretch_scope"] == "dc_pair"
    assert len(segment["customer_deployments"]) == 2


def test_the_office_wifi_segments_are_a_guest_and_a_staff_one_on_the_access_switches() -> None:
    """Both segments are realized in the office on both access switches (same VLAN id on the pair), and the guest one has
    no permit rule but the one to the internet."""
    rows = {row["segment"]: row for row in _deployment_rows() if row["deployment"] == "C900-OFFICE"}
    assert set(rows) == {"c900-wifi-guest-p", "c900-wifi-corp-p"}
    for row in rows.values():
        vlans = row["vlans"]["data"]
        assert {vlan["device"] for vlan in vlans} == {"bcn-office-as-01", "bcn-office-as-02"}
        assert len({vlan["vlan_id"] for vlan in vlans}) == 1

    policies = {
        item["segment"]: item
        for _, doc in _documents()
        if (doc.get("spec") or {}).get("kind") == "SecurityPolicy"
        for item in doc["spec"]["data"]
        if item["segment"].startswith("c900-wifi")
    }
    guest = policies["c900-wifi-guest-p"]["rules"]["data"]
    assert [(rule["action"], rule.get("destination_zone"), rule.get("destination_segment")) for rule in guest] == [
        ("permit", "INTERNET", None)
    ]
    assert policies["c900-wifi-guest-p"]["default_action"] == "deny"
    assert any(
        rule.get("destination_segment") == "c900-web-p" for rule in policies["c900-wifi-corp-p"]["rules"]["data"]
    )
