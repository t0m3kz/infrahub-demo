"""Direct execution assembles the same native fragments as repository registration."""

from pathlib import Path

from graphql import FragmentDefinitionNode, FragmentSpreadNode, parse
from graphql.language.visitor import Visitor, visit

from utils.graphql_queries import load_query


class Spreads(Visitor):
    def __init__(self) -> None:
        super().__init__()
        self.names: set[str] = set()

    def enter_fragment_spread(self, node: FragmentSpreadNode, *_args: object) -> None:
        self.names.add(node.name.value)


def test_all_native_query_spreads_are_assembled(root_dir: Path) -> None:
    """Every standalone query's transitive fragments are present, with no duplicate definitions."""

    for path in (root_dir / "queries").rglob("*.gql"):
        if path.parent.name == "fragments":
            continue
        document = parse(load_query(path))
        definitions = [d.name.value for d in document.definitions if isinstance(d, FragmentDefinitionNode)]
        visitor = Spreads()
        visit(document, visitor)
        assert len(definitions) == len(set(definitions)), path
        assert visitor.names <= set(definitions), path


def test_every_shared_fragment_is_defined_in_one_file_only(root_dir: Path) -> None:
    """.infrahub.yml loads the whole queries/fragments/ directory: a fragment name defined twice there is ambiguous."""
    seen: dict[str, str] = {}
    for path in sorted((root_dir / "queries/fragments").glob("*.gql")):
        for item in parse(path.read_text()).definitions:
            if isinstance(item, FragmentDefinitionNode):
                assert item.name.value not in seen, (
                    f"{item.name.value} defined in {seen[item.name.value]} and {path.name}"
                )
                seen[item.name.value] = path.name
