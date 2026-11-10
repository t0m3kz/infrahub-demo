"""The footprint a routed SVI is steered for, whether the device steers (and so has a ManagedPBR), and rule S4."""

from __future__ import annotations

from typing import Any

from checks.vrf_instances import missing_pbr_errors, svi_content_errors, unneeded_pbr_warnings
from tests.unit.vrf_instances_helpers import facts_of, raw_device, raw_footprint, raw_row, raw_svi


def _device(rows: list[dict[str, Any]], svis: list[dict[str, Any]], *, pbr: bool = False) -> Any:
    return facts_of(raw_device("d1", "leaf-1", rows=rows, interfaces=svis, pbr=pbr))


def test_a_routed_svi_is_steered_for_the_footprint_of_its_dc_that_has_a_context() -> None:
    """The footprint in the device's hosting DC with a serving context decides."""
    rows = [raw_row("r1", 100, "PROD", footprints=[raw_footprint("fp-a")])]
    facts = _device(rows, [raw_svi("svi-1", 100, "seg")])
    assert facts.pbr_footprints_by_name() == {"Vlan100": "fp-a"}
    assert facts.steers()


def test_a_footprint_in_another_dc_or_without_a_context_does_not_steer() -> None:
    """A stretched segment lists the footprint of each DC; only the local one with a context counts."""
    rows = [
        raw_row("r1", 100, "PROD", "a", footprints=[raw_footprint("fp-far", "dc-other")]),
        raw_row("r2", 101, "PROD", "b", footprints=[raw_footprint("fp-nocontext", context=False)]),
        raw_row("r3", 102, "PROD", "c"),
    ]
    svis = [raw_svi("svi-1", 100, "a"), raw_svi("svi-2", 101, "b"), raw_svi("svi-3", 102, "c")]
    facts = _device(rows, svis)
    assert facts.pbr_footprints_by_name() == {"Vlan100": None, "Vlan101": None, "Vlan102": None}
    assert not facts.steers()


def test_the_lowest_footprint_id_decides_when_a_segment_has_two_in_the_dc() -> None:
    """The same answer every run."""
    rows = [raw_row("r1", 100, "PROD", footprints=[raw_footprint("fp-b"), raw_footprint("fp-a")])]
    assert _device(rows, [raw_svi("svi-1", 100, "seg")]).pbr_footprints_by_name() == {"Vlan100": "fp-a"}


def test_the_managed_pbr_of_the_device_is_read_from_its_capabilities() -> None:
    """A ManagedPBR among the device's individual capabilities is what says it steers."""
    rows = [raw_row("r1", 100, "PROD", footprints=[raw_footprint("fp-a")])]
    assert _device(rows, [], pbr=True).pbr_ids == ("pbr-d1",)
    assert _device(rows, []).pbr_ids == ()


def test_rule_i_a_steering_device_needs_its_managed_pbr_and_only_then() -> None:
    """Steering without the capability renders no PBR (ERROR); the capability without steering is a leftover."""
    steering = [raw_row("r1", 100, "PROD", footprints=[raw_footprint("fp-a")])]
    idle = [raw_row("r1", 100, "PROD", footprints=[raw_footprint("fp-a", context=False)])]
    assert "has no ManagedPBR" in missing_pbr_errors(_device(steering, []))[0]
    assert missing_pbr_errors(_device(steering, [], pbr=True)) == []
    assert unneeded_pbr_warnings(_device(steering, [], pbr=True)) == []
    assert "steers no SVI" in unneeded_pbr_warnings(_device(idle, [], pbr=True))[0]
    assert unneeded_pbr_warnings(_device(idle, [])) == []


def test_rule_s4_accepts_the_vrf_of_an_svi_but_not_another_capability() -> None:
    """Its VRF instance is the one capability an SVI may have."""
    rows = [raw_row("r1", 100, "PROD", footprints=[raw_footprint("fp-a")])]
    in_vrf = _device(
        rows,
        [raw_svi("svi-1", 100, "seg", "vrf-1", "PROD", capabilities=[{"__typename": "ManagedVRF", "id": "vrf-1"}])],
    )
    assert svi_content_errors(in_vrf) == []
    other = {"__typename": "ManagedBGPPeering", "id": "x"}
    with_other = _device(rows, [raw_svi("svi-1", 100, "seg", capabilities=[other])])
    assert "a capability other than its VRF" in svi_content_errors(with_other)[0]


def test_a_border_leaf_steers_nothing_until_its_templates_render_pbr() -> None:
    """The SVI of a stretched segment on a border leaf has a footprint with a context, but the border templates render
    no PBR: it is not steered, so it gets no ManagedPBR."""
    rows = [raw_row("r1", 100, "PROD", footprints=[raw_footprint("fp-a")])]
    leaf = facts_of(raw_device("d1", "leaf-1", rows=rows, interfaces=[raw_svi("svi-1", 100, "seg")]))
    border = facts_of(
        raw_device("d1", "bl-1", role="border-leaf", rows=rows, interfaces=[raw_svi("svi-1", 100, "seg")])
    )
    assert leaf.pbr_footprints_by_name() == {"Vlan100": "fp-a"}
    assert border.pbr_footprints_by_name() == {"Vlan100": None}


def test_a_terminated_segment_is_no_svi_and_so_not_steered() -> None:
    """A segment terminated on a firewall stays L2 on the fabric: no routed row, no SVI, no steering."""
    rows = [raw_row("r1", 100, "PROD", terminated=True, footprints=[raw_footprint("fp-a")])]
    facts = facts_of(raw_device("d1", "leaf-1", rows=rows))
    assert facts.pbr_footprints_by_name() == {}


def _firewall_port(
    name: str, tenant: str | None, served: list[str], namespace: str = "PROD", ctx_id: str = "ctx-1"
) -> dict[str, Any]:
    """A service port facing a firewall member of one context: a leg in the tenant VRF and one in INTERNET."""
    from tests.unit.vrf_instances_helpers import raw_service_port

    return raw_service_port(
        name,
        "firewall",
        "ManagedFirewallContext",
        3000,
        ("internal-firewall", namespace, "prod"),
        ("internal-firewall", "INTERNET", "internet"),
        context_id=ctx_id,
        tenant=tenant,
        served=tuple((fp, "p") for fp in served),
    )


def _border(ports: list[dict[str, Any]], role: str = "border-leaf") -> Any:
    return facts_of(raw_device("d1", "bl-1", role=role, service_ports=ports))


def test_a_border_leaf_steers_the_footprints_its_contexts_serve_through_the_l3_vni_svi() -> None:
    """The shared context serves two footprints and a dedicated one its tenant: all land on the PROD L3 VNI SVI."""
    facts = _border(
        [
            _firewall_port("Ethernet1/25", None, ["fp-a", "fp-b"]),
            _firewall_port("Ethernet1/26", "fp-c", [], ctx_id="ctx-2"),
        ]
    )
    assert facts.pbr_overlay_footprints_by_name() == {"Vlan3900": frozenset({"fp-a", "fp-b", "fp-c"})}


def test_only_a_border_role_steers_the_overlay_ingress() -> None:
    """A leaf has no context on a service port; any other role gets no overlay steering."""
    assert _border([_firewall_port("Ethernet1/25", None, ["fp-a"])], role="leaf").pbr_overlay_footprints_by_name() == {}
    assert _border([], role="border-leaf").pbr_overlay_footprints_by_name() == {}
