"""Exact ongoing dataset snapshot; the historical stage2 lossless relocation proof remains in its session report."""

from __future__ import annotations

import hashlib
import subprocess
from unittest.mock import MagicMock

import pytest
from infrahub_sdk.spec.object import ObjectFile

from tests.integration.demo_data import (
    _lineage,
    _resolve,
    declared_in,
    human_friendly_id,
    relationship,
    relationship_names,
)
from tests.traversal import load_topology
from tests.unit.full_topology_layout_helpers import canonical, fingerprints


def test_numbered_roots_cover_every_object_once_in_standard_sdk_order() -> None:
    """Definitions, customer objects and late bindings are ordinary recursive directories."""
    root = load_topology.TOPOLOGY
    assert sorted(path.name for path in root.iterdir() if path.is_dir()) == [
        "00_common",
        "10_topology",
        "20_C900",
        "21_C901",
        "90_bindings",
    ]
    assert not list(root.rglob("*.toml"))
    files = sorted(root.rglob("*.yml"))
    sdk_files = sorted(ObjectFile.load_from_disk(paths=[root]), key=lambda file: file.location)
    for file in sdk_files:
        file.validate_content()
    assert len(files) == len(sdk_files) == 303
    assert [file.location for file in sdk_files] == files
    assert all(file.valid and file.kind.value == "Object" for file in sdk_files)
    assert len({file.location for file in sdk_files}) == len(sdk_files)
    assert files[0].parts[-3:] == ("00_common", "00_global", "00_providers.yml")
    assert files[-1].is_relative_to(root / "90_bindings")


@pytest.mark.asyncio
async def test_lossless_multiset_includes_nested_hfids_parent_bindings_ranges_and_duplicate_upserts() -> None:
    """Protect the intentional WAW service delta, including every nested parent and duplicate upsert.

    Stage2's historical 809/1373/564 multiset is preserved in stage2_report.json.
    WAW prerequisites first added 9/10/1; the complete declarative service adds
    another 36/59/23 (top/flat/parent bindings), without renaming legacy objects.
    Native HA-ID reservation and cleanup-safe group-side bindings add 4/8/4.
    The shared HA pool now lives in bootstrap; zone references omit their suffix.
    This is now an exact current-dataset guard, not a claim that additions are lossless relocation.
    """
    result = await fingerprints(load_topology.TOPOLOGY)
    assert {key: len(value) for key, value in result.items()} == {"top": 857, "flat": 1449, "bindings": 592}
    assert hashlib.sha256(canonical(result).encode()).hexdigest() == (
        "5fa5e94b8e7e6c6b005ad61d305e01f8dc15b3f0b61fae67c2fffedf7fc96d98"
    )
    assert len(result["top"]) > len(set(result["top"]))  # Deliberate redundant late upserts must remain.


def test_authored_non_interface_hfid_definitions_precede_their_consumers() -> None:
    """Check known authored references; bootstrap and template-created ports are proven by the fresh SDK load."""
    root = load_topology.TOPOLOGY
    positions = {path: index for index, path in enumerate(sorted(root.rglob("*.yml")))}
    objects = declared_in(root)
    definitions: dict[tuple[str, str], int] = {}
    for obj in objects:
        for alias in _lineage(obj.kind):
            fields = human_friendly_id(alias)
            if not fields:
                continue
            try:
                parts = [_resolve(obj, part.split("__")) for part in fields]
            except LookupError:
                continue
            if None not in parts:
                key = (alias, canonical([str(part) for part in parts]))
                definitions[key] = min(definitions.get(key, positions[obj.path]), positions[obj.path])
    checked = 0
    errors = []
    for obj in objects:
        for field in relationship_names(obj.kind):
            rel = relationship(obj.kind, field)
            assert rel is not None
            peer = rel["peer"]
            if "DcimInterface" in _lineage(peer):
                continue
            value = obj.item.get(field)
            if isinstance(value, str):
                references = [[value]]
            elif isinstance(value, list) and rel.get("cardinality", "one") == "many":
                references = [[item] if isinstance(item, str) else item for item in value]
            elif isinstance(value, list):
                references = [value]
            else:
                continue
            for reference in references:
                if not isinstance(reference, list) or not all(isinstance(part, str) for part in reference):
                    continue
                defined = definitions.get((peer, canonical(reference)))
                if defined is not None:
                    checked += 1
                    if defined > positions[obj.path]:
                        errors.append((str(obj.path.relative_to(root)), field, reference))
    assert checked > 500
    assert not errors, f"Definitions load after their consumers: {errors}"


def test_late_trunk_upserts_keep_both_original_and_firewall_segment_memberships() -> None:
    """Moving definitions earlier must not let an old one-segment upsert overwrite the final expanded trunk."""
    wanted: dict[tuple[str, str], set[str]] = {}
    final: dict[tuple[str, str], set[str]] = {}
    for obj in declared_in(load_topology.TOPOLOGY):
        if obj.kind not in {"DcimLAGInterface", "DcimPhysicalInterface"} or "segments" not in obj.item:
            continue
        key = (str(_resolve(obj, ["device", "name", "value"])), obj.item["name"])
        segments = set(obj.item["segments"])
        wanted.setdefault(key, set()).update(segments)
        final[key] = segments
    assert len(final) >= 20
    assert final == wanted
    for site in ("ktw1", "waw1", "gdn1"):
        assert f"c900-internal-fw-{site}-p" in final[(f"{site}-srv-01", "bond0")]
        assert len(final[(f"{site}-srv-01", "bond0")]) == 2


