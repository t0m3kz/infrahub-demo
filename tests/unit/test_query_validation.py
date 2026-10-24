"""Unit tests for `invoke validate-queries` and its helper `utils/query_validation.py`."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from graphql import FragmentDefinitionNode, parse
from infrahub_sdk.exceptions import GraphQLError, ServerNotReachableError
from invoke import Context, Exit

import tasks
from utils import query_validation as qv
from utils.query_validation import QueryResult, ToolingError

FRAGMENTS_SOURCE = """
fragment Outer on DcimDevice {
  name { value }
  ...Inner
}

fragment Inner on DcimDevice {
  role { value }
  ...Leaf
}

fragment Leaf on DcimDevice {
  status { value }
}

fragment Unrelated on DcimDevice {
  description { value }
}
"""


def _fragments() -> dict[str, FragmentDefinitionNode]:
    """Return the fragment library used by the tests."""
    return {d.name.value: d for d in parse(FRAGMENTS_SOURCE).definitions if isinstance(d, FragmentDefinitionNode)}


def _defined(query: str) -> list[str]:
    """Return the fragment names defined in `query`, in order, duplicates kept."""
    return [d.name.value for d in parse(query).definitions if isinstance(d, FragmentDefinitionNode)]


class FakeClient:
    """Stand-in for InfrahubClient: records calls and answers from a script."""

    def __init__(self, outcome: Any = None) -> None:
        """Store the outcome: an exception to raise, a callable taking the query, or None for success."""
        self.outcome = outcome
        self.calls: list[dict[str, Any]] = []

    async def execute_graphql(
        self, query: str, variables: dict | None = None, branch_name: str | None = None
    ) -> dict[str, Any]:
        """Record the call, then raise or return as scripted."""
        self.calls.append({"query": query, "variables": variables, "branch_name": branch_name})
        outcome = self.outcome(query) if callable(self.outcome) else self.outcome
        if isinstance(outcome, Exception):
            raise outcome
        return {}


def _graphql_error(*messages: str) -> GraphQLError:
    """Build the SDK error the server's error list turns into."""
    return GraphQLError(errors=[{"message": message} for message in messages])


# ---------------------------------------------------------------------------
# Fragments
# ---------------------------------------------------------------------------


def test_append_fragments_without_spreads_returns_query_unchanged() -> None:
    """A query that spreads nothing is returned as written."""
    query = "query { DcimDevice { count } }"
    assert qv.append_fragments(query, _fragments()) == query


def test_append_fragments_appends_only_the_missing_ones() -> None:
    """Only the spread fragment is appended, not the whole library."""
    query = "query { DcimDevice { edges { node { ...Leaf } } } }"
    result = qv.append_fragments(query, _fragments())
    assert _defined(result) == ["Leaf"]
    assert result.startswith(query)


def test_append_fragments_follows_nested_spreads() -> None:
    """A fragment spreading another fragment pulls it in, transitively."""
    query = "query { DcimDevice { edges { node { ...Outer } } } }"
    result = qv.append_fragments(query, _fragments())
    assert sorted(_defined(result)) == ["Inner", "Leaf", "Outer"]
    assert "Unrelated" not in result


def test_append_fragments_does_not_duplicate_fragments_the_query_defines() -> None:
    """A fragment defined in the file itself is neither replaced nor appended again."""
    query = "query { DcimDevice { edges { node { ...Inner } } } }\nfragment Inner on DcimDevice { name { value } }\n"
    result = qv.append_fragments(query, _fragments())
    assert _defined(result).count("Inner") == 1
    assert "Leaf" not in _defined(result)
    parse(result)


def test_append_fragments_spread_twice_is_appended_once() -> None:
    """Two spreads of one fragment, directly and through another, give one definition."""
    query = "query { A: DcimDevice { ...Leaf } B: DcimDevice { ...Inner } }"
    result = qv.append_fragments(query, _fragments())
    assert sorted(_defined(result)) == ["Inner", "Leaf"]


def test_append_fragments_leaves_unknown_spread_for_the_server_to_report() -> None:
    """A spread no library fragment answers is not invented; the result still parses."""
    query = "query { DcimDevice { ...Nowhere } }"
    assert qv.append_fragments(query, _fragments()) == query


