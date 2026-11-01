"""The pure teardown planner (generators/helpers/provider_teardown.py): what is deleted, in what order, what is kept."""

from __future__ import annotations

from generators.helpers.provider_teardown import (
    DECOMMISSION,
    DELETE,
    KIND_ADDRESS,
    KIND_CARRIER,
    KIND_INTERFACE,
    KIND_PEERING,
    KIND_PREFIX,
    KIND_SESSION,
    CarrierEndFacts,
    CarrierFacts,
    EndFacts,
    PrefixFacts,
    SessionFacts,
    owns_interface,
    plan_carrier_teardown,
    plan_session_teardown,
    plan_stale_end_teardown,
    sessions_to_remove,
)

VIRTUAL = "DcimVirtualInterface"
PHYSICAL = "DcimPhysicalInterface"


def _end(
    end_id: str,
    name: str,
    *,
    parent: str | None = "port",
    parent_name: str | None = None,
    kind: str = VIRTUAL,
    address: str | None = None,
    prefix: str | None = None,
    peerings: tuple[str, ...] = (),
    others: tuple[str, ...] = (),
    children: tuple[str, ...] = (),
) -> EndFacts:
    """An end interface; ``parent_name`` defaults to the part of the name before the dot."""
    return EndFacts(
        id=end_id,
        name=name,
        kind=kind,
        parent_id=parent,
        parent_name=parent_name if parent_name is not None else name.split(".")[0],
        address_id=address,
        address_prefix_id=prefix,
        peering_ids=frozenset(peerings),
        other_session_ids=frozenset(others),
        child_ids=frozenset(children),
    )


def _session(session_id: str = "s1", *ends: EndFacts, link_prefix: str | None = None) -> SessionFacts:
    """A session circuit named ``C-L1`` with the given ends."""
    return SessionFacts(session_id, "C-L1", link_prefix, ends)


def _full(session_id: str = "s1", suffix: str = "") -> tuple[SessionFacts, dict[str, PrefixFacts]]:
    """A session with two sub-interfaces, an address each, one peering and the /31 they sit in."""
    near = _end(
        f"near{suffix}", "Ethernet1/1.100", address=f"a1{suffix}", prefix=f"p{suffix}", peerings=(f"bgp{suffix}",)
    )
    far = _end(
        f"far{suffix}",
        "vnic-C.100",
        parent="nic",
        address=f"a2{suffix}",
        prefix=f"p{suffix}",
        peerings=(f"bgp{suffix}",),
    )
    prefixes = {f"p{suffix}": PrefixFacts(f"p{suffix}", frozenset({f"a1{suffix}", f"a2{suffix}"}), frozenset())}
    return _session(session_id, near, far), prefixes


def _order(plan) -> list[tuple[str, str]]:  # noqa: ANN001
    return [(step.kind, step.id) for step in plan.steps]


def test_a_full_session_is_torn_down_in_a_dependency_free_order() -> None:
    """Peering, addresses, prefix, circuit, interfaces: nothing references something already deleted."""
    session, prefixes = _full()

    plan = plan_session_teardown([session], prefixes)

    assert _order(plan) == [
        (KIND_PEERING, "bgp"),
        (KIND_ADDRESS, "a1"),
        (KIND_ADDRESS, "a2"),
        (KIND_PREFIX, "p"),
        (KIND_SESSION, "s1"),
        (KIND_INTERFACE, "near"),
        (KIND_INTERFACE, "far"),
    ]
    assert all(step.action == DELETE for step in plan.steps)
    assert plan.kept == ()


def test_a_provider_assigned_link_prefix_is_never_deleted_but_its_addresses_are() -> None:
    """The authored range (link_prefix) stays; the addresses add_virtual_circuit put in it go."""
    session, prefixes = _full()
    session = SessionFacts(session.id, session.name, "p", session.ends)

    plan = plan_session_teardown([session], prefixes)

    assert KIND_PREFIX not in {step.kind for step in plan.steps}
    assert plan.ids(KIND_ADDRESS) == ["a1", "a2"]
    assert ("p", "link prefix of C-L1 is authored (assigned by the provider)") in plan.kept


