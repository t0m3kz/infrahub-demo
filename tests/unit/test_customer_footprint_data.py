"""Customer footprints and segments in the object files: one place, no restated or removed fields.

A customer boards with ONE footprint file per kind (data/demos/30_all/06_customer_boarding/<customer>/...); the
DC footprints of a customer, one per DC it uses, share `<customer>/dc/01_deployments.yml`. A footprint states
only what is intent: no `status`, no `org_id` (the owner has it), no `cloud_account` (the CloudAccount carries
the environment). A footprint name is computed from its owner, so two declarations that compute the same name
are one object loaded twice, never two customers' footprints.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import pytest

from tests.integration.demo_data import ROOT, Declared, attribute, declared_in, name, schema

_DATA = ROOT / "data"
_ALL_DEMO = _DATA / "demos" / "30_all"
_BOARDING = _ALL_DEMO / "06_customer_boarding"
_FOOTPRINT_KINDS = (
    "TopologyCustomerDC",
    "TopologyCustomerColocation",
    "TopologyCustomerCloud",
    "TopologyCustomerOffice",
)
_REMOVED_FOOTPRINT_KEYS = ("status", "org_id", "cloud_account")
_SEGMENT_TEST_DATA = ROOT / "tests" / "integration" / "data"


def _footprints(*directories: Path) -> list[Declared]:
    return [obj for obj in declared_in(*directories) if obj.kind in _FOOTPRINT_KINDS]


def _rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _customer(path: Path) -> str:
    """`.../06_customer_boarding/c005/dc/01_deployments.yml` -> c005."""
    return path.relative_to(_BOARDING).parts[0]


# ---------------------------------------------------------------------------
# One footprint file per kind per customer
# ---------------------------------------------------------------------------


def test_the_scan_finds_the_boarded_footprints() -> None:
    """Guard against the walker silently finding nothing: every kind boards at least one customer."""
    assert {obj.kind for obj in _footprints(_BOARDING)} == set(_FOOTPRINT_KINDS)


def test_each_customer_has_exactly_one_footprint_file_per_kind() -> None:
    """c005's DC10 and DC12 footprints share one file; a customer never splits a kind over several files."""
    files: dict[tuple[str, str], set[str]] = defaultdict(set)
    for obj in _footprints(_BOARDING):
        files[(_customer(obj.path), obj.kind)].add(_rel(obj.path))
    split = {key: sorted(paths) for key, paths in files.items() if len(paths) > 1}
    assert not split, f"footprints of one kind spread over several files: {split}"


def test_dc_footprints_sit_in_the_customers_dc_deployments_file() -> None:
    """`<customer>/dc/01_deployments.yml`, whatever the DC: the per-DC folders hold instances, not footprints."""
    misplaced = [
        _rel(obj.path)
        for obj in _footprints(_BOARDING)
        if obj.kind == "TopologyCustomerDC"
        and obj.path.relative_to(_BOARDING).parts[1:] != ("dc", "01_deployments.yml")
    ]
    assert not misplaced, misplaced


def test_dc_footprint_files_declare_their_group_in_flow_style() -> None:
    """`member_of_groups: [customer_deployments_dc]` on one line, on every DC footprint (franc-mcp reads it)."""
    offenders = []
    for path in sorted({obj.path for obj in _footprints(_BOARDING) if obj.kind == "TopologyCustomerDC"}):
        text = path.read_text()
        flow = text.count("member_of_groups: [customer_deployments_dc]")
        block = text.count("member_of_groups:\n")
        rows = sum(1 for obj in _footprints(path.parent) if obj.path == path)
        if block or flow != rows:
            offenders.append(f"{_rel(path)}: {rows} footprints, {flow} flow-style groups, {block} block-style")
    assert not offenders, "\n".join(offenders)


def test_no_new_customers_dc_directory_is_left() -> None:
    """The 03_dc/new_customers footprints moved into 06_customer_boarding/<customer>/dc/01_deployments.yml."""
    assert not (_ALL_DEMO / "03_dc" / "new_customers").exists()
    assert not [path for path in _DATA.rglob("new_customers") if path.is_dir()]


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------


