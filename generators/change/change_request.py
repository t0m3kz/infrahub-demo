"""Derive the change request of a Proposed Change from what the PC changes. The run reads the PC, its artifact and data
diffs, the artifact targets' facts and the other open PCs' requests, and writes what utils/change_rules.py and
utils/change_text.py derive. It never writes an author field nor external_id; a re-run with nothing new writes nothing,
and a PC no longer open is left alone.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from utils.change_rules import (
    CHANGE_REQUEST_KIND,
    ChangeInputs,
    affected_customers,
    device_facts,
    effective_risk,
    parse_datetime,
    prod_objects,
)
from utils.change_sources import fetch_artifact_diff, fetch_data_diff
from utils.change_text import derive_change_request, render_pc_comment
from utils.data_cleaning import clean_data
from utils.query_flatten import load_flat_query

from ..common import CommonGenerator
from ..helpers.edges import add_edges, remove_edge

_QUERIES = Path(__file__).resolve().parents[2] / "queries" / "change"
FACTS_QUERY = _QUERIES / "change_request_facts.gql"
PEERS_QUERY = _QUERIES / "change_request_peers.gql"
OPEN_STATE = "open"
DATETIME_FIELDS = frozenset({"planned_start", "planned_end"})
PC_THREAD_LABEL = "Change request"
_THREAD_QUERY = (
    'query ChangeRequestPcThread($pc: ID!) { CoreChangeThread(change__ids: [$pc], label__value: "Change request") '
    "{ edges { node { id comments { edges { node { id text { value } } } } } } } }"
)
_COMMENT_UPDATE = "mutation ChangeRequestPcComment($id: String!, $text: String!) { CoreThreadCommentUpdate(data: {id: $id, text: {value: $text}}) { ok } }"
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
    """The derived fields whose value differs from the request's, as mutation values."""
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
        # The run writes no node of its own: the target keeps the run's group non-empty (the SDK skips cleanup of an
        # empty group)
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
        diff = await fetch_data_diff(self.client, branch)
        facts = await self._facts(branch, sorted({a.target_id for a in artifacts if a.target_id}))
        devices = [device_facts(row) for row in facts.get("DcimDeviceManaged") or []]
        customers = affected_customers(facts, diff.objects)
        derived = derive_change_request(
            ChangeInputs(
                pc_name=str(pc.get("name") or request.get("name")),
                pc_description=pc.get("description"),
                branch=branch,
                now=utc_now(),
                artifacts=artifacts,
                diff_counts=diff.counts,
                only_standard=diff.only_standard,
                routing_changed=diff.routing_changed,
                objects=diff.objects,
                objects_truncated=diff.objects_truncated,
                devices=devices,
                customers=sorted(customers.values()),
                prod_objects=prod_objects(diff.objects),
                conflicts=await self._conflicts(pc["id"], branch),
                current_planned_start=parse_datetime(request.get("planned_start")),
                rows=facts,
            )
        )
        await self._write(request, derived, {d.id for d in devices}, set(customers))
        risk = effective_risk(derived["risk"], request.get("risk_override"))
        url = f"/objects/{CHANGE_REQUEST_KIND}/{request['id']}?branch={branch}"
        text = render_pc_comment(str(request.get("name")), url, derived, risk, len(devices), request.get("external_id"))
        await self._pc_comment(pc["id"], text)
        self.logger.info(
            f"{request.get('name')}: {derived['change_type']} change, risk {derived['risk']} ({derived['risk_score']}), "
            f"{len(devices)} device(s), {len(customers)} customer(s)"
        )

    async def _pc_comment(self, pc_id: str, text: str) -> None:
        """Keep the PC's "Change request" thread (the conversation tab lists threads) saying ``text``; untracked."""
        found = await self.client.execute_graphql(query=_THREAD_QUERY, variables={"pc": pc_id})
        threads = found["CoreChangeThread"]["edges"]
        if not threads:
            thread = await self.client.create(
                kind="CoreChangeThread", label=PC_THREAD_LABEL, resolved=False, change=pc_id
            )
            await thread.save(update_group_context=False)
            thread_id = thread.id
            comments: list[Any] = []
        else:
            thread_id = threads[0]["node"]["id"]
            comments = threads[0]["node"]["comments"]["edges"]
        if comments and comments[0]["node"]["text"]["value"] == text:
            return
        if comments:
            await self.client.execute_graphql(
                query=_COMMENT_UPDATE, variables={"id": comments[0]["node"]["id"], "text": text}
            )
            return
        comment = await self.client.create(kind="CoreThreadComment", text=text, thread=thread_id)
        await comment.save(update_group_context=False)

    async def _facts(self, branch: str, device_ids: list[str]) -> dict[str, Any]:
        """The device rows of change_request_facts.gql in one round trip."""
        if not device_ids:
            return {}
        result = await self.client.execute_graphql(
            query=load_flat_query(FACTS_QUERY), branch_name=branch, variables={"device_ids": device_ids}
        )
        return clean_data(result)

    async def _conflicts(self, pc_id: str, branch: str) -> dict[str, list[str]]:
        """``<pc name> (<branch>)`` -> the devices its request lists, for every other open proposed change."""
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
        """Write the changed fields (one mutation) and the changed edge peers (one add, removes one by one)."""
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
