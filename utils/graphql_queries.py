"""Assemble native repository queries with only the shared fragments they reference."""

from functools import cache
from pathlib import Path

from graphql import FragmentDefinitionNode, FragmentSpreadNode, parse, print_ast
from graphql.language.visitor import Visitor, visit


class _Spreads(Visitor):
    def __init__(self) -> None:
        super().__init__()
        self.names: set[str] = set()

    def enter_fragment_spread(self, node: FragmentSpreadNode, *_args: object) -> None:
        self.names.add(node.name.value)


@cache
def _catalog(directory: Path) -> dict[str, FragmentDefinitionNode]:
    return {
        definition.name.value: definition
        for path in sorted(directory.glob("*.gql"))
        for definition in parse(path.read_text()).definitions
        if isinstance(definition, FragmentDefinitionNode)
    }


def load_query(path: Path) -> str:
    """Append transitive shared fragments, retaining in-file definitions and native response names."""
    text = path.read_text()
    document = parse(text)
    defined = {d.name.value for d in document.definitions if isinstance(d, FragmentDefinitionNode)}
    spreads = _Spreads()
    visit(document, spreads)
    catalog = _catalog(Path(__file__).resolve().parents[1] / "queries/fragments")
    pending = sorted(spreads.names - defined)
    while pending:
        name = pending.pop(0)
        if name in defined:
            continue
        if name not in catalog:
            raise ValueError(f"{path}: unknown fragment {name}")
        definition = catalog[name]
        text += "\n" + print_ast(definition) + "\n"
        defined.add(name)
        nested = _Spreads()
        visit(definition, nested)
        pending.extend(sorted(nested.names - defined))
    return text
