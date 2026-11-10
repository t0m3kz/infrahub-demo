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


def test_every_registered_query_renders_with_the_servers_fragment_resolution(root_dir: Path) -> None:
    """The server inlines shared fragments from the spreads of the OPERATION only: a fragment defined inside a query
    file is taken as self-contained, so one that spreads a shared fragment fails the repository sync."""
    import yaml
    from infrahub_sdk.graphql.query_renderer import render_query_with_fragments

    fragments = [path.read_text() for path in sorted((root_dir / "queries/fragments").glob("*.gql"))]
    registered = yaml.safe_load((root_dir / ".infrahub.yml").read_text())["queries"]
    for query in registered:
        rendered = render_query_with_fragments((root_dir / query["file_path"]).read_text(), fragments)
        document = parse(rendered)
        defined = {d.name.value for d in document.definitions if isinstance(d, FragmentDefinitionNode)}
        spreads = Spreads()
        visit(document, spreads)
        assert spreads.names <= defined, f"{query['name']}: undefined {sorted(spreads.names - defined)}"
