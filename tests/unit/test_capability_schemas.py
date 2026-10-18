"""Schema tests for the per-node count limits on the capability kinds.

ManagedMLAG caps its device edge at exactly two peers; ManagedBGP and ManagedOSPF keep an
uncapped many edge (a many relationship cannot be capped at one). The cap holds only for
writes from the MLAG side, which is why the runtime member-count guards stay.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml


def _capability_node(root_dir: Path, filename: str, name: str) -> dict[str, Any]:
    """Return the node ``name`` declared in schemas/extensions/capabilities/<filename>."""
    schema = yaml.safe_load((root_dir / "schemas" / "extensions" / "capabilities" / filename).read_text())
    nodes = [node for node in schema["nodes"] if node["name"] == name]
    assert len(nodes) == 1, f"{name} must be declared exactly once in {filename}"
    return nodes[0]


def _relationship(node: dict[str, Any], name: str) -> dict[str, Any]:
    """Return the relationship ``name`` declared on ``node``."""
    matches = [rel for rel in node.get("relationships", []) if rel["name"] == name]
    assert len(matches) == 1, f"{node['name']} must declare relationship {name} exactly once"
    return matches[0]


def _generic_capabilities(root_dir: Path) -> dict[str, Any]:
    """The `capabilities` relationship of the ManagedGenericDevice generic."""
    schema = yaml.safe_load((root_dir / "schemas" / "extensions" / "capabilities" / "capabilities.yml").read_text())
    (generic,) = [g for g in schema["generics"] if g["name"] == "GenericDevice"]
    return _relationship(generic, "capabilities")


class TestMlagCapabilitiesCount:
    """ManagedMLAG re-declares `capabilities` with exactly two peers, mandatory."""

    def test_mlag_requires_exactly_two_devices(self, root_dir: Path) -> None:
        """min_count and max_count are both 2 and the edge is not optional (optional would ignore min_count)."""
        rel = _relationship(_capability_node(root_dir, "mlag.yml", "MLAG"), "capabilities")

        assert rel["min_count"] == 2
        assert rel["max_count"] == 2
        assert rel["optional"] is False

    def test_mlag_redeclaration_matches_the_generic_edge(self, root_dir: Path) -> None:
        """The node-local declaration replaces the generic's wholesale, so peer, kind, direction, identifier agree."""
        rel = _relationship(_capability_node(root_dir, "mlag.yml", "MLAG"), "capabilities")
        generic = _generic_capabilities(root_dir)

        for key in ("peer", "cardinality", "kind", "direction", "identifier"):
            assert rel[key] == generic[key], key

    def test_generic_edge_stays_uncapped(self, root_dir: Path) -> None:
        """A device-side write resolves the generic's declaration, which must not carry counts of its own."""
        generic = _generic_capabilities(root_dir)

        assert generic["cardinality"] == "many"
        assert "min_count" not in generic
        assert "max_count" not in generic

    def test_mlag_no_longer_inherits_capabilities(self, root_dir: Path) -> None:
        """The MLAG-to-interface edge was write-only and is gone; MLAG is a device capability only."""
        node = _capability_node(root_dir, "mlag.yml", "MLAG")

        assert "ManagedGenericInterfaces" not in node["inherit_from"]
        assert "ManagedGenericDevice" in node["inherit_from"]


class TestRoutingProcessesStayMany:
    """A many relationship cannot be capped at one, so BGP and OSPF keep an uncapped many edge."""

    @pytest.mark.parametrize(("filename", "name"), [("bgp.yml", "BGP"), ("ospf.yml", "OSPF")])
    def test_process_capabilities_is_many_and_uncapped(self, root_dir: Path, filename: str, name: str) -> None:
        """No count limits and still cardinality many (a cardinality-one edge is a separate, unproven schema)."""
        rel = _relationship(_capability_node(root_dir, filename, name), "capabilities")

        assert rel["cardinality"] == "many"
        assert "min_count" not in rel
        assert "max_count" not in rel
