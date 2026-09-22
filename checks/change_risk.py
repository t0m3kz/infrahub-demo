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
* `client.reachable_nodes()` takes no relationship filter, so it is used only to
  *shortlist*: unrestricted reachability is a superset of reachability over
  `TRAVERSAL_EDGES`, one call per changed object, and anything it does not
  return needs no further thought.
* `client.traverse_paths()` does take a relationship filter, so it decides each
  shortlisted candidate exactly. That is one call per (source, candidate) pair,
  which is why the pair count is budgeted and a cut budget is reported as
  unknown impact rather than quietly dropped.
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
    shortlist_from_reachable,
)

#: Changed objects to traverse from. A diff larger than this is a bulk import or
#: a schema migration, not a change whose blast radius is worth walking one node
#: at a time; the excess is reported rather than walked.
MAX_SOURCES = 25
#: Traversals in flight. Each is a graph query on a shared repository worker.
MAX_CONCURRENCY = 8
#: (source, candidate) pairs the confirming pass may spend, across both branches.
#: Reached only by a change whose shortlist is enormous, and reported when it is.
MAX_PAIRS = 600


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

        # Shortlisted candidates are named before anything is confirmed, because
        # the confirming pass needs to know which instances belong to which
        # component in order to decide redundancy.
        candidates = {node_id for shortlist in before_lists + after_lists for node_id in shortlist.candidates}
        payloads = await self._exposure(candidates | set(before.sources) | set(after.sources), branch)
        payloads += await self._exposure(candidates | set(before.sources) | set(after.sources), base)
        instances = {
            component["id"]: list(component.get("instances") or [])
            for component in index_exposure(payloads).components.values()
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

        # Whatever the confirmed paths turned up that the first pass did not
        # name: the circuits, accounts and customers along the way.
        seen = set(before.nodes) | set(after.nodes) | set(before.sources) | set(after.sources)
        payloads += await self._exposure(seen - candidates, branch)

        return build_report(
            changes=sources,
            before=before,
            after=after,
            exposure=index_exposure(payloads),
            unresolved_edges=unresolved,
        )

    # -- pass 1: shortlist -------------------------------------------------

    async def _shortlist(self, sources: list[ChangedNode], branch: str) -> list[Shortlist]:
        """One `reachable_nodes` call per source, unfiltered, on one branch."""
        semaphore = asyncio.Semaphore(MAX_CONCURRENCY)

        async def one(source: ChangedNode) -> Shortlist:
            async with semaphore:
                payload = await self._reachable(source.id, branch)
            return shortlist_from_reachable(source, payload, max_results=MAX_RESULTS)

        return list(await asyncio.gather(*(one(source) for source in sources)))

    async def _reachable(self, source_id: str, branch: str) -> dict[str, Any] | None:
        """Shortlist one source, or None when it does not exist on this branch."""
        # `target_kinds` is declared as `list[str | type[SchemaType]]`, and `list`
        # is invariant, so a plain `list[str]` is not assignable to it.
        target_kinds: list[Any] = list(SHORTLIST_KINDS)
        try:
            result = await self.client.reachable_nodes(
                source=source_id,
                target_kinds=target_kinds,
                max_depth=MAX_DEPTH,
                max_results=MAX_RESULTS,
                branch=branch,
            )
        except VersionNotSupportedError:
            raise
        except SdkError:
            # The usual cause is the node not existing on this branch, which is
            # a result rather than a failure. A genuine transport error would
            # also land here; the missing-source finding names it either way.
            return None
        return result.model_dump()

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
                payload = await self._paths(source_id, destination_id, branch, relationship_filter)
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
    ) -> dict[str, Any] | None:
        """Shortest path from source to destination over the curated edges."""
        try:
            result = await self.client.traverse_paths(
                source=source_id,
                destination=destination_id,
                max_depth=MAX_DEPTH,
                max_paths=1,
                relationship_filter=relationship_filter,
                shortest_paths_only=True,
                branch=branch,
            )
        except VersionNotSupportedError:
            raise
        except SdkError:
            return None
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
