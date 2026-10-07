"""Consistency of the circuit data the circuit generators run on.

add_circuit / add_virtual_circuit (generators/topology/circuit.py) only run
for members of their target groups, and build the session a circuit's
peering_role asks for. The loader accepts a circuit outside its group or a
dci circuit the generator would refuse, so both only surface as a missing
session or a failed task after the 30_all load — and a hand-written peering
left next to a generated one is a duplicate session to the same neighbour.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

_DEMOS = Path(__file__).parents[2] / "data" / "demos"
_BOOTSTRAP_GROUPS = Path(__file__).parents[2] / "data" / "bootstrap" / "00_groups.yml"
_TARGET_GROUPS = {"TopologyPhysicalCircuit": "physical_circuits", "TopologyVirtualCircuit": "virtual_circuits"}
_ONRAMP = _DEMOS / "30_all" / "08_interconnects" / "01_colo_onramp"
_DCI_CIRCUITS = {"DF-DC10-EQXFR2", "DF-DC11-EQXFR2", "DF-DC12-EQXPA4"}


def _objects(root: Path, kind: str) -> list[tuple[Path, dict[str, Any]]]:
    """(file, entry) for every data entry of `kind` under root."""
    entries: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(root.rglob("*.yml")):
        for doc in yaml.safe_load_all(path.read_text()):
            if isinstance(doc, dict) and doc.get("kind") == "Object" and doc["spec"]["kind"] == kind:
                entries.extend((path, entry) for entry in doc["spec"]["data"])
    return entries


def _circuit_name(entry: dict[str, Any]) -> str:
    return entry.get("circuit_id") or entry["name"]


@pytest.mark.parametrize("kind", sorted(_TARGET_GROUPS))
def test_every_circuit_is_in_its_generator_target_group(kind: str) -> None:
    """Without membership the generator never runs for the circuit."""
    outside = [
        f"{path.relative_to(_DEMOS)}: {_circuit_name(entry)}"
        for path, entry in _objects(_DEMOS, kind)
        if _TARGET_GROUPS[kind] not in (entry.get("member_of_groups") or [])
    ]
    assert not outside, f"{kind} outside {_TARGET_GROUPS[kind]}: {outside}"


def test_circuit_target_groups_are_generator_groups() -> None:
    """Target groups are CoreGeneratorGroups, like the other generator targets."""
    generator_groups = {
        entry["name"]
        for doc in yaml.safe_load_all(_BOOTSTRAP_GROUPS.read_text())
        if doc and doc["spec"]["kind"] == "CoreGeneratorGroup"
        for entry in doc["spec"]["data"]
    }
    assert set(_TARGET_GROUPS.values()) <= generator_groups


def test_dark_fibres_are_the_dci_circuits() -> None:
    """peering_role is explicit: exactly the three DC <-> cage fibres are dci."""
    dci = {
        _circuit_name(e)
        for _, e in _objects(_DEMOS / "30_all", "TopologyPhysicalCircuit")
        if e.get("peering_role") == "dci"
    }
    assert dci == _DCI_CIRCUITS


@pytest.mark.parametrize("circuit_id", sorted(_DCI_CIRCUITS))
def test_dci_circuit_has_a_border_leaf_and_a_cage_edge_end(circuit_id: str) -> None:
    """add_circuit refuses anything but two endpoints on two devices, one of them a DC device."""
    circuit = next(e for _, e in _objects(_ONRAMP, "TopologyPhysicalCircuit") if e["circuit_id"] == circuit_id)
    ends = [tuple(i) for i in (circuit.get("customer_interfaces") or []) + (circuit.get("provider_interfaces") or [])]
    devices = sorted(device for device, _ in ends)

    assert len(set(ends)) == 2
    assert devices[0].startswith("bl-dc") and devices[1].startswith("eg-")


def test_no_hand_written_dci_session_or_addressing_is_left() -> None:
    """The generator owns the DCI sessions, their /127s and the interface addressing."""
    assert not [
        e["name"] for _, e in _objects(_DEMOS / "30_all", "ManagedBGPPeering") if e.get("peering_role") == "dci"
    ]
    assert not _objects(_ONRAMP, "DcimPhysicalInterface")
    assert not [e["prefix"] for _, e in _objects(_ONRAMP, "IpamPrefix") if e["prefix"].endswith("/127")]


def test_generated_peering_names_do_not_collide_with_data() -> None:
    """DCI-<circuit_id> / <virtual circuit>-bgp share the peering/circuit name space."""
    names = {e["name"] for _, e in _objects(_DEMOS, "ManagedBGPPeering")}
    names |= {_circuit_name(e) for kind in _TARGET_GROUPS for _, e in _objects(_DEMOS, kind)}
    generated = {f"DCI-{c}" for c in _DCI_CIRCUITS}
    generated |= {f"{e['name']}-bgp" for _, e in _objects(_DEMOS, "TopologyVirtualCircuit")}

    assert not generated & names
