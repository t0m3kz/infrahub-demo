"""Integration test — re-running the 30_all generators changes nothing.

Every generator here tracks what it writes and deletes what a run no longer
produces (delete_unused_nodes=True), so a run that misses an object it owns
deletes it. This module snapshots what the generators produced on the branch
test_59 built, runs them once more by hand over every target, and asserts
the snapshot is unchanged: no object lost, none added, no rule renumbered,
no relationship dropped, no number or address reallocated.

Two passes: the security path (rules, policies, segment attachments, firewall
contexts with their sub-interfaces and P2P addresses, orchestrator routing
groups), then the topology path (devices, interfaces, cables, addressing and
pools of DCs, pods, racks, colocation metros, endpoints, MLAG domains,
circuits, LB next-hops, per-instance segment tagging, SD-WAN edges and office fabrics).

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
from .test_constants import (
    ALL_DEMO_BRANCH,
    ALL_DEMO_FABRIC_PEERINGS,
    ALL_DEMO_VIRTUAL_CIRCUITS,
    ALL_DEMO_ZONE_SITE_VIPS,
    HANDOFF_KIND,
    INTERCONNECT_KIND,
)
from .test_helpers import diff_snapshots, snapshot_topology
from .workflow_helpers import run_generator, verify_no_failed_tasks, wait_for_tasks_completion

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

SCENARIO_NAME = "Scenario 30_all: Generator Idempotency"
# One rerun is enough to expose a generator that deletes or re-creates what it owns; a second pass only
# catches A -> B -> A flapping, which no history in this repo shows, and costs ~10 minutes.
RERUNS = 1

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
    "add_virtual_colocation_orchestrator_routing": "virtual_colocations",
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
# add_virtual_circuit for the Interconnect and Handoff circuits riding it). The queue can go
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
        source_security_tag { node { id } }
        destination_security_tag { node { id } }
      }
    }
  }
  # Segment and component group tags: the SGT-POOL allocation (group_id) of
  # each, and the segment or component it groups. A rerun that drew a new
  # number or a second tag is a diff.
  SecurityTag {
    edges {
      node {
        id
        name { value }
        group_id { value }
        components { edges { node { id } } }
        network_segments { edges { node { id } } }
      }
    }
  }
  AppComponent { edges { node { id security_tag { node { id } } } } }
  CloudSecurityGroup { edges { node { id name { value } } } }
  CloudSecurityGroupRule { edges { node { id name { value } } } }
  ProxyPolicyRule { edges { node { id name { value } } } }
  ManagedNetworkSegment {
    edges {
      node {
        id
        isolation_mode { value }
        security_zone { node { id } }
        security_policy { node { id } }
        security_tag { node { id } }
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
        snat_address { node { id address { value } } }
        # The members' transit sub-interfaces and the physical service ports tagged with the
        # context (a DC's border-leaf ports, the colocation metro's edge ports).
        capabilities {
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
  TopologyRoutedExchange {
    edges {
      node {
        id
        name { value }
        namespace_a { node { id } }
        namespace_z { node { id } }
        gateway { node { id } }
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
    "add_colocation_zone": "colocation_zones",
    "add_virtual_colocation": "virtual_colocations",
    "add_circuit": "physical_circuits",
    # virtual_circuits holds the Interconnect and Handoff circuits (no Tunnel: it has no session). The rerun must
    # keep each one's /31 (/127), the addresses on its ports, its <name>-bgp or <name>-underlay/<name>-evpn
    # peerings and a DCI's per-circuit key exactly as they were: same ids, same values (snapshot_topology).
    "add_virtual_circuit": "virtual_circuits",
    "add_endpoint": "endpoints",
    "add_app_component_segment": "app_components",
    "add_sdwan_edge": "customer_deployments_office",
    "add_office_fabric": "customer_deployments_office",
}


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
                _peer_id(n, "source_security_tag"),
                _peer_id(n, "destination_security_tag"),
            )
            for n in _nodes(result, "SecurityPolicyRule")
        },
        "SecurityTag": {
            n["id"]: (
                _value(n, "name"),
                _value(n, "group_id"),
                _peer_ids(n, "components"),
                _peer_ids(n, "network_segments"),
            )
            for n in _nodes(result, "SecurityTag")
        },
        "AppComponent": {n["id"]: _peer_id(n, "security_tag") for n in _nodes(result, "AppComponent")},
        "CloudSecurityGroup": {n["id"]: _value(n, "name") for n in _nodes(result, "CloudSecurityGroup")},
        "CloudSecurityGroupRule": {n["id"]: _value(n, "name") for n in _nodes(result, "CloudSecurityGroupRule")},
        "ProxyPolicyRule": {n["id"]: _value(n, "name") for n in _nodes(result, "ProxyPolicyRule")},
        "ManagedNetworkSegment": {
            n["id"]: (
                _value(n, "isolation_mode"),
                _peer_id(n, "security_policy"),
                _peer_ids(n, "inbound_rules"),
                _peer_id(n, "security_zone"),
                _peer_id(n, "security_tag"),
            )
            for n in _nodes(result, "ManagedNetworkSegment")
        },
        "ManagedFirewallContext": {
            n["id"]: (
                _value(n, "name"),
                _value(n, "vlan_id"),
                _peer_id(n, "cluster"),
                _peer_ids(n, "served_deployments"),
                # The SNAT /32: same address object and value after every rerun.
                _peer_id(n, "snat_address"),
                ((n.get("snat_address") or {}).get("node") or {}).get("address", {}).get("value"),
                tuple(
                    sorted(
                        (
                            edge["node"]["id"],
                            _value(edge["node"], "name"),
                            _peer_id(edge["node"], "ip_address"),
                            ((edge["node"].get("ip_address") or {}).get("node") or {}).get("address", {}).get("value"),
                        )
                        for edge in (n.get("capabilities") or {}).get("edges", [])
                    )
                ),
            )
            for n in _nodes(result, "ManagedFirewallContext")
        },
        # `{context}-{A}-{Z}` per context and tenant VRF, DC and colocation metro alike.
        "TopologyRoutedExchange": {
            n["id"]: (
                _value(n, "name"),
                _peer_id(n, "namespace_a"),
                _peer_id(n, "namespace_z"),
                _peer_id(n, "gateway"),
            )
            for n in _nodes(result, "TopologyRoutedExchange")
        },
    }


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
            topology_after, labels_after = await snapshot_topology(client, branch)
            changes = diff_snapshots(topology_before, topology_after, {**labels_after, **labels_before})
            changes += diff_snapshots(security_before, await self._take_snapshot(client, branch))
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
        topology_before, labels = await snapshot_topology(client, scenario_branch)
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

    @pytest.mark.order(411)
    @pytest.mark.dependency(scope="session", depends=["all_demo_idempotent_reruns"])
    @pytest.mark.asyncio
    async def test_02b_no_generated_security_groups(self, workflow_state: dict[str, Any]) -> None:
        """A cloud flow is a SecurityPolicyRule: no sg-{app} CloudSecurityGroup is generated, and the
        ones an older run created were deleted by the application's tracking group."""
        logging.info("=== %s - Step 2b: No sg-* Security Groups ===", SCENARIO_NAME)

        groups = workflow_state["idempotency_snapshot"]["CloudSecurityGroup"]
        generated = sorted(str(name) for name in groups.values() if str(name).startswith("sg-"))
        assert not generated, f"Generated cloud security groups still present: {generated}"

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
        before, labels = await snapshot_topology(client, scenario_branch)
        security_before = await self._take_snapshot(client, scenario_branch)
        logging.info("Topology snapshot: %s", {kind: len(objects) for kind, objects in before.items()})
        for kind in ("DcimDevice", "DcimInterface", "DcimCable", "IpamIPAddress"):
            assert before[kind], f"No {kind} on the branch to compare"
        # The zone edge pairs must be in the snapshot, or a second interlink cable / FABRIC session
        # / shared-VIP address would go unnoticed (a duplicate is a new id, hence an "added" diff).
        for peering in ALL_DEMO_FABRIC_PEERINGS.values():
            held = [node_id for node_id, state in before["ManagedBGPPeering"].items() if repr(peering) in repr(state)]
            assert len(held) == 1, f"{peering} is {len(held)} ManagedBGPPeering object(s) in the snapshot, not 1"
        for vip in ALL_DEMO_ZONE_SITE_VIPS.values():
            holders = [node_id for node_id, state in before["IpamIPAddress"].items() if repr(vip) in repr(state)]
            assert len(holders) == 1, f"the shared zone VIP {vip} is {len(holders)} IpamIPAddress object(s), not 1"
        # The sessions add_virtual_circuit builds must be in the snapshot, one object each, or a duplicate session
        # (new id) or a re-created per-circuit key would go unnoticed: an Interconnect holds <name>-underlay,
        # <name>-evpn and <name>-dci-key, a Handoff <name>-bgp and no key of its own (its key is data-loaded).
        for circuit, expected in ALL_DEMO_VIRTUAL_CIRCUITS.items():
            if expected["kind"] == INTERCONNECT_KIND:
                peerings = [f"{circuit}-underlay", f"{circuit}-evpn"]
                keys = [f"{circuit}-dci-key"]
            elif expected["kind"] == HANDOFF_KIND:
                peerings, keys = [f"{circuit}-bgp"], []
            else:
                peerings, keys = [], []
            for peering in peerings:
                held = [
                    n for n, state in before["ManagedBGPPeering"].items() if repr(("value", peering)) in repr(state)
                ]
                assert len(held) == 1, f"{peering} is {len(held)} ManagedBGPPeering object(s) in the snapshot, not 1"
            for key in keys:
                held = [n for n, state in before["RoutingPassword"].items() if repr(("value", key)) in repr(state)]
                assert len(held) == 1, f"{key} is {len(held)} RoutingPassword object(s) in the snapshot, not 1"

        targets = await self._targets(client, scenario_branch, TOPOLOGY_RERUN_GENERATORS, set())
        await self._rerun(client, scenario_branch, "Topology rerun", targets, security_before, before, labels)
        logging.info("%d topology reruns left the branch unchanged", RERUNS)
        logging.info("=== %s - COMPLETED ===", SCENARIO_NAME)
