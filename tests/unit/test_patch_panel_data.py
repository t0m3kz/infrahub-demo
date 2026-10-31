"""Every front port of a panel in the demo data is paired with a rear port.

An object template creates a panel's ports but cannot point a front port at its rear port (the server refuses a template
port as the peer), and ``rear_port`` is optional. The pairing is a data file next to the panel, so this test is the
guard: a panel created from a template in ``data/demos`` must have a mapping for each front port its template makes.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

_ROOT = Path(__file__).parents[2]
_BOOTSTRAP = _ROOT / "data" / "bootstrap"
_DEMOS = _ROOT / "data" / "demos"
_RANGE = re.compile(r"\[(\d+)-(\d+)\]")


def _expand(value: str) -> list[str]:
    """``a:[1-3]`` -> ``a:1``, ``a:2``, ``a:3``; a value with no range is itself."""
    match = _RANGE.search(value)
    if not match:
        return [value]
    low, high = int(match.group(1)), int(match.group(2))
    return [value[: match.start()] + str(n) + value[match.end() :] for n in range(low, high + 1)]


def _documents(root: Path) -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*.y*ml")):
        documents.extend(d for d in yaml.safe_load_all(path.read_text()) if isinstance(d, dict))
    return documents


def _template_front_counts() -> dict[str, int]:
    """Template name -> number of front ports its template creates."""
    counts: dict[str, int] = {}
    for document in _documents(_BOOTSTRAP):
        spec = document.get("spec") or {}
        if spec.get("kind") != "TemplateDcimPatchPanel":
            continue
        for template in spec["data"]:
            block = template.get("front_ports")
            if block:
                counts[template["template_name"]] = sum(len(_expand(port["name"])) for port in block["data"])
    return counts


def _panels_from_templates() -> dict[tuple[str, str], str]:
    """(rack, panel name) -> template name, for every panel of the demo data made from a template."""
    panels: dict[tuple[str, str], str] = {}
    for document in _documents(_DEMOS):
        spec = document.get("spec") or {}
        if spec.get("kind") != "DcimPatchPanel":
            continue
        for panel in spec["data"]:
            if panel.get("object_template"):
                panels[(panel["rack"], panel["name"])] = panel["object_template"]
    return panels


def _mapped_fronts() -> dict[tuple[str, str], dict[str, str]]:
    """(rack, panel name) -> {front port name: rear port id} from the DcimFrontPatchPanelPort mapping files."""
    mapped: dict[tuple[str, str], dict[str, str]] = {}
    for document in _documents(_DEMOS):
        spec = document.get("spec") or {}
        if spec.get("kind") != "DcimFrontPatchPanelPort":
            continue
        for entry in spec["data"]:
            if not entry.get("rear_port"):
                continue
            fronts, rears = _expand(entry["name"]), _expand(entry["rear_port"])
            assert len(fronts) == len(rears), entry
            rack, panel = entry["panel"]
            mapped.setdefault((rack, panel), {}).update(zip(fronts, rears, strict=True))
    return mapped


def test_every_front_port_of_a_demo_panel_is_paired_with_a_rear_port_of_the_same_panel() -> None:
    """A panel made from a template has N front ports; the mapping must pair each, and only with its own panel's ports."""
    counts, mapped = _template_front_counts(), _mapped_fronts()
    panels = _panels_from_templates()

    assert panels, "the demo data has no panel made from a template"
    for (rack, name), template in panels.items():
        pairs = mapped.get((rack, name), {})
        assert sorted(pairs, key=int) == [str(n) for n in range(1, counts[template] + 1)], (rack, name)
        assert all(rear.startswith(f"{rack}:{name}:") for rear in pairs.values()), (rack, name)


def test_a_mapping_pairs_each_rear_port_of_a_jack_panel_once() -> None:
    """On a one-to-one panel, front N takes rear N and no rear port is used twice."""
    for (rack, name), pairs in _mapped_fronts().items():
        rears = list(pairs.values())

        assert len(rears) == len(set(rears)), (rack, name)
        assert all(rear == f"{rack}:{name}:{front}" for front, rear in pairs.items()), (rack, name)


def test_expand_handles_ranges_and_plain_values() -> None:
    """The range helper the guards rely on."""
    assert _expand("a:[1-3]") == ["a:1", "a:2", "a:3"]
    assert _expand("a:7") == ["a:7"]
