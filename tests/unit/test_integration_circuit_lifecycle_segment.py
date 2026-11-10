"""The segment half of test_66 (test_04, and the empty bystander instance), run on raw GraphQL payloads.

The readers consume the results of ``_HOST_CABLES_QUERY`` and ``_SWITCH_VRF_QUERY``; the judges say why the switches are
not in the expected state. A wrong path in a payload, a predicate that passes on an empty state or a comparison that
cannot fail would only show in a 55-minute run, so they are pinned here.
"""

from __future__ import annotations

from typing import Any

import pytest
from graphql import parse

from tests.integration import test_66_all_demo_circuit_lifecycle as lifecycle
from tests.integration.circuit_lifecycle_helpers import (
    _HOST_CABLES_QUERY,
    _SWITCH_VRF_QUERY,
    SEGMENT_NAME,
    SEGMENT_NAMESPACE,
    back_errors,
    parse_switch_vrf_state,
    precondition_errors,
    svis_gone_errors,
    switches_of_host,
    vrf_gone_errors,
)

DC_VRF = "vrf-management-dc11"
LEAVES = ["leaf-a", "leaf-b"]


def _name(value: str) -> dict[str, str]:
    return {"value": value}


def _device(device: str) -> dict[str, Any]:
    return {"node": {"name": _name(device)}}


def _namespace(namespace: str) -> dict[str, Any]:
    return {"node": {"ip_namespace": {"node": {"name": _name(namespace)}}}}


def _cable_port(host: str, ends: list[str | None] | None) -> dict[str, Any]:
    """A host port; ``ends`` are the devices at the cable's endpoints (None: a non-physical endpoint), or no cable."""
    if ends is None:
        return {"device": _device(host), "cable": {"node": None}}
    endpoints = [{"node": {"device": _device(end)} if end else {}} for end in ends]
    return {"device": _device(host), "cable": {"node": {"endpoints": {"edges": endpoints}}}}


def _cables(*ports: dict[str, Any]) -> dict[str, Any]:
    return {"DcimPhysicalInterface": {"edges": [{"node": port} for port in ports]}}


def _vrf(vrf_id: str, namespace: str, members: list[str], name: str = "vrf") -> dict[str, Any]:
    return {
        "id": vrf_id,
        "name": _name(name),
        "security_zone": {"node": {"name": _name(namespace)}},
        "shared_devices": {"edges": [{"node": {"name": _name(member)}} for member in members]},
    }


def _gateway(segment: str) -> str:
    return f"gw-{segment}"


def _svi(device: str, name: str, segment: str | None, vrf_id: str | None, role: str = "svi") -> dict[str, Any]:
    """An interface; ``segment`` names the segment whose anycast gateway it carries (None: no address)."""
    return {
        "id": f"id-{device}-{name}",
        "name": _name(name),
        "role": _name(role),
        "device": _device(device),
        "ip_address": {"node": {"id": _gateway(segment)} if segment else None},
        "zone_capabilities": {
            "edges": [
                {
                    "node": {
                        "__typename": "ManagedVRF",
                        "id": vrf_id,
                        "security_zone": {"node": {"name": _name(SEGMENT_NAMESPACE)}},
                    }
                }
            ]
            if vrf_id
            else []
        },
    }