def test_append_fragments_survives_circular_library_fragments() -> None:
    """Fragments spreading each other terminate."""
    library = {
        d.name.value: d
        for d in parse("fragment A on X { ...B } fragment B on X { ...A }").definitions
        if isinstance(d, FragmentDefinitionNode)
    }
    result = qv.append_fragments("query { X { ...A } }", library)
    assert sorted(_defined(result)) == ["A", "B"]


# ---------------------------------------------------------------------------
# Variables
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("declaration", "expected"),
    [
        ("String", "x"),
        ("String!", "x"),
        ("ID", "x"),
        ("ID!", "x"),
        ("Int", 0),
        ("Int!", 0),
        ("Float", 0),
        ("Boolean", False),
        ("Boolean!", False),
        ("[String]", ["x"]),
        ("[String!]!", ["x"]),
        ("[Int!]", [0]),
        ("[[Boolean]]", [[False]]),
        ("SomeInputObject", {}),
        ("[SomeInputObject!]", [{}]),
    ],
)
def test_fill_variables_by_type(declaration: str, expected: Any) -> None:
    """Each declared type gets its dummy value, lists wrap the element dummy."""
    assert qv.fill_variables(f"query Q($v: {declaration}) {{ DcimDevice(name__value: $v) {{ count }} }}") == {
        "v": expected
    }


def test_fill_variables_handles_several_variables_and_none() -> None:
    """All variables are filled; a query without any gives an empty mapping."""
    query = "query Q($name: String!, $limit: Int, $flag: Boolean, $names: [String]) { DcimDevice { count } }"
    assert qv.fill_variables(query) == {"name": "x", "limit": 0, "flag": False, "names": ["x"]}
    assert qv.fill_variables("query { DcimDevice { count } }") == {}


def test_fill_variables_ignores_a_variable_name_inside_a_string() -> None:
    """Variables come from the parsed operation, not from a regex over the text."""
    query = 'query Q($a: String) { DcimDevice(description__value: "$b: Int") { count } }'
    assert qv.fill_variables(query) == {"a": "x"}


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        'Cannot query field "foo" on type "DcimDevice".',
        'Unknown type "Nope".',
        'Unknown argument "bar" on field "Query.DcimDevice".',
        'Unknown fragment "Missing".',
        'Fragment "F" cannot be spread here as objects of type "A" can never be of type "B".',
        'Unknown directive "@foo".',
        'Field "DcimDevice" argument "name" of type "String!" is required, but it was not provided.',
        "Syntax Error: Expected Name, found <EOF>.",
        'Field "role" of type "TextAttribute" must have a selection of subfields.',
    ],
)
def test_is_schema_error_keeps_validation_messages(message: str) -> None:
    """GraphQL validation messages are real schema errors."""
    assert qv.is_schema_error(message)


@pytest.mark.parametrize(
    "message",
    [
        "Unable to find the node 'x' in the database.",
        "Unable to find node x",
        "You do not have the permission to perform this action",
        "Permission denied",
        'Value error: "x" is not a valid IP address',
        "Expected value of type Int, found {}",
        "Node not found",
        "",
    ],
)
def test_is_schema_error_ignores_runtime_and_data_errors(message: str) -> None:
    """Runtime errors caused by dummy variables are not schema errors."""
    assert not qv.is_schema_error(message)


def test_first_schema_error_skips_runtime_noise() -> None:
    """The first schema error wins even after runtime errors; none gives None."""
    assert qv.first_schema_error(["Unable to find the node", 'Cannot query field "a"', 'Unknown type "B"']) == (
        'Cannot query field "a"'
    )
    assert qv.first_schema_error(["Unable to find the node", "Permission denied"]) is None
    assert qv.first_schema_error([]) is None


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def _write(tmp_path: Path, name: str, text: str) -> Path:
    """Write a query file under tmp_path and return its path."""
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


async def test_run_queries_sends_query_variables_and_branch(tmp_path: Path) -> None:
    """The client gets the query with its fragments, dummy variables and the requested branch."""
    path = _write(tmp_path, "q.gql", "query Q($name: String!) { DcimDevice(name__value: $name) { ...Leaf } }")
    client = FakeClient()
    results = await qv.run_queries(client, [path], _fragments(), "feature")
    assert results == [QueryResult(path, qv.STATUS_OK)]
    (call,) = client.calls
    assert call["variables"] == {"name": "x"}
    assert call["branch_name"] == "feature"
    assert _defined(call["query"]) == ["Leaf"]


