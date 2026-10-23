"""Validate the ports a physical circuit terminates on.

A physical circuit is a cable-like link: two ends, each a physical port, and a port carries at most one of them.
The schema caps the ends (``capabilities``: min_count 2, max_count 2) only when the circuit side is
written. A write from the interface side (``DcimInterface.capabilities`` is declared by the generic
without counts) is never checked against the cap, and deleting a device leaves its circuits with one end, so the
rules the schema cannot hold are held here, on the circuits as a whole:

1. a port is an end of at most one physical circuit,
2. both ends are a DcimPhysicalInterface (not a virtual interface or another capability),
3. a circuit has exactly two ends.

Virtual circuits are out of scope (a tunnel port carries several; the one-session-per-port rule of the
Interconnect and Handoff circuits is checks/circuit_placement.py's).
"""

from __future__ import annotations

from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from utils.data_cleaning import clean_data

PHYSICAL_PORT_KIND = "DcimPhysicalInterface"
CIRCUIT_ENDS = 2
"""A physical circuit joins exactly two ports, one at each end."""


def _port_label(end: dict[str, Any]) -> str:
    """The port as ``device:port`` when both are known, else what identifies it."""
    name = end.get("name")
    device = (end.get("device") or {}).get("name")
    if name and device:
        return f"{device}:{name}"
    return str(name or end.get("id") or "<unknown>")


class CheckPhysicalCircuitPorts(InfrahubCheck):
    """Physical circuits: two physical ends each, no port shared between circuits."""

    query = "physical_circuit_ports"

    def validate(self, data: Any) -> None:
        circuits = clean_data(data).get("TopologyPhysicalCircuit") or []
        circuits_by_port: dict[str, tuple[str, list[str]]] = {}

        for circuit in circuits:
            name = circuit.get("circuit_id") or circuit.get("id") or "<unnamed-circuit>"
            ends = [end for end in circuit.get("capabilities") or [] if isinstance(end, dict)]

            if len(ends) != CIRCUIT_ENDS:
                self._error(
                    circuit,
                    f"Physical circuit '{name}' has {len(ends)} end(s), a physical circuit terminates on "
                    f"exactly {CIRCUIT_ENDS} ports.",
                )

            for end in ends:
                if end.get("typename") != PHYSICAL_PORT_KIND:
                    self._error(
                        circuit,
                        f"Physical circuit '{name}' has an end that is not a {PHYSICAL_PORT_KIND}: "
                        f"'{_port_label(end)}' is a {end.get('typename')}.",
                    )
                    continue
                port_circuits = circuits_by_port.setdefault(str(end.get("id")), (_port_label(end), []))[1]
                if name not in port_circuits:
                    port_circuits.append(name)

        for port_id, (label, port_circuits) in circuits_by_port.items():
            if len(port_circuits) > 1:
                self.log_error(
                    message=f"Port '{label}' is an end of {len(port_circuits)} physical circuits "
                    f"({', '.join(sorted(map(str, port_circuits)))}), a port terminates at most one.",
                    object_id=port_id,
                    object_type="DcimPhysicalInterface",
                )

    def _error(self, circuit: dict[str, Any], message: str) -> None:
        """Log one finding against the circuit."""
        self.log_error(message=message, object_id=circuit.get("id"), object_type="TopologyPhysicalCircuit")
