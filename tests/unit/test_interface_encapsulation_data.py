"""The sample data authors an interface's parent and VLAN tag as a ManagedInterfaceEncapsulation, never as fields."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
DEMOS = ROOT / "data" / "demos"
DATASETS = {
    "00_full_topology": DEMOS / "00_full_topology",
    "30_all": DEMOS / "30_all",
}
EXTRA_ROOTS = (ROOT / "data" / "bootstrap", ROOT / "tests" / "integration" / "data")
ENCAPSULATION = "ManagedInterfaceEncapsulation"
INTERFACE_KINDS = frozenset(
    {
        "DcimVirtualInterface",
        "DcimPhysicalInterface",
        "DcimLAGInterface",
        "CloudPeeringInterface",
        "TopologyProviderInterface",
        "TopologyInternetInterface",
    }
)
REMOVED_KEYS = ("parent_interface", "vlan_id")


@dataclass(frozen=True)
class Found:
    """One object met while walking an object file."""

    path: Path
    kind: str | None
    item: dict[str, Any]
    device: str | None  # the device that owns the interface (the item's `device`, else the enclosing device's name)
    interface: str | None  # the interface an encapsulation is nested under


def _documents(root: Path) -> Iterator[tuple[Path, dict[str, Any]]]:
    """Every ``kind: Object`` document of every yml file under ``root`` (a missing directory has none)."""
    for path in sorted(root.rglob("*.yml")) if root.exists() else []:
        for document in yaml.safe_load_all(path.read_text()):
            if isinstance(document, dict) and document.get("kind") == "Object":
                yield path, document


def _walk(path: Path, value: Any, kind: str | None, device: str | None, interface: str | None) -> Iterator[Found]:
    """Every object below ``value``:"""
    if isinstance(value, list):
        for entry in value:
            yield from _walk(path, entry, kind, device, interface)
        return
    if not isinstance(value, dict):
        return
    if "data" in value and set(value) <= {"kind", "data", "parameters"}:
        yield from _walk(path, value["data"], value.get("kind", kind), device, interface)
        return
    own_device = value["device"] if isinstance(value.get("device"), str) else device
    yield Found(path, kind, value, own_device, interface)
    is_interface = kind in INTERFACE_KINDS
    # a device names the interfaces nested under it; an interface names the encapsulations nested under it
    nested_device = own_device if is_interface or kind == ENCAPSULATION else value.get("name", device)
    nested_interface = value.get("name") if is_interface else interface
    for child in value.values():
        yield from _walk(path, child, None, nested_device, nested_interface)


def _objects(*roots: Path) -> list[Found]:
    found: list[Found] = []
    for root in roots:
        for path, document in _documents(root):
            spec = document["spec"]
            found.extend(_walk(path, spec.get("data") or [], spec["kind"], None, None))
    return found


def _encapsulations(found: list[Found]) -> list[Found]:
    return [obj for obj in found if obj.kind == ENCAPSULATION]


def _name(obj: Found) -> str:
    return f"{obj.path.relative_to(ROOT)}: {obj.item.get('name')}"


@pytest.fixture(scope="module")
def everything() -> list[Found]:
    """Every object of the demos, the bootstrap data and the integration test data."""
    return _objects(DEMOS, *EXTRA_ROOTS)


def test_no_interface_object_keeps_parent_interface_or_a_vlan_id(everything: list[Found]) -> None:
    """The fields are gone from the interface kinds: the tag and the parent live in the encapsulation."""
    interfaces = [obj for obj in everything if obj.kind in INTERFACE_KINDS]
    assert interfaces, "the data holds interfaces"
    offenders = [f"{_name(obj)} has {key}" for obj in interfaces for key in REMOVED_KEYS if key in obj.item]
    assert offenders == []


@pytest.mark.parametrize("dataset", sorted(DATASETS))
def test_every_dataset_authors_encapsulations_with_a_parent(dataset: str) -> None:
    """A dataset has encapsulations and each points at a parent (the schema makes it mandatory)."""
    encapsulations = _encapsulations(_objects(DATASETS[dataset]))
    assert encapsulations
    assert [_name(obj) for obj in encapsulations if not obj.item.get("parent")] == []


@pytest.mark.parametrize("root", [DEMOS, *EXTRA_ROOTS], ids=lambda root: root.name)
def test_an_authored_encapsulation_is_named_after_its_interface_and_has_a_two_string_parent(root: Path) -> None:
    """``<owning device>:<owning interface>``."""
    problems: list[str] = []
    for obj in _encapsulations(_objects(root)):
        item = obj.item
        label = f"{obj.path.relative_to(ROOT)}: {item.get('name')}"
        if obj.interface is None:
            # an encapsulation authored on its own has no owner to derive the name from: only its shape is judged
            if ":" not in str(item.get("name")):
                problems.append(f"{label}: name is not <device>:<interface>")
        elif item.get("name") != f"{obj.device}:{obj.interface}":
            problems.append(f"{label}: expected the name {obj.device}:{obj.interface}")
        parent = item.get("parent")
        if not (isinstance(parent, list) and len(parent) == 2 and all(isinstance(part, str) for part in parent)):
            problems.append(f"{label}: parent {parent!r} is not a [device, interface] HFID of strings")
        if "vlan_id" in item and (not isinstance(item["vlan_id"], int) or isinstance(item["vlan_id"], bool)):
            problems.append(f"{label}: vlan_id {item['vlan_id']!r} is not an integer")
        if "vlan_id" in item and isinstance(item["vlan_id"], int) and not 1 <= item["vlan_id"] <= 4094:
            problems.append(f"{label}: vlan_id {item['vlan_id']} is outside 1..4094")
    assert problems == []


@pytest.mark.parametrize("dataset", sorted(DATASETS))
def test_encapsulation_names_are_unique_per_dataset(dataset: str) -> None:
    """One encapsulation per interface: a name seen twice is two objects fighting over one upsert."""
    names = Counter(str(obj.item.get("name")) for obj in _encapsulations(_objects(DATASETS[dataset])))
    assert [name for name, count in names.items() if count > 1] == []


def test_the_walker_finds_the_encapsulation_nested_under_an_interface_of_a_device(tmp_path: Path) -> None:
    """The walker covers the nested ``{kind, data}`` form: an encapsulation under an interface under a device."""
    (tmp_path / "devices.yml").write_text(
        """
---
apiVersion: infrahub.app/v1
kind: Object
spec:
  kind: DcimPhysicalDevice
  data:
    - name: sw1
      interfaces:
        - kind: DcimVirtualInterface
          data:
            name: Ethernet1.10
            capabilities:
              - kind: ManagedInterfaceEncapsulation
                data:
                  name: "sw1:Ethernet1.10"
                  vlan_id: 10
                  parent: [sw1, Ethernet1]
"""
    )
    found = _objects(tmp_path)
    (encapsulation,) = _encapsulations(found)

    assert (encapsulation.device, encapsulation.interface) == ("sw1", "Ethernet1.10")
    assert [obj.kind for obj in found if obj.kind in INTERFACE_KINDS] == ["DcimVirtualInterface"]
