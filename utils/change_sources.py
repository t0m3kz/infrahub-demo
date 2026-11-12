"""Read what a Proposed Change changes: its artifact diff (REST) and its data diff (GraphQL ``DiffTree``).

The one reader for add_change_request (generators/change/change_request.py) and checks/change_request.py, so the
fingerprint the generator writes and the one the check recomputes come from the same call. Raises on a failed read
(a layer below the generator or check, which decides what the run does); the parsing is utils/change_rules.py.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import quote

from utils.change_rules import ArtifactChange, DiffNode, parse_artifact_diff, parse_diff_nodes
from utils.query_flatten import load_flat_query

ARTIFACT_DIFF_PATH = "/api/diff/artifacts"
DIFF_PAGE_SIZE = 500
DIFF_MAX_NODES = 5000
"""The data diff is read in pages up to this many changed nodes: enough to describe a change, and a bulk load (the
full topology sample is ~90k nodes) is described by the server's counts instead."""

_DIFF_QUERY = Path(__file__).resolve().parents[1] / "queries" / "change" / "change_request_diff.gql"
_SUMMARY_KEYS = {"num_added": "ADDED", "num_updated": "UPDATED", "num_removed": "REMOVED"}


async def fetch_artifact_diff(client: Any, branch: str) -> list[ArtifactChange]:
    """The artifact diff of ``branch`` (``GET /api/diff/artifacts?branch=``), through the SDK's authenticated GET."""
    response = await client._get(url=f"{client.address}{ARTIFACT_DIFF_PATH}?branch={quote(branch, safe='')}")
    response.raise_for_status()
    return parse_artifact_diff(response.json())


async def fetch_data_diff(client: Any, branch: str) -> tuple[list[DiffNode], dict[str, int], bool]:
    """(changed nodes, the server's counts by status, whether the read stopped at DIFF_MAX_NODES)."""
    query = load_flat_query(_DIFF_QUERY)
    raw: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    offset = 0
    while True:
        result = await client.execute_graphql(
            query=query,
            branch_name=branch,
            variables={"branch": branch, "limit": DIFF_PAGE_SIZE, "offset": offset, "with_summary": offset == 0},
        )
        if offset == 0:
            summary = result.get("DiffTreeSummary") or {}
            counts = {status: int(summary.get(key) or 0) for key, status in _SUMMARY_KEYS.items()}
        page = (result.get("DiffTree") or {}).get("nodes") or []
        raw.extend(page)
        offset += DIFF_PAGE_SIZE
        if len(page) < DIFF_PAGE_SIZE:
            return parse_diff_nodes(raw), counts, False
        if offset >= DIFF_MAX_NODES:
            return parse_diff_nodes(raw), counts, True
