"""The readers and judges of test_66 (the circuit lifecycle), run offline on canned GraphQL payloads.

test_66 decommissions a circuit on a live stack and polls until its session is gone, then back. What it polls on and
asserts are pure functions of a GraphQL result, so a typo in a query path, a predicate that is true too early (peerings
still active "after" a decommission) or a key check that cannot fail shows up here instead of in a 55-minute run.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from graphql import parse

from tasks import ALL_DEMO_BRANCH
from tests.integration import test_66_all_demo_circuit_lifecycle as lifecycle
from tests.integration.all_demo_circuit_constants import ALL_DEMO_VIRTUAL_CIRCUITS, HANDOFF_KIND, INTERCONNECT_KIND
from tests.integration.test_66_all_demo_circuit_lifecycle import (
    HANDOFF_CIRCUIT,
    HANDOFF_KEY,
    INTERCONNECT_CIRCUIT,
    State,
    Subject,
    subject_for,
)

DCI = subject_for(INTERCONNECT_CIRCUIT)
HANDOFF = subject_for(HANDOFF_CIRCUIT, key=HANDOFF_KEY)


def _peering(
    name: str, status: str, key_id: str | None = "key-1", processes: tuple[str, ...] = ("bgp-1", "bgp-2")
) -> Any:
    return {
        "id": f"id-{name}",
        "name": {"value": name},
        "status": {"value": status},
        "password": {"node": {"id": key_id}} if key_id else {"node": None},
        "bgp_processes": {"edges": [{"node": {"id": process}} for process in processes]},
    }


def _port(subject: Subject, end: tuple[str, str], address: str | None) -> dict[str, Any]:
    device, name = end
    return {
        "id": f"port-{device}-{name}",
        "name": {"value": name},
        "device": {"node": {"name": {"value": device}}},
        "ip_address": {"node": {"id": f"addr-{device}", "address": {"value": address}}} if address else {"node": None},
    }


def _result(
    subject: Subject,
    *,
    peerings: dict[str, str] | None = None,
    key: tuple[str, str] | None = ("key-1", "s3cret"),
    prefixes: int = 1,
    addresses: int = 2,
    port_addresses: tuple[str | None, str | None] = ("10.0.0.0/31", "10.0.0.1/31"),
    bystanders: dict[str, str] | None = None,
) -> dict[str, Any]:
    """A ``state_query`` result: ``peerings`` name -> status of the subject's sessions (default: all active)."""
    peerings = {name: "active" for name in subject.peerings} if peerings is None else peerings
    if bystanders is None:
        bystanders = {name: "active" for name in subject.bystanders}
    nodes = [_peering(name, status) for name, status in {**peerings, **bystanders}.items()]
    return {
        "ManagedBGPPeering": {"edges": [{"node": node} for node in nodes]},
        "RoutingPassword": {
            "edges": [{"node": {"id": key[0], "name": {"value": subject.key}, "password": {"value": key[1]}}}]
            if key
            else []
        },
        "IpamPrefix": {
            "edges": [
                {
                    "node": {
                        "id": f"pfx-{i}",
                        "prefix": {"value": f"10.0.{i}.0/31"},
                        "description": {"value": subject.description},
                    }
                }
                for i in range(prefixes)
            ]
            + [
                # Another circuit's prefix is never this one's.
                {"node": {"id": "pfx-other", "prefix": {"value": "10.9.9.0/31"}, "description": {"value": "DCI other"}}}
            ]
        },
        "IpamIPAddress": {"count": addresses},
        subject.port_kind: {
            "edges": [
                {"node": _port(subject, end, address)}
                for end, address in zip(subject.ports, port_addresses, strict=True)
            ]
            # A same-named port of another device matched by the cross product of the filters.
            + [{"node": _port(subject, ("other-device", subject.ports[0][1]), "10.1.1.1/31")}]
        },
    }


def _state(subject: Subject = DCI, **kwargs: Any) -> State:
    return lifecycle.parse_state(_result(subject, **kwargs), subject)