def _switch_result(
    vrfs: list[dict[str, Any]], virtual: list[dict[str, Any]], physical: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    return {
        "ManagedVxlanSegment": {"edges": [{"node": {"gateway": {"node": {"id": _gateway(SEGMENT_NAME)}}}}]},
        "ManagedVRF": {"edges": [{"node": vrf} for vrf in vrfs]},
        "DcimVirtualInterface": {"edges": [{"node": node} for node in virtual]},
        "DcimPhysicalInterface": {"edges": [{"node": node} for node in physical or []]},
    }


def _healthy() -> dict[str, Any]:
    """Both leaves members of the DC's MANAGEMENT instance, each with one SVI of the segment in it; an instance of
    another namespace is a bystander."""
    return _switch_result(
        [_vrf(DC_VRF, SEGMENT_NAMESPACE, LEAVES), _vrf("vrf-prod", "PROD", LEAVES)],
        [_svi(leaf, "Vlan100", SEGMENT_NAME, DC_VRF) for leaf in LEAVES],
    )


def _state(result: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    return parse_switch_vrf_state(result or _healthy(), LEAVES)


# ---------------------------------------------------------------------------
# The queries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("query", [_HOST_CABLES_QUERY, _SWITCH_VRF_QUERY])
def test_the_segment_queries_parse(query: str) -> None:
    """graphql-core parses both (no schema offline) and they filter on the device names the test passes."""
    parse(query)
    assert "device__name__values" in query


# ---------------------------------------------------------------------------
# switches_of_host
# ---------------------------------------------------------------------------


def test_switches_of_host_are_the_far_ends_of_its_cables() -> None:
    """Both of the host's cabled ports contribute their far end; the host itself is never one of its switches."""
    result = _cables(_cable_port("host-1", ["host-1", "leaf-a"]), _cable_port("host-1", ["leaf-b", "host-1"]))
    assert switches_of_host(result, "host-1") == {"leaf-a", "leaf-b"}


def test_a_port_with_a_null_cable_adds_no_switch() -> None:
    """An uncabled port (cable node null) and a non-physical endpoint (no device) are skipped, not an error."""
    result = _cables(_cable_port("host-1", None), _cable_port("host-1", ["host-1", None, "leaf-a"]))
    assert switches_of_host(result, "host-1") == {"leaf-a"}
    assert switches_of_host({}, "host-1") == set()


def test_switches_of_host_ignore_the_ports_of_another_host() -> None:
    """The query is filtered by a list of hosts: the other host's ports must not leak into this one's switches."""
    result = _cables(_cable_port("host-1", ["host-1", "leaf-a"]), _cable_port("host-2", ["host-2", "leaf-z"]))
    assert switches_of_host(result, "host-1") == {"leaf-a"}
    assert switches_of_host(result, "host-2") == {"leaf-z"}


# ---------------------------------------------------------------------------
# parse_switch_vrf_state
# ---------------------------------------------------------------------------


def test_the_state_of_a_healthy_pair_has_the_instance_and_the_segments_svis() -> None:
    """Membership is the id of the MANAGEMENT instance only (the PROD one is not the segment's namespace)."""
    state = _state()
    assert state == {
        leaf: {"vrf": DC_VRF, "svis": {"Vlan100": {"id": f"id-{leaf}-Vlan100", "vrf": DC_VRF}}, "foreign": []}
        for leaf in LEAVES
    }


def test_a_device_that_is_not_asked_for_is_not_in_the_state() -> None:
    """A member of the instance outside ``devices`` (a border leaf) is ignored."""
    result = _switch_result([_vrf(DC_VRF, SEGMENT_NAMESPACE, [*LEAVES, "border-1"])], [])
    state = parse_switch_vrf_state(result, LEAVES)
    assert sorted(state) == LEAVES
    assert all(facts["vrf"] == DC_VRF for facts in state.values())


def test_an_svi_of_another_segment_in_the_namespace_is_foreign_demand() -> None:
    """An SVI of the segment is listed as such; one of another segment, routed in MANAGEMENT, is other demand."""
    result = _healthy()
    result["DcimVirtualInterface"]["edges"].append({"node": _svi("leaf-a", "Vlan200", "other-segment", DC_VRF)})
    state = _state(result)
    assert sorted(state["leaf-a"]["svis"]) == ["Vlan100"]
    assert state["leaf-a"]["foreign"] == ["Vlan200 (virtual)"]
    assert state["leaf-b"]["foreign"] == []


def test_the_instances_own_l3vni_svi_is_not_foreign_demand() -> None:
    """The role-l3vni SVI (Vlan3903 for MANAGEMENT) is there because the device is a member of the instance."""
    result = _healthy()
    result["DcimVirtualInterface"]["edges"].append({"node": _svi("leaf-a", "Vlan3903", None, DC_VRF, role="l3vni")})
    state = _state(result)
    assert state["leaf-a"]["foreign"] == []
    assert precondition_errors(state) == []


def test_an_svi_without_an_address_is_foreign_not_a_crash() -> None:
    """An SVI that carries no address (no gateway) cannot belong to the segment."""
    result = _healthy()
    result["DcimVirtualInterface"]["edges"].append({"node": _svi("leaf-b", "Vlan300", None, DC_VRF)})
    assert _state(result)["leaf-b"]["foreign"] == ["Vlan300 (virtual)"]


def test_a_non_svi_virtual_interface_and_a_physical_address_in_the_namespace_are_foreign() -> None:
    """A loopback in the namespace's VRF and a routed physical port addressed in it also demand the namespace; one in
    another namespace does not."""
    loopback = _svi("leaf-a", "Loopback10", None, DC_VRF, role="loopback")
    elsewhere = _svi("leaf-b", "Loopback11", None, None, role="loopback")
    physical = [
        {"name": _name("Ethernet1/9"), "device": _device("leaf-b"), "ip_address": _namespace(SEGMENT_NAMESPACE)},
        {"name": _name("Ethernet1/10"), "device": _device("leaf-b"), "ip_address": _namespace("PROD")},
    ]
    result = _healthy()
    result["DcimVirtualInterface"]["edges"] += [{"node": loopback}, {"node": elsewhere}]
    result["DcimPhysicalInterface"]["edges"] += [{"node": node} for node in physical]
    state = _state(result)
    assert state["leaf-a"]["foreign"] == ["Loopback10 (virtual)"]
    assert state["leaf-b"]["foreign"] == ["Ethernet1/9 (physical)"]


def test_an_empty_result_gives_empty_facts_per_device() -> None:
    """No instance, no interface: every asked device is present, a member of nothing."""
    assert parse_switch_vrf_state({}, LEAVES) == {leaf: {"vrf": None, "svis": {}, "foreign": []} for leaf in LEAVES}


# ---------------------------------------------------------------------------
# The judges
# ---------------------------------------------------------------------------


def test_precondition_passes_for_the_healthy_pair() -> None:
    """Both leaves in one instance, SVIs in it, nothing else routing there."""
    assert precondition_errors(_state()) == []
    assert back_errors(_state()) == []


def test_precondition_fails_for_a_switch_outside_the_instance() -> None:
    """One leaf in no instance: reported, and the two leaves are not members of one instance."""
    result = _healthy()
    result["ManagedVRF"]["edges"][0]["node"]["shared_devices"]["edges"].pop()
    errors = precondition_errors(_state(result))
    assert f"leaf-b is a member of no {SEGMENT_NAMESPACE} instance" in errors
    assert any("not members of one" in e for e in errors)


def test_precondition_fails_for_a_switch_without_svis() -> None:
    """The segment must have an SVI on every switch under test."""
    result = _healthy()
    result["DcimVirtualInterface"]["edges"].pop()
    assert precondition_errors(_state(result)) == [f"leaf-b has no SVI of {SEGMENT_NAME}"]


def test_precondition_fails_for_an_svi_outside_the_instance() -> None:
    """An SVI in another VRF than the switch's instance is wrong even when the membership is right."""
    result = _healthy()
    result["DcimVirtualInterface"]["edges"][0]["node"] = _svi("leaf-a", "Vlan100", SEGMENT_NAME, "vrf-elsewhere")
    assert precondition_errors(_state(result)) == [f"leaf-a: an SVI of {SEGMENT_NAME} is not in the instance {DC_VRF}"]


def test_the_segments_svi_is_not_demand_but_a_foreign_svi_is() -> None:
    """The same shape (an SVI in MANAGEMENT) passes when it is the segment's and fails when another segment's."""
    assert precondition_errors(_state()) == []
    result = _healthy()
    result["DcimVirtualInterface"]["edges"].append({"node": _svi("leaf-a", "Vlan200", "other-segment", DC_VRF)})
    assert precondition_errors(_state(result)) == [f"leaf-a has other {SEGMENT_NAMESPACE} demand: Vlan200 (virtual)"]


def test_svis_gone_reports_each_switch_that_still_has_one() -> None:
    """Quiet when the segment's SVIs are gone, whatever the membership; names the remaining SVIs otherwise."""
    assert svis_gone_errors(_state(_switch_result([_vrf(DC_VRF, SEGMENT_NAMESPACE, LEAVES)], []))) == []
    assert svis_gone_errors(_state()) == ["leaf-a still has SVI(s) ['Vlan100']", "leaf-b still has SVI(s) ['Vlan100']"]


def test_vrf_gone_needs_no_svi_and_no_membership() -> None:
    """The membership goes after the SVIs: SVIs left and a membership left are both reported."""
    members_only = _state(_switch_result([_vrf(DC_VRF, SEGMENT_NAMESPACE, LEAVES)], []))
    assert vrf_gone_errors(members_only) == [
        f"leaf-a is still a member of {DC_VRF}",
        f"leaf-b is still a member of {DC_VRF}",
    ]
    assert len(vrf_gone_errors(_state())) == 4
    assert vrf_gone_errors(_state(_switch_result([], []))) == []


def test_back_requires_the_membership_and_the_svis_again() -> None:
    """After the rebuild, a switch with the SVI but no membership is not back."""
    gone = _state(_switch_result([], [_svi("leaf-a", "Vlan100", SEGMENT_NAME, None)]))
    assert back_errors(gone)


# ---------------------------------------------------------------------------
# The empty bystander instance
# ---------------------------------------------------------------------------


def _vrfs(instances: dict[str, tuple[str, list[str]]]) -> dict[str, Any]:
    """The ``parse_vrfs`` shape for {instance: (id, members)}, all members being end devices in the namespace."""
    return {
        "all": {name: {"id": vid, "devices": frozenset(members)} for name, (vid, members) in instances.items()},
        "memberships": {m: name for name, (_, members) in instances.items() for m in members},
        "other_demand": [],
    }


def _fleet(before_devices: list[str], now: list[str] | None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Two states of the instance 'am-prod' (id v1) plus a bystander instance with ``before_devices`` / ``now``."""
    base = {"am-prod": ("v1", ["eg-am-ne01", "eg-am-ne02"])}
    before = {"vrfs": _vrfs({**base, "empty": ("v2", before_devices)})}
    before["vrfs"]["memberships"] = {"eg-am-ne02": "am-prod"}
    after_instances = {"am-prod": ("v1", ["eg-am-ne01"])}
    if now is not None:
        after_instances["empty"] = ("v2", now)
    return before, {"vrfs": _vrfs(after_instances)}


def test_a_bystander_instance_with_no_members_that_stays_is_not_an_error() -> None:
    """An instance with no members in the state is neither 'its last member left' nor 'members remain': unchanged is fine."""
    before, down = _fleet([], [])
    assert lifecycle.vrf_down_errors(before, down) == []


def test_a_bystander_instance_with_no_members_that_goes_is_reported() -> None:
    """The memberless instance is not the leaving device's: its disappearance is collateral damage."""
    before, down = _fleet([], None)
    errors = lifecycle.vrf_down_errors(before, down)
    assert len(errors) == 1
    assert errors == ["empty: it had no members but the instance went"]
