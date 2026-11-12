"""Derive the change request of a Proposed Change from what the PC changes.

The target is the ``ChangeRequest`` on the PC's source branch: a request created with ``member_of_groups:
[change_requests]`` starts the run (data/events/99_group_triggers.yml), and the change service re-runs it once the PC's
artifacts are computed. The run reads the PC through the request's ``proposed_change``, the artifact and data diffs of
its branch (utils/change_sources.py), the facts of the affected devices and the owners of the changed objects in one
query (change_request_facts.gql) and the requests of the other open PCs (change_request_peers.gql, on their branches),
and writes what utils/change_rules.py derives. It never writes an AUTHOR field (justification, risk_override,
risk_override_reason, requested_by) nor the external_id. A field already equal is not written and the two edges are
changed peer by peer, so a re-run with nothing new writes nothing. A PC that is no longer open is left alone.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from utils.change_rules import (
    CHANGE_REQUEST_KIND,
    OWNER_DIFF_KINDS,
    REMOVED,
    ChangeInputs,
    affected_customers,
    derive_change_request,
    device_facts,
    parse_datetime,
    prod_objects,
)
from utils.change_sources import fetch_artifact_diff, fetch_data_diff
from utils.data_cleaning import clean_data
from utils.query_flatten import load_flat_query

from ..common import CommonGenerator
from ..helpers.edges import add_edges, remove_edge

_QUERIES = Path(__file__).resolve().parents[2] / "queries" / "change"
FACTS_QUERY = _QUERIES / "change_request_facts.gql"
PEERS_QUERY = _QUERIES / "change_request_peers.gql"
OPEN_STATE = "open"
DATETIME_FIELDS = frozenset({"planned_start", "planned_end"})
_UPDATE_MUTATION = (
    "mutation ChangeRequestDerive($data: ChangeRequestUpdateInput!) { ChangeRequestUpdate(data: $data) { ok } }"
)


def utc_now() -> datetime:
    """The time of the run (a function, so a test pins it)."""
    return datetime.now(UTC)


def _id(value: Any) -> str | None:
    """The id of a cleaned relationship value (a bare id or a node dict)."""
    if isinstance(value, Mapping):
        return value.get("id")
    return value if isinstance(value, str) else None


def _ids(values: Any) -> set[str]:
    return {node_id for value in values or [] if (node_id := _id(value))}


def changed_fields(current: Mapping[str, Any], derived: Mapping[str, Any]) -> dict[str, Any]:
    """The derived fields whose value differs from the request's, as mutation values (datetimes in ISO UTC)."""
    changes: dict[str, Any] = {}
    for name, value in derived.items():
        if name in DATETIME_FIELDS:
            if parse_datetime(current.get(name)) != value:
                changes[name] = value.astimezone(UTC).isoformat() if value else None
        elif current.get(name) != value:
            changes[name] = value
    return changes


