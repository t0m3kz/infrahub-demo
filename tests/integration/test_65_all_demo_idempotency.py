"""Integration test — re-running the 30_all generators changes nothing.

Every generator here tracks what it writes and deletes what a run no longer
produces (delete_unused_nodes=True), so a run that misses an object it owns
deletes it. This module snapshots what the generators produced on the branch
test_59 built, runs them twice more by hand over every target, and asserts
the snapshot is unchanged: no object lost, none added, no rule renumbered,
no relationship dropped, no number or address reallocated.

Two passes: the security path (rules, policies, segment attachments, firewall
contexts with their sub-interfaces and P2P addresses, orchestrator routing
groups), then the topology path (devices, interfaces, cables, addressing and
pools of DCs, pods, racks, colocation metros, endpoints, MLAG domains,
circuits, LB next-hops, per-instance segment tagging and SD-WAN edges).

Each pass compares both snapshots, and the topology snapshot also covers
objects the rerun generators do not own — data-loaded devices, racks, pods,
DCs, controllers, routing and security objects, groups and pools — so a
generator whose cleanup deletes a node it merely touched is caught too.

decommission_pod is left out: deleting a pod is what it is for.

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
# the security path: rules and policies (applications, dependencies,
# components), segment policy attachments (segments), serving firewall
# contexts (deployments), orchestrator routing-group membership.
RERUN_GENERATORS = {
    "add_app_application": "app_applications",
    "add_app_dependency": "app_dependencies",
    "add_app_component": "app_components",
    "add_vxlan_segment": "vxlan_segments",
    "add_customer_deployment_dc": "customer_deployments_dc",
    "add_customer_deployment_colocation": "customer_deployments_colocation",
    "add_application_orchestrator_routing": "app_applications",
    "add_customer_dc_orchestrator_routing": "customer_deployments_dc",
    "add_customer_colocation_orchestrator_routing": "customer_deployments_colocation",
    "add_customer_cloud_orchestrator_routing": "customer_deployments_cloud",
    "add_customer_office_orchestrator_routing": "customer_deployments_office",
}
# These must have targets in 30_all; the others are skipped when their group is empty.
REQUIRED_RERUN_GENERATORS = {
    "add_app_application",
    "add_app_dependency",
    "add_vxlan_segment",
    "add_customer_deployment_dc",
    "add_customer_deployment_colocation",
}

# Generators that dispatch others without waiting (dc_pod_cascade ->
# pod_rack_cascade -> add_rack -> row-dependent add_rack; add_app_dependency
# and add_app_component -> add_app_application; add_circuit ->
# add_virtual_circuit for the overlay circuits riding it). The queue can go
# quiet between waves, so their reruns wait for a longer quiet spell before
# the next generator starts.
FAN_OUT_GENERATORS = {
    "dc_pod_cascade",
    "pod_rack_cascade",
    "add_rack",
    "add_app_dependency",
    "add_app_component",
    "add_circuit",
}
FAN_OUT_STABLE_ZERO = 10

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
  ManagedFirewallContext {
    edges {
      node {
        id
        name { value }
        vlan_id { value }
        cluster { node { id } }
        served_deployments { edges { node { id } } }
        interface_capabilities {
          edges {
            node {
              id
              name { value }
              ... on DcimVirtualInterface { ip_address { node { id address { value } } } }
            }
          }
        }
      }
    }
  }
}
"""

# Generator definition -> target group for the topology pass, in build order:
# a rack rerun reads the pod's pools, an endpoint the rack's leafs, a
# component's segment tagging the endpoint's cables.
TOPOLOGY_RERUN_GENERATORS = {
    "add_dc": "topologies_dc",
    "dc_pod_cascade": "topologies_dc",
    "add_pod": "topologies_pod",
    "pod_rack_cascade": "topologies_pod",
    "add_rack": "topologies_rack",
    "add_mlag": "mlag_domains",
    "add_colocation_metro": "colocation_metros",
    "add_circuit": "physical_circuits",
    "add_virtual_circuit": "virtual_circuits",
    "add_endpoint": "endpoints",
    "add_lb_backend_nexthop": "loadbalancer_vips",
    "add_app_component_segment": "app_components",
    "add_sdwan_edge": "customer_deployments_office",
}

