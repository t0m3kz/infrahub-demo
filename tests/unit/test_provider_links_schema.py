"""The provider and owner reverse edges (schemas/extensions/topology/topology_provider_links.yml) mirror their forward edges."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

_SCHEMAS = Path(__file__).parents[2] / "schemas"

# (kind declaring the reverse, reverse name, kind declaring the forward edge, forward name)
_PAIRS = [
    ("OrganizationEntity", "owned_devices", "DcimDeviceManaged", "owner"),
    ("OrganizationProvider", "circuits", "TopologyCircuit", "provider"),
    ("OrganizationProvider", "provider_ports", "DcimProviderPort", "provider"),
    ("OrganizationProvider", "service_endpoints", "CloudServiceEndpoint", "provider"),
]


def _relationships() -> dict[tuple[str, str], dict[str, Any]]:
    """(full kind, relationship name) -> relationship, over every node, generic and extension."""
    found: dict[tuple[str, str], dict[str, Any]] = {}
    for path in _SCHEMAS.rglob("*.yml"):
        document = yaml.safe_load(path.read_text()) or {}
        entries = [
            (f"{n.get('namespace', '')}{n['name']}", n)
            for key in ("nodes", "generics")
            for n in document.get(key) or []
        ]
        entries += [(n["kind"], n) for n in (document.get("extensions") or {}).get("nodes") or []]
        for kind, entry in entries:
            for relationship in entry.get("relationships") or []:
                found[(kind, relationship["name"])] = relationship
    return found


@pytest.mark.parametrize(("reverse_kind", "reverse", "forward_kind", "forward"), _PAIRS)
def test_a_reverse_edge_mirrors_its_forward_edge(
    reverse_kind: str, reverse: str, forward_kind: str, forward: str
) -> None:
    """Same identifier, and each side's peer is the kind that declares the other."""
    relationships = _relationships()
    back, front = relationships[(reverse_kind, reverse)], relationships[(forward_kind, forward)]
    assert back["identifier"] == front["identifier"]
    assert back["peer"] == forward_kind
    assert front["peer"] == reverse_kind
    assert back["cardinality"] == "many"
    assert back.get("optional") is True
    assert "order_weight" in back and back.get("description")