class ChangeRequestGenerator(CommonGenerator):
    """add_change_request: the derived fields and the affected devices and customers of one change request."""

    async def generate(self, data: dict[str, Any]) -> None:
        requests = clean_data(data).get(CHANGE_REQUEST_KIND) or []
        if not requests:
            self.fail(f"No {CHANGE_REQUEST_KIND} data in GraphQL response")
        request = requests[0]
        if not request.get("id"):
            self.fail("Change request missing id: cannot proceed")
        # The run writes no node of its own: the target keeps the run's group non-empty (the SDK skips the cleanup
        # of an empty group).
        self.client.group_context.related_node_ids.append(request["id"])
        pc = request.get("proposed_change") or {}
        if not pc.get("id"):
            self.fail(f"{request.get('name')}: no proposed change")
        if pc.get("state") != OPEN_STATE:
            self.logger.info(
                f"{request.get('name')}: proposed change {pc.get('name')} is {pc.get('state')}, not derived"
            )
            return
        branch = pc.get("source_branch") or self.branch_name
        if branch != self.branch_name:
            self.logger.warning(f"{request.get('name')}: runs on {self.branch_name}, its PC's branch is {branch}")

        artifacts = await fetch_artifact_diff(self.client, branch)
        nodes, counts, truncated = await fetch_data_diff(self.client, branch)
        facts = await self._facts(
            branch,
            device_ids=sorted({a.target_id for a in artifacts if a.target_id}),
            object_ids=sorted({n.uuid for n in nodes if n.kind in OWNER_DIFF_KINDS and n.status != REMOVED and n.uuid}),
        )
        devices = [device_facts(row) for row in facts.get("DcimDeviceManaged") or []]
        customers = affected_customers(facts)
        derived = derive_change_request(
            ChangeInputs(
                pc_name=str(pc.get("name") or request.get("name")),
                pc_description=pc.get("description"),
                branch=branch,
                now=utc_now(),
                artifacts=artifacts,
                diff_nodes=nodes,
                diff_counts=counts,
                diff_truncated=truncated,
                devices=devices,
                customers=sorted(customers.values()),
                prod_objects=prod_objects(facts),
                conflicts=await self._conflicts(pc["id"], branch),
                current_planned_start=parse_datetime(request.get("planned_start")),
            )
        )
        await self._write(request, derived, {d.id for d in devices}, set(customers))
        self.logger.info(
            f"{request.get('name')}: {derived['change_type']} change, risk {derived['risk']} ({derived['risk_score']}), "
            f"{len(devices)} device(s), {len(customers)} customer(s)"
        )

    async def _facts(self, branch: str, *, device_ids: list[str], object_ids: list[str]) -> dict[str, Any]:
        """The device and owner rows of change_request_facts.gql in one round trip (none when there is nothing to ask)."""
        if not device_ids and not object_ids:
            return {}
        result = await self.client.execute_graphql(
            query=load_flat_query(FACTS_QUERY),
            branch_name=branch,
            variables={
                "device_ids": device_ids,
                "object_ids": object_ids,
                "with_devices": bool(device_ids),
                "with_objects": bool(object_ids),
            },
        )
        return clean_data(result)

    async def _conflicts(self, pc_id: str, branch: str) -> dict[str, list[str]]:
        """``<pc name> (<branch>)`` -> the devices its request lists, for every other OPEN proposed change."""
        query = load_flat_query(PEERS_QUERY)
        own = clean_data(await self.client.execute_graphql(query=query, branch_name=branch))
        found: dict[str, list[str]] = {}
        for other in own.get("CoreProposedChange") or []:
            other_branch = other.get("source_branch")
            if other.get("id") == pc_id or not other_branch or other_branch == branch:
                continue
            try:
                rows = clean_data(await self.client.execute_graphql(query=query, branch_name=other_branch))
            except Exception as exc:  # noqa: BLE001 - one unreadable branch must not stop the request
                self.logger.warning(f"Proposed change {other.get('name')}: branch {other_branch} unreadable: {exc}")
                continue
            found[f"{other.get('name')} ({other_branch})"] = sorted(
                str(device.get("name"))
                for row in rows.get("ChangeRequest") or []
                if _id(row.get("proposed_change")) == other.get("id")
                for device in row.get("affected_devices") or []
                if isinstance(device, Mapping) and device.get("name")
            )
        return found

    async def _write(
        self, request: Mapping[str, Any], derived: Mapping[str, Any], device_ids: set[str], customer_ids: set[str]
    ) -> None:
        """Write the fields that changed (one mutation) and the edge peers that changed (one add, removes one by one)."""
        changes = changed_fields(request, derived)
        if changes:
            await self.client.execute_graphql(
                query=_UPDATE_MUTATION,
                variables={"data": {"id": request["id"], **{name: {"value": v} for name, v in changes.items()}}},
            )
        for relation, desired in (("affected_devices", device_ids), ("affected_customers", customer_ids)):
            current = _ids(request.get(relation))
            if missing := sorted(desired - current):
                await add_edges(self.client, node_id=request["id"], relation=relation, peer_ids=missing)
            for stale in sorted(current - desired):
                await remove_edge(self.client, node_id=request["id"], relation=relation, peer_id=stale)
