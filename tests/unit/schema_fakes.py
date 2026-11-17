"""The schema a live Infrahub serves, rebuilt from the schema files of the repository (``schemas/``) and the core kinds
the generators write: what ``client.schema.all()`` returns to the schema pre-check of the reconciler
(generators/schema_check.py).

A node has the attributes and relationships of the generics it inherits plus its own and what the extensions add, as the
server serves them; every kind has the relationships every node of a live schema has (``member_of_groups``,
``object_template``). The core kinds a plan writes are described here by hand, as far as a plan reaches them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from functools import cache
from pathlib import Path
from typing import Any

import yaml
from infrahub_sdk.schema import AttributeSchemaAPI, NodeSchemaAPI, RelationshipSchemaAPI

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_FILES = ("schemas/base", "schemas/extensions")


def _attribute(name: str, *, optional: bool = False, kind: str = "Text", default: Any = None) -> dict[str, Any]:
    return {"name": name, "kind": kind, "optional": optional, "default_value": default}


def _relationship(name: str, peer: str, *, optional: bool = True, cardinality: str = "one") -> dict[str, Any]:
    return {"name": name, "peer": peer, "optional": optional, "cardinality": cardinality, "identifier": f"fake__{name}"}


# kind -> (inherit_from, attributes, relationships): the core kinds the plans write
_CORE: dict[str, tuple[list[str], list[dict[str, Any]], list[dict[str, Any]]]] = {
    "CoreStandardGroup": (
        ["CoreGroup"],
        [_attribute("name"), _attribute("description", optional=True)],
        [],
    ),
    "CoreNumberPool": (
        ["CoreResourcePool"],
        [
            _attribute("name"),
            _attribute("description", optional=True),
            _attribute("node"),
            _attribute("node_attribute"),
            _attribute("start_range", kind="Number"),
            _attribute("end_range", kind="Number"),
        ],
        [],
    ),
    "CoreIPPrefixPool": (
        ["CoreResourcePool"],
        [
            _attribute("name"),
            _attribute("description", optional=True),
            _attribute("default_prefix_length", optional=True, kind="Number"),
            _attribute("default_prefix_type", optional=True),
            _attribute("default_member_type", optional=True, default="prefix"),
        ],
        [
            _relationship("resources", "BuiltinIPPrefix", optional=False, cardinality="many"),
            _relationship("ip_namespace", "BuiltinIPNamespace", optional=False),
        ],
    ),
    "CoreIPAddressPool": (
        ["CoreResourcePool"],
        [
            _attribute("name"),
            _attribute("description", optional=True),
            _attribute("default_prefix_length", optional=True, kind="Number"),
            _attribute("default_address_type"),
        ],
        [
            _relationship("resources", "BuiltinIPPrefix", optional=False, cardinality="many"),
            _relationship("ip_namespace", "BuiltinIPNamespace", optional=False),
        ],
    ),
}
# the core generics the repository's nodes inherit (a kind is served with what it inherits)
_CORE_GENERICS: dict[str, dict[str, list[dict[str, Any]]]] = {
    "BuiltinIPAddress": {
        "attributes": [_attribute("address", kind="IPHost"), _attribute("description", optional=True)],
        "relationships": [
            _relationship("ip_namespace", "BuiltinIPNamespace"),
            _relationship("ip_prefix", "BuiltinIPPrefix"),
        ],
    }
}
_GROUP_MEMBERSHIP = _relationship("member_of_groups", "CoreGroup", cardinality="many")
_EVERY_NODE = [_GROUP_MEMBERSHIP, _relationship("object_template", "CoreObjectTemplate")]
"""What a node of the repository has; a core kind has only the group membership (a pool has no object template)."""


def _documents() -> list[dict[str, Any]]:
    return [
        yaml.safe_load(path.read_text()) for folder in SCHEMA_FILES for path in sorted((ROOT / folder).rglob("*.yml"))
    ]


def _full_name(item: Mapping[str, Any]) -> str:
    return f"{item['namespace']}{item['name']}"


@cache
def real_schema() -> dict[str, Any]:
    """Kind -> schema (an SDK ``NodeSchemaAPI``) of every node of the repository and the core kinds a plan writes."""
    documents = _documents()
    generics = {_full_name(g): g for doc in documents for g in doc.get("generics") or []}
    generics.update(_CORE_GENERICS)
    extensions: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for doc in documents:
        for extension in (doc.get("extensions") or {}).get("nodes") or []:
            found = extensions.setdefault(extension["kind"], {"attributes": [], "relationships": []})
            found["attributes"].extend(extension.get("attributes") or [])
            found["relationships"].extend(extension.get("relationships") or [])
    schema: dict[str, Any] = {}
    for doc in documents:
        for node in doc.get("nodes") or []:
            kind = _full_name(node)
            attributes: dict[str, Mapping[str, Any]] = {}
            relationships: dict[str, Mapping[str, Any]] = {}
            for generic_name in node.get("inherit_from") or []:
                generic = generics.get(generic_name)
                if generic is None:
                    continue
                attributes.update({a["name"]: a for a in generic.get("attributes") or []})
                relationships.update({r["name"]: r for r in generic.get("relationships") or []})
            for generic_name in node.get("inherit_from") or []:
                extension = extensions.get(generic_name, {})
                attributes.update({a["name"]: a for a in extension.get("attributes") or []})
                relationships.update({r["name"]: r for r in extension.get("relationships") or []})
            for source in (node, extensions.get(kind, {})):
                attributes.update({a["name"]: a for a in source.get("attributes") or []})
                relationships.update({r["name"]: r for r in source.get("relationships") or []})
            relationships.update({r["name"]: r for r in _EVERY_NODE if r["name"] not in relationships})
            schema[kind] = _api_node(
                node["name"],
                node["namespace"],
                list(node.get("inherit_from") or []),
                list(attributes.values()),
                list(relationships.values()),
            )
    for kind, (inherit, core_attributes, core_relationships) in _CORE.items():
        namespace = "Core"
        schema[kind] = _api_node(
            kind.removeprefix(namespace), namespace, inherit, core_attributes, [*core_relationships, _GROUP_MEMBERSHIP]
        )
    return schema


def _api_node(
    name: str,
    namespace: str,
    inherit_from: list[str],
    attributes: Sequence[Mapping[str, Any]],
    relationships: Sequence[Mapping[str, Any]],
) -> NodeSchemaAPI:
    return NodeSchemaAPI(
        name=name,
        namespace=namespace,
        inherit_from=inherit_from,
        attributes=[_attribute_api(a) for a in attributes],
        relationships=[_relationship_api(r) for r in relationships],
    )


def _attribute_api(attribute: Mapping[str, Any]) -> AttributeSchemaAPI:
    fields: dict[str, Any] = {"kind": "Text"}
    fields.update(
        {key: attribute[key] for key in ("name", "kind", "optional", "default_value", "read_only") if key in attribute}
    )
    return AttributeSchemaAPI(**fields)


def _relationship_api(relationship: Mapping[str, Any]) -> RelationshipSchemaAPI:
    fields: dict[str, Any] = {"identifier": f"fake__{relationship['name']}"}
    fields |= {
        key: relationship[key]
        for key in ("name", "peer", "optional", "cardinality", "identifier", "direction", "kind", "read_only")
        if key in relationship
    }
    return RelationshipSchemaAPI(**fields)


class FakeSchema:
    """``client.schema``: ``all()`` is the schema the pre-check reads."""

    def __init__(self, kinds: Mapping[str, Any] | None = None) -> None:
        self.kinds = kinds if kinds is not None else real_schema()

    async def all(self, **_: Any) -> Mapping[str, Any]:
        return self.kinds