# Kind -> the selection that must not change on a rerun. Generic kinds, so
# every concrete device (DcimPhysicalDevice, DcimVirtualDevice, ...),
# interface (DcimPhysicalInterface, DcimVirtualInterface, DcimLAGInterface,
# ...) and pool kind is covered; a reallocated address or number shows up as a
# changed value or a deleted-and-added id. The kinds after the generator
# outputs are mostly data-loaded or owned by another generator: an id lost
# there is a foreign deletion, i.e. a generator cleaned up a node it does not
# own. interface_capabilities carries the segment tags on switch ports.
TOPOLOGY_SNAPSHOT_KINDS = {
    "DcimDevice": "name { value }",
    "DcimInterface": "name { value } interface_capabilities { edges { node { id } } }",
    "DcimCable": "endpoints { edges { node { id } } }",
    "IpamIPAddress": "address { value }",
    "IpamPrefix": "prefix { value }",
    "CoreNumberPool": "name { value }",
    "CoreIPPrefixPool": "name { value }",
    "CoreIPAddressPool": "name { value }",
    "RoutingAutonomousSystem": "asn { value }",
    "ManagedBGP": "local_as { node { id } }",
    "ManagedHA": "group_id { value }",
    "ManagedSegmentDeployment": "vni { value } local_vni_override { value }",
    "ManagedVlanDomainSegment": "vlan_id { value }",
    "LocationRack": "name { value }",
    "TopologyPod": "name { value }",
    "TopologyDataCenter": "name { value }",
    "ManagedController": "name { value }",
    "ManagedFirewallHA": "name { value }",
    "ManagedFirewallContext": "name { value }",
    "ManagedMLAG": "name { value }",
    "ManagedStandaloneVlanDomain": "name { value }",
    "RoutingOSPFArea": "name { value } area { value }",
    "RoutingPassword": "name { value }",
    "RoutingBGPAddressFamily": "",
    "SecurityZone": "name { value }",
    "SecurityPolicy": "name { value }",
    "ProxyPolicy": "name { value }",
    "CoreStandardGroup": "name { value } members { edges { node { id } } }",
}

# Kind -> name prefixes left out of the snapshot. Resource locks are
# CoreStandardGroups that every run creates and deletes again.
SNAPSHOT_EXCLUDED_NAME_PREFIXES = {"CoreStandardGroup": ("lock-",)}

# Explicit GraphQL paging: the branch holds thousands of interfaces and
# addresses, more than one unpaged query is guaranteed to return.
PAGE_SIZE = 1000


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
            n["id"]: (
                _value(n, "name"),
                _value(n, "vlan_id"),
                _peer_id(n, "cluster"),
                _peer_ids(n, "served_deployments"),
                tuple(
                    sorted(
                        (
                            edge["node"]["id"],
                            _value(edge["node"], "name"),
                            _peer_id(edge["node"], "ip_address"),
                            ((edge["node"].get("ip_address") or {}).get("node") or {}).get("address", {}).get("value"),
                        )
                        for edge in (n.get("interface_capabilities") or {}).get("edges", [])
                    )
                ),
            )
            for n in _nodes(result, "ManagedFirewallContext")
        },
    }


def _canonical(value: Any) -> Any:
    """Hashable form of a GraphQL node: edges become a sorted tuple."""
    if isinstance(value, dict):
        if "edges" in value:
            return tuple(sorted((_canonical(edge["node"]) for edge in value["edges"]), key=repr))
        return tuple(sorted((key, _canonical(item)) for key, item in value.items()))
    return value


def _excluded(kind: str, node: dict[str, Any]) -> bool:
    prefixes = SNAPSHOT_EXCLUDED_NAME_PREFIXES.get(kind)
    return bool(prefixes) and str(_value(node, "name") or "").startswith(prefixes)