def test_a_prefix_holding_another_address_or_a_child_is_kept() -> None:
    """Only a prefix that holds nothing but the removed addresses is released to the pool."""
    session, _ = _full()
    other_address = {"p": PrefixFacts("p", frozenset({"a1", "a2", "someone-else"}), frozenset())}
    child = {"p": PrefixFacts("p", frozenset({"a1", "a2"}), frozenset({"child"}))}

    assert KIND_PREFIX not in {s.kind for s in plan_session_teardown([session], other_address).steps}
    assert KIND_PREFIX not in {s.kind for s in plan_session_teardown([session], child).steps}


def test_a_physical_port_or_a_nic_at_the_end_is_never_touched() -> None:
    """Only a sub-interface or a Tunnel of a virtual interface is a leg interface."""
    port = _end("port1", "Ethernet1/1", kind=PHYSICAL, parent=None, address="a1")
    nic = _end("nic1", "vnic-C", parent=None, address="a2")

    plan = plan_session_teardown([_session("s1", port, nic)])

    assert plan.ids(KIND_INTERFACE) == []
    assert plan.ids(KIND_ADDRESS) == []
    assert plan.ids(KIND_SESSION) == ["s1"]
    assert {i for i, _ in plan.kept} == {"port1", "nic1"}


def test_an_interface_used_by_another_session_or_a_sub_interface_is_kept_and_held() -> None:
    """The plan keeps it and says so, so the caller's group cleanup does not delete it either."""
    shared = _end("shared", "Ethernet1/1.100", address="a1", others=("s2",))
    carrying = _end("carrying", "vnic-C.100", parent="nic", address="a2", children=("sub",))

    plan = plan_session_teardown([_session("s1", shared, carrying)])

    assert plan.ids(KIND_INTERFACE) == []
    assert plan.ids(KIND_ADDRESS) == []
    assert set(plan.held) == {"shared", "carrying"}


def test_a_tunnel_pair_is_torn_down_like_sub_interfaces() -> None:
    """A Tunnel<n> needs no dot in its name."""
    a = _end("t1", "Tunnel1", address="a1", prefix="p", peerings=("bgp",))
    b = _end("t2", "Tunnel3", parent="nic", address="a2", prefix="p")
    prefixes = {"p": PrefixFacts("p", frozenset({"a1", "a2"}), frozenset())}

    plan = plan_session_teardown([_session("s1", a, b)], prefixes)

    assert plan.ids(KIND_INTERFACE) == ["t1", "t2"]


def test_two_connections_on_one_stub_each_remove_only_their_own_legs() -> None:
    """The service edge and its Loopback0 are not ends; a second connection's NIC sub-interface is not in this plan."""
    mine, my_prefixes = _full("s1", "-a")

    plan = plan_session_teardown([mine], my_prefixes)

    assert {step.id for step in plan.steps if step.kind == KIND_INTERFACE} == {"near-a", "far-a"}
    assert not any("loopback" in step.id.lower() for step in plan.steps)


def test_a_half_torn_state_plans_only_what_is_left_and_a_rerun_plans_nothing() -> None:
    """The peering and addresses are gone already: the plan is the circuit and the interfaces; with nothing, empty."""
    near = _end("near", "Ethernet1/1.100")
    far = _end("far", "vnic-C.100", parent="nic")

    plan = plan_session_teardown([_session("s1", near, far)])

    assert _order(plan) == [(KIND_SESSION, "s1"), (KIND_INTERFACE, "near"), (KIND_INTERFACE, "far")]
    assert plan_session_teardown([]).steps == ()


def test_a_prefix_shared_by_two_removed_sessions_is_deleted_once() -> None:
    """Defence: the same prefix reached from two ends of two sessions is one step."""
    first, prefixes = _full("s1")
    second = _session("s2", _end("near2", "Ethernet1/2.100", address="a1", prefix="p"))

    plan = plan_session_teardown([first, second], prefixes)

    assert plan.ids(KIND_PREFIX) == ["p"]


def test_owns_interface_needs_a_virtual_interface_with_a_matching_parent() -> None:
    """A name that does not start with the parent's name plus a dot is not a leg interface."""
    assert owns_interface(_end("x", "Ethernet1/1.100"))
    assert owns_interface(_end("x", "Tunnel12", parent=None))
    assert not owns_interface(_end("x", "Loopback0", parent=None))
    assert not owns_interface(_end("x", "Ethernet1/1.100", parent_name="Ethernet2/2"))
    assert not owns_interface(_end("x", "Ethernet1/1.100", kind=PHYSICAL))


