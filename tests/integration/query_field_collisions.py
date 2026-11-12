"""Find a field selected more than once on one object, where a later selection asks for what the first does not."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from graphql import (
    FieldNode,
    FragmentDefinitionNode,
    FragmentSpreadNode,
    GraphQLInterfaceType,
    GraphQLNamedType,
    GraphQLObjectType,
    GraphQLSchema,
    GraphQLUnionType,
    InlineFragmentNode,
    NamedTypeNode,
    OperationDefinitionNode,
    SelectionSetNode,
    get_named_type,
    parse,
)
from infrahub_sdk.graphql.query_renderer import render_query_with_fragments

MAX_DEPTH = 16


@dataclass(frozen=True)
class Collision:
    """One response key selected twice on one object type, with an example of what the later selection loses."""

    query: str
    path: str
    lost: str


class _Walker:
    def __init__(self, schema: GraphQLSchema, fragments: dict[str, FragmentDefinitionNode], query: str) -> None:
        self.schema, self.fragments, self.query = schema, fragments, query
        self.found: set[Collision] = set()

    def _applies(self, condition: NamedTypeNode | None, concrete: GraphQLObjectType) -> bool:
        if condition is None:
            return True
        wanted = self.schema.get_type(condition.name.value)
        if wanted is concrete:
            return True
        return isinstance(wanted, (GraphQLInterfaceType, GraphQLUnionType)) and self.schema.is_sub_type(
            wanted, concrete
        )

    def _collect(self, selections: SelectionSetNode, concrete: GraphQLObjectType) -> list[FieldNode]:
        fields: list[FieldNode] = []
        for selection in selections.selections:
            if isinstance(selection, FieldNode):
                fields.append(selection)
            elif isinstance(selection, InlineFragmentNode):
                if self._applies(selection.type_condition, concrete):
                    fields += self._collect(selection.selection_set, concrete)
            elif isinstance(selection, FragmentSpreadNode):
                fragment = self.fragments[selection.name.value]
                if self._applies(fragment.type_condition, concrete):
                    fields += self._collect(fragment.selection_set, concrete)
        return fields

    def _concrete(self, named: GraphQLNamedType) -> list[GraphQLObjectType]:
        if isinstance(named, (GraphQLInterfaceType, GraphQLUnionType)):
            return list(self.schema.get_possible_types(named))
        return [named] if isinstance(named, GraphQLObjectType) else []

    def _paths(self, fields: Iterable[FieldNode], named: GraphQLNamedType, prefix: tuple[str, ...] = ()) -> set[str]:
        paths: set[str] = set()
        for concrete in self._concrete(named):
            for field in fields:
                if field.selection_set is None:
                    continue
                for sub in self._collect(field.selection_set, concrete):
                    key = prefix + ((sub.alias or sub.name).value,)
                    paths.add("/".join(key))
                    definition = concrete.fields.get(sub.name.value)
                    if definition is not None and sub.selection_set is not None:
                        paths |= self._paths([sub], get_named_type(definition.type), key)
        return paths

    def walk(self, selections: SelectionSetNode, named: GraphQLNamedType, path: tuple[str, ...] = ()) -> None:
        if len(path) > MAX_DEPTH:
            return
        for concrete in self._concrete(named):
            by_key: dict[str, list[FieldNode]] = {}
            for field in self._collect(selections, concrete):
                by_key.setdefault((field.alias or field.name).value, []).append(field)
            for key, fields in by_key.items():
                definition = concrete.fields.get(fields[0].name.value)
                selected = [field for field in fields if field.selection_set is not None]
                if definition is None or not selected:
                    continue
                result_type = get_named_type(definition.type)
                if len(selected) > 1:
                    lost = self._paths(selected[1:], result_type) - self._paths(selected[:1], result_type)
                    if lost:
                        where = "/".join((*path, f"{concrete.name}.{key}"))
                        self.found.add(Collision(self.query, where, sorted(lost)[0]))
                # What the server returns is the first selection
                first = selected[0].selection_set
                assert first is not None
                self.walk(first, result_type, (*path, key))


def query_collisions(schema: GraphQLSchema, query_path: Path, fragment_texts: list[str]) -> set[Collision]:
    """The collisions of one query file, rendered with the shared fragments the way the repository sync renders it."""
    document = parse(render_query_with_fragments(query_path.read_text(), fragment_texts))
    fragments = {d.name.value: d for d in document.definitions if isinstance(d, FragmentDefinitionNode)}
    walker = _Walker(schema, fragments, str(query_path))
    root = schema.query_type
    assert root is not None
    for definition in document.definitions:
        if isinstance(definition, OperationDefinitionNode):
            walker.walk(definition.selection_set, root)
    return walker.found