def _torn_down(subject: Subject = DCI, **kwargs: Any) -> State:
    defaults: dict[str, Any] = {"peerings": {}, "prefixes": 0, "addresses": 0, "port_addresses": (None, None)}
    return _state(subject, **{**defaults, **kwargs})


# ---------------------------------------------------------------------------
# The subjects come from the 30_all expectations
# ---------------------------------------------------------------------------


def test_the_interconnect_subject_is_the_dci_and_its_ports() -> None:
    """Names, description and ports are derived from ALL_DEMO_VIRTUAL_CIRCUITS and the generator's naming."""
    assert DCI.kind == INTERCONNECT_KIND
    assert DCI.peerings == ("ICX-DC12-EQXPA4-B-underlay", "ICX-DC12-EQXPA4-B-evpn")
    assert DCI.key == "ICX-DC12-EQXPA4-B-dci-key"
    assert DCI.description == "DCI ICX-DC12-EQXPA4-B"
    assert DCI.ports == (("bl-dc1212101", "Ethernet1/36"), ("eg-pa402", "Ethernet1/10"))
    assert DCI.port_kind == "DcimPhysicalInterface"
    assert ALL_DEMO_VIRTUAL_CIRCUITS[INTERCONNECT_CIRCUIT]["carrier"] == ("DF-DC12-EQXPA4-B",)


def test_the_handoff_subject_is_the_aws_vif_and_its_subinterfaces() -> None:
    """A handoff has one session, a prefix described 'Handoff <name>' and VLAN sub-interface ends."""
    assert HANDOFF.kind == HANDOFF_KIND
    assert HANDOFF.peerings == ("HANDOFF-FR2-AWS-EUC1-PROD-bgp",)
    assert HANDOFF.description == "Handoff HANDOFF-FR2-AWS-EUC1-PROD"
    assert set(HANDOFF.ports) == {("eg-fr201", "Ethernet1/13.1000"), ("EXT-AWS-DX-FR2", "et-0/0/0.1000")}
    assert HANDOFF.port_kind == "DcimVirtualInterface"


def test_the_bystanders_are_every_other_session_of_the_branch() -> None:
    """The sibling DCI and the other handoffs are watched; the subject's own sessions and any tunnel are not."""
    assert "ICX-DC12-EQXPA4-underlay" in DCI.bystanders
    assert "HANDOFF-FR2-AWS-EUC1-PROD-bgp" in DCI.bystanders
    assert not set(DCI.peerings) & set(DCI.bystanders)
    assert not any("SDWAN" in name and "HANDOFF" not in name for name in HANDOFF.bystanders)


def test_a_tunnel_has_no_subject() -> None:
    """A tunnel has no session: asking for its subject is a mistake of the test, not data."""
    tunnel = next(
        name for name, expected in ALL_DEMO_VIRTUAL_CIRCUITS.items() if expected["kind"].endswith("TunnelCircuit")
    )
    with pytest.raises(AssertionError, match="no session"):
        subject_for(tunnel)


# ---------------------------------------------------------------------------
# The queries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("port_kind", ["DcimPhysicalInterface", "DcimVirtualInterface"])
def test_the_state_query_parses_and_selects_what_the_parser_reads(port_kind: str) -> None:
    """graphql-core parses it (no schema offline), and every field parse_state reads is selected."""
    query = lifecycle.state_query(port_kind)
    parse(query)
    assert f"{port_kind}(device__name__values: $devices, name__values: $ports)" in query
    for field in (
        "ManagedBGPPeering(name__values: $peerings)",
        "RoutingPassword(name__values: $keys)",
        "IpamPrefix(description__values: $descriptions)",
        "IpamIPAddress(description__values: $descriptions) { count }",
    ):
        assert field in query
    for selected in (
        "status { value }",
        "bgp_processes { edges { node { id } } }",
        "password { value }",
        "ip_address { node { id address { value } } }",
    ):
        assert selected in query


def test_the_processes_query_parses() -> None:
    """The survivors query is valid GraphQL."""
    parse(lifecycle.PROCESSES_QUERY)


# ---------------------------------------------------------------------------
# Reading the state
# ---------------------------------------------------------------------------