async def _topology_snapshot(client: InfrahubClient, branch: str) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """Kind -> {id: comparable state} for TOPOLOGY_SNAPSHOT_KINDS, paged, plus
    id -> "<concrete kind> <display_label>" so a diff can name what moved."""
    snapshot: dict[str, dict[str, Any]] = {}
    labels: dict[str, str] = {}
    for kind, selection in TOPOLOGY_SNAPSHOT_KINDS.items():
        objects: dict[str, Any] = {}
        offset = 0
        while True:
            query = (
                f"query {{ {kind}(limit: {PAGE_SIZE}, offset: {offset}) "
                f"{{ count edges {{ node {{ id __typename display_label {selection} }} }} }} }}"
            )
            result = (await client.execute_graphql(query=query, branch_name=branch))[kind]
            for edge in result["edges"]:
                node = dict(edge["node"])
                if _excluded(kind, node):
                    continue
                node_id = node.pop("id")
                labels[node_id] = f"{node.pop('__typename')} '{node.pop('display_label')}'"
                objects[node_id] = _canonical(node)
            offset += PAGE_SIZE
            if offset >= result["count"]:
                break
        snapshot[kind] = objects
    return snapshot, labels


def _diff(
    before: dict[str, dict[str, Any]],
    after: dict[str, dict[str, Any]],
    labels: dict[str, str] | None = None,
) -> list[str]:
    """Deleted, added and changed objects per kind; `labels` (id -> kind and
    display label, from both snapshots) names each one."""
    labels = labels or {}

    def _name(kind: str, node_id: str) -> str:
        return f"{kind} {node_id} [{labels[node_id]}]" if node_id in labels else f"{kind} {node_id}"

    changes: list[str] = []
    for kind, old in before.items():
        new = after[kind]
        changes += [f"{_name(kind, node_id)} deleted: {old[node_id]}" for node_id in sorted(old.keys() - new.keys())]
        changes += [f"{_name(kind, node_id)} added: {new[node_id]}" for node_id in sorted(new.keys() - old.keys())]
        changes += [
            f"{_name(kind, node_id)} changed: {old[node_id]} -> {new[node_id]}"
            for node_id in sorted(old.keys() & new.keys())
            if old[node_id] != new[node_id]
        ]
    return changes


async def _failed_tasks(client: InfrahubClient, branch: str) -> list[str]:
    """Failed tasks on the branch, as report lines. Catches what a run's own
    outcome cannot: generators a rerun dispatched without waiting."""
    try:
        await verify_no_failed_tasks(client=client, branch=branch)
    except AssertionError as exc:
        return [str(exc)]
    return []


def _rerun_report(label: str, failed: list[str], changes: list[str]) -> str:
    """Generator failures first: a delete the server refuses fails the run yet
    leaves the object in place, so the diff alone would hide it."""
    parts = []
    if failed:
        parts.append(f"{len(failed)} generator run(s) failed:\n  " + "\n  ".join(failed))
    if changes:
        parts.append(f"{len(changes)} object(s) changed:\n  " + "\n  ".join(changes[:50]))
    return f"{label}: " + "\n".join(parts)


