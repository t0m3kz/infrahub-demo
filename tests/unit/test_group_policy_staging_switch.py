"""The group-policy staging switch (``group_policy_enforce``) across layers, and the schema description limit.

The switch is declared in the schema (TopologyDataCenter), set by the demo data (DC10 true, DC11 and DC12 absent
and therefore false) and read by the config and matrix queries. An integration run once failed on a 233-character
attribute description (Infrahub caps attribute and relationship descriptions at 128 characters), so the offline
scan of every schema file is here too.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / "schemas"
DESCRIPTION_LIMIT = 128
"""Infrahub's maximum length of an attribute or relationship description."""
SWITCH = "group_policy_enforce"

DATA_CENTERS = {
    "DC10": ROOT / "data/demos/30_all/03_dc/dc10/01_topology.yml",
    "DC11": ROOT / "data/demos/30_all/03_dc/dc11/02_topology.yml",
    "DC12": ROOT / "data/demos/30_all/03_dc/dc12/03_topology.yml",
}
QUERIES_READING_THE_SWITCH = (
    "queries/config/leaf.gql",
    "queries/config/border_leaf.gql",
    "queries/config/border_spine.gql",
    "queries/topology/segmentation_matrix.gql",
)


def _data_center(path: Path, name: str) -> dict[str, Any]:
    """The TopologyDataCenter object ``name`` of an object file (several YAML documents are allowed)."""
    for document in yaml.safe_load_all(path.read_text()):
        spec = (document or {}).get("spec") or {}
        if spec.get("kind") == "TopologyDataCenter":
            for item in spec.get("data") or []:
                if item.get("name") == name:
                    return item
    raise AssertionError(f"{name} is not defined in {path}")


def _schema_attributes(path: Path) -> list[tuple[str, dict[str, Any]]]:
    """(owner, attribute-or-relationship) of every node, generic and extension in a schema file."""
    document = yaml.safe_load(path.read_text()) or {}
    owners = [*(document.get("nodes") or []), *(document.get("generics") or [])]
    owners += (document.get("extensions") or {}).get("nodes") or []
    found = []
    for owner in owners:
        label = f"{path.name}:{owner.get('name') or owner.get('kind')}"
        for section in ("attributes", "relationships"):
            found.extend((f"{label}.{item.get('name')}", item) for item in owner.get(section) or [])
    return found


def _too_long(path: Path) -> list[str]:
    """The attributes and relationships of a schema file whose description exceeds the limit."""
    return [
        f"{where} ({len(' '.join(str(item['description']).split()))} chars)"
        for where, item in _schema_attributes(path)
        if len(" ".join(str(item.get("description") or "").split())) > DESCRIPTION_LIMIT
    ]


class TestStagingSwitchAcrossLayers:
    """Schema -> data -> query: the switch is an optional Boolean defaulting to false, set only on DC10."""

    @staticmethod
    def _attribute() -> dict[str, Any]:
        document = yaml.safe_load((SCHEMAS / "extensions/topology/topology_dc.yml").read_text())
        node = next(item for item in document["nodes"] if item["name"] == "DataCenter")
        return next(item for item in node["attributes"] if item["name"] == SWITCH)

    def test_the_schema_declares_an_optional_boolean_defaulting_to_false(self) -> None:
        attribute = self._attribute()
        assert attribute["kind"] == "Boolean"
        assert attribute["optional"] is True
        assert attribute["default_value"] is False

    def test_the_schema_description_fits_the_infrahub_limit(self) -> None:
        description = " ".join(self._attribute()["description"].split())
        assert 0 < len(description) <= DESCRIPTION_LIMIT, len(description)

    def test_dc10_enforces(self) -> None:
        assert _data_center(DATA_CENTERS["DC10"], "DC10")[SWITCH] is True

    @pytest.mark.parametrize("name", ["DC11", "DC12"])
    def test_the_other_data_centers_leave_the_switch_unset_and_so_stage(self, name: str) -> None:
        """Absent means the schema default (false): contracts are configured, enforcement is not."""
        assert SWITCH not in _data_center(DATA_CENTERS[name], name)

    @pytest.mark.parametrize("query", QUERIES_READING_THE_SWITCH)
    def test_every_config_and_matrix_query_reads_the_switch(self, query: str) -> None:
        assert f"{SWITCH} {{ value }}" in (ROOT / query).read_text()


class TestSchemaDescriptionLimit:
    """Attribute and relationship descriptions are capped at 128 characters (choice descriptions are not)."""

    def test_the_scan_sees_the_schemas(self) -> None:
        files = sorted(SCHEMAS.rglob("*.yml"))
        assert files
        assert sum(len(_schema_attributes(path)) for path in files) > 100

    @pytest.mark.parametrize("path", sorted(SCHEMAS.rglob("*.yml")), ids=lambda path: str(path.relative_to(SCHEMAS)))
    def test_no_attribute_or_relationship_description_is_longer_than_the_limit(self, path: Path) -> None:
        assert _too_long(path) == []

    def test_the_scan_catches_a_233_character_description(self, tmp_path: Path) -> None:
        """A synthetic schema file shaped like the one that failed the integration run is flagged."""
        schema = tmp_path / "bad.yml"
        schema.write_text(
            yaml.safe_dump(
                {
                    "nodes": [
                        {
                            "name": "X",
                            "attributes": [{"name": "a", "kind": "Text", "description": "d" * 233}],
                            "relationships": [{"name": "r", "peer": "Y", "description": "short"}],
                        }
                    ]
                }
            )
        )
        assert _too_long(schema) == ["bad.yml:X.a (233 chars)"]