def test_parse_state_reads_a_complete_session() -> None:
    """Sessions, key, the one prefix, the addresses and both ports; foreign prefixes and ports are ignored."""
    state = _state()
    assert sorted(state["peerings"]) == sorted(DCI.peerings)
    assert state["peerings"][DCI.peerings[0]] == {
        "id": f"id-{DCI.peerings[0]}",
        "status": "active",
        "password_id": "key-1",
        "process_ids": ["bgp-1", "bgp-2"],
    }
    assert state["key"] == {"id": "key-1", "password": "s3cret"}
    assert [prefix["prefix"] for prefix in state["prefixes"]] == ["10.0.0.0/31"]
    assert state["address_count"] == 2
    assert {end: port["address"] for end, port in state["ports"].items()} == {
        DCI.ports[0]: "10.0.0.0/31",
        DCI.ports[1]: "10.0.0.1/31",
    }
    assert sorted(state["bystanders"]) == sorted(DCI.bystanders)


def test_parse_state_of_an_empty_result_has_nothing() -> None:
    """No peering, key, prefix, address or port: every reader falls back to empty rather than raising."""
    state = lifecycle.parse_state({}, DCI)
    assert state == {
        "vrfs": {"all": {}, "memberships": {}, "other_demand": []},
        "peerings": {},
        "bystanders": {},
        "key": None,
        "prefixes": [],
        "address_count": 0,
        "ports": {},
    }


# ---------------------------------------------------------------------------
# The polling predicates
# ---------------------------------------------------------------------------


def test_a_complete_session_is_rebuilt_and_not_torn_down() -> None:
    """The before-state satisfies the rebuilt judge and fails the torn-down one."""
    state = _state()
    assert lifecycle.rebuilt_errors(state, DCI) == []
    errors = lifecycle.torn_down_errors(state, DCI)
    assert any("peering ICX-DC12-EQXPA4-B-underlay still exists" in e for e in errors)
    assert any("prefix 10.0.0.0/31" in e for e in errors)
    assert any("2 address(es)" in e for e in errors)
    assert any("still has address" in e for e in errors)


def test_a_torn_down_session_is_torn_down_and_not_rebuilt() -> None:
    """Nothing left of the session, ports and key kept: the torn-down judge is quiet, the rebuilt one is not."""
    state = _torn_down()
    assert lifecycle.torn_down_errors(state, DCI) == []
    errors = lifecycle.rebuilt_errors(state, DCI)
    assert "peering ICX-DC12-EQXPA4-B-underlay is missing" in errors
    assert "0 prefix(es) described 'DCI ICX-DC12-EQXPA4-B', expected exactly 1" in errors
    assert "port bl-dc1212101:Ethernet1/36 has no address" in errors


def test_peerings_still_active_after_the_decommission_keep_the_predicate_false() -> None:
    """The bug: the circuit was decommissioned, the run built nothing, and the old peerings stayed active."""
    state = _torn_down(peerings={name: "active" for name in DCI.peerings})
    errors = lifecycle.torn_down_errors(state, DCI)
    assert errors == [f"peering {name} still exists (active)" for name in sorted(DCI.peerings)]


def test_a_peering_left_in_provisioning_is_not_torn_down() -> None:
    """Any status counts: a peering the generator left behind is still a session."""
    state = _torn_down(peerings={DCI.peerings[1]: "provisioning"})
    assert lifecycle.torn_down_errors(state, DCI) == [f"peering {DCI.peerings[1]} still exists (provisioning)"]


@pytest.mark.parametrize(
    ("kwargs", "fragment"),
    [
        ({"prefixes": 1}, "prefix 10.0.0.0/31 (DCI ICX-DC12-EQXPA4-B) still exists"),
        ({"addresses": 1}, "1 address(es) described"),
        ({"port_addresses": ("10.0.0.0/31", None)}, "port bl-dc1212101:Ethernet1/36 still has address 10.0.0.0/31"),
    ],
)
def test_each_leftover_of_the_session_keeps_the_teardown_predicate_false(kwargs: dict[str, Any], fragment: str) -> None:
    """One leftover is enough: the prefix, an orphan address object, an address still on a port."""
    errors = lifecycle.torn_down_errors(_torn_down(**kwargs), DCI)
    assert len(errors) == 1
    assert fragment in errors[0]