class TestAllDemoIdempotency(TestInfrahubDockerWithClient):
    """Re-running the generators leaves the 30_all branch unchanged."""

    @pytest.fixture(scope="class")
    def scenario_branch(self) -> str:
        return ALL_DEMO_BRANCH

    async def _take_snapshot(self, client: InfrahubClient, branch: str) -> dict[str, dict[str, Any]]:
        result = await client.execute_graphql(query=SNAPSHOT_QUERY, branch_name=branch)
        return _snapshot(result)

    async def _group_members(self, client: InfrahubClient, group: str, branch: str) -> list[str]:
        result = await client.execute_graphql(query=GROUP_MEMBERS_QUERY, variables={"name": group}, branch_name=branch)
        groups = result["CoreGroup"]["edges"]
        assert groups, f"Group '{group}' not found"
        return list(_peer_ids(groups[0]["node"], "members"))

    async def _targets(
        self, client: InfrahubClient, branch: str, generators: dict[str, str], required: set[str]
    ) -> dict[str, list[str]]:
        """Generator -> its target group's members; `required` ones must have some."""
        targets: dict[str, list[str]] = {}
        for generator, group in generators.items():
            members = await self._group_members(client, group, branch)
            if members:
                targets[generator] = members
            else:
                assert generator not in required, f"Group '{group}' has no members to rerun {generator} over"
                logging.info("Skipping %s: group '%s' is empty", generator, group)
        assert targets, "No generator has a target to rerun over"
        return targets

    async def _rerun(
        self,
        client: InfrahubClient,
        branch: str,
        label: str,
        targets: dict[str, list[str]],
        security_before: dict[str, dict[str, Any]],
        topology_before: dict[str, dict[str, Any]],
        labels_before: dict[str, str],
    ) -> None:
        """Run every generator over its targets RERUNS times; after each pass
        assert both snapshots are unchanged and no task failed."""
        failed: list[str] = []
        for attempt in range(1, RERUNS + 1):
            for generator, node_ids in targets.items():
                logging.info("%s %d/%d: %s over %d target(s)", label, attempt, RERUNS, generator, len(node_ids))
                outcome = await run_generator(client=client, generator_name=generator, node_ids=node_ids, branch=branch)
                if not outcome["success"]:
                    failed.append(f"rerun {attempt} {generator}: {outcome}")
                if generator in FAN_OUT_GENERATORS:
                    await wait_for_tasks_completion(client, branch, stable_zero_count=FAN_OUT_STABLE_ZERO)
                else:
                    await wait_for_tasks_completion(client, branch)

            failed += await _failed_tasks(client, branch)
            topology_after, labels_after = await _topology_snapshot(client, branch)
            changes = _diff(topology_before, topology_after, {**labels_after, **labels_before})
            changes += _diff(security_before, await self._take_snapshot(client, branch))
            assert not failed and not changes, _rerun_report(f"{label} {attempt}", failed, changes)

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
        each object, its rule index and its relationships as they were, and
        delete nothing they do not own."""
        logging.info("=== %s - Step 1: Rerun Generators ===", SCENARIO_NAME)

        client = async_client_main
        await wait_for_tasks_completion(client, scenario_branch)
        before = await self._take_snapshot(client, scenario_branch)
        topology_before, labels = await _topology_snapshot(client, scenario_branch)
        workflow_state["idempotency_snapshot"] = before
        logging.info("Snapshot: %s", {kind: len(objects) for kind, objects in before.items()})
        logging.info("Topology snapshot: %s", {kind: len(objects) for kind, objects in topology_before.items()})
        assert before["SecurityPolicyRule"], "No SecurityPolicyRule on the branch to compare"

        targets = await self._targets(client, scenario_branch, RERUN_GENERATORS, REQUIRED_RERUN_GENERATORS)
        await self._rerun(client, scenario_branch, "Rerun", targets, before, topology_before, labels)
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

    @pytest.mark.order(413)
    @pytest.mark.dependency(
        scope="session", name="all_demo_idempotent_topology_reruns", depends=["all_demo_idempotent_reruns"]
    )
    @pytest.mark.asyncio
    async def test_04_topology_reruns_change_nothing(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Two more runs of every topology generator, in build order, keep each
        device, port, segment tag, cable, address, prefix, pool and allocated
        number, delete nothing they do not own, and leave the security path
        test_01 checked untouched."""
        logging.info("=== %s - Step 4: Rerun Topology Generators ===", SCENARIO_NAME)

        client = async_client_main
        await wait_for_tasks_completion(client, scenario_branch)
        before, labels = await _topology_snapshot(client, scenario_branch)
        security_before = await self._take_snapshot(client, scenario_branch)
        logging.info("Topology snapshot: %s", {kind: len(objects) for kind, objects in before.items()})
        for kind in ("DcimDevice", "DcimInterface", "DcimCable", "IpamIPAddress"):
            assert before[kind], f"No {kind} on the branch to compare"

        targets = await self._targets(client, scenario_branch, TOPOLOGY_RERUN_GENERATORS, set())
        await self._rerun(client, scenario_branch, "Topology rerun", targets, security_before, before, labels)
        logging.info("%d topology reruns left the branch unchanged", RERUNS)
        logging.info("=== %s - COMPLETED ===", SCENARIO_NAME)
