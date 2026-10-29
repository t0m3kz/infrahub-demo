"""Integration test — the sessions of a 30_all circuit follow its status, both ways.

add_virtual_circuit builds the BGP sessions of an Interconnect (a DCI) or a Handoff circuit and tracks what it
writes (the /31 or /127 prefix, its two addresses, the peerings). A run that builds nothing -- the circuit
decommissioned, or its whole carrier decommissioned -- must still delete what an earlier run built. It did not: the SDK
skips the whole tracked cleanup when a run tracked nothing, so the sessions of a decommissioned carrier stayed
`active` for good. The generator now tracks the circuit itself first, and this module proves the fix on a live stack:

  * test_01 decommissions an Interconnect's CARRIER (the physical dark fibre). add_circuit reacts to the status change
    and fans out to add_virtual_circuit, which must remove both peerings, the circuit's prefix and the two port
    addresses, and leave the ports, the per-circuit key (same id, same secret) and every other circuit's sessions
    alone. The carrier is then set active again and the sessions must come back, with the same key.
  * test_02 does the same for a Handoff through the circuit's OWN status (its rule runs add_virtual_circuit
    directly): the sub-interfaces, the circuit, both BGP processes and the data-loaded key stay.

  * test_03 (and the sole-demand handoff it uses) proves the VRF instance of a handoff follows its only demand.
  * test_04 does the same for a segment: the instance and SVIs a segment's rows demand on a switch go when the
    segment no longer lands there (see the module-level constants of that test), and come back with it.

A Tunnel is not covered: it has no trigger rule and is not in the generator's group, so setting its status starts
nothing (tests/unit/test_event_rules.py pins both), and "no peering appeared" is true of every tunnel anyway.

Every test restores the status it changed in a `finally`, so a failure leaves the branch as it found it.

It runs AFTER test_65 because it mutates the branch the other modules read (and the idempotency snapshot must not
see a half-rebuilt session): what it deletes it rebuilds, but the prefix may be a different /31 and the ids of the
rebuilt peerings are new.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest
from infrahub_sdk import InfrahubClient

from tasks import ALL_DEMO_BRANCH

from .all_demo_circuit_constants import ALL_DEMO_VIRTUAL_CIRCUITS, HANDOFF_KIND, INTERCONNECT_KIND
from .conftest import TestInfrahubDockerWithClient
from .wait_helpers import wait_for_condition
from .workflow_helpers import run_generator, verify_no_failed_tasks, wait_for_tasks_completion

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

SCENARIO_NAME = "Scenario 30_all: Circuit Lifecycle"

# add_circuit (carrier status -> fan-out -> add_virtual_circuit) is two task hops, each queued behind whatever else the
# workers run; the probe that found the bug saw the sessions settle well inside a minute on an idle stack. 30 x 10 s
# = 5 minutes is several times that, short enough that a generator that never reacts fails the run, not the CI budget.
MAX_ATTEMPTS = 30
POLL_INTERVAL = 10

DECOMMISSIONED = "decommissioned"
ACTIVE = "active"
PHYSICAL_CIRCUIT_KIND = "TopologyPhysicalCircuit"

# The DCI under test: the *-B fibre of DC12 to PA4. Its sibling ICX-DC12-EQXPA4 shares both devices and must not move.
INTERCONNECT_CIRCUIT = "ICX-DC12-EQXPA4-B"
# The AWS transit VIF handoff. Its key is data-loaded (data/demos/30_all/08_interconnects/05_cloud_endpoints/
# 00_vif_handoff.yml), not generated: it must be there before and after.
HANDOFF_CIRCUIT = "HANDOFF-FR2-AWS-EUC1-PROD"
HANDOFF_KEY = "eqx-fr2-aws-vif-key"
# The second AWS VIF, held by the Network Edge router eg-am-ne02. That router is on no other circuit of 30_all (data:
# 05_cloud_endpoints/00_vif_handoff_am_ne.yml; no segment, no other handoff, no transit), so the /31 of this handoff is
# the only thing that demands its PROD VRF instance: decommissioning the circuit must delete the instance, and the
# rebuild must bring it back. (eg-fr201, the end of HANDOFF_CIRCUIT, has other PROD handoffs and segments: there the
# instance must stay, with the same id.)
SOLE_DEMAND_HANDOFF = "HANDOFF-AMNE-AWS-EUC1-PROD"
SOLE_DEMAND_DEVICE = "eg-am-ne02"
SOLE_DEMAND_KEY = "eqx-am-ne-aws-vif-key"

_PORT_KINDS = {INTERCONNECT_KIND: "DcimPhysicalInterface", HANDOFF_KIND: "DcimVirtualInterface"}
_PEERING_SUFFIXES = {INTERCONNECT_KIND: ("underlay", "evpn"), HANDOFF_KIND: ("bgp",)}
_PREFIX_LABELS = {INTERCONNECT_KIND: "DCI", HANDOFF_KIND: "Handoff"}
_ENDS = 2

State = dict[str, Any]

# Everything the generator owns or must leave alone, in one round trip: the sessions (and the other circuits', to prove
# they are untouched), the keys, the circuit's prefix and addresses (found by the description the generator writes, "DCI
# <name>" / "Handoff <name>"), and the two ports with the address they carry.
#
# Also the VRF instances (ManagedVRF, every one: the ones of other devices are the bystanders) and what else on the
# circuit's devices demands an instance of the circuit's namespace: an address on another interface, an SVI.
_STATE_QUERY = """
query ($peerings: [String], $keys: [String], $descriptions: [String], $devices: [String], $ports: [String]) {
  vrfs: ManagedVRF { edges { node { id name { value } } } }
  addressed_physical: DcimPhysicalInterface(device__name__values: $devices) {
    edges {
      node {
        name { value }
        device { node { name { value } } }
        ip_address { node { ip_namespace { node { name { value } } } } }
      }
    }
  }
  addressed_virtual: DcimVirtualInterface(device__name__values: $devices) {
    edges {
      node {
        name { value }
        role { value }
        device { node { name { value } } }
        ip_address { node { ip_namespace { node { name { value } } } } }
        vrf { node { namespace { node { name { value } } } } }
      }
    }
  }
  ManagedBGPPeering(name__values: $peerings) {
    edges {
      node {
        id
        name { value }
        status { value }
        password { node { id } }
        bgp_processes { edges { node { id } } }
      }
    }
  }
  RoutingPassword(name__values: $keys) { edges { node { id name { value } password { value } } } }
  IpamPrefix(description__values: $descriptions) {
    edges { node { id prefix { value } description { value } } }
  }
  IpamIPAddress(description__values: $descriptions) { count }
  %(port_kind)s(device__name__values: $devices, name__values: $ports) {
    edges {
      node {
        id
        name { value }
        device { node { name { value } } }
        ip_address { node { id address { value } } }
      }
    }
  }
}
"""

# The BGP processes the sessions ran on belong to their devices: they outlive the session.
PROCESSES_QUERY = "query ($ids: [ID]) { ManagedBGP(ids: $ids) { count } }"


def state_query(port_kind: str) -> str:
    """The state query for a circuit whose ports are ``port_kind`` (DcimPhysicalInterface or DcimVirtualInterface)."""
    return _STATE_QUERY % {"port_kind": port_kind}


@dataclass(frozen=True)
class Subject:
    """One circuit under test, read from the 30_all expectations (ALL_DEMO_VIRTUAL_CIRCUITS)."""

    circuit: str
    kind: str
    key: str
    ports: tuple[tuple[str, str], ...]

    @property
    def peerings(self) -> tuple[str, ...]:
        """The sessions add_virtual_circuit names after the circuit."""
        return tuple(f"{self.circuit}-{suffix}" for suffix in _PEERING_SUFFIXES[self.kind])

    @property
    def description(self) -> str:
        """The description the generator gives the circuit's prefix and addresses."""
        return f"{_PREFIX_LABELS[self.kind]} {self.circuit}"

    @property
    def port_kind(self) -> str:
        """The interface kind of the circuit's ends: a physical port, or a VLAN sub-interface."""
        return _PORT_KINDS[self.kind]

    @property
    def namespace(self) -> str | None:
        """The IPAM namespace the circuit's /31 lives in (None for an Interconnect: ``default``, no instance)."""
        return ALL_DEMO_VIRTUAL_CIRCUITS[self.circuit]["ip_namespace"]

    @property
    def instances(self) -> dict[str, str]:
        """device name -> the name of its ManagedVRF of the circuit's namespace, for each end device (a party's
        device has none: the state simply does not find it)."""
        if not self.namespace:
            return {}
        return {device: f"{device}-vrf-{self.namespace.lower()}" for device in sorted({d for d, _ in self.ports})}

    @property
    def bystanders(self) -> tuple[str, ...]:
        """The sessions of every OTHER interconnect and handoff circuit of 30_all."""
        return tuple(
            f"{name}-{suffix}"
            for name, expected in sorted(ALL_DEMO_VIRTUAL_CIRCUITS.items())
            if name != self.circuit and expected["kind"] in _PEERING_SUFFIXES
            for suffix in _PEERING_SUFFIXES[expected["kind"]]
        )


