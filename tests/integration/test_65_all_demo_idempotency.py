"""Integration test — re-running the 30_all security generators changes nothing.

Every generator here tracks what it writes and deletes what a run no longer
produces (delete_unused_nodes=True), so a run that misses an object it owns
deletes it. This module snapshots what the security path produced on the
branch test_59 built, runs those generators twice more by hand over every
target, and asserts the snapshot is unchanged: no object lost, none added,
no rule renumbered, no relationship dropped.

It runs last because the reruns rewrite the branch the other modules read.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from infrahub_sdk import InfrahubClient

from .conftest import TestInfrahubDockerWithClient
from .test_constants import ALL_DEMO_BRANCH
from .workflow_helpers import run_generator, verify_no_failed_tasks, wait_for_tasks_completion

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

SCENARIO_NAME = "Scenario 30_all: Generator Idempotency"
RERUNS = 2

# Generator definition -> the group it targets (.infrahub.yml). These write
# the security path: rules and policies (applications), segment policy
# attachments (segments), serving firewall contexts (deployments).
RERUN_GENERATORS = {
    "add_app_application": "app_applications",
    "add_vxlan_segment": "vxlan_segments",
    "add_customer_deployment_dc": "customer_deployments_dc",
    "add_customer_deployment_colocation": "customer_deployments_colocation",
}

GROUP_MEMBERS_QUERY = """
query ($name: String!) {
  CoreGroup(name__value: $name) { edges { node { members { edges { node { id } } } } } }
}
"""

SNAPSHOT_QUERY = """
query {
  SecurityPolicy { edges { node { id name { value } } } }
  SecurityPolicyRule {
    edges {
      node {
        id
        name { value }
        index { value }
        disabled { value }
        policy { node { id } }
        source_segment { node { id } }
        destination_segment { node { id } }
        security_profile { node { id } }
      }
    }
  }
  SecurityTagRule { edges { node { id } } }
  CloudSecurityGroupRule { edges { node { id name { value } } } }
  ProxyPolicyRule { edges { node { id name { value } } } }
  ManagedNetworkSegment {
    edges {
      node {
        id
        isolation_mode { value }
        security_policies { edges { node { id } } }
        inbound_rules { edges { node { id } } }
      }
    }
  }
  ManagedFirewallContext { edges { node { id served_deployments { edges { node { id } } } } } }
}
"""


def _value(node: dict[str, Any], attribute: str) -> Any:
    return (node.get(attribute) or {}).get("value")


def _peer_id(node: dict[str, Any], relationship: str) -> str | None:
    return ((node.get(relationship) or {}).get("node") or {}).get("id")


def _peer_ids(node: dict[str, Any], relationship: str) -> tuple[str, ...]:
    return tuple(sorted(edge["node"]["id"] for edge in (node.get(relationship) or {}).get("edges", [])))


def _nodes(result: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return [edge["node"] for edge in result[kind]["edges"]]


def _snapshot(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Kind -> {id: comparable state}, so a diff names the object that moved."""
    return {
        "SecurityPolicy": {n["id"]: _value(n, "name") for n in _nodes(result, "SecurityPolicy")},
        "SecurityPolicyRule": {
            n["id"]: (
                _value(n, "name"),
                _value(n, "index"),
                _value(n, "disabled"),
                _peer_id(n, "policy"),
                _peer_id(n, "source_segment"),
                _peer_id(n, "destination_segment"),
                _peer_id(n, "security_profile"),
            )
            for n in _nodes(result, "SecurityPolicyRule")
        },
        "SecurityTagRule": {n["id"]: None for n in _nodes(result, "SecurityTagRule")},
        "CloudSecurityGroupRule": {n["id"]: _value(n, "name") for n in _nodes(result, "CloudSecurityGroupRule")},
        "ProxyPolicyRule": {n["id"]: _value(n, "name") for n in _nodes(result, "ProxyPolicyRule")},
        "ManagedNetworkSegment": {
            n["id"]: (
                _value(n, "isolation_mode"),
                _peer_ids(n, "security_policies"),
                _peer_ids(n, "inbound_rules"),
            )
            for n in _nodes(result, "ManagedNetworkSegment")
        },
        "ManagedFirewallContext": {
            n["id"]: _peer_ids(n, "served_deployments") for n in _nodes(result, "ManagedFirewallContext")
        },
    }


def _diff(before: dict[str, dict[str, Any]], after: dict[str, dict[str, Any]]) -> list[str]:
    changes: list[str] = []
    for kind, old in before.items():
        new = after[kind]
        changes += [f"{kind} {node_id} deleted: {old[node_id]}" for node_id in sorted(old.keys() - new.keys())]
        changes += [f"{kind} {node_id} added: {new[node_id]}" for node_id in sorted(new.keys() - old.keys())]
        changes += [
            f"{kind} {node_id} changed: {old[node_id]} -> {new[node_id]}"
            for node_id in sorted(old.keys() & new.keys())
            if old[node_id] != new[node_id]
        ]
    return changes