@pytest.mark.parametrize(
    ("kwargs", "fragment"),
    [
        ({"peerings": {DCI.peerings[0]: "active"}}, "peering ICX-DC12-EQXPA4-B-evpn is missing"),
        (
            {"peerings": {DCI.peerings[0]: "active", DCI.peerings[1]: "provisioning"}},
            "ICX-DC12-EQXPA4-B-evpn is provisioning, expected active",
        ),
        ({"prefixes": 2}, "2 prefix(es) described"),
        ({"prefixes": 0}, "0 prefix(es) described"),
        ({"addresses": 1}, "1 address(es) described"),
        ({"port_addresses": ("10.0.0.0/31", None)}, "port eg-pa402:Ethernet1/10 has no address"),
        ({"port_addresses": ("10.0.0.0/31", "10.0.0.0/31")}, "the ports share an address"),
        ({"key": None}, "is keyed with key-1, expected ICX-DC12-EQXPA4-B-dci-key (None)"),
    ],
)
def test_an_incomplete_rebuild_keeps_the_rebuilt_predicate_false(kwargs: dict[str, Any], fragment: str) -> None:
    """Missing or half-active peerings, a duplicate prefix, a missing address, shared or absent addresses, an unkeyed
    session: each leaves the polling predicate false and says why."""
    errors = lifecycle.rebuilt_errors(_state(**kwargs), DCI)
    assert any(fragment in e for e in errors), errors


def test_a_peering_keyed_with_another_secret_is_not_rebuilt() -> None:
    """The sessions must hang on the circuit's own key, not on whatever password a rebuild picked."""
    result = _result(DCI)
    result["RoutingPassword"]["edges"][0]["node"]["id"] = "key-2"
    errors = lifecycle.rebuilt_errors(lifecycle.parse_state(result, DCI), DCI)
    assert any("is keyed with key-1, expected ICX-DC12-EQXPA4-B-dci-key (key-2)" in e for e in errors)


def test_a_missing_port_is_reported_in_both_directions() -> None:
    """A deleted port fails the rebuilt judge and the survivor judge, so a teardown that takes it along is caught."""
    result = _result(DCI)
    result[DCI.port_kind]["edges"].pop(0)
    after = lifecycle.parse_state(result, DCI)
    assert "port bl-dc1212101:Ethernet1/36 does not exist" in lifecycle.rebuilt_errors(after, DCI)
    assert lifecycle.survivor_errors(_state(), after, DCI) == ["port bl-dc1212101:Ethernet1/36 was deleted"]


def test_the_handoff_session_is_judged_like_the_dci() -> None:
    """One peering named -bgp, a Handoff prefix, VLAN sub-interface ends; the data-loaded key is the one to keep."""
    state = _state(HANDOFF)
    assert lifecycle.rebuilt_errors(state, HANDOFF) == []
    assert lifecycle.torn_down_errors(state, HANDOFF)
    gone = _torn_down(HANDOFF)
    assert lifecycle.torn_down_errors(gone, HANDOFF) == []
    assert lifecycle.survivor_errors(state, gone, HANDOFF) == []


# ---------------------------------------------------------------------------
# What must survive
# ---------------------------------------------------------------------------


def test_survivors_hold_when_ports_and_key_are_as_before() -> None:
    """A teardown that leaves both ports and the key alone has nothing to report."""
    assert lifecycle.survivor_errors(_state(), _torn_down(), DCI) == []


def test_a_key_rotated_by_the_rebuild_fails_the_key_assertion() -> None:
    """Same id, new secret: the far side's key changed under it."""
    after = {"id": "key-1", "password": "other"}
    assert lifecycle.key_errors({"id": "key-1", "password": "s3cret"}, after, "k") == [
        "key k was rotated: its secret changed"
    ]
    assert lifecycle.survivor_errors(_state(), _torn_down(key=("key-1", "other")), DCI) == [
        "key ICX-DC12-EQXPA4-B-dci-key was rotated: its secret changed"
    ]