async def test_run_queries_without_variables_sends_none(tmp_path: Path) -> None:
    """A query without variables passes no variables mapping."""
    path = _write(tmp_path, "q.gql", "query { DcimDevice { count } }")
    client = FakeClient()
    await qv.run_queries(client, [path], {}, "main")
    assert client.calls[0]["variables"] is None


async def test_run_queries_reports_a_schema_error_with_its_message(tmp_path: Path) -> None:
    """A validation error fails the file and carries the first schema message."""
    path = _write(tmp_path, "bad.gql", "query { DcimDevice { nope } }")
    client = FakeClient(_graphql_error("Unable to find the node", 'Cannot query field "nope" on type "X".'))
    (result,) = await qv.run_queries(client, [path], {}, "main")
    assert result.status == qv.STATUS_FAIL
    assert result.message == 'Cannot query field "nope" on type "X".'


@pytest.mark.parametrize(
    "message",
    [
        "Unable to find the node 'x' in the database.",
        "You do not have the permission to perform this action",
        'Value error: "x" is not valid',
    ],
)
async def test_run_queries_ignores_runtime_errors(tmp_path: Path, message: str) -> None:
    """Runtime and data errors caused by the dummy variables do not fail the file."""
    path = _write(tmp_path, "q.gql", "query Q($n: String) { DcimDevice(name__value: $n) { count } }")
    (result,) = await qv.run_queries(FakeClient(_graphql_error(message)), [path], {}, "main")
    assert result.status == qv.STATUS_OK


@pytest.mark.parametrize(
    "error",
    [ConnectionRefusedError("refused"), ServerNotReachableError("http://localhost:8100"), RuntimeError("boom")],
)
async def test_run_queries_connection_error_is_a_tooling_error(tmp_path: Path, error: Exception) -> None:
    """Anything that is not a GraphQL error aborts the run as a tooling error."""
    path = _write(tmp_path, "q.gql", "query { DcimDevice { count } }")
    with pytest.raises(ToolingError, match=r"q\.gql"):
        await qv.run_queries(FakeClient(error), [path], {}, "main")


async def test_run_queries_stops_at_the_first_tooling_error(tmp_path: Path) -> None:
    """With the server down, the remaining files are not tried."""
    paths = [_write(tmp_path, f"{n}.gql", "query { DcimDevice { count } }") for n in ("a", "b", "c")]
    client = FakeClient(ConnectionRefusedError("refused"))
    with pytest.raises(ToolingError):
        await qv.run_queries(client, paths, {}, "main")
    assert len(client.calls) == 1


async def test_run_queries_skips_fragment_only_files_without_calling_the_server(tmp_path: Path) -> None:
    """A fragment library file is not executed."""
    path = _write(tmp_path, "frag.gql", "fragment F on DcimDevice { name { value } }")
    client = FakeClient()
    (result,) = await qv.run_queries(client, [path], {}, "main")
    assert result.status == qv.STATUS_SKIP
    assert client.calls == []


async def test_run_queries_reports_a_syntax_error_without_calling_the_server(tmp_path: Path) -> None:
    """A file that does not parse fails offline."""
    path = _write(tmp_path, "broken.gql", "query { DcimDevice { ")
    client = FakeClient()
    (result,) = await qv.run_queries(client, [path], {}, "main")
    assert result.status == qv.STATUS_FAIL
    assert result.message.startswith("Syntax Error")
    assert client.calls == []


async def test_run_queries_mixed_results_keep_file_order(tmp_path: Path) -> None:
    """Each file gets its own verdict, in the order given."""
    good = _write(tmp_path, "good.gql", "query { DcimDevice { count } }")
    bad = _write(tmp_path, "bad.gql", "query { Nope { count } }")

    def outcome(query: str) -> Exception | None:
        return _graphql_error('Cannot query field "Nope" on type "Query".') if "Nope" in query else None

    results = await qv.run_queries(FakeClient(outcome), [good, bad], {}, "main")
    assert [r.status for r in results] == [qv.STATUS_OK, qv.STATUS_FAIL]


# ---------------------------------------------------------------------------
# Summary and exit code
# ---------------------------------------------------------------------------


def test_exit_code_is_zero_without_failures_and_one_with() -> None:
    """Skipped and OK files keep the exit code at 0; one failure makes it 1."""
    ok = QueryResult(Path("a.gql"), qv.STATUS_OK)
    skip = QueryResult(Path("f.gql"), qv.STATUS_SKIP)
    fail = QueryResult(Path("b.gql"), qv.STATUS_FAIL, "Cannot query field")
    assert qv.exit_code([ok, skip]) == qv.EXIT_OK == 0
    assert qv.exit_code([ok, fail]) == qv.EXIT_QUERY_ERRORS == 1
    assert qv.exit_code([]) == 0