def test_stale_ends_lose_the_interface_but_never_the_address() -> None:
    """A kept session reuses its link addresses for its new ends, so only the old interfaces go."""
    stale = _end("old", "Ethernet1/1.100", address="a1")
    shared = _end("busy", "Ethernet1/2.100", address="a2", others=("s9",))

    plan = plan_stale_end_teardown([stale, shared])

    assert _order(plan) == [(KIND_INTERFACE, "old")]
    assert plan.held == ("busy",)


def test_sessions_off_the_plan_are_removed_and_all_of_them_when_decommissioned() -> None:
    """A link that left the plan is removed; a decommissioned connection removes every session."""
    existing = [("s1", "C-L1"), ("s2", "C-L2")]

    assert sessions_to_remove(existing, ["C-L1"], False) == ["s2"]
    assert sessions_to_remove(existing, ["C-L1"], True) == ["s1", "s2"]


def _end_facts(name: str, kind: str = VIRTUAL, **kwargs: frozenset[str]) -> CarrierEndFacts:
    return CarrierEndFacts(id=f"id-{name}", name=name, kind=kind, **kwargs)


def _carrier(
    name: str = "C-XC",
    *,
    circuit_id: str | None = None,
    status: str = "provisioning",
    connections: tuple[str, ...] = ("conn",),
    ends: tuple[CarrierEndFacts, ...] | None = None,
) -> CarrierFacts:
    ends = ends if ends is not None else (_end_facts("Ethernet1/1", PHYSICAL), _end_facts("vnic-C"))
    return CarrierFacts(
        "car", name, circuit_id if circuit_id is not None else name, status, frozenset(connections), ends
    )


def test_a_generated_untouched_carrier_is_deleted_with_its_nic() -> None:
    """Provisioning with the placeholder circuit_id: nobody ordered it, so it goes with the NIC the generator made."""
    plan = plan_carrier_teardown("conn", "C", [_carrier()])

    assert [(s.action, s.kind) for s in plan.steps] == [(DELETE, KIND_CARRIER), (DELETE, KIND_INTERFACE)]
    assert plan.steps[1].id == "id-vnic-C"


def test_a_generated_carrier_a_person_filled_in_is_only_decommissioned() -> None:
    """A real provider id is the record of an order: kept, status decommissioned, its NIC stays."""
    plan = plan_carrier_teardown("conn", "C", [_carrier(circuit_id="EQX-123")])

    assert [(s.action, s.kind, s.id) for s in plan.steps] == [(DECOMMISSION, KIND_CARRIER, "car")]
    active = plan_carrier_teardown("conn", "C", [_carrier(status="active")])
    assert [s.action for s in active.steps] == [DECOMMISSION]


def test_an_authored_or_reused_carrier_is_left_alone() -> None:
    """A carrier of another name was not created by the generator."""
    plan = plan_carrier_teardown("conn", "C", [_carrier(name="AWS-DX-1", circuit_id="dx-1")])

    assert plan.steps == ()
    assert plan.kept[0][0] == "car"


def test_a_carrier_listed_by_another_connection_or_still_in_use_is_kept() -> None:
    """Shared underlay: another connection, another carrier on the end, a surviving sub-interface or session."""
    other = plan_carrier_teardown("conn", "C", [_carrier(connections=("conn", "other"))])
    on_port = plan_carrier_teardown(
        "conn", "C", [_carrier(ends=(_end_facts("Ethernet1/1", PHYSICAL, other_carrier_ids=frozenset({"x"})),))]
    )
    sub = plan_carrier_teardown("conn", "C", [_carrier(ends=(_end_facts("vnic-C", child_ids=frozenset({"sub"})),))])
    session = plan_carrier_teardown("conn", "C", [_carrier(ends=(_end_facts("vnic-C", session_ids=frozenset({"s"})),))])

    assert other.steps == on_port.steps == sub.steps == session.steps == ()


def test_a_decommissioned_carrier_is_not_touched_again() -> None:
    """A rerun after the carrier was decommissioned writes nothing."""
    plan = plan_carrier_teardown("conn", "C", [_carrier(circuit_id="EQX-123", status="decommissioned")])

    assert plan.steps == ()


def test_no_carrier_no_steps() -> None:
    """A connection that never had a carrier (a vpn)."""
    assert plan_carrier_teardown("conn", "C", []).steps == ()
