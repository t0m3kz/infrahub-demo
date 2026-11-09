"""Direct execution assembles the same native fragments as repository registration."""

from pathlib import Path

from graphql import FragmentDefinitionNode, FragmentSpreadNode, parse
from graphql.language.visitor import Visitor, visit

from utils.graphql_queries import load_query


def test_all_native_query_spreads_are_assembled(root_dir: Path) -> None:
    """Every standalone query's transitive fragments are present, with no duplicate definitions."""

    class Spreads(Visitor):
        def __init__(self) -> None:
            super().__init__()
            self.names: set[str] = set()

        def enter_fragment_spread(self, node: FragmentSpreadNode, *_args: object) -> None:
            self.names.add(node.name.value)

    for path in (root_dir / "queries").rglob("*.gql"):
        if path.parent.name == "fragments":
            continue
        document = parse(load_query(path))
        definitions = [d.name.value for d in document.definitions if isinstance(d, FragmentDefinitionNode)]
        visitor = Spreads()
        visit(document, visitor)
        assert len(definitions) == len(set(definitions)), path
        assert visitor.names <= set(definitions), path