def test_format_result_and_summary(tmp_path: Path) -> None:
    """Lines read `OK file` / `FAIL file -> message` and the summary counts each status."""
    ok = QueryResult(tmp_path / "queries" / "a.gql", qv.STATUS_OK)
    fail = QueryResult(tmp_path / "queries" / "b.gql", qv.STATUS_FAIL, 'Cannot query field "x"')
    skip = QueryResult(tmp_path / "queries" / "f.gql", qv.STATUS_SKIP)
    assert qv.format_result(ok, tmp_path) == "OK   queries/a.gql"
    assert qv.format_result(fail, tmp_path) == 'FAIL queries/b.gql -> Cannot query field "x"'
    assert qv.summarize([ok, fail, skip]) == "3 files: 1 ok, 1 with schema errors, 1 fragment-only skipped"


# ---------------------------------------------------------------------------
# Task wiring
# ---------------------------------------------------------------------------


def test_validate_queries_task_is_registered_with_defaults() -> None:
    """The task exists at the root, with the testcontainers defaults."""
    assert "validate-queries" in tasks.ns.task_names
    defaults = {arg.name: arg.default for arg in tasks.validate_queries.get_arguments()}
    assert defaults == {
        "address": "http://localhost:8100",
        "token": "06438eb2-8019-4776-878c-0941b1f1d1ec",
        "branch": "main",
    }


def test_validate_queries_task_exits_2_on_tooling_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A tooling error becomes a distinct, non-zero exit."""

    async def boom(*_: Any, **__: Any) -> list[QueryResult]:
        raise ToolingError("ConnectionRefusedError while running x.gql")

    monkeypatch.setattr(tasks, "run_queries", boom)
    with pytest.raises(Exit) as excinfo:
        tasks.validate_queries(Context())
    assert excinfo.value.code == 2
    assert "TOOLING ERROR" in str(excinfo.value.message)


def test_validate_queries_task_exits_1_on_schema_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A schema error in any query makes the task exit 1, and none exits cleanly."""
    failing = [QueryResult(Path("queries/b.gql"), qv.STATUS_FAIL, "Cannot query field")]

    async def fake_failing(*_: Any, **__: Any) -> list[QueryResult]:
        return failing

    async def fake_clean(*_: Any, **__: Any) -> list[QueryResult]:
        return [QueryResult(Path("queries/a.gql"), qv.STATUS_OK)]

    monkeypatch.setattr(tasks, "run_queries", fake_failing)
    with pytest.raises(Exit) as excinfo:
        tasks.validate_queries(Context())
    assert excinfo.value.code == 1

    monkeypatch.setattr(tasks, "run_queries", fake_clean)
    tasks.validate_queries(Context())


# ---------------------------------------------------------------------------
# The repository's own queries, offline
# ---------------------------------------------------------------------------


def test_collect_queries_finds_the_repository_queries(root_dir: Any) -> None:
    """The collector sees queries/ and tests/ and finds a plausible number of files."""
    files = qv.collect_queries(Path(root_dir))
    assert len(files) >= 70
    assert any("queries/fragments" in str(p) for p in files)
    assert any("tests/integration/queries" in str(p) for p in files)
    assert files == sorted(files)


def test_every_repository_query_parses_and_resolves_its_fragments(root_dir: Any) -> None:
    """Each .gql parses, and each query has every spread answered by itself or by queries/fragments."""
    root = Path(root_dir)
    fragments = qv.load_fragments(root / qv.FRAGMENTS_DIR)
    assert fragments
    for path in qv.collect_queries(root):
        text = path.read_text()
        document = parse(text)
        if qv.is_fragment_only(document):
            assert qv.FRAGMENTS_DIR.as_posix() in path.as_posix(), f"{path} has no operation"
            continue
        completed = parse(qv.append_fragments(text, fragments))
        defined = {d.name.value for d in completed.definitions if isinstance(d, FragmentDefinitionNode)}
        spread = qv._spread_names(completed)
        assert spread <= defined, f"{path} spreads undefined fragments {sorted(spread - defined)}"
        assert isinstance(qv.fill_variables(text), dict)
