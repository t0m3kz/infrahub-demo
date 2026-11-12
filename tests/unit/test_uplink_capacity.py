"""Unit tests asserting every device template has enough uplinks for its pod."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

# Roles that cable to the pod's spine tier; "l2-leaf" hangs off leaf downlinks instead.
SPINE_ATTACHED_RACK_ROLES = ("leaf", "tor")

# A pod's top tier is whichever of these it declares; border-spine collapses spine and border-leaf.
POD_SPINE_ROLES = ("spine", "border-spine")


def _expand_count(name: str) -> int:
    """Number of interfaces one template entry creates, honouring ``[a-b]`` ranges."""
    match = re.search(r"\[(\d+)-(\d+)\]", name)
    if not match:
        return 1
    return int(match.group(2)) - int(match.group(1)) + 1


def _documents(path: Path) -> list[dict[str, Any]]:
    return [doc for doc in yaml.safe_load_all(path.read_text()) if doc]


@pytest.fixture(scope="module")
def uplinks_by_template(root_dir: Path) -> dict[str, int]:
    """Uplink port count of every device template, keyed by template_name."""
    counts: dict[str, int] = {}
    template_files = sorted((root_dir / "data" / "bootstrap").glob("*_device*templates_*.y*ml"))
    assert template_files, "no device template files found — fixture is not reading bootstrap data"

    for path in template_files:
        for doc in _documents(path):
            spec = doc.get("spec") or {}
            if spec.get("kind") != "TemplateDcimPhysicalDevice":
                continue
            for device in spec.get("data") or []:
                interfaces = (device.get("interfaces") or {}).get("data") or []
                counts[device["template_name"]] = sum(
                    _expand_count(iface.get("name", "")) for iface in interfaces if iface.get("role") == "uplink"
                )
    return counts


@pytest.fixture(scope="module")
def datacenter_topologies(root_dir: Path) -> list[dict[str, Any]]:
    """Every TopologyDataCenter defined in the DC demo data."""
    topologies: list[dict[str, Any]] = []
    paths = sorted((root_dir / "data" / "demos" / "01_data_center").glob("*/01_topology.yml"))
    assert paths, "no DC topology files found — fixture is not reading demo data"

    for path in paths:
        for doc in _documents(path):
            spec = doc.get("spec") or {}
            if spec.get("kind") != "TopologyDataCenter":
                continue
            for dc in spec.get("data") or []:
                topologies.append({"source": path.name, "dc": dc})
    return topologies


def _templates_by_role(entry: dict[str, Any]) -> dict[str, list[tuple[int, str]]]:
    """``fabric_templates`` (``[quantity, role, template]`` triples) as role -> entries."""
    by_role: dict[str, list[tuple[int, str]]] = {}
    for quantity, role, template in entry.get("fabric_templates") or []:
        by_role.setdefault(role, []).append((int(quantity), template))
    return by_role


def _pod_spine_count(pod: dict[str, Any]) -> int:
    by_role = _templates_by_role(pod)
    return sum(quantity for role in POD_SPINE_ROLES for quantity, _ in by_role.get(role, []))


def _iter_spine_attached(
    topologies: list[dict[str, Any]],
) -> list[tuple[str, str, str, int]]:
    """Yield ``(context, role, template, required_uplinks)`` for every rack device
    that dual-homes to its pod's spine tier."""
    cases: list[tuple[str, str, str, int]] = []
    for item in topologies:
        dc = item["dc"]
        for pod in (dc.get("children") or {}).get("data") or []:
            spine_count = _pod_spine_count(pod)
            if not spine_count:
                continue
            for rack in (pod.get("racks") or {}).get("data") or []:
                for _quantity, role, template in rack.get("fabric_templates") or []:
                    if role not in SPINE_ATTACHED_RACK_ROLES:
                        continue
                    context = f"{dc['name']} pod {pod['index']} rack {rack.get('shortname', rack.get('index'))}"
                    cases.append((context, role, template, spine_count))
    return cases


def _iter_border_leafs(topologies: list[dict[str, Any]]) -> list[tuple[str, str, str, int]]:
    """Same, for DC-tier border-leafs cabling up to the super-spine tier."""
    cases: list[tuple[str, str, str, int]] = []
    for item in topologies:
        dc = item["dc"]
        by_role = _templates_by_role(dc)
        super_spines = sum(quantity for quantity, _ in by_role.get("super-spine", []))
        if not super_spines:
            continue
        for _quantity, template in by_role.get("border-leaf", []):
            cases.append((f"{dc['name']} DC tier", "border-leaf", template, super_spines))
    return cases


class TestTemplatesAreKnown:
    """If a topology names a template that does not exist, every capacity
    assertion below would silently read an uplink count of zero."""

    def test_every_referenced_template_exists(
        self, datacenter_topologies: list[dict[str, Any]], uplinks_by_template: dict[str, int]
    ) -> None:
        missing: list[str] = []
        cases = _iter_spine_attached(datacenter_topologies) + _iter_border_leafs(datacenter_topologies)
        assert cases, "no spine-attached devices found — the topology traversal is broken"

        for context, role, template, _required in cases:
            if template not in uplinks_by_template:
                missing.append(f"{context}: {role} references unknown template '{template}'")

        assert not missing, "templates referenced by demo data but not defined in bootstrap:\n  " + "\n  ".join(missing)


class TestUplinkCapacity:
    def test_rack_devices_have_one_uplink_per_spine(
        self, datacenter_topologies: list[dict[str, Any]], uplinks_by_template: dict[str, int]
    ) -> None:
        """The regression: LEAF_MIXED's 2 uplinks in DC4's 4-spine S_MIXED pod."""
        shortfalls: list[str] = []
        for context, role, template, required in _iter_spine_attached(datacenter_topologies):
            available = uplinks_by_template.get(template, 0)
            if available < required:
                shortfalls.append(
                    f"{context}: {role} template '{template}' has {available} uplink(s) "
                    f"but the pod has {required} spine(s)"
                )

        assert not shortfalls, (
            "device templates with fewer uplinks than the spines they must all reach "
            "(each would double-book a port):\n  " + "\n  ".join(shortfalls)
        )

    def test_border_leafs_have_one_uplink_per_super_spine(
        self, datacenter_topologies: list[dict[str, Any]], uplinks_by_template: dict[str, int]
    ) -> None:
        shortfalls: list[str] = []
        for context, role, template, required in _iter_border_leafs(datacenter_topologies):
            available = uplinks_by_template.get(template, 0)
            if available < required:
                shortfalls.append(
                    f"{context}: {role} template '{template}' has {available} uplink(s) "
                    f"but the DC has {required} super-spine(s)"
                )

        assert not shortfalls, "border-leaf templates with fewer uplinks than super-spines:\n  " + "\n  ".join(
            shortfalls
        )


class TestExpandCount:
    """The capacity checks are only as good as the range expansion."""

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("Ethernet[27-30]/1", 4),
            ("Ethernet1/[1-30]", 30),
            ("Ethernet[11-26]/1", 16),
            ("Management1", 1),
            ("mgmt0", 1),
        ],
    )
    def test_expand_count(self, name: str, expected: int) -> None:
        assert _expand_count(name) == expected
