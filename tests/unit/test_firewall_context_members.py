"""checks/common.py validate_context_members: the members of a firewall context are exactly one HA pair.

A context holds no HA edge (its members are its ``shared_devices``), so the schema cannot tie the two: a device in an HA
domain enforces only contexts whose members are that domain's members, a standalone device only contexts of which it is
the one member. Shapes are cleaned query data (queries/config/firewall.gql).
"""

from typing import Any

import pytest

from checks.common import validate_context_members


def _ha(name: str, members: list[str]) -> dict[str, Any]:
    """A cleaned ManagedFirewallHA capability with its member devices."""
    return {"typename": "ManagedFirewallHA", "name": name, "capabilities": [{"name": member} for member in members]}


def _context(name: str, members: list[str]) -> dict[str, Any]:
    """A cleaned ManagedFirewallContext shared capability with its members."""
    return {"typename": "ManagedFirewallContext", "name": name, "shared_devices": [{"name": m} for m in members]}


def _device(name: str, *, ha: dict[str, Any] | None, contexts: list[dict[str, Any]]) -> dict[str, Any]:
    """A cleaned firewall device: its individual capabilities (the HA) and its shared ones (the contexts)."""
    return {"name": name, "capabilities": [ha] if ha else [], "shared_capabilities": contexts}


def test_a_context_of_exactly_the_ha_pair_passes() -> None:
    """Both members of the HA are the context's members, in any order."""
    device = _device(
        "fw-01", ha=_ha("fw-ha", ["fw-01", "fw-02"]), contexts=[_context("shared-fw-ha-prod", ["fw-02", "fw-01"])]
    )
    assert validate_context_members(device) == []


@pytest.mark.parametrize(
    "members",
    [["fw-01"], ["fw-01", "fw-03"], ["fw-01", "fw-02", "fw-03"]],
    ids=["one member short", "a member of another pair", "a third member"],
)
def test_a_context_that_is_not_the_ha_pair_is_an_error(members: list[str]) -> None:
    """Any other member set than the HA's is reported, naming the context, the HA and both sets."""
    device = _device("fw-01", ha=_ha("fw-ha", ["fw-01", "fw-02"]), contexts=[_context("shared-fw-ha-prod", members)])
    (error,) = validate_context_members(device)
    assert "shared-fw-ha-prod" in error and "'fw-ha'" in error
    assert str(sorted(members)) in error and "['fw-01', 'fw-02']" in error


def test_a_standalone_firewall_may_enforce_a_context_alone() -> None:
    """With no HA domain, the device is the context's one member."""
    device = _device("fw-solo", ha=None, contexts=[_context("dedicated-solo-prod", ["fw-solo"])])
    assert validate_context_members(device) == []


def test_a_standalone_firewall_in_a_two_member_context_is_an_error() -> None:
    """A device with no HA cannot share a context: the pair it would form is not modelled."""
    device = _device("fw-solo", ha=None, contexts=[_context("shared-x-prod", ["fw-solo", "fw-other"])])
    (error,) = validate_context_members(device)
    assert "shared-x-prod" in error and "in no HA domain" in error


def test_each_context_is_judged_on_its_own() -> None:
    """One bad context among good ones is the only error."""
    ha = _ha("fw-ha", ["fw-01", "fw-02"])
    good, bad = _context("a-prod", ["fw-01", "fw-02"]), _context("b-prod", ["fw-01"])
    errors = validate_context_members(_device("fw-01", ha=ha, contexts=[good, bad]))
    assert len(errors) == 1 and "b-prod" in errors[0]


def test_a_device_with_no_context_has_nothing_to_report() -> None:
    """No shared firewall context, nothing to judge; a VRF membership is not a context."""
    vrf = {"typename": "ManagedVRF", "name": "dc1-vrf-prod"}
    device = {"name": "fw-01", "capabilities": [_ha("fw-ha", ["fw-01", "fw-02"])], "shared_capabilities": [vrf]}
    assert validate_context_members(device) == []
