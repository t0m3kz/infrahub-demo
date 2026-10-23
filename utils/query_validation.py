"""Validate every ``.gql`` file of the repository against a running Infrahub schema.

The point is a fast feedback loop: a schema change that breaks a query (``Cannot query field``, ``Unknown
type``, a renamed argument) otherwise surfaces tens of minutes into an integration run. This module holds the
logic behind ``invoke validate-queries``; everything but :func:`run_queries` is pure, and ``run_queries`` takes
any client with ``execute_graphql(query=..., variables=..., branch_name=...)`` so unit tests need no server.

Rules:

* A query that spreads fragments it does not define gets them appended from ``queries/fragments``, directly and
  through other fragments. A fragment the query defines itself is never duplicated.
* Variables are filled with dummy values derived from the operation's variable definitions.
* Only GraphQL validation errors count as failures. Runtime or data errors caused by the dummy values (node not
  found, permission, value errors) are ignored.
* Anything that is not a GraphQL error (connection refused, authentication, unknown branch) raises
  :class:`ToolingError`: the run is broken, not the queries.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from graphql import (
    DocumentNode,
    FragmentDefinitionNode,
)
from graphql import GraphQLError as GraphQLSyntaxError
from graphql import ListTypeNode, NamedTypeNode, NonNullTypeNode, OperationDefinitionNode, TypeNode, parse, print_ast
from graphql.language import FragmentSpreadNode, Visitor, visit
from infrahub_sdk.exceptions import GraphQLError

QUERY_ROOTS = ("queries", "tests")
FRAGMENTS_DIR = Path("queries") / "fragments"

EXIT_OK = 0
EXIT_QUERY_ERRORS = 1
EXIT_TOOLING_ERROR = 2

STATUS_OK = "OK"
STATUS_FAIL = "FAIL"
STATUS_SKIP = "SKIP"

# GraphQL validation messages (graphql-core rule names in parentheses) that mean the query does not fit the schema.
_SCHEMA_ERROR_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"^Syntax Error",
        r"Cannot query field",  # FieldsOnCorrectType
        r"^Unknown type",  # KnownTypeNames
        r"^Unknown argument",  # KnownArgumentNames
        r"^Unknown fragment",  # KnownFragmentNames
        r"^Unknown directive",  # KnownDirectives
        r"cannot be spread",  # PossibleFragmentSpreads
        r"^Field .* argument .* is required",  # ProvidedRequiredArguments
        r"must have a selection of subfields",  # ScalarLeafs
        r"must not have a selection since type",  # ScalarLeafs
        r"There can be only one fragment named",  # UniqueFragmentNames
        r"used in position expecting",  # VariablesInAllowedPosition
    )
)

_DUMMY_BY_SCALAR: dict[str, Any] = {"String": "x", "ID": "x", "Int": 0, "Float": 0, "Boolean": False}


class ToolingError(Exception):
    """The validation run itself is broken (server unreachable, bad token, unknown branch)."""


class GraphQLClient(Protocol):
    """The slice of ``InfrahubClient`` the runner needs."""

    async def execute_graphql(
        self, query: str, variables: dict | None = None, branch_name: str | None = None
    ) -> dict[str, Any]:
        """Execute one query and return its data, raising on GraphQL errors."""
        ...


@dataclass(frozen=True)
class QueryResult:
    """Outcome for one ``.gql`` file."""

    path: Path
    status: str
    message: str = ""


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def collect_queries(root: Path) -> list[Path]:
    """Return every ``.gql`` under ``queries/`` and ``tests/`` of ``root``, sorted."""
    return sorted(path for name in QUERY_ROOTS for path in (root / name).rglob("*.gql"))


def is_fragment_only(document: DocumentNode) -> bool:
    """Return True when the document defines no operation (a fragment library file)."""
    return not any(isinstance(definition, OperationDefinitionNode) for definition in document.definitions)


def load_fragments(fragments_dir: Path) -> dict[str, FragmentDefinitionNode]:
    """Parse every fragment of ``fragments_dir`` into a name -> definition map."""
    fragments: dict[str, FragmentDefinitionNode] = {}
    for path in sorted(fragments_dir.glob("*.gql")):
        for definition in parse(path.read_text()).definitions:
            if isinstance(definition, FragmentDefinitionNode):
                fragments[definition.name.value] = definition
    return fragments


class _SpreadCollector(Visitor):
    """Collect the names of all fragment spreads below a node."""

    def __init__(self) -> None:
        super().__init__()
        self.names: set[str] = set()

    def enter_fragment_spread(self, node: FragmentSpreadNode, *_: Any) -> None:
        """Record one spread."""
        self.names.add(node.name.value)


def _spread_names(node: Any) -> set[str]:
    collector = _SpreadCollector()
    visit(node, collector)
    return collector.names


def append_fragments(query: str, fragments: dict[str, FragmentDefinitionNode]) -> str:
    """Append the library fragments ``query`` needs but does not define, following nested spreads.

    Fragments the query defines itself are never appended again. A spread no library fragment answers is left
    alone: the server then reports ``Unknown fragment``, which is a real error.
    """
    document = parse(query)
    defined = {d.name.value for d in document.definitions if isinstance(d, FragmentDefinitionNode)}
    pending = sorted(_spread_names(document) - defined)
    appended: list[FragmentDefinitionNode] = []
    seen = set(defined)
    while pending:
        name = pending.pop(0)
        if name in seen:
            continue
        seen.add(name)
        fragment = fragments.get(name)
        if fragment is None:
            continue
        appended.append(fragment)
        pending.extend(sorted(_spread_names(fragment) - seen))
    if not appended:
        return query
    return query.rstrip("\n") + "\n\n" + "\n\n".join(print_ast(fragment) for fragment in appended) + "\n"


def dummy_value(type_node: TypeNode) -> Any:
    """Return a dummy variable value for a GraphQL type.

    ``String`` and ``ID`` give ``"x"``, ``Int`` and ``Float`` give ``0``, ``Boolean`` gives ``False``, a list gives a
    one-element list of the element dummy and any other (input object, enum, custom scalar) gives ``{}``.
    """
    if isinstance(type_node, NonNullTypeNode):
        return dummy_value(type_node.type)
    if isinstance(type_node, ListTypeNode):
        return [dummy_value(type_node.type)]
    if isinstance(type_node, NamedTypeNode):
        return _DUMMY_BY_SCALAR.get(type_node.name.value, {})
    return {}


def fill_variables(query: str) -> dict[str, Any]:
    """Return dummy values for the variables of every operation in ``query``."""
    variables: dict[str, Any] = {}
    for definition in parse(query).definitions:
        if isinstance(definition, OperationDefinitionNode):
            for variable in definition.variable_definitions or ():
                variables[variable.variable.name.value] = dummy_value(variable.type)
    return variables


def is_schema_error(message: str) -> bool:
    """Return True when ``message`` is a GraphQL validation error rather than a runtime or data error."""
    return any(pattern.search(message) for pattern in _SCHEMA_ERROR_PATTERNS)


def first_schema_error(messages: list[str]) -> str | None:
    """Return the first schema error among ``messages``, or None when they are all runtime or data errors."""
    return next((message for message in messages if is_schema_error(message)), None)


def error_messages(error: GraphQLError) -> list[str]:
    """Extract the message strings of an SDK GraphQL error."""
    return [str(item.get("message", item)) if isinstance(item, dict) else str(item) for item in error.errors]


def exit_code(results: list[QueryResult]) -> int:
    """Return 0 when no file failed, 1 otherwise. Tooling errors are raised, never part of the results."""
    return EXIT_QUERY_ERRORS if any(result.status == STATUS_FAIL for result in results) else EXIT_OK


def format_result(result: QueryResult, root: Path | None = None) -> str:
    """Return the ``OK``/``FAIL``/``SKIP`` line of one file."""
    path = result.path.relative_to(root) if root is not None and result.path.is_relative_to(root) else result.path
    line = f"{result.status:<4} {path}"
    return f"{line} -> {result.message}" if result.message else line


def summarize(results: list[QueryResult]) -> str:
    """Return the one-line final summary."""
    counts = {status: sum(1 for r in results if r.status == status) for status in (STATUS_OK, STATUS_FAIL, STATUS_SKIP)}
    return (
        f"{len(results)} files: {counts[STATUS_OK]} ok, {counts[STATUS_FAIL]} with schema errors, "
        f"{counts[STATUS_SKIP]} fragment-only skipped"
    )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


async def validate_query(
    client: GraphQLClient, path: Path, fragments: dict[str, FragmentDefinitionNode], branch: str
) -> QueryResult:
    """Run one file against the server and classify the outcome.

    Raises:
        ToolingError: the request failed for a reason other than a GraphQL error.
    """
    text = path.read_text()
    try:
        document = parse(text)
        if is_fragment_only(document):
            return QueryResult(path, STATUS_SKIP, "fragment library, validated through the queries that spread it")
        query = append_fragments(text, fragments)
        variables = fill_variables(query)
    except GraphQLSyntaxError as error:
        return QueryResult(path, STATUS_FAIL, f"Syntax Error: {error.message}")
    try:
        await client.execute_graphql(query=query, variables=variables or None, branch_name=branch)
    except GraphQLError as error:
        schema_error = first_schema_error(error_messages(error))
        if schema_error is None:
            return QueryResult(path, STATUS_OK)
        return QueryResult(path, STATUS_FAIL, schema_error[:300])
    except Exception as error:
        raise ToolingError(f"{type(error).__name__} while running {path}: {str(error)[:300]}") from error
    return QueryResult(path, STATUS_OK)


async def run_queries(
    client: GraphQLClient, files: list[Path], fragments: dict[str, FragmentDefinitionNode], branch: str
) -> list[QueryResult]:
    """Validate ``files`` one after the other and return their results.

    Raises:
        ToolingError: on the first request that fails for a reason other than a GraphQL error; continuing would
            report every remaining query as broken for the same reason.
    """
    return [await validate_query(client, path, fragments, branch) for path in files]
