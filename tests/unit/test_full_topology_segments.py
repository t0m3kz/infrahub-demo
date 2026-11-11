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
_SVI_KIND = "DcimVirtualInterface"


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
    """The realizations with their VLAN rows."""
    return [row for row in _deployment_declarations() if "vlans" in row]


def _svis() -> dict[tuple[str, str], Any]:
    """(device, name) -> the ``ip_address`` of every segment SVI (role svi) the sample declares."""
    return {
        (item["device"], item["name"]): item.get("ip_address")
        for _, doc in _documents()
        if (doc.get("spec") or {}).get("kind") == "DcimVirtualInterface"
        for item in doc["spec"]["data"]
        if item.get("role") == "svi"
    }


def _gateways() -> dict[str, list[str]]:
    """Segment name -> the HFID [address, namespace] of its anycast gateway (nested under the segment)."""
    return {
        f"{item['owner'].lower()}-{item['customer_name']}-{item['environment']}": [
            item["gateway"]["data"]["address"],
            item["gateway"]["data"]["ip_namespace"],
        ]
        for _, doc in _documents()
        if (doc.get("spec") or {}).get("kind") == "ManagedVxlanSegment"
        for item in doc["spec"]["data"]
        if item.get("gateway")
    }


def _row_bindings() -> list[dict]:
    """The port lists of the VLAN rows (90_bindings/96_segment_ports upserts each row by [device, vlan_id] with its
    ``capabilities``)."""
    return [
        row
        for _, doc in _documents()
        if (doc.get("spec") or {}).get("kind") == "ManagedSegmentVlan"
        for row in doc["spec"]["data"]
        if "capabilities" in row
    ]


def test_every_svi_is_a_row_of_its_device_and_carries_its_segment_s_gateway() -> None:
    """The deployment's nested row names no SVI (the SVI is a member of the row, 96_segment_ports); the SVI is named for
    its row's VLAN on the row's device, and it carries the segment's anycast gateway (the existing address object,
    referenced by [address, namespace])."""
    segment_of = {
        (vlan["device"], f"Vlan{vlan['vlan_id']}"): row["segment"]
        for row in _deployment_rows()
        for vlan in row["vlans"]["data"]
    }
    assert all("svi" not in vlan for row in _deployment_rows() for vlan in row["vlans"]["data"])
    gateways = _gateways()
    svis = _svis()
    assert svis
    for key, ip_address in svis.items():
        assert key in segment_of, key
        assert ip_address == gateways[segment_of[key]], key


def _ports(row: dict) -> list[dict]:
    """The members of a row's ``capabilities`` that are ports (its SVI is the one member that is not)."""
    return [member for member in row["capabilities"] if member["kind"] != _SVI_KIND]


def _svi_members(row: dict) -> list[dict]:
    return [member for member in row["capabilities"] if member["kind"] == _SVI_KIND]


def test_every_port_of_a_row_is_a_switch_port_of_the_row_s_switch() -> None:
    """A port is a member of its own switch's VLAN row, a physical port or a LAG, each row's list declared once (an
    upsert of the list replaces it), and the row is one its deployment declares (a person only picks an existing
    row)."""
    declared = {
        (vlan["device"], vlan["vlan_id"]): (row["segment"], row["deployment"])
        for row in _deployment_rows()
        for vlan in row["vlans"]["data"]
    }
    bindings = _row_bindings()
    keys = [(row["device"], row["vlan_id"]) for row in bindings]
    assert bindings and len(keys) == len(set(keys))
    for row in bindings:
        assert declared.get((row["device"], row["vlan_id"])) == tuple(row["segment_deployment"]), row
        for port in _ports(row):
            assert port["kind"] in {"DcimPhysicalInterface", "DcimLAGInterface"}, port
            assert port["data"]["device"] == row["device"], (row["device"], port)


