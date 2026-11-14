"""Unit tests for utils/device_services.py:"""

from __future__ import annotations

from typing import Any

from tests.unit.service_contexts_helpers import LB, make_context, make_far, make_iface, make_member, make_switch
from tests.unit.vrf_instances_helpers import raw_device, raw_footprint, raw_row
from utils.data_cleaning import clean_data
from utils.device_services import overlay_pbr_bindings, pbr_bindings


def _leaf(role: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """A cleaned switch in the pod of DC ``dc-1``, with the given segment rows."""
    return clean_data(raw_device("d1", "sw-1", role, rows=rows))


def _row(row_id: str, vlan: int, *footprints: dict[str, Any], terminated: bool = False) -> dict[str, Any]:
    """A routed PROD row whose segment lists the footprints."""
    return raw_row(row_id, vlan, "PROD", f"seg{vlan}", footprints=list(footprints), terminated=terminated)


def test_segment_svi_is_steered_for_its_footprint_with_a_context() -> None:
    """A routed segment of a footprint in the device's DC that has a serving context is steered for it."""
    leaf = _leaf("leaf", [_row("r1", 100, raw_footprint("fp-1"))])
    assert pbr_bindings(leaf) == {"Vlan100": "fp-1"}


def test_footprint_without_context_or_in_another_dc_is_not_steered() -> None:
    """No serving context, or a footprint standing in another DC, steers nothing."""
    rows = [
        _row("r1", 100, raw_footprint("fp-1", context=False)),
        _row("r2", 101, raw_footprint("fp-2", parent_id="dc-other")),
    ]
    assert pbr_bindings(_leaf("leaf", rows)) == {}


def test_two_footprints_of_one_segment_in_one_dc_take_the_lowest_id() -> None:
    """Both footprints stand in the DC with a context: the lowest id decides, every run."""
    leaf = _leaf("leaf", [_row("r1", 100, raw_footprint("fp-b"), raw_footprint("fp-a"))])
    assert pbr_bindings(leaf) == {"Vlan100": "fp-a"}


def test_terminated_segment_has_no_steering() -> None:
    """A segment terminated on a firewall context has no SVI on the fabric, so nothing to steer."""
    leaf = _leaf(
        "leaf", [_row("r1", 100, raw_footprint("fp-1"), terminated=True), _row("r2", 101, raw_footprint("fp-1"))]
    )
    assert pbr_bindings(leaf) == {"Vlan101": "fp-1"}


def test_border_leaf_steers_no_segment_svi() -> None:
    """Border roles render no segment PBR, so none of their SVIs is bound."""
    assert pbr_bindings(_leaf("border-leaf", [_row("r1", 100, raw_footprint("fp-1"))])) == {}


def _border(ctx: dict[str, Any], role: str = "border-leaf", port_role: str = "firewall") -> dict[str, Any]:
    """A border switch (deployment ``dc-1``) with one service port facing a member of ``ctx``."""
    fw = make_member("fw-1", [ctx], role="internal-firewall")
    cabled = make_switch(
        {"Ethernet1/25": (port_role, [make_far(fw, make_iface("fw-1:ethernet1/1", "ethernet1/1", "uplink"))])}
    )
    device = clean_data(raw_device("d1", "sw-1", role))
    device["service_ports"] = cabled["service_ports"]
    return device


def test_overlay_pbr_steers_the_l3_vni_svi_for_the_footprints_of_the_context() -> None:
    """A PROD footprint served by a context on a border leaf's firewall port binds ``Vlan3900``, a NON-PROD one ``Vlan3901``."""
    ctx = make_context("ctx", 3000, served=(("fp-1", "p"), ("fp-2", "n")))
    assert overlay_pbr_bindings(_border(ctx)) == {"Vlan3900": frozenset({"fp-1"}), "Vlan3901": frozenset({"fp-2"})}


def test_overlay_pbr_includes_the_dedicated_tenant() -> None:
    """A dedicated context serves its tenant even when ``served_deployments`` is empty."""
    ctx = make_context("ctx", 3001, tenant="fp-9")
    assert overlay_pbr_bindings(_border(ctx)) == {"Vlan3900": frozenset({"fp-9"})}


def test_overlay_pbr_ignores_load_balancer_contexts_and_non_border_roles() -> None:
    """Only firewall contexts steer, and only border leaves and spines hold the context ports."""
    assert (
        overlay_pbr_bindings(_border(make_context("lb", 3100, LB, served=(("fp-1", "p"),)), port_role="load-balancer"))
        == {}
    )
    assert overlay_pbr_bindings(_border(make_context("ctx", 3000, served=(("fp-1", "p"),)), role="leaf")) == {}