def test_no_two_footprint_declarations_of_30_all_compute_the_same_name() -> None:
    """A computed name is the footprint's identity: a second file declaring it is a stale copy."""
    declarations: dict[str, list[str]] = defaultdict(list)
    for obj in _footprints(_ALL_DEMO):
        declarations[f"{obj.kind} {name(obj)}"].append(_rel(obj.path))
    duplicates = {key: paths for key, paths in declarations.items() if len(paths) > 1}
    assert not duplicates, duplicates


@pytest.mark.parametrize(
    ("kind", "pattern"),
    [
        ("TopologyCustomerOffice", "{owner}-OFFICE"),
        ("TopologyCustomerCloud", "{owner}-{customer_name}-{parent}"),
    ],
)
def test_office_and_cloud_names_carry_no_environment(kind: str, pattern: str) -> None:
    """C005-OFFICE, C003-VAULTEX-AWS-EU-CENTRAL-1: no `-P-`, neither kind has an environment."""
    footprints = [obj for obj in _footprints(_ALL_DEMO) if obj.kind == kind]
    assert footprints
    for obj in footprints:
        assert "environment" not in obj.item, _rel(obj.path)
        owner = str(obj.item["owner"])
        expected = pattern.format(
            owner=owner, customer_name=obj.item.get("customer_name", ""), parent=obj.item["parent"]
        ).upper()
        assert name(obj) == expected


# ---------------------------------------------------------------------------
# Removed fields and kinds
# ---------------------------------------------------------------------------


def test_footprints_declare_no_status_org_id_or_cloud_account() -> None:
    """`status` and `org_id` left the TopologyCustomer generic and `cloud_account` the cloud footprint."""
    offenders = [
        f"{_rel(obj.path)}: {obj.kind} {key}"
        for obj in _footprints(_DATA, _SEGMENT_TEST_DATA)
        for key in _REMOVED_FOOTPRINT_KEYS
        if key in obj.item
    ]
    assert not offenders, "\n".join(offenders)


@pytest.mark.parametrize("kind", ["TopologyCustomerSaas", "TopologyOfficeCustomer"])
def test_removed_footprint_kinds_appear_nowhere_in_the_data(kind: str) -> None:
    """The SaaS footprint and the office wrapper were deleted from the schema; no object file may name them."""
    offenders = [
        _rel(path)
        for root in (_DATA, _SEGMENT_TEST_DATA)
        for path in sorted(root.rglob("*.yml"))
        if kind in path.read_text()
    ]
    assert not offenders, offenders


def test_no_footprint_declares_an_environment_where_the_kind_has_none() -> None:
    """Only the environment-scoped kinds (DC, colocation) state one: an office is PROD, a cloud follows its account."""
    scoped = {kind for kind in _FOOTPRINT_KINDS if attribute(kind, "environment") is not None}
    assert scoped == {"TopologyCustomerDC", "TopologyCustomerColocation"}
    offenders = [
        f"{_rel(obj.path)}: {obj.kind} {name(obj)}"
        for obj in _footprints(_DATA, _SEGMENT_TEST_DATA)
        if obj.kind not in scoped and "environment" in obj.item
    ]
    assert not offenders, "\n".join(offenders)


# ---------------------------------------------------------------------------
# Segments
# ---------------------------------------------------------------------------


def _managed_segment_kinds() -> set[str]:
    """Every kind whose schema requires an `environment` and that is a network segment."""
    return {
        kind
        for kind in schema()
        if kind.endswith("Segment")
        and (attribute(kind, "environment") or {}).get("optional") is False
        and "default_value" not in (attribute(kind, "environment") or {})
    }


def test_the_segment_kinds_requiring_an_environment_include_vxlan() -> None:
    """Guard for the scan below."""
    assert "ManagedVxlanSegment" in _managed_segment_kinds()


def test_every_segment_object_in_the_data_sets_its_environment() -> None:
    """The schema requires it with no default (a silent `p` would be the PROD namespace, zone and firewall leg)."""
    kinds = _managed_segment_kinds()
    objects = [obj for obj in declared_in(_DATA, _SEGMENT_TEST_DATA) if obj.kind in kinds]
    assert objects
    offenders = [
        f"{_rel(obj.path)}: {obj.kind} {obj.item.get('customer_name') or obj.item.get('name')}"
        for obj in objects
        if not obj.item.get("environment")
    ]
    assert not offenders, "\n".join(offenders)
