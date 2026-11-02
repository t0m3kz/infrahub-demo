"""Guard the two ends of the terminated-segment edge (``ManagedVxlanSegment.terminated_on`` and its reverse).

A segment is terminated on ONE firewall context (its gateway is the pair's floating VIP), a context terminates many
segments. The segment end is an attribute-kind relationship (written with the segment), the context end a generic
many; both share one identifier and neither is declared on any other kind. Each end has a production reader: a field
nothing reads is dead (AGENTS.md, Simplicity Rules).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

IDENTIFIER = "firewall_context__terminated_segments"
SEGMENT_END = "terminated_on"
CONTEXT_END = "terminated_segments"


def _declarations(root_dir: Path, name: str) -> list[tuple[str, dict[str, Any]]]:
    """Every relationship called ``name`` in the schema files, as (kind, declaration); an extension names its kind."""
    found: list[tuple[str, dict[str, Any]]] = []
    for path in sorted((root_dir / "schemas").rglob("*.y*ml")):
        document = yaml.safe_load(path.read_text()) or {}
        items = [
            (f"{item.get('namespace', '')}{item['name']}", item)
            for section in ("generics", "nodes")
            for item in document.get(section) or []
        ] + [(item["kind"], item) for item in (document.get("extensions") or {}).get("nodes") or []]
        found.extend(
            (kind, relationship)
            for kind, item in items
            for relationship in item.get("relationships") or []
            if relationship["name"] == name
        )
    return found


class TestTheTwoEnds:
    def test_the_segment_end_is_an_optional_attribute_to_one_firewall_context(self, root_dir: Path) -> None:
        """terminated_on: one, optional, an Attribute, peering the concrete firewall context (not the LB one)."""
        ((kind, end),) = _declarations(root_dir, SEGMENT_END)

        assert kind == "ManagedVxlanSegment"
        assert end["peer"] == "ManagedFirewallContext"
        assert end["cardinality"] == "one"
        assert end["kind"] == "Attribute"
        assert end["optional"] is True
        assert end["identifier"] == IDENTIFIER

    def test_the_context_end_is_an_optional_generic_many(self, root_dir: Path) -> None:
        """terminated_segments: many, optional, written only from the segment side (a Generic, like served_deployments)."""
        ((kind, end),) = _declarations(root_dir, CONTEXT_END)

        assert kind == "ManagedFirewallContext"
        assert end["peer"] == "ManagedVxlanSegment"
        assert end["cardinality"] == "many"
        assert end["kind"] == "Generic"
        assert end["optional"] is True
        assert end["identifier"] == IDENTIFIER

    def test_the_identifier_pairs_exactly_these_two_ends(self, root_dir: Path) -> None:
        """No third relationship shares the identifier (it would join it to the pair)."""
        sharing = [
            (kind, relationship["name"])
            for path in sorted((root_dir / "schemas").rglob("*.y*ml"))
            for document in [yaml.safe_load(path.read_text()) or {}]
            for item in [
                *(document.get("generics") or []),
                *(document.get("nodes") or []),
                *((document.get("extensions") or {}).get("nodes") or []),
            ]
            for kind in [item.get("kind") or f"{item.get('namespace', '')}{item['name']}"]
            for relationship in item.get("relationships") or []
            if relationship.get("identifier") == IDENTIFIER
        ]

        assert sorted(sharing) == [("ManagedFirewallContext", CONTEXT_END), ("ManagedVxlanSegment", SEGMENT_END)]

    def test_the_weights_are_explicit(self, root_dir: Path) -> None:
        """Every relationship carries an explicit order_weight (an unset one ties with the server's numbering)."""
        for name in (SEGMENT_END, CONTEXT_END):
            ((_, end),) = _declarations(root_dir, name)
            assert isinstance(end.get("order_weight"), int)


class TestEachEndIsRead:
    """No dead field: the segment end gates the fabric and drives the reconcile, the context end feeds the firewall."""

    @staticmethod
    def _production_text(root_dir: Path, folders: tuple[str, ...], suffixes: tuple[str, ...]) -> str:
        return "\n".join(
            path.read_text()
            for folder in folders
            for path in sorted((root_dir / folder).rglob("*"))
            if path.suffix in suffixes and "__pycache__" not in path.parts
        )

    def test_the_segment_end_is_read_by_production_code(self, root_dir: Path) -> None:
        """utils (the fabric gate), generators (the port reconcile), the checks and the queries read it."""
        for folder in ("utils", "generators", "checks"):
            assert SEGMENT_END in self._production_text(root_dir, (folder,), (".py",)), folder
        assert SEGMENT_END in self._production_text(root_dir, ("queries",), (".gql",))

    def test_the_context_end_is_read_by_the_firewall_query_and_transform(self, root_dir: Path) -> None:
        """The firewall config query selects it and the transform derives the sub-interface from it."""
        queries = self._production_text(root_dir, ("queries/fragments", "queries/config"), (".gql",))
        transforms = self._production_text(root_dir, ("transforms",), (".py",))

        assert CONTEXT_END in queries
        assert CONTEXT_END in transforms