def test_a_key_re_created_by_the_rebuild_fails_the_key_assertion() -> None:
    """A new id (even with the same secret) means the key was deleted and written again."""
    errors = lifecycle.key_errors({"id": "key-1", "password": "s"}, {"id": "key-9", "password": "s"}, "k")
    assert errors == ["key k was re-created: id key-1 -> key-9"]


def test_a_key_deleted_by_the_teardown_fails_the_key_assertion() -> None:
    """The key is untracked on purpose: a cleanup that takes it along is the regression."""
    assert lifecycle.key_errors({"id": "key-1", "password": "s"}, None, "k") == ["key k was deleted"]
    assert lifecycle.survivor_errors(_state(), _torn_down(key=None), DCI) == [
        "key ICX-DC12-EQXPA4-B-dci-key was deleted"
    ]


def test_a_missing_key_before_is_a_precondition_failure() -> None:
    """Nothing to compare with: the test must not pass vacuously."""
    assert lifecycle.key_errors(None, {"id": "key-1", "password": "s"}, "k") == ["key k did not exist before"]


def test_an_unchanged_key_has_no_errors() -> None:
    """Same id, same secret."""
    key = {"id": "key-1", "password": "s"}
    assert lifecycle.key_errors(key, dict(key), "k") == []


def test_other_circuits_sessions_unchanged_have_no_errors() -> None:
    """The bystanders of the before and the after state are the same ids and statuses."""
    assert lifecycle.bystander_errors(_state(), _torn_down()) == []


def test_a_neighbouring_session_deleted_re_created_or_changed_is_reported() -> None:
    """Collateral damage of the cleanup: a deleted session, a new id, another status, an appeared one."""
    before = _state()
    victim, other = sorted(DCI.bystanders)[:2]
    after = copy.deepcopy(before)
    del after["bystanders"][victim]
    after["bystanders"][other] = ("id-new", "active")
    after["bystanders"]["ICX-NEW-bgp"] = ("id-x", "active")
    assert lifecycle.bystander_errors(before, after) == [
        f"session {victim} was deleted",
        "session ICX-NEW-bgp appeared",
        f"session {other} changed: {before['bystanders'][other]} -> ('id-new', 'active')",
    ]


@pytest.mark.parametrize(("alive", "ok"), [(2, True), (1, False), (0, False)])
def test_process_errors_compare_the_surviving_processes(alive: int, ok: bool) -> None:
    """The BGP processes the sessions ran on belong to their devices: all of them must be left."""
    errors = lifecycle.process_errors(2, {"ManagedBGP": {"count": alive}})
    assert (errors == []) is ok


# ---------------------------------------------------------------------------
# Driving the stack
# ---------------------------------------------------------------------------


def _node(status: str) -> MagicMock:
    node = MagicMock()
    node.status.value = status
    node.save = AsyncMock()
    return node


async def test_set_status_changes_saves_and_returns_the_previous_status() -> None:
    """The carrier is fetched on the branch, set and saved; the status it had comes back for the restore."""
    node = _node("active")
    client = MagicMock()
    client.get = AsyncMock(return_value=node)
    previous = await lifecycle.set_status(
        client, ALL_DEMO_BRANCH, "TopologyPhysicalCircuit", "decommissioned", circuit_id__value="DF"
    )
    assert previous == "active"
    assert node.status.value == "decommissioned"
    node.save.assert_awaited_once()
    client.get.assert_awaited_once_with(kind="TopologyPhysicalCircuit", branch=ALL_DEMO_BRANCH, circuit_id__value="DF")


async def test_set_status_to_the_current_status_saves_nothing() -> None:
    """Restoring a node nothing changed (the decommission never went through) is a no-op."""
    node = _node("active")
    client = MagicMock()
    client.get = AsyncMock(return_value=node)
    assert (
        await lifecycle.set_status(client, ALL_DEMO_BRANCH, "TopologyHandoffCircuit", "active", name__value="H")
        == "active"
    )
    node.save.assert_not_awaited()