def subject_for(circuit: str, key: str | None = None) -> Subject:
    """The subject for a 30_all Interconnect (its generated ``<name>-dci-key``) or Handoff (``key`` is the data's)."""
    expected = ALL_DEMO_VIRTUAL_CIRCUITS[circuit]
    kind = expected["kind"]
    assert kind in _PEERING_SUFFIXES, f"{circuit} is a {kind}: it has no session"
    ends = tuple((str(device), str(port)) for device, port in expected["ends"])
    return Subject(circuit=circuit, kind=kind, key=key or f"{circuit}-dci-key", ports=ends)


# ----------------------------------------------------------------------------------------------------------------------
# Reading the state (pure: GraphQL result -> comparable dict)
# ----------------------------------------------------------------------------------------------------------------------


def _edges(result: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return [edge["node"] for edge in (result.get(kind) or {}).get("edges", [])]


def _value(node: dict[str, Any], attribute: str) -> Any:
    return (node.get(attribute) or {}).get("value")


def parse_state(result: dict[str, Any], subject: Subject) -> State:
    """The state of ``subject`` in a ``state_query`` result.

    ``peerings``: name -> {id, status, password_id, process_ids} for the subject's sessions; ``bystanders``: name ->
    (id, status) for the other circuits' sessions; ``key``: {id, password} or None; ``prefixes``: the circuit's
    prefixes; ``address_count``: the addresses carrying its description; ``ports``: (device, interface) -> {id,
    address} for each end that exists, address None when unaddressed.
    """
    peerings: dict[str, dict[str, Any]] = {}
    bystanders: dict[str, tuple[str, Any]] = {}
    for node in _edges(result, "ManagedBGPPeering"):
        name = _value(node, "name")
        if name in subject.peerings:
            peerings[name] = {
                "id": node["id"],
                "status": _value(node, "status"),
                "password_id": ((node.get("password") or {}).get("node") or {}).get("id"),
                "process_ids": sorted(
                    edge["node"]["id"] for edge in (node.get("bgp_processes") or {}).get("edges", [])
                ),
            }
        else:
            bystanders[name] = (node["id"], _value(node, "status"))
    keys = [node for node in _edges(result, "RoutingPassword") if _value(node, "name") == subject.key]
    ports: dict[tuple[str, str], dict[str, Any]] = {}
    for node in _edges(result, subject.port_kind):
        device = (((node.get("device") or {}).get("node") or {}).get("name") or {}).get("value")
        end = (str(device), str(_value(node, "name")))
        if end in subject.ports:
            address = (node.get("ip_address") or {}).get("node") or {}
            ports[end] = {"id": node["id"], "address": (address.get("address") or {}).get("value")}
    return {
        "vrfs": parse_vrfs(result, subject),
        "peerings": peerings,
        "bystanders": bystanders,
        "key": {"id": keys[0]["id"], "password": _value(keys[0], "password")} if keys else None,
        "prefixes": [
            {"id": node["id"], "prefix": _value(node, "prefix")}
            for node in _edges(result, "IpamPrefix")
            if _value(node, "description") == subject.description
        ],
        "address_count": (result.get("IpamIPAddress") or {}).get("count", 0),
        "ports": ports,
    }


def parse_vrfs(result: dict[str, Any], subject: Subject) -> dict[str, Any]:
    """``instances``: device -> id of its instance of the circuit's namespace (those that exist); ``others``: name ->
    id of every other ManagedVRF; ``other_demand``: the end devices that something besides the circuit's own port
    demands an instance of the namespace for (an address on another interface, an SVI routed in it)."""
    names = {name: device for device, name in subject.instances.items()}
    instances: dict[str, str] = {}
    others: dict[str, str] = {}
    for node in _edges(result, "vrfs"):
        name = _value(node, "name")
        if name in names:
            instances[names[name]] = node["id"]
        else:
            others[name] = node["id"]
    other_demand: set[str] = set()
    for kind in ("addressed_physical", "addressed_virtual"):
        for node in _edges(result, kind):
            device = (((node.get("device") or {}).get("node") or {}).get("name") or {}).get("value")
            if device not in subject.instances or (str(device), str(_value(node, "name"))) in subject.ports:
                continue
            address_ns = (((node.get("ip_address") or {}).get("node") or {}).get("ip_namespace") or {}).get(
                "node"
            ) or {}
            vrf_ns = ((((node.get("vrf") or {}).get("node") or {}).get("namespace") or {}).get("node")) or {}
            if subject.namespace in (_value(address_ns, "name"), _value(vrf_ns, "name")):
                other_demand.add(str(device))
    return {"instances": instances, "others": others, "other_demand": sorted(other_demand)}


# ----------------------------------------------------------------------------------------------------------------------
# Judging the state (pure: state -> the reasons it is not what is expected)
# ----------------------------------------------------------------------------------------------------------------------


def torn_down_errors(state: State, subject: Subject) -> list[str]:
    """Why the circuit still has a session: a peering (whatever its status), a prefix, an address on it or on a port."""
    errors = [
        f"peering {name} still exists ({peering['status']})" for name, peering in sorted(state["peerings"].items())
    ]
    errors += [f"prefix {prefix['prefix']} ({subject.description}) still exists" for prefix in state["prefixes"]]
    if state["address_count"]:
        errors.append(f"{state['address_count']} address(es) described '{subject.description}' still exist")
    errors += [
        f"port {device}:{interface} still has address {port['address']}"
        for (device, interface), port in sorted(state["ports"].items())
        if port["address"]
    ]
    return errors


def rebuilt_errors(state: State, subject: Subject) -> list[str]:
    """Why the circuit's session is not complete: every peering active on the circuit's key, one prefix, two addresses
    on two ports."""
    errors: list[str] = []
    key_id = (state["key"] or {}).get("id")
    for name in subject.peerings:
        peering = state["peerings"].get(name)
        if peering is None:
            errors.append(f"peering {name} is missing")
            continue
        if peering["status"] != ACTIVE:
            errors.append(f"peering {name} is {peering['status']}, expected {ACTIVE}")
        if key_id is None or peering["password_id"] != key_id:
            errors.append(f"peering {name} is keyed with {peering['password_id']}, expected {subject.key} ({key_id})")
    if len(state["prefixes"]) != 1:
        errors.append(f"{len(state['prefixes'])} prefix(es) described '{subject.description}', expected exactly 1")
    if state["address_count"] != _ENDS:
        errors.append(f"{state['address_count']} address(es) described '{subject.description}', expected {_ENDS}")
    addresses = [port["address"] for port in state["ports"].values()]
    for end in subject.ports:
        port = state["ports"].get(end)
        if port is None:
            errors.append(f"port {end[0]}:{end[1]} does not exist")
        elif not port["address"]:
            errors.append(f"port {end[0]}:{end[1]} has no address")
    if len(set(addresses)) != len(addresses):
        errors.append(f"the ports share an address: {addresses}")
    return errors


def survivor_errors(before: State, after: State, subject: Subject) -> list[str]:
    """What a session teardown must NOT take along: both ports (same ids) and the key (same id, same secret)."""
    errors = [
        f"port {device}:{interface} {'was deleted' if after['ports'].get((device, interface)) is None else 're-created'}"
        for device, interface in subject.ports
        if before["ports"].get((device, interface), {}).get("id")
        != after["ports"].get((device, interface), {}).get("id")
    ]
    return errors + key_errors(before["key"], after["key"], subject.key)


def key_errors(before: dict[str, Any] | None, after: dict[str, Any] | None, name: str) -> list[str]:
    """Why ``after`` is not the key ``before`` was: deleted, re-created (new id) or rotated (new secret)."""
    if before is None:
        return [f"key {name} did not exist before"]
    if after is None:
        return [f"key {name} was deleted"]
    errors: list[str] = []
    if after["id"] != before["id"]:
        errors.append(f"key {name} was re-created: id {before['id']} -> {after['id']}")
    if after["password"] != before["password"]:
        errors.append(f"key {name} was rotated: its secret changed")
    return errors


def bystander_errors(before: State, after: State) -> list[str]:
    """Why another circuit's sessions are not as they were: missing, a new id (re-created) or another status."""
    errors = [f"session {name} was deleted" for name in sorted(set(before["bystanders"]) - set(after["bystanders"]))]
    errors += [f"session {name} appeared" for name in sorted(set(after["bystanders"]) - set(before["bystanders"]))]
    errors += [
        f"session {name} changed: {before['bystanders'][name]} -> {after['bystanders'][name]}"
        for name in sorted(set(before["bystanders"]) & set(after["bystanders"]))
        if before["bystanders"][name] != after["bystanders"][name]
    ]
    return errors


def vrf_down_errors(before: State, down: State) -> list[str]:
    """The VRF instances once the session is gone: an end device's instance is deleted when nothing else demands the
    namespace there, and kept with the same id when something does; every other device's instance is untouched."""
    errors: list[str] = []
    for device, instance_id in sorted(before["vrfs"]["instances"].items()):
        kept = down["vrfs"]["instances"].get(device)
        if device in down["vrfs"]["other_demand"]:
            if kept != instance_id:
                errors.append(f"{device}: other demand remains but its VRF instance went ({instance_id} -> {kept})")
        elif kept is not None:
            errors.append(f"{device}: no demand left but its VRF instance {kept} still exists")
    if down["vrfs"]["others"] != before["vrfs"]["others"]:
        errors.append("the VRF instances of other devices changed")
    return errors


def vrf_up_errors(before: State, after: State) -> list[str]:
    """The VRF instances once the session is rebuilt: each end device that had an instance has one again (the same id
    where it was never deleted), and every other device's instances are as they were."""
    errors: list[str] = []
    for device, instance_id in sorted(before["vrfs"]["instances"].items()):
        back = after["vrfs"]["instances"].get(device)
        if back is None:
            errors.append(f"{device}: its VRF instance did not come back")
        elif device in before["vrfs"]["other_demand"] and back != instance_id:
            errors.append(f"{device}: its VRF instance was re-created ({instance_id} -> {back})")
    if after["vrfs"]["others"] != before["vrfs"]["others"]:
        errors.append("the VRF instances of other devices changed")
    return errors


def process_errors(expected: int, result: dict[str, Any]) -> list[str]:
    """Why fewer BGP processes than the sessions ran on are left (``PROCESSES_QUERY`` result)."""
    alive = (result.get("ManagedBGP") or {}).get("count", 0)
    return [f"{alive} of the {expected} BGP process(es) of the sessions are left"] if alive != expected else []


# ----------------------------------------------------------------------------------------------------------------------
# Driving the stack
# ----------------------------------------------------------------------------------------------------------------------


async def fetch_state(client: InfrahubClient, branch: str, subject: Subject) -> State:
    """Read the subject's session state on the branch."""
    result = await client.execute_graphql(
        query=state_query(subject.port_kind),
        variables={
            "peerings": [*subject.peerings, *subject.bystanders],
            "keys": [subject.key],
            "descriptions": [subject.description],
            "devices": sorted({device for device, _ in subject.ports}),
            "ports": sorted({port for _, port in subject.ports}),
        },
        branch_name=branch,
    )
    return parse_state(result, subject)


async def set_status(client: InfrahubClient, branch: str, kind: str, status: str, **filters: Any) -> str:
    """Set the status of the one ``kind`` node matching ``filters`` on the branch; returns the status it had.

    Saved only when it differs, so restoring an unchanged node is a no-op.
    """
    node = await client.get(kind=kind, branch=branch, **filters)
    attribute = getattr(node, "status")  # noqa: B009 - SDK attributes are dynamic
    previous = str(attribute.value)
    if previous != status:
        attribute.value = status
        await node.save()
    return previous


async def wait_for_state(
    client: InfrahubClient,
    branch: str,
    subject: Subject,
    judge: Callable[[State, Subject], list[str]],
    description: str,
) -> State:
    """Poll until ``judge`` finds nothing wrong with the subject's state; a timeout fails with what is still wrong."""
    last: list[State] = []

    async def _check() -> tuple[bool, State]:
        state = await fetch_state(client, branch, subject)
        last[:] = [state]
        errors = judge(state, subject)
        if errors:
            logging.info("%s: not yet: %s", description, "; ".join(errors))
        return not errors, state

    try:
        return await wait_for_condition(
            _check, max_attempts=MAX_ATTEMPTS, poll_interval=POLL_INTERVAL, description=description
        )
    except TimeoutError as exc:
        raise AssertionError(f"{exc}\n  - " + "\n  - ".join(judge(last[0], subject))) from exc


async def decommission_and_restore(
    client: InfrahubClient,
    branch: str,
    subject: Subject,
    toggled_kind: str,
    *,
    sole_demand: str | None = None,
    **toggled: Any,
) -> None:
    """The lifecycle of one circuit's session, driven through the status of the node ``toggled_kind`` / ``toggled``
    (the carrier of an Interconnect, the Handoff itself): decommission it and assert the session is gone and only the
    session; set the status back (in a `finally`, so a failure leaves the branch as it was) and assert it is rebuilt
    on the same key, with no other circuit's session touched and no failed task.

    The VRF instances follow (vrf_down_errors / vrf_up_errors): an end device's instance of the circuit's namespace is
    deleted with the session when nothing else demands it, kept (same id) when something does, and every other
    device's instance is untouched. ``sole_demand`` names an end device the circuit is the only demand for: the test
    asserts that precondition on the state read first, so the "deleted" branch is really exercised."""
    await wait_for_tasks_completion(client, branch)
    before = await fetch_state(client, branch, subject)
    errors = rebuilt_errors(before, subject)
    assert not errors, (
        f"{subject.circuit} has no complete session on '{branch}' to decommission:\n  - " + "\n  - ".join(errors)
    )
    assert before["bystanders"], "no other circuit has a session: nothing proves they are left alone"
    if sole_demand:
        assert sole_demand in before["vrfs"]["instances"], (
            f"{sole_demand} has no VRF instance of {subject.namespace} before the decommission"
        )
        assert sole_demand not in before["vrfs"]["other_demand"], (
            f"{sole_demand} has other demand for {subject.namespace}: the instance would stay"
        )
    processes = sorted({pid for peering in before["peerings"].values() for pid in peering["process_ids"]})
    assert processes, f"{subject.circuit}'s session runs on no BGP process"
    toggled_id = (await client.get(kind=toggled_kind, branch=branch, **toggled)).id
    logging.info("Before: %s", {name: peering["id"] for name, peering in before["peerings"].items()})

    original = await set_status(client, branch, toggled_kind, DECOMMISSIONED, **toggled)
    try:
        logging.info("%s %s: %s -> %s", toggled_kind, toggled, original, DECOMMISSIONED)
        await wait_for_state(client, branch, subject, torn_down_errors, f"{subject.circuit} session removed")
        # The run that removed the session may still be finishing (its logs, the fan-out): let the queue drain, then
        # judge what is left, so a late write cannot pass for a clean teardown.
        await wait_for_tasks_completion(client, branch)
        down = await fetch_state(client, branch, subject)
        left = await client.execute_graphql(query=PROCESSES_QUERY, variables={"ids": processes}, branch_name=branch)
        errors = [
            *torn_down_errors(down, subject),
            *survivor_errors(before, down, subject),
            *bystander_errors(before, down),
            *vrf_down_errors(before, down),
            *process_errors(len(processes), left),
        ]
        assert (await client.get(kind=toggled_kind, branch=branch, **toggled)).id == toggled_id, (
            f"{toggled_kind} {toggled} was re-created"
        )
        assert not errors, "decommissioned, but:\n  - " + "\n  - ".join(errors)
    finally:
        await set_status(client, branch, toggled_kind, original, **toggled)
        await wait_for_tasks_completion(client, branch)
        logging.info("%s %s restored to %s", toggled_kind, toggled, original)

    await wait_for_state(client, branch, subject, rebuilt_errors, f"{subject.circuit} session rebuilt")
    await wait_for_tasks_completion(client, branch)
    after = await fetch_state(client, branch, subject)
    errors = [
        *rebuilt_errors(after, subject),
        *key_errors(before["key"], after["key"], subject.key),
        *bystander_errors(before, after),
        *vrf_up_errors(before, after),
    ]
    assert not errors, "reactivated, but:\n  - " + "\n  - ".join(errors)
    await verify_no_failed_tasks(client=client, branch=branch)


# The segment under test: the MANAGEMENT-gateway segment of the Infrastructure Services pseudo-customer
# (data/demos/30_all/07_applications/infra/00_segment.yml). Nothing else of 30_all routes in the MANAGEMENT namespace on
# an access leaf (the tenant segments are PROD / NON-PROD, and the firewall MANAGEMENT legs sit on border leaves), so on
# the leaves of one host it is the ONLY demand of the MANAGEMENT instance. Its two components (dns, ntp) have the same
# two VMs (07_applications/infra/01_application.yml) on two hosts racked in different rows, hence on different
# access-leaf pairs (06_customer_boarding/c006/dc/dc11/02_instances.yml). Taking the second VM off both components
# leaves the segment no port on the second host's leaves: their rows, SVIs and MANAGEMENT instance must go while the
# first host's leaves keep theirs. The switches are read from the cabling, not named here.
SEGMENT_NAME = "infra-core-dns-ntp-p"
SEGMENT_NAMESPACE = "MANAGEMENT"
SEGMENT_COMPONENT_FQDNS = (
    "dns.core-services.infra.demo.local",
    "ntp.core-services.infra.demo.local",
)
KEPT_INSTANCE = "infra-core-dc11-1"
KEPT_HOST = "dc11-pod1-server-1"
REMOVED_INSTANCE = "infra-core-dc11-2"
REMOVED_HOST = "dc11-pod1-server-2"
COMPONENT_SEGMENT_GENERATOR = "add_app_component_segment"
SEGMENT_GENERATOR = "add_vxlan_segment"
SVI_ROLE = "svi"

_HOST_CABLES_QUERY = """
query ($hosts: [String]) {
  DcimPhysicalInterface(device__name__values: $hosts) {
    edges {
      node {
        device { node { name { value } } }
        cable {
          node {
            endpoints {
              edges { node { ... on DcimPhysicalInterface { device { node { name { value } } } } } }
            }
          }
        }
      }
    }
  }
}
"""

_SWITCH_VRF_QUERY = """
query ($devices: [String], $vrfs: [String]) {
  ManagedVRF(name__values: $vrfs) { edges { node { id name { value } } } }
  DcimVirtualInterface(device__name__values: $devices) {
    edges {
      node {
        id
        name { value }
        role { value }
        device { node { name { value } } }
        ip_address { node { ip_namespace { node { name { value } } } } }
        vrf { node { id namespace { node { name { value } } } } }
        vlan_domain_segment {
          node { segment_deployment { node { segment { node { ... on ManagedVxlanSegment { name { value } } } } } } }
        }
      }
    }
  }
  DcimPhysicalInterface(device__name__values: $devices) {
    edges { node { name { value } device { node { name { value } } } ip_address { node { ip_namespace { node { name { value } } } } } } }
  }
}
"""

COMPONENT_QUERY = (
    "query ($fqdns: [String]) { AppComponent(fqdn__values: $fqdns) { edges { node { id fqdn { value } } } } }"
)
SEGMENT_QUERY = "query ($name: String) { ManagedVxlanSegment(name__value: $name) { edges { node { id } } } }"


def _device_name(node: dict[str, Any]) -> str:
    return str((((node.get("device") or {}).get("node") or {}).get("name") or {}).get("value"))


def _namespace_name(holder: dict[str, Any] | None, key: str) -> str | None:
    namespace = (((holder or {}).get(key) or {}).get("node") or {}).get("name") or {}
    return namespace.get("value")


def switches_of_host(result: dict[str, Any], host: str) -> set[str]:
    """The switches at the far end of the cables of ``host``'s ports (a ``_HOST_CABLES_QUERY`` result)."""
    switches: set[str] = set()
    for port in _edges(result, "DcimPhysicalInterface"):
        if _device_name(port) != host:
            continue
        endpoints = (((port.get("cable") or {}).get("node") or {}).get("endpoints") or {}).get("edges", [])
        switches |= {_device_name(edge["node"]) for edge in endpoints if edge["node"]} - {host, "None"}
    return switches


def vrf_name(device: str) -> str:
    """The name of ``device``'s ManagedVRF of the segment's namespace."""
    return f"{device}-vrf-{SEGMENT_NAMESPACE.lower()}"


def parse_switch_vrf_state(result: dict[str, Any], devices: list[str]) -> dict[str, dict[str, Any]]:
    """device -> {vrf: id of its instance or None, svis: {name: {id, vrf}} of the segment's SVIs, foreign: what else on
    the device demands the namespace (an SVI of another segment, an address on another interface)}."""
    state: dict[str, dict[str, Any]] = {device: {"vrf": None, "svis": {}, "foreign": []} for device in devices}
    for node in _edges(result, "ManagedVRF"):
        for device in devices:
            if _value(node, "name") == vrf_name(device):
                state[device]["vrf"] = node["id"]
    for node in _edges(result, "DcimVirtualInterface"):
        device, name = _device_name(node), str(_value(node, "name"))
        vrf = (node.get("vrf") or {}).get("node") or {}
        if _value(node, "role") == SVI_ROLE:
            row = (node.get("vlan_domain_segment") or {}).get("node") or {}
            deployment = (row.get("segment_deployment") or {}).get("node") or {}
            segment = ((deployment.get("segment") or {}).get("node") or {}).get("name") or {}
            if segment.get("value") == SEGMENT_NAME:
                state[device]["svis"][name] = {"id": node["id"], "vrf": vrf.get("id")}
                continue
        if SEGMENT_NAMESPACE in (_namespace_name(node, "ip_address"), _namespace_name(vrf, "namespace")):
            state[device]["foreign"].append(f"{name} (virtual)")
    for node in _edges(result, "DcimPhysicalInterface"):
        if _namespace_name(node, "ip_address") == SEGMENT_NAMESPACE:
            state[_device_name(node)]["foreign"].append(f"{_value(node, 'name')} (physical)")
    return state


async def fetch_switch_vrf_state(client: InfrahubClient, branch: str, devices: list[str]) -> dict[str, dict[str, Any]]:
    """Read the instance and the segment's SVIs of each of ``devices`` on the branch."""
    result = await client.execute_graphql(
        query=_SWITCH_VRF_QUERY,
        variables={"devices": devices, "vrfs": [vrf_name(device) for device in devices]},
        branch_name=branch,
    )
    return parse_switch_vrf_state(result, devices)


def precondition_errors(state: dict[str, dict[str, Any]]) -> list[str]:
    """Why the segment is not the only demand of the namespace on these switches (it must have SVIs and an instance
    there, and nothing else may route in the namespace)."""
    errors: list[str] = []
    for device, facts in sorted(state.items()):
        if facts["vrf"] is None:
            errors.append(f"{device} has no {vrf_name(device)} instance")
        if not facts["svis"]:
            errors.append(f"{device} has no SVI of {SEGMENT_NAME}")
        elif any(svi["vrf"] != facts["vrf"] for svi in facts["svis"].values()):
            errors.append(f"{device}: an SVI of {SEGMENT_NAME} is not in {vrf_name(device)}")
        if facts["foreign"]:
            errors.append(f"{device} has other {SEGMENT_NAMESPACE} demand: {', '.join(facts['foreign'])}")
    return errors


def svis_gone_errors(state: dict[str, dict[str, Any]]) -> list[str]:
    """Why a switch still has SVIs of the segment."""
    return [f"{device} still has SVI(s) {sorted(f['svis'])}" for device, f in sorted(state.items()) if f["svis"]]


def vrf_gone_errors(state: dict[str, dict[str, Any]]) -> list[str]:
    """Why a switch still has the instance (or SVIs: the instance goes after them)."""
    return svis_gone_errors(state) + [
        f"{device} still has {vrf_name(device)} ({f['vrf']})" for device, f in sorted(state.items()) if f["vrf"]
    ]


def back_errors(state: dict[str, dict[str, Any]]) -> list[str]:
    """Why the switches do not have the segment's SVIs and instance again, the SVIs in that instance."""
    return precondition_errors(state)


async def poll_switches(
    client: InfrahubClient,
    branch: str,
    devices: list[str],
    judge: Callable[[dict[str, dict[str, Any]]], list[str]],
    description: str,
) -> dict[str, dict[str, Any]]:
    """Poll until ``judge`` finds nothing wrong with the switches' state; a timeout fails with what is still wrong."""
    last: list[dict[str, dict[str, Any]]] = []

    async def _check() -> tuple[bool, dict[str, dict[str, Any]]]:
        state = await fetch_switch_vrf_state(client, branch, devices)
        last[:] = [state]
        errors = judge(state)
        if errors:
            logging.info("%s: not yet: %s", description, "; ".join(errors))
        return not errors, state

    try:
        return await wait_for_condition(
            _check, max_attempts=MAX_ATTEMPTS, poll_interval=POLL_INTERVAL, description=description
        )
    except TimeoutError as exc:
        raise AssertionError(f"{exc}\n  - " + "\n  - ".join(judge(last[0]))) from exc


async def nudge_segment(client: InfrahubClient, branch: str, component_ids: list[str], segment_id: str) -> None:
    """Run the segment reconcile from both of its entry points and let the queue drain: the first run must have finished
    before the second starts (run_generator waits for the branch to settle), and the cleanup of a run that deletes a
    deployment runs after its generate(), so the next run is what removes what that cleanup freed."""
    for generator, ids in ((COMPONENT_SEGMENT_GENERATOR, component_ids), (SEGMENT_GENERATOR, [segment_id])):
        outcome = await run_generator(client=client, generator_name=generator, node_ids=ids, branch=branch)
        assert outcome["success"], f"{generator} failed: {outcome}"
        await wait_for_tasks_completion(client, branch)


async def set_component_instances(
    client: InfrahubClient, branch: str, fqdn: str, instance_id: str, *, present: bool
) -> None:
    """Make the AppComponent ``fqdn`` list (``present``) or not list the instance ``instance_id``; a no-op when it
    already is so, so restoring an untouched component writes nothing."""
    component = await client.get(kind="AppComponent", branch=branch, fqdn__value=fqdn, include=["instances"])
    relationship = getattr(component, "instances")  # noqa: B009 - SDK relationships are dynamic
    listed = instance_id in {peer.id for peer in relationship.peers}
    if present and not listed:
        relationship.add(instance_id)
    elif not present and listed:
        relationship.remove(instance_id)
    else:
        return
    await component.save()


class TestAllDemoCircuitLifecycle(TestInfrahubDockerWithClient):
    """Decommissioning a circuit deletes its sessions; reactivating it rebuilds them; the rest stays."""

    @pytest.fixture(scope="class")
    def scenario_branch(self) -> str:
        return ALL_DEMO_BRANCH

    @pytest.mark.order(420)
    @pytest.mark.dependency(
        scope="session",
        name="all_demo_interconnect_lifecycle",
        depends=["all_demo_idempotent_topology_reruns"],
    )
    @pytest.mark.asyncio
    async def test_01_interconnect_follows_its_carrier(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Decommissioning the DCI's fibre removes both sessions, the /31 (/127) and the port addresses; ports and key
        stay untouched, other circuits' sessions too; setting the fibre active again rebuilds it all on the same key."""
        logging.info("=== %s - Step 1: Interconnect via its carrier ===", SCENARIO_NAME)

        (carrier,) = ALL_DEMO_VIRTUAL_CIRCUITS[INTERCONNECT_CIRCUIT]["carrier"]
        await decommission_and_restore(
            async_client_main,
            scenario_branch,
            subject_for(INTERCONNECT_CIRCUIT),
            PHYSICAL_CIRCUIT_KIND,
            circuit_id__value=carrier,
        )
        logging.info("%s: sessions removed with its carrier, rebuilt on the same key", INTERCONNECT_CIRCUIT)

    @pytest.mark.order(421)
    @pytest.mark.dependency(
        scope="session",
        name="all_demo_handoff_lifecycle",
        depends=["all_demo_interconnect_lifecycle"],
    )
    @pytest.mark.asyncio
    async def test_02_handoff_follows_its_status(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Decommissioning the AWS handoff removes its session, /31 and sub-interface addresses; the sub-interfaces,
        the circuit, the BGP processes and the data-loaded key stay; setting it active again rebuilds the session."""
        logging.info("=== %s - Step 2: Handoff via its own status ===", SCENARIO_NAME)

        await decommission_and_restore(
            async_client_main,
            scenario_branch,
            subject_for(HANDOFF_CIRCUIT, key=HANDOFF_KEY),
            HANDOFF_KIND,
            name__value=HANDOFF_CIRCUIT,
        )
        logging.info("%s: session removed with its status, rebuilt on the same key", HANDOFF_CIRCUIT)

    @pytest.mark.order(422)
    @pytest.mark.dependency(
        scope="session",
        name="all_demo_handoff_vrf_lifecycle",
        depends=["all_demo_handoff_lifecycle"],
    )
    @pytest.mark.asyncio
    async def test_03_handoff_vrf_instance_follows_its_only_demand(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """The handoff is the only thing eg-am-ne02 routes in PROD: decommissioning it deletes the device's PROD VRF
        instance (add_virtual_circuit reconciles after its cleanup), the rebuild creates it again, and every other
        device's instance keeps its id."""
        logging.info("=== %s - Step 3: Handoff VRF instance ===", SCENARIO_NAME)

        await decommission_and_restore(
            async_client_main,
            scenario_branch,
            subject_for(SOLE_DEMAND_HANDOFF, key=SOLE_DEMAND_KEY),
            HANDOFF_KIND,
            sole_demand=SOLE_DEMAND_DEVICE,
            name__value=SOLE_DEMAND_HANDOFF,
        )
        logging.info("%s: instance removed with its only demand, back with the rebuild", SOLE_DEMAND_DEVICE)

    @pytest.mark.order(423)
    @pytest.mark.dependency(
        scope="session",
        name="all_demo_segment_vrf_lifecycle",
        depends=["all_demo_handoff_vrf_lifecycle"],
    )
    @pytest.mark.asyncio
    async def test_04_segment_vrf_instance_follows_its_rows(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """The DNS/NTP segment is the only MANAGEMENT demand on the leaves of the host of its second VM: taking that VM
        off the components removes the segment's SVIs there, then the switches' MANAGEMENT instance (the reconcile
        deletes the instance after the last SVI); the host that stays keeps both. Giving the VM back restores them, the
        SVIs in the instance."""
        client, branch = async_client_main, scenario_branch
        logging.info("=== %s - Step 4: Segment VRF instance ===", SCENARIO_NAME)

        await wait_for_tasks_completion(client, branch)
        cables = await client.execute_graphql(
            query=_HOST_CABLES_QUERY, variables={"hosts": [KEPT_HOST, REMOVED_HOST]}, branch_name=branch
        )
        kept_switches = switches_of_host(cables, KEPT_HOST)
        lost = sorted(switches_of_host(cables, REMOVED_HOST) - kept_switches)
        assert lost, f"{REMOVED_HOST} is cabled to no switch {KEPT_HOST} is not: pick another segment or host"
        kept = sorted(kept_switches)
        assert kept, f"{KEPT_HOST} is cabled to no switch"

        before = await fetch_switch_vrf_state(client, branch, [*lost, *kept])
        errors = precondition_errors(before)
        assert not errors, (
            f"{SEGMENT_NAME} is not the only {SEGMENT_NAMESPACE} demand of {lost} (and present on {kept}); pick another "
            "segment:\n  - " + "\n  - ".join(errors)
        )

        component_ids = [
            (await client.get(kind="AppComponent", branch=branch, fqdn__value=fqdn)).id
            for fqdn in SEGMENT_COMPONENT_FQDNS
        ]
        segment_id = (await client.get(kind="ManagedVxlanSegment", branch=branch, name__value=SEGMENT_NAME)).id
        removed_id = (await client.get(kind="DcimVirtualDevice", branch=branch, name__value=REMOVED_INSTANCE)).id

        try:
            for fqdn in SEGMENT_COMPONENT_FQDNS:
                await set_component_instances(client, branch, fqdn, removed_id, present=False)
            logging.info("%s taken off %s", REMOVED_INSTANCE, SEGMENT_COMPONENT_FQDNS)
            await wait_for_tasks_completion(client, branch)
            await nudge_segment(client, branch, component_ids, segment_id)

            await poll_switches(
                client, branch, lost, lambda st: svis_gone_errors(st), f"{lost} lose the SVIs of {SEGMENT_NAME}"
            )
            down = await poll_switches(
                client, branch, lost, lambda st: vrf_gone_errors(st), f"{lost} lose their {SEGMENT_NAMESPACE} instance"
            )
            assert not vrf_gone_errors(down)
            still = await fetch_switch_vrf_state(client, branch, kept)
            assert still == {d: before[d] for d in kept}, f"the switches of {KEPT_HOST} changed: {still}"
        finally:
            for fqdn in SEGMENT_COMPONENT_FQDNS:
                await set_component_instances(client, branch, fqdn, removed_id, present=True)
            await wait_for_tasks_completion(client, branch)
            await nudge_segment(client, branch, component_ids, segment_id)
            logging.info("%s given back to %s", REMOVED_INSTANCE, SEGMENT_COMPONENT_FQDNS)

        after = await poll_switches(
            client, branch, [*lost, *kept], back_errors, f"{lost} get the SVIs and the instance of {SEGMENT_NAME} back"
        )
        assert not back_errors(after)
        for device in kept:
            assert after[device]["vrf"] == before[device]["vrf"], f"{device}: its instance was re-created"
        await verify_no_failed_tasks(client=client, branch=branch)
        logging.info("=== %s - COMPLETED ===", SCENARIO_NAME)
