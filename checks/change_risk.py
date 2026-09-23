"""Change-risk check — blast radius and verdict for a Proposed Change.

Runs once per Proposed Change (no `targets:` in .infrahub.yml), reads the branch
diff, and walks the graph outward from every changed object using Infrahub's own
server-side traversal. See [docs/change_risk.md](../docs/change_risk.md).

The check is the I/O half; the model in [risk_model.py](risk_model.py) is pure
and holds the edge set, the severity comparison and the scoring.

Three things shape the flow:

* The counterfactual is not a simulation. The proposed branch already contains
  the change, so "what breaks" is the same traversal run on the branch and on
  its base, then compared.
* `client.traverse_paths()` is the only traversal endpoint that takes a
  relationship filter, so it is the one that decides anything. That is one call
  per (source, candidate) pair, which is why the pair count is budgeted and a
  cut budget is reported as unknown impact rather than quietly dropped.
* `client.reachable_nodes()` is not used, though it looks made for pass 1. It
  takes no relationship filter, so on demo-sized data it fans out through every
  shared container in a site and the server runs out of heap past depth 5 —
  while a colo-to-cloud path is 10 hops and the model allows 14. Pass 1 is
  therefore an enumeration of the branch's scoring targets, which are few, and
  pass 2 decides each one over the curated edges, which is cheap.
"""

from __future__ import annotations

import asyncio
from typing import Any

from infrahub_sdk.checks import InfrahubCheck
from infrahub_sdk.exceptions import Error as SdkError
from infrahub_sdk.exceptions import VersionNotSupportedError

from .risk_model import (
    BATCH_SIZE,
    CONFIRM_KINDS,
    EXPOSURE_QUERY,
    MAX_DEPTH,
    MAX_RESULTS,
    SHORTLIST_KINDS,
    ChangedNode,
    ReachSet,
    RiskReport,
    Shortlist,
    build_report,
    changes_from_diff,
    index_exposure,
    resolve_relationship_filter,
    shortlist_from_targets,
)

#: Every scoring target on the branch, id and kind only — the candidate set of
#: pass 1. Generated from `SHORTLIST_KINDS` so the two cannot drift apart.
TARGETS_QUERY = (
    "query RiskTargets {\n"
    + "\n".join(f"  {kind} {{ edges {{ node {{ id __typename }} }} }}" for kind in SHORTLIST_KINDS)
    + "\n}\n"
)

#: Which of a set of ids exist on a branch. Asked directly rather than inferred
#: from a traversal that came back empty.
PRESENT_QUERY = """
query RiskSourcesPresent($ids: [ID]) {
  CoreNode(ids: $ids) { edges { node { id } } }
}
"""

#: Changed objects to traverse from. A diff larger than this is a bulk import or
#: a schema migration, not a change whose blast radius is worth walking one node
#: at a time; the excess is reported rather than walked.
MAX_SOURCES = 25
#: Traversals in flight. Each is a graph query on a shared repository worker.
MAX_CONCURRENCY = 8
#: (source, candidate) pairs the confirming pass may spend, across both branches.
#: One pair is one filtered traversal, measured at roughly half a second on demo
#: data, so this is about a minute of wall clock at `MAX_CONCURRENCY`. A change
#: with more pairs than this is under-reported, and says so.
MAX_PAIRS = 600


def _brief(exc: Exception) -> str:
    """One readable line from an exception whose text may embed a whole query."""
    text = " ".join(str(exc).split())
    return text if len(text) <= 160 else f"{text[:160]}…"