async def test_wait_for_state_returns_once_the_judge_is_satisfied(monkeypatch: pytest.MonkeyPatch) -> None:
    """The first state still has the session, the second does not: two reads, the second returned."""
    states = [_state(), _torn_down()]
    reads = AsyncMock(side_effect=states)
    monkeypatch.setattr(lifecycle, "fetch_state", reads)
    monkeypatch.setattr(lifecycle, "POLL_INTERVAL", 0)
    result = await lifecycle.wait_for_state(MagicMock(), ALL_DEMO_BRANCH, DCI, lifecycle.torn_down_errors, "removal")
    assert result is states[1]
    assert reads.await_count == 2


async def test_wait_for_state_that_never_settles_fails_with_what_is_still_wrong(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bug's signature: the peerings stay active. The timeout becomes an AssertionError naming them."""
    monkeypatch.setattr(lifecycle, "fetch_state", AsyncMock(return_value=_state()))
    monkeypatch.setattr(lifecycle, "POLL_INTERVAL", 0)
    monkeypatch.setattr(lifecycle, "MAX_ATTEMPTS", 3)
    with pytest.raises(
        AssertionError, match=r"Timeout waiting for removal after 3 attempts[\s\S]*ICX-DC12-EQXPA4-B-evpn still exists"
    ):
        await lifecycle.wait_for_state(MagicMock(), ALL_DEMO_BRANCH, DCI, lifecycle.torn_down_errors, "removal")


class _FakeStack:
    """A client whose one toggled node decides what fetch_state reports: decommissioned means no session."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, reacts: bool) -> None:
        self.node = _node("active")
        self.node.id = "toggled-id"
        self.client = MagicMock()
        self.client.get = AsyncMock(return_value=self.node)
        self.client.execute_graphql = AsyncMock(return_value={"ManagedBGP": {"count": 2}})
        self.statuses: list[str] = []
        self.reacts = reacts
        monkeypatch.setattr(lifecycle, "fetch_state", self.fetch_state)
        monkeypatch.setattr(lifecycle, "wait_for_tasks_completion", AsyncMock())
        self.verify = AsyncMock()
        monkeypatch.setattr(lifecycle, "verify_no_failed_tasks", self.verify)
        monkeypatch.setattr(lifecycle, "POLL_INTERVAL", 0)
        monkeypatch.setattr(lifecycle, "MAX_ATTEMPTS", 2)

    async def fetch_state(self, client: Any, branch: str, subject: Subject) -> State:
        """The session exists unless the toggled node is decommissioned and the generator reacts to it."""
        self.statuses.append(self.node.status.value)
        gone = self.reacts and self.node.status.value == "decommissioned"
        return _torn_down(subject) if gone else _state(subject)


async def test_the_lifecycle_decommissions_waits_restores_and_waits_for_the_rebuild(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: the status is set, the session goes, the status is set back, the session returns."""
    stack = _FakeStack(monkeypatch, reacts=True)
    await lifecycle.decommission_and_restore(
        stack.client, ALL_DEMO_BRANCH, DCI, "TopologyPhysicalCircuit", circuit_id__value="DF"
    )
    assert stack.node.status.value == "active"
    assert stack.node.save.await_count == 2
    assert "decommissioned" in stack.statuses
    stack.verify.assert_awaited_once()


async def test_a_generator_that_ignores_the_status_fails_the_lifecycle_and_the_status_is_restored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bug: the sessions never go. The run fails, naming them, and the `finally` still sets the status back."""
    stack = _FakeStack(monkeypatch, reacts=False)
    with pytest.raises(AssertionError, match="ICX-DC12-EQXPA4-B-underlay still exists"):
        await lifecycle.decommission_and_restore(
            stack.client, ALL_DEMO_BRANCH, DCI, "TopologyPhysicalCircuit", circuit_id__value="DF"
        )
    assert stack.node.status.value == "active"
    assert stack.node.save.await_count == 2
    stack.verify.assert_not_awaited()


async def test_a_lifecycle_that_starts_without_a_session_refuses_to_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """No complete session before: nothing is changed on the branch."""
    stack = _FakeStack(monkeypatch, reacts=True)
    stack.node.status.value = "decommissioned"
    monkeypatch.setattr(lifecycle, "fetch_state", AsyncMock(return_value=_torn_down(DCI)))
    with pytest.raises(AssertionError, match="has no complete session"):
        await lifecycle.decommission_and_restore(
            stack.client, ALL_DEMO_BRANCH, DCI, "TopologyPhysicalCircuit", circuit_id__value="DF"
        )
    stack.node.save.assert_not_awaited()


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_the_module_follows_test_65_and_runs_in_the_30_all_task() -> None:
    """test_66 depends on test_65's last test and the 30_all invoke task lists it after test_65."""
    root = Path(__file__).resolve().parents[2]
    source = (root / "tests/integration/test_66_all_demo_circuit_lifecycle.py").read_text()
    assert '"all_demo_idempotent_topology_reruns"' in source
    idempotency = (root / "tests/integration/test_65_all_demo_idempotency.py").read_text()
    assert 'name="all_demo_idempotent_topology_reruns"' in idempotency
    tasks = (root / "tasks.py").read_text()
    assert tasks.index("test_65_all_demo_idempotency.py") < tasks.index("test_66_all_demo_circuit_lifecycle.py")


# ---------------------------------------------------------------------------
# The VRF memberships of the end devices
# ---------------------------------------------------------------------------

AMNE = lifecycle.subject_for(lifecycle.SOLE_DEMAND_HANDOFF, key=lifecycle.SOLE_DEMAND_KEY)
AM_PROD = "am-vrf-prod"
OTHER_PROD = "dc10-vrf-prod"


def _vrf_result(
    subject: Subject,
    *,
    instances: dict[str, tuple[str, list[str]]],
    namespace: str = "PROD",
    demand: tuple[str, ...] | list[str] = (),
) -> dict[str, Any]:
    """A ``state_query`` result reduced to the VRF part: ``instances`` is {name: (id, member device names)}, all in
    ``namespace``; ``demand`` lists the devices with an address in the circuit's namespace on an interface that is not
    the circuit's own port."""
    return {
        "vrfs": {
            "edges": [
                {
                    "node": {
                        "id": vrf_id,
                        "name": {"value": name},
                        "namespace": {"node": {"name": {"value": namespace}}},
                        "shared_devices": {"edges": [{"node": {"name": {"value": member}}} for member in members]},
                    }
                }
                for name, (vrf_id, members) in instances.items()
            ]
        },
        "addressed_physical": {
            "edges": [
                {
                    "node": {
                        "name": {"value": "Ethernet1/9"},
                        "device": {"node": {"name": {"value": device}}},
                        "ip_address": {"node": {"ip_namespace": {"node": {"name": {"value": subject.namespace}}}}},
                    }
                }
                for device in demand
            ]
        },
        "addressed_virtual": {
            "edges": [
                # The circuit's own sub-interface never counts as other demand.
                {
                    "node": {
                        "name": {"value": port},
                        "role": {"value": "customer"},
                        "device": {"node": {"name": {"value": device}}},
                        "ip_address": {"node": {"ip_namespace": {"node": {"name": {"value": subject.namespace}}}}},
                    }
                }
                for device, port in subject.ports
            ]
        },
    }


def _vrf_state(**kwargs: Any) -> State:
    return {"vrfs": lifecycle.parse_vrfs(_vrf_result(AMNE, **kwargs), AMNE)}


def test_parse_vrfs_finds_every_instance_the_end_memberships_and_the_other_demand() -> None:
    """The circuit's own port is not demand; an address on another interface of an end device is."""
    result = _vrf_result(
        AMNE,
        instances={AM_PROD: ("v1", ["eg-am-ne01", "eg-am-ne02"]), OTHER_PROD: ("v2", ["leaf-1"])},
        demand=["eg-am-ne02"],
    )

    assert lifecycle.parse_vrfs(result, AMNE) == {
        "all": {
            AM_PROD: {"id": "v1", "devices": frozenset({"eg-am-ne01", "eg-am-ne02"})},
            OTHER_PROD: {"id": "v2", "devices": frozenset({"leaf-1"})},
        },
        "memberships": {"eg-am-ne02": AM_PROD},
        "other_demand": ["eg-am-ne02"],
    }
    quiet = _vrf_result(AMNE, instances={AM_PROD: ("v1", ["eg-am-ne02"])})
    assert lifecycle.parse_vrfs(quiet, AMNE)["other_demand"] == []


def test_an_interconnect_has_instances_but_no_membership_to_judge() -> None:
    """The default namespace has no instance of its own: nothing of the circuit's ends is a member."""
    state = lifecycle.parse_vrfs(_vrf_result(DCI, instances={OTHER_PROD: ("v2", ["leaf-1"])}), DCI)

    assert state["memberships"] == {}
    assert set(state["all"]) == {OTHER_PROD}


def test_a_member_with_no_other_demand_leaves_and_the_instance_stays_for_the_others() -> None:
    """Sole demand, other members: the device is gone from the instance, which keeps its id."""
    before = _vrf_state(instances={AM_PROD: ("v1", ["eg-am-ne01", "eg-am-ne02"])})
    left = _vrf_state(instances={AM_PROD: ("v1", ["eg-am-ne01"])})

    assert lifecycle.vrf_down_errors(before, left) == []
    assert "members" in lifecycle.vrf_down_errors(before, before)[0]


def test_the_instance_goes_with_its_last_member() -> None:
    """The last member leaves: the instance is deleted, and still existing is an error."""
    before = _vrf_state(instances={AM_PROD: ("v1", ["eg-am-ne02"])})

    assert lifecycle.vrf_down_errors(before, _vrf_state(instances={})) == []
    assert "still exists" in lifecycle.vrf_down_errors(before, before)[0]


def test_a_member_with_other_demand_stays_a_member_of_the_same_instance() -> None:
    """Other demand: leaving, or the instance deleted or re-created, is an error."""
    before = _vrf_state(instances={AM_PROD: ("v1", ["eg-am-ne02"])}, demand=["eg-am-ne02"])

    assert lifecycle.vrf_down_errors(before, before) == []
    assert "went" in lifecycle.vrf_down_errors(before, _vrf_state(instances={}, demand=["eg-am-ne02"]))[0]
    recreated = _vrf_state(instances={AM_PROD: ("v9", ["eg-am-ne02"])}, demand=["eg-am-ne02"])
    assert "re-created" in lifecycle.vrf_down_errors(before, recreated)[0]


def test_another_instances_change_is_reported_both_ways() -> None:
    """A bystander instance whose members changed or that went during the teardown or the rebuild."""
    before = _vrf_state(instances={AM_PROD: ("v1", ["eg-am-ne02"]), OTHER_PROD: ("v2", ["leaf-1", "leaf-2"])})
    changed = _vrf_state(instances={OTHER_PROD: ("v2", ["leaf-1"])})

    assert any(OTHER_PROD in error for error in lifecycle.vrf_down_errors(before, changed))
    assert any(OTHER_PROD in error for error in lifecycle.vrf_up_errors(before, changed, changed))


def test_the_rebuild_restores_the_deleted_instance_under_a_new_id_and_keeps_a_survivor() -> None:
    """Back (any id) when it was deleted; the same id when it survived the decommission."""
    before = _vrf_state(instances={AM_PROD: ("v1", ["eg-am-ne02"])})
    gone = _vrf_state(instances={})
    survived = _vrf_state(instances={AM_PROD: ("v1", ["eg-am-ne01"])})
    rebuilt = _vrf_state(instances={AM_PROD: ("v2", ["eg-am-ne02"])})

    assert lifecycle.vrf_up_errors(before, gone, rebuilt) == []
    assert "did not come back" in lifecycle.vrf_up_errors(before, gone, gone)[0]
    assert "re-created" in lifecycle.vrf_up_errors(before, survived, rebuilt)[0]
    assert "members" in lifecycle.vrf_up_errors(before, survived, survived)[0]


def test_the_end_devices_of_a_circuit_are_its_ports_devices() -> None:
    """A party's device is an end too; it is simply never a member."""
    assert DCI.end_devices == tuple(sorted({device for device, _ in DCI.ports}))
    assert AMNE.end_devices == ("EXT-AWS-DX-FR2", "eg-am-ne02")
