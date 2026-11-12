"""Read what a Proposed Change changes: its artifact diff (REST) and its data diff (GraphQL ``DiffTree``).

Raises on a failed read; parsing is in utils/change_rules.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from utils.change_facts import OBJECT_DIFF_KINDS
from utils.change_rules import (
    BOOKKEEPING_KINDS,
    ROUTING_KINDS,
    STANDARD_KINDS,
    ArtifactChange,
    ObjectDiff,
    parse_artifact_diff,
    parse_diff_summaries,
    parse_object_nodes,
)
from utils.query_flatten import load_flat_query

ARTIFACT_DIFF_PATH = "/api/diff/artifacts"
OBJECT_PAGE_SIZE = 100
OBJECT_MAX = 1000
"""The objects the description names carry their values: small pages, and the description lists a few of them, so the
read stops at OBJECT_MAX (the counts are the server's, whatever the number)."""

_DIFF_QUERY = Path(__file__).resolve().parents[1] / "queries" / "change" / "change_request_diff.gql"


async def fetch_artifact_diff(client: Any, branch: str) -> list[ArtifactChange]:
    """The artifact diff of ``branch`` (``GET /api/diff/artifacts``) through the SDK's authenticated GET."""
    response = await client._get(url=f"{client.address}{ARTIFACT_DIFF_PATH}?branch={quote(branch, safe='')}")
    response.raise_for_status()
    return parse_artifact_diff(response.json())


@dataclass(frozen=True)
class DataDiff:
    """What the data diff of a branch says: the server's counts and the described objects."""

    counts: dict[str, int]
    only_standard: bool
    routing_changed: bool
    objects: list[ObjectDiff]
    objects_truncated: bool


async def fetch_data_diff(client: Any, branch: str) -> DataDiff:
    """The ``DiffTreeSummary`` counts and, in pages up to OBJECT_MAX, the objects of OBJECT_DIFF_KINDS with values."""
    query = load_flat_query(_DIFF_QUERY)
    raw: list[dict[str, Any]] = []
    summaries: dict[str, Any] = {}
    offset = 0
    while True:
        result = await client.execute_graphql(
            query=query,
            branch_name=branch,
            variables={
                "branch": branch,
                "limit": OBJECT_PAGE_SIZE,
                "offset": offset,
                "kinds": sorted(OBJECT_DIFF_KINDS),
                "bookkeeping": sorted(BOOKKEEPING_KINDS),
                "non_standard": sorted(STANDARD_KINDS | BOOKKEEPING_KINDS),
                "routing": sorted(ROUTING_KINDS),
                "with_summary": offset == 0,
            },
        )
        if offset == 0:
            summaries = result
        page = (result.get("objects") or {}).get("nodes") or []
        raw.extend(page)
        offset += OBJECT_PAGE_SIZE
        if len(page) < OBJECT_PAGE_SIZE or offset >= OBJECT_MAX:
            counts, only_standard, routing = parse_diff_summaries(summaries)
            return DataDiff(counts, only_standard, routing, parse_object_nodes(raw), len(page) == OBJECT_PAGE_SIZE)