def test_waw_dedicated_service_has_no_competing_shared_source_association() -> None:
    """Normal loading must declare the pair and select it without any after-load generator action."""
    objects = declared_in(load_topology.TOPOLOGY)
    context_name = "c900-waw1-fw-01-waw1-fw-02-prod"
    contexts = [
        obj
        for obj in objects
        if obj.kind == "ManagedFirewallContext" and obj.item.get("name") == context_name and "vlan_id" in obj.item
    ]
    assert len(contexts) == 1
    pair_names = {"c900-waw1-fw-01-prod", "c900-waw1-fw-02-prod"}
    members = [
        obj
        for obj in objects
        if obj.kind == "DcimVirtualDevice" and obj.item.get("name") in pair_names and "hosting_device" in obj.item
    ]
    assert {obj.item["name"] for obj in members} == pair_names
    for member in members:
        assert member.item["hosting_device"] == member.item["name"].removeprefix("c900-").removesuffix("-prod")
        assert member.item["object_template"] == "PA-VM_EDGE_CUSTOMER_S"
        assert "member_of_groups" not in member.item
        groups = [obj for obj in objects if obj.kind == "CoreStandardGroup" and obj.item["name"] == "firewalls"]
        assert any(node["data"]["name"] == member.item["name"] for group in groups for node in group.item["members"])
    serving = [
        obj
        for obj in objects
        if obj.kind == "TopologyCustomerDC"
        and obj.item.get("owner") == "C900"
        and obj.item.get("parent") == "DC-WAW1"
        and "serving_firewall_context" in obj.item
    ]
    assert serving and {obj.item["serving_firewall_context"] for obj in serving} == {context_name}
    segments = [
        obj
        for obj in objects
        if obj.kind == "ManagedVxlanSegment" and obj.item.get("customer_name") == "internal-fw-waw1"
    ]
    segments = {str(obj.path): obj for obj in segments}
    assert len(segments) == 1
    segment = next(iter(segments.values()))
    assert segment.item["terminated_on"] == context_name
    assert contexts[0].path < segment.path < serving[-1].path


def test_loader_uses_the_ordinary_recursive_sdk_command(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both traversal and integration still call the same public, sorted-directory loader."""
    run = MagicMock(return_value=subprocess.CompletedProcess(["infrahubctl"], 0, stdout="", stderr=""))
    monkeypatch.setattr(load_topology.subprocess, "run", run)
    load_topology.object_load("http://localhost:8000", "private-value", "isolated")
    assert run.call_args.args[0] == [
        "uv",
        "run",
        "infrahubctl",
        "object",
        "load",
        str(load_topology.TOPOLOGY),
        "--branch",
        "isolated",
    ]
    assert run.call_args.kwargs["env"]["INFRAHUB_ADDRESS"] == "http://localhost:8000"
    assert run.call_args.kwargs["env"]["INFRAHUB_API_TOKEN"] == "private-value"


def test_waw_role_group_bindings_do_not_replace_cleanup_ownership_memberships() -> None:
    """Role checks retain membership without a node-side list that could erase SDK generator-output edges."""
    objects = declared_in(load_topology.TOPOLOGY)
    names = {
        "firewalls": {"c900-waw1-fw-01-prod", "c900-waw1-fw-02-prod"},
        "ha_domains": {"c900-waw1-fw-01-waw1-fw-02-ha-prod"},
        "service_contexts": {"c900-waw1-fw-01-waw1-fw-02-prod"},
    }
    protected = set().union(*names.values())
    for obj in objects:
        if obj.item.get("name") in protected:
            assert "member_of_groups" not in obj.item
    for group, expected in names.items():
        rows = [obj for obj in objects if obj.kind == "CoreStandardGroup" and obj.item.get("name") == group]
        assert len(rows) == 1
        assert {node["data"]["name"] for node in rows[0].item["members"]} == expected


@pytest.mark.asyncio
async def test_loader_preserves_its_public_result_and_exposes_unresolved_reference_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An SDK failure is not swallowed or retried in a fallback order, and its token is redacted."""
    result = subprocess.CompletedProcess(["infrahubctl"], 0, stdout="", stderr="")
    client = MagicMock(config=MagicMock(address="http://localhost:8000", api_token="private-value"))
    monkeypatch.setattr(load_topology, "object_load", lambda *args: result)
    assert await load_topology.apply(client, "isolated") == "objects loaded"
    result.returncode = 1
    result.stderr = "bad.yml: unresolved HFID private-value"
    with pytest.raises(RuntimeError, match="bad.yml: unresolved HFID") as error:
        await load_topology.apply(client, "isolated")
    assert "private-value" not in str(error.value) and "[REDACTED]" in str(error.value)
