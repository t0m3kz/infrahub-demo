"""Guard the segment's firewall gateway flag (``ManagedVxlanSegment.firewall_gateway``) and the removed edge."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

FLAG = "firewall_gateway"


def _segment_attribute(root_dir: Path) -> dict[str, Any]:
    document = yaml.safe_load((root_dir / "schemas/extensions/service/segment.yml").read_text())
    node = next(item for item in document["nodes"] if item["name"] == "VxlanSegment")
    return next(attribute for attribute in node["attributes"] if attribute["name"] == FLAG)


def test_the_flag_is_a_required_boolean_off_by_default(root_dir: Path) -> None:
    """A segment is routed by the fabric unless it says otherwise."""
    attribute = _segment_attribute(root_dir)

    assert attribute["kind"] == "Boolean"
    assert attribute["optional"] is False
    assert attribute["default_value"] is False
    assert isinstance(attribute.get("order_weight"), int)


def test_the_old_edge_and_its_reverse_are_gone(root_dir: Path) -> None:
    """Greenfield: no schema file declares either end any more."""
    text = "\n".join(path.read_text() for path in sorted((root_dir / "schemas").rglob("*.y*ml")))

    assert "terminated_on" not in text
    assert "terminated_segments" not in text


def test_the_flag_is_read_by_production_code(root_dir: Path) -> None:
    """utils (the fabric gate), generators (the port reconcile), the checks and the queries read it."""
    for folder in ("utils", "generators", "checks"):
        text = "\n".join(path.read_text() for path in sorted((root_dir / folder).rglob("*.py")))
        assert FLAG in text, folder
    queries = "\n".join(path.read_text() for path in sorted((root_dir / "queries").rglob("*.gql")))
    assert FLAG in queries