class CheckChangeRisk(InfrahubCheck):
    """Score the blast radius of a Proposed Change and set a verdict."""

    # `InfrahubCheck` requires a non-empty query name and `collect_data()` can
    # only pass static parameters, so the declared query is the trivial anchor
    # and the real, diff-seeded work happens in validate().
    query = "placeholder"

    #: Remaining `traverse_paths` calls. Decremented without awaiting in
    #: between, so the gathered coroutines cannot race each other for it.
    _budget: int = MAX_PAIRS

    # `InfrahubCheck.run()` dispatches on `inspect.iscoroutinefunction(self.validate)`,
    # so an async override is supported at run time; the base class is simply
    # typed as sync, which is what `ty` objects to.
    async def validate(self, data: Any) -> None:  # ty: ignore[invalid-method-override]  # noqa: ARG002 - anchor query output is unused
        branch = self.branch_name
        base = self.client.default_branch
        if not branch or branch == base:
            self.log_info(f"Change risk: nothing to compare, already on '{base}'.")
            return

        changes = changes_from_diff(await self.client.get_diff_summary(branch=branch))
        if not changes:
            self.log_info(f"Change risk: branch '{branch}' has no node changes.")
            return

        relationship_filter, unresolved = await self._relationship_filter(branch)
        if not relationship_filter:
            self.log_error(
                message=(
                    "Change risk: no traversal edge resolved against the schema, so no blast radius "
                    "could be walked. Check that the topology schema is loaded on this branch."
                )
            )
            return

        sources = changes[:MAX_SOURCES]
        self._budget = MAX_PAIRS
        try:
            report = await self._assess(sources, base, branch, relationship_filter, unresolved)
        except VersionNotSupportedError:
            self.log_error(
                message=(
                    "Change risk: this Infrahub does not provide graph traversal (needs 1.10 or later), "
                    "so the blast radius of this change is unknown."
                )
            )
            return

        self._report(report, changes=changes, sources=sources)

    async def _assess(
        self,
        sources: list[ChangedNode],
        base: str,
        branch: str,
        relationship_filter: list[str],
        unresolved: list[tuple[str, str]],
    ) -> RiskReport:
        """The two passes, on both branches, folded into a report."""
        before_lists, after_lists = await asyncio.gather(
            self._shortlist(sources, base),
            self._shortlist(sources, branch),
        )
        before, after = ReachSet(), ReachSet()
        for shortlist in before_lists:
            before.note_shortlist(shortlist)
        for shortlist in after_lists:
            after.note_shortlist(shortlist)

        # Which instances belong to which component, needed *before* anything is
        # confirmed because the confirming pass decides redundancy from it. Read
        # over the whole candidate set, and deliberately not kept for the report:
        # the candidate set is every application on the branch, so scoring it
        # would report every application as impacted-with-unknown-fate.
        candidates = {node_id for shortlist in before_lists + after_lists for node_id in shortlist.candidates}
        instances = {
            component["id"]: list(component.get("instances") or [])
            for component in index_exposure(await self._exposure(candidates, branch)).components.values()
        }

        await asyncio.gather(
            self._confirm(before, before_lists, base, relationship_filter, instances),
            self._confirm(after, after_lists, branch, relationship_filter, instances),
        )

        # A source missing on one branch is the change itself — added, or
        # deleted. Measure that side from what still sits next to it, otherwise
        # everything the object touched looks like it was only ever reachable
        # through the object.
        await asyncio.gather(
            self._reseed(before, after, base, relationship_filter, instances),
            self._reseed(after, before, branch, relationship_filter, instances),
        )

        # Exposure is read for what the traversal actually confirmed — the
        # applications and segments it reached, the changed objects themselves,
        # and the circuits, accounts and customers along the way. On both
        # branches, because an object deleted on the proposed branch can only be
        # named from the base.
        seen = set(before.nodes) | set(after.nodes) | set(before.sources) | set(after.sources)
        payloads = await self._exposure(seen, branch)
        payloads += await self._exposure(seen, base)

        return build_report(
            changes=sources,
            before=before,
            after=after,
            exposure=index_exposure(payloads),
            unresolved_edges=unresolved,
        )

    # -- pass 1: candidate set ---------------------------------------------

    async def _shortlist(self, sources: list[ChangedNode], branch: str) -> list[Shortlist]:
        """Pair every source with the branch's scoring targets.

        Two cheap queries for the whole pass, and no traversal at all.
        `reachable_nodes` was the obvious way to prune this list and cannot be
        used: unfiltered reachability fans out through every shared container in
        a site, and past depth 5 the server answers `Java heap space` — where
        this model needs depth 14. Pruning is not needed anyway; there are tens
        of scoring targets, not thousands.

        Enumerated per branch on purpose: an application added on the proposed
        branch must not be searched for on the base, where its id does not
        resolve and the failure would be indistinguishable from a real one.
        """
        targets, present = await asyncio.gather(
            self._targets(branch),
            self._present([source.id for source in sources], branch),
        )
        return [
            shortlist_from_targets(source, targets, present=source.id in present, max_results=MAX_RESULTS)
            for source in sources
        ]

    async def _targets(self, branch: str) -> dict[str, str]:
        """Every application, component and segment on the branch: id -> kind."""
        response = await self.client.execute_graphql(query=TARGETS_QUERY, branch_name=branch)
        targets: dict[str, str] = {}
        for kind in SHORTLIST_KINDS:
            for edge in (response.get(kind) or {}).get("edges") or []:
                node = edge.get("node") or {}
                node_id = node.get("id")
                if isinstance(node_id, str) and node_id:
                    targets[node_id] = node.get("__typename") or kind
        return targets

    async def _present(self, source_ids: list[str], branch: str) -> set[str]:
        """The subset of these ids that exists on the branch."""
        if not source_ids:
            return set()
        present: set[str] = set()
        for start in range(0, len(source_ids), BATCH_SIZE):
            response = await self.client.execute_graphql(
                query=PRESENT_QUERY,
                variables={"ids": source_ids[start : start + BATCH_SIZE]},
                branch_name=branch,
            )
            for edge in (response.get("CoreNode") or {}).get("edges") or []:
                node_id = (edge.get("node") or {}).get("id")
                if isinstance(node_id, str) and node_id:
                    present.add(node_id)
        return present

    # -- pass 2: confirm ---------------------------------------------------

    async def _confirm(
        self,
        reach: ReachSet,
        shortlists: list[Shortlist],
        branch: str,
        relationship_filter: list[str],
        instances: dict[str, list[str]],
    ) -> None:
        """Decide each shortlisted candidate over the curated edge set.

        Components and applications first; then, only for the components that
        turned out to be reachable, their instances — which is what tells a full
        outage apart from the loss of one of several paths. Checking every
        instance of every shortlisted component up front would spend most of the
        budget on components the change never reaches.
        """
        semaphore = asyncio.Semaphore(MAX_CONCURRENCY)

        async def one(source_id: str, destination_id: str) -> None:
            if not self._spend(source_id, reach):
                return
            async with semaphore:
                try:
                    payload = await self._paths(source_id, destination_id, branch, relationship_filter)
                except VersionNotSupportedError:
                    raise
                except SdkError as exc:
                    reach.note_error(source_id, _brief(exc))
                    return
            reach.absorb_paths(payload)

        await asyncio.gather(
            *(
                one(shortlist.source_id, destination_id)
                for shortlist in shortlists
                if not shortlist.missing
                for destination_id in sorted(shortlist.of_kinds(CONFIRM_KINDS))
            )
        )

        await asyncio.gather(
            *(
                one(reached.path[0], instance_id)
                for component_id, instance_ids in instances.items()
                if (reached := reach.nodes.get(component_id)) is not None and reached.path
                for instance_id in instance_ids
                if instance_id not in reach.nodes
            )
        )

    async def _reseed(
        self,
        target: ReachSet,
        other: ReachSet,
        branch: str,
        relationship_filter: list[str],
        instances: dict[str, list[str]],
    ) -> None:
        """Re-run both passes from the neighbours a missing source had elsewhere."""
        ring: set[str] = set()
        for source_id in target.missing_sources:
            ring |= other.ring(source_id)
        ring -= set(target.sources) | target.missing_sources
        if not ring:
            return

        neighbours = [
            ChangedNode(id=node_id, kind=other.kinds.get(node_id, ""), action="neighbour", label=node_id)
            for node_id in sorted(ring)[:MAX_SOURCES]
        ]
        shortlists = await self._shortlist(neighbours, branch)
        for shortlist in shortlists:
            target.note_shortlist(shortlist)
        await self._confirm(target, shortlists, branch, relationship_filter, instances)

    async def _paths(
        self,
        source_id: str,
        destination_id: str,
        branch: str,
        relationship_filter: list[str],
    ) -> dict[str, Any]:
        """Shortest path from source to destination over the curated edges.

        Errors are deliberately not caught here: the caller records them as an
        unknown radius, because "the server refused" and "there is no path" are
        the same empty result and opposite conclusions.
        """
        result = await self.client.traverse_paths(
            source=source_id,
            destination=destination_id,
            max_depth=MAX_DEPTH,
            max_paths=1,
            relationship_filter=relationship_filter,
            shortest_paths_only=True,
            branch=branch,
        )
        return result.model_dump()

    def _spend(self, source_id: str, reach: ReachSet) -> bool:
        """Take one pair from the budget, or mark the source under-reported."""
        if self._budget <= 0:
            reach.truncated_sources.add(source_id)
            return False
        self._budget -= 1
        return True

    # -- exposure ----------------------------------------------------------

    async def _relationship_filter(self, branch: str) -> tuple[list[str], list[tuple[str, str]]]:
        """Resolve the traversal edge set against the branch schema."""
        schema = await self.client.schema.all(branch=branch)
        identifiers, missing = resolve_relationship_filter(schema)
        return identifiers, missing

    async def _exposure(self, ids: set[str], branch: str) -> list[dict[str, Any]]:
        """Run the exposure query over an id set, in batches."""
        if not ids:
            return []
        ordered = sorted(ids)
        payloads: list[dict[str, Any]] = []
        for start in range(0, len(ordered), BATCH_SIZE):
            response = await self.client.query_gql_query(
                name=EXPOSURE_QUERY,
                variables={"ids": ordered[start : start + BATCH_SIZE]},
                branch_name=branch,
            )
            payloads.append(response.get("data") or response)
        return payloads

    # -- reporting ---------------------------------------------------------

    def _report(self, report: RiskReport, changes: list[ChangedNode], sources: list[ChangedNode]) -> None:
        """Turn the report into check log lines.

        `log_error` is what makes the Proposed Change fail, so it carries the
        verdict and nothing else; every other finding is informational however
        interesting it is.
        """
        kinds = ", ".join(f"{kind} x{count}" for kind, count in sorted(report.kinds.items())) or "nothing"
        self.log_info(
            message=(
                f"Change risk {report.verdict}: score {report.score:.1f}, confidence "
                f"{report.confidence:.0%}, {report.impacted} impacted object(s) ({kinds}) "
                f"from {len(changes)} changed object(s)."
            )
        )

        if len(changes) > len(sources):
            self.log_info(
                message=(
                    f"Only the first {len(sources)} of {len(changes)} changed objects were traversed; "
                    "the blast radius of the rest is unknown."
                )
            )

        for app in report.applications:
            self.log_info(
                message=(
                    f"  {app.severity:8s} {app.criticality or 'unrated':8s} '{app.name}' "
                    f"({app.environment or 'no environment'}) — components: "
                    f"{', '.join(app.components) or 'none resolved'}"
                ),
                object_id=app.id,
                object_type="AppApplication",
            )

        for finding in report.findings:
            log = self.log_error if finding.level == "error" else self.log_info
            log(
                message=f"[{finding.code}] {finding.message}",
                object_id=finding.object_id,
                object_type=finding.object_kind,
            )