class TestAllDemoIdempotency(TestInfrahubDockerWithClient):
    """Re-running the security generators leaves the 30_all branch unchanged."""

    @pytest.fixture(scope="class")
    def scenario_branch(self) -> str:
        return ALL_DEMO_BRANCH

    async def _take_snapshot(self, client: InfrahubClient, branch: str) -> dict[str, dict[str, Any]]:
        result = await client.execute_graphql(query=SNAPSHOT_QUERY, branch_name=branch)
        return _snapshot(result)

    @pytest.mark.order(410)
    @pytest.mark.dependency(
        scope="session", name="all_demo_idempotent_reruns", depends=["app_catalogue_every_dependency_enforced"]
    )
    @pytest.mark.asyncio
    async def test_01_reruns_change_nothing(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
        workflow_state: dict[str, Any],
    ) -> None:
        """Two more runs of every security generator over every target keep
        each object, its rule index and its relationships as they were."""
        logging.info("=== %s - Step 1: Rerun Generators ===", SCENARIO_NAME)

        client = async_client_main
        await wait_for_tasks_completion(client, scenario_branch)
        before = await self._take_snapshot(client, scenario_branch)
        workflow_state["idempotency_snapshot"] = before
        logging.info("Snapshot: %s", {kind: len(objects) for kind, objects in before.items()})
        assert before["SecurityPolicyRule"], "No SecurityPolicyRule on the branch to compare"

        targets: dict[str, list[str]] = {}
        for generator, group in RERUN_GENERATORS.items():
            result = await client.execute_graphql(
                query=GROUP_MEMBERS_QUERY, variables={"name": group}, branch_name=scenario_branch
            )
            groups = result["CoreGroup"]["edges"]
            assert groups, f"Group '{group}' not found"
            targets[generator] = list(_peer_ids(groups[0]["node"], "members"))
            assert targets[generator], f"Group '{group}' has no members to rerun {generator} over"

        failed: list[str] = []
        for attempt in range(1, RERUNS + 1):
            for generator, node_ids in targets.items():
                logging.info("Rerun %d/%d: %s over %d target(s)", attempt, RERUNS, generator, len(node_ids))
                outcome = await run_generator(
                    client=client, generator_name=generator, node_ids=node_ids, branch=scenario_branch
                )
                if not outcome["success"]:
                    failed.append(f"rerun {attempt} {generator}: {outcome}")
                await wait_for_tasks_completion(client, scenario_branch)

            after = await self._take_snapshot(client, scenario_branch)
            changes = _diff(before, after)
            assert not changes, f"Rerun {attempt} changed {len(changes)} object(s):\n  " + "\n  ".join(changes[:50])

        assert not failed, "Generator reruns failed:\n  " + "\n  ".join(failed)
        await verify_no_failed_tasks(client=client, branch=scenario_branch)
        logging.info("%d reruns left the branch unchanged", RERUNS)

    @pytest.mark.order(411)
    @pytest.mark.dependency(scope="session", depends=["all_demo_idempotent_reruns"])
    @pytest.mark.asyncio
    async def test_02_no_reverse_return_rules(self, workflow_state: dict[str, Any]) -> None:
        """The generator no longer writes a microsegmented "-return" rule; the
        destination leaf's ACL answers the flow from inbound_rules instead."""
        logging.info("=== %s - Step 2: No -return Rules ===", SCENARIO_NAME)

        rules = workflow_state["idempotency_snapshot"]["SecurityPolicyRule"]
        returns = sorted(state[0] for state in rules.values() if str(state[0]).endswith("-return"))
        assert not returns, f"Generated return rules still present: {returns}"

    @pytest.mark.order(412)
    @pytest.mark.dependency(scope="session", depends=["all_demo_idempotent_reruns"])
    @pytest.mark.asyncio
    async def test_03_inbound_rules_mirror_destination_segment(self, workflow_state: dict[str, Any]) -> None:
        """inbound_rules is the reverse of SecurityPolicyRule.destination_segment:
        every rule into a segment is listed on it, and nothing else is."""
        logging.info("=== %s - Step 3: inbound_rules Consistency ===", SCENARIO_NAME)

        snapshot = workflow_state["idempotency_snapshot"]
        expected: dict[str, set[str]] = {}
        for rule_id, state in snapshot["SecurityPolicyRule"].items():
            destination = state[5]
            if destination:
                expected.setdefault(destination, set()).add(rule_id)

        mismatched = [
            f"{segment_id}: listed {sorted(state[2])}, expected {sorted(expected.get(segment_id, set()))}"
            for segment_id, state in snapshot["ManagedNetworkSegment"].items()
            if set(state[2]) != expected.get(segment_id, set())
        ]
        assert expected, "No SecurityPolicyRule has a destination_segment"
        assert not mismatched, "inbound_rules out of step with destination_segment:\n  " + "\n  ".join(mismatched)
