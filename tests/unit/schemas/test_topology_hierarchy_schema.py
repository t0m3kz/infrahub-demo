"""The hierarchy of the topology kinds"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_TOPOLOGY = Path(__file__).parents[3] / "schemas" / "extensions" / "topology"


def _nodes() -> dict[str, dict[str, Any]]:
    """Every topology node by its kind (namespace + name)."""
    nodes: dict[str, dict[str, Any]] = {}
    for path in sorted(_TOPOLOGY.glob("*.yml")):
        for node in (yaml.safe_load(path.read_text()) or {}).get("nodes") or []:
            nodes[f"{node['namespace']}{node['name']}"] = node
    return nodes


def test_a_parent_that_names_its_children_has_no_other_kind_below_it() -> None:
    """If a node names a child kind, every node that names it as parent is that kind (or it uses ``""``)."""
    nodes = _nodes()
    for kind, node in nodes.items():
        children = node.get("children")
        if not children:
            continue
        below = {k for k, n in nodes.items() if n.get("parent") == kind}
        assert below <= {children}, f"{kind} lists only {children}, hiding {sorted(below - {children})}"


def test_the_cloud_region_shows_its_zones_and_the_customer_footprints() -> None:
    """The region accepts every kind that names it as parent: zones and customer clouds."""
    nodes = _nodes()

    assert nodes["TopologyCloudRegion"]["children"] == ""
    assert nodes["TopologyCloudZone"]["parent"] == "TopologyCloudRegion"
    assert nodes["TopologyCustomerCloud"]["parent"] == "TopologyCloudRegion"