def test_one_row_of_a_segment_per_port() -> None:
    """A port names at most one row of a segment (checks/interface_layers.py R4)."""
    seen: dict[tuple[str, str, str], list[int]] = {}
    for row in _row_bindings():
        segment = row["segment_deployment"][0]
        for port in _ports(row):
            seen.setdefault((port["data"]["device"], port["data"]["name"], segment), []).append(row["vlan_id"])
    assert {key: vlans for key, vlans in seen.items() if len(vlans) > 1} == {}


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
        svis, gateway = _svis(), _gateways()[row["segment"]]
        for vlan in vlans:
            key = (vlan["device"], f"Vlan{vlan['vlan_id']}")
            assert (key not in svis) if terminated else (svis.get(key) == gateway), key
        assert {vlan["device"].split("-")[0] for vlan in vlans} == {row["deployment"].split("-")[1].lower()}


def test_the_stretched_segment_is_shared_one_vni_and_the_vlan_it_gives_on_every_leaf_of_both_dcs() -> None:
    """c900-sync-p: two realizations (KTW1, WAW1) with ONE VNI, and on the eight VTEPs (leaves and border leaves, no port needed) the VLAN id 2500 + VNI - 40000."""
    rows = [row for row in _deployment_rows() if row["segment"] == _STRETCHED]
    assert sorted(row["deployment"] for row in rows) == ["DC-KTW1", "DC-WAW1"]
    vnis = {row["vni"] for row in rows}
    assert len(vnis) == 1 and shared_vlan_id(vnis.pop()) is not None
    assert {vlan["vlan_id"] for row in rows for vlan in row["vlans"]["data"]} == {shared_vlan_id(rows[0]["vni"])}
    assert len([vlan for row in rows for vlan in row["vlans"]["data"]]) == 8
    # A border leaf carries the stretched segment as VLAN and VNI only: it has no SVI, a leaf has one with the gateway.
    svis, gateway = _svis(), _gateways()[_STRETCHED]
    for vlan in (vlan for row in rows for vlan in row["vlans"]["data"]):
        key = (vlan["device"], f"Vlan{vlan['vlan_id']}")
        assert (svis.get(key) == gateway) if "-leaf-" in vlan["device"] else (key not in svis), key
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


def _port_key(port: dict[str, Any]) -> tuple[str, str, str]:
    return (port["kind"], port["data"]["device"], port["data"]["name"])


def test_the_memberships_are_exactly_the_requests_per_deployment() -> None:
    """00_full runs no generator, so it authors the intent (the ports assigned to a deployment, its ``capabilities``,
    95b_segment_requests) and its realization (the rows' ports, 96_segment_ports) by hand: per deployment, the same
    set of ports."""
    requests: dict[tuple[str, str], set[tuple[str, str, str]]] = {}
    for row in _deployment_declarations():
        if "capabilities" in row:
            key = (row["segment"], row["deployment"])
            assert key not in requests, key
            requests[key] = {_port_key(port) for port in row["capabilities"]}
    memberships: dict[tuple[str, str], set[tuple[str, str, str]]] = {}
    for row in _row_bindings():
        if ports := _ports(row):  # a row that lists only its SVI has no request behind it
            memberships.setdefault(tuple(row["segment_deployment"]), set()).update(_port_key(port) for port in ports)
    assert requests and requests == memberships
    for ports in requests.values():
        assert {kind for kind, _, _ in ports} <= {"DcimPhysicalInterface", "DcimLAGInterface"}


def test_every_authored_svi_is_a_member_of_exactly_the_row_of_its_device_and_segment() -> None:
    """The SVI is a member of its row (the row's ``capabilities``, the interface edge a port is a member through): the
    row of its device whose VLAN its name bears and whose segment's gateway it carries; a row has at most one SVI and
    every SVI member is an authored one."""
    segment_of = {
        (vlan["device"], vlan["vlan_id"]): row["segment"] for row in _deployment_rows() for vlan in row["vlans"]["data"]
    }
    gateways, svis = _gateways(), _svis()
    seen: set[tuple[str, str]] = set()
    for row in _row_bindings():
        members = _svi_members(row)
        assert len(members) <= 1, row
        for member in members:
            device, name = member["data"]["device"], member["data"]["name"]
            assert device == row["device"] and name == f"Vlan{row['vlan_id']}", member
            assert svis[(device, name)] == gateways[segment_of[(device, row["vlan_id"])]], member
            seen.add((device, name))
    assert seen == set(svis), set(svis) - seen
