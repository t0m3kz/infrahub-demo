"""routing_filter_enforce: the staging switch of the BGP route protection, its schema, its data and its readers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

DEMO = "data/demos/30_all"
HOSTING_KINDS = ("TopologyDataCenter", "TopologyColocationMetro")
# The device queries whose transforms derive the limit (they carry the VIP root and the hosting pool).
PROTECTED_QUERIES = ("leaf", "border_leaf", "border_spine", "spine", "super_spine", "edge")


def _objects(path: Path, kind: str) -> list[dict[str, Any]]:
    """Every object of ``kind`` in one object file, nested children included."""
    found: list[dict[str, Any]] = []

    def walk(spec: Any) -> None:
        if not isinstance(spec, dict):
            return
        if spec.get("kind") == kind:
            found.extend(item for item in spec.get("data") or [] if isinstance(item, dict))
        for item in spec.get("data") or []:
            if isinstance(item, dict):
                walk(item.get("children"))

    for document in yaml.safe_load_all(path.read_text()):
        walk((document or {}).get("spec"))
    return found


def test_the_switch_is_a_boolean_defaulting_to_staged(root_dir: Path) -> None:
    """On the hosting generic, false by default: a fabric stages until an owner turns it on."""
    schema = yaml.safe_load((root_dir / "schemas/extensions/topology/topology.yml").read_text())
    generic = next(g for g in schema["generics"] if g["name"] == "SegmentHosting")
    attribute = next(a for a in generic["attributes"] if a["name"] == "routing_filter_enforce")
    assert attribute["kind"] == "Boolean"
    assert attribute["default_value"] is False


@pytest.mark.parametrize("kind", HOSTING_KINDS)
def test_every_dc_and_metro_of_the_all_demo_enforces(root_dir: Path, kind: str) -> None:
    """The 30_all demo turns the filters on, so the stack exercises them (the precedent is group_policy_enforce)."""
    objects = [obj for path in sorted((root_dir / DEMO).rglob("*.yml")) for obj in _objects(path, kind)]
    assert objects, kind
    assert [o["name"] for o in objects if o.get("routing_filter_enforce") is not True] == []


@pytest.mark.parametrize("query", PROTECTED_QUERIES)
def test_the_device_queries_carry_the_vip_root_and_the_hosting_facts(root_dir: Path, query: str) -> None:
    """transforms/helpers/bgp_facts.py reads the VIP root and, through the fragment, the pool and the switch."""
    text = (root_dir / f"queries/config/{query}.gql").read_text()
    assert 'DcimVirtualInterface(role__value: "multisite-vip")' in text
    if query != "edge":
        assert "...EvpnRtAsOnDeploymentFields" in text
    fragment = (root_dir / "queries/fragments/evpn_fabric.gql").read_text()
    assert "routing_filter_enforce { value }" in fragment
    assert fragment.count("loopback_pool {") == 2
