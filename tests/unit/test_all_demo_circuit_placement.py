"""The real CheckCircuitPlacement rules over payloads built from the 30_all object files.

``tests/unit/simulators/circuit_data_payloads.py`` shapes the YAML the demo loads like the response of
``queries/validation/circuit_placement.gql`` (ports with the carrier circuits and cables found on them, devices
with their BGP processes, places, namespaces, VIF VLANs), adding what the generators build at load time. The check
then runs unchanged: this is the offline proof that the rules and the shipped data agree. The tests that break the
data on purpose show the proof is not vacuous.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from checks.circuit_placement import CheckCircuitPlacement
from tests.integration.all_demo_circuit_constants import ALL_DEMO_VIRTUAL_CIRCUITS
from tests.unit.simulators.circuit_data_payloads import (
    CARRIER,
    HANDOFF,
    INTERCONNECT,
    SESSION,
    TUNNEL,
    build_payloads,
)
from utils.circuit_underlay import derived_underlay, has_carrier
from utils.data_cleaning import clean_data

_VIRTUAL_TYPES = (TUNNEL, INTERCONNECT, HANDOFF)


def _findings(payloads: dict[str, Any]) -> list[dict[str, Any]]:
    """Run the check over the payloads and return what it logged (message, object id and kind)."""
    check = cast(Any, CheckCircuitPlacement.__new__(CheckCircuitPlacement))
    findings: list[dict[str, Any]] = []
    check.log_error = lambda **kwargs: findings.append(kwargs)
    check.validate(payloads)
    return findings


def _virtual_circuits(payloads: dict[str, Any]) -> dict[str, tuple[str, dict[str, Any]]]:
    """Circuit name -> (circuit_type, cleaned circuit) for every session circuit of the payloads."""
    cleaned = clean_data(payloads)
    return {circuit["name"]: (circuit["circuit_type"], circuit) for circuit in cleaned.get(SESSION) or []}


def test_the_shipped_data_passes_every_placement_rule() -> None:
    """Zero findings: every physical, tunnel, interconnect and handoff circuit of 30_all satisfies the check."""
    assert [finding["message"] for finding in _findings(build_payloads())] == []


def test_the_payloads_hold_every_circuit_the_expectations_name() -> None:
    """No circuit is silently missing from the simulation: the virtual ones are exactly the expected ones."""
    payloads = build_payloads()
    assert set(_virtual_circuits(payloads)) == set(ALL_DEMO_VIRTUAL_CIRCUITS)
    assert {kind for kind, _ in _virtual_circuits(payloads).values()} == set(_VIRTUAL_TYPES)
    names = {circuit["name"] for circuit in clean_data(payloads)[CARRIER]}
    expected_carriers = {circuit for expected in ALL_DEMO_VIRTUAL_CIRCUITS.values() for circuit in expected["carrier"]}
    assert expected_carriers <= names


@pytest.mark.parametrize("circuit_name", sorted(ALL_DEMO_VIRTUAL_CIRCUITS))
def test_each_circuit_has_the_expected_kind_ends_locations_and_derived_carrier(circuit_name: str) -> None:
    """The simulated circuit is the expected one, and its carrier is derived from its ports (never listed)."""
    expected = ALL_DEMO_VIRTUAL_CIRCUITS[circuit_name]
    kind, circuit = _virtual_circuits(build_payloads())[circuit_name]

    assert kind == expected["circuit_type"]
    assert sorted((p["device"]["name"], p["name"]) for p in circuit["capabilities"]) == sorted(expected["ends"])
    assert sorted(location["name"] for location in circuit["locations"]) == sorted(expected["locations"])
    assert sorted(carrier["name"] for carrier in derived_underlay(circuit)) == sorted(expected["carrier"])


@pytest.mark.parametrize(
    "circuit_name", sorted(n for n, e in ALL_DEMO_VIRTUAL_CIRCUITS.items() if e["circuit_type"] != TUNNEL)
)
def test_a_session_circuit_has_the_carried_ends_the_generator_requires(circuit_name: str) -> None:
    """add_virtual_circuit builds a session only when BOTH ends of an Interconnect, or AT LEAST ONE end of a Handoff,
    have a carrier (a carrier circuit, or a cable at the port or its parent): the same rule, over the same ports."""
    kind, circuit = _virtual_circuits(build_payloads())[circuit_name]
    carried = [has_carrier(port) for port in circuit["capabilities"]]
    assert len(carried) == 2
    assert all(carried) if kind == INTERCONNECT else any(carried)


def test_session_circuits_share_no_port() -> None:
    """One port, one session: no port is an end of two Interconnect or Handoff circuits (the generator refuses it)."""
    seen: dict[str, str] = {}
    for circuit_name, (kind, circuit) in _virtual_circuits(build_payloads()).items():
        if kind == TUNNEL:
            continue
        for port in circuit["capabilities"]:
            assert port["id"] not in seen, f"{port['id']}: {seen[port['id']]} and {circuit_name}"
            seen[port["id"]] = circuit_name


def test_without_the_cables_the_cable_carried_handoffs_are_reported() -> None:
    """The gateway handoff and the Fabric hop to AM-NE have only a cable under them: without it the check says so."""
    messages = [finding["message"] for finding in _findings(build_payloads(without_cables=True))]
    reported = {circuit for circuit in ALL_DEMO_VIRTUAL_CIRCUITS if any(f"'{circuit}'" in m for m in messages)}
    assert reported == {"HANDOFF-FR2-SDWAN-GW1-PROD", "HANDOFF-FR2-AMNE-PROD"}


def test_without_the_megaport_cross_connect_its_handoff_is_reported() -> None:
    """HANDOFF-PA4-MEGAPORT-PROD is carried by XC-EQXPA4-MEGAPORT on the parent ports only."""
    messages = [finding["message"] for finding in _findings(build_payloads(without_circuits=["XC-EQXPA4-MEGAPORT"]))]
    assert len(messages) == 1 and "HANDOFF-PA4-MEGAPORT-PROD" in messages[0]


def test_without_the_partner_internet_access_the_partner_tunnel_is_reported() -> None:
    """The tunnel's partner end must ride an internet circuit with an end at an ISP or partner topology."""
    messages = [finding["message"] for finding in _findings(build_payloads(without_circuits=["INET-ACME-COGENT"]))]
    assert len(messages) == 1
    assert "C001-PARTNER-ACME-FR2" in messages[0] and "EXT-ACME-EDGE1:et-0/0/0" in messages[0]


def test_without_the_gateway_access_every_tunnel_is_reported() -> None:
    """Every tunnel ends on the gateway's eth1, which rides INET-LUMEN-EQXFR2-GW: the six tunnels are reported."""
    findings = _findings(build_payloads(without_circuits=["INET-LUMEN-EQXFR2-GW"]))
    tunnels = {name for name, expected in ALL_DEMO_VIRTUAL_CIRCUITS.items() if expected["circuit_type"] == TUNNEL}
    assert len(tunnels) == 6
    assert {name for name in tunnels if any(f"'{name}'" in f["message"] for f in findings)} == tunnels
