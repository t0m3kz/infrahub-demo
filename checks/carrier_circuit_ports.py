"""Validate the ports a carrier circuit terminates on.

A carrier circuit is a cable-like link: two ends, each a physical port, and a port carries at most one of them. A
``virtual_connection`` (an Equinix Fabric connection or a Megaport VXC) is the exception to both of those rules.
The schema caps the ends (``capabilities``: min_count 2, max_count 2) only when the circuit side is
written. A write from the interface side (``DcimInterface.capabilities`` is declared by the generic
without counts) is never checked against the cap, and deleting a device leaves its circuits with one end, so the
rules the schema cannot hold are held here, on the circuits as a whole:

1. a port is an end of at most one carrier circuit; a ``virtual_connection`` is exempt (a provider port hosts one
   connection per S-tag), the other types on that same port still hold the rule among themselves,
2. both ends are a DcimPhysicalInterface (not a virtual interface or another capability); a ``virtual_connection``
   may also end on a DcimVirtualInterface (the NIC of a provider-hosted appliance, which has no cable),
3. a circuit has exactly two ends.

Session circuits are out of scope (a tunnel port carries several; the one-session-per-port rule of the
dci and handoff circuits is checks/circuit_placement.py's).
"""

from __future__ import annotations

from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from utils.data_cleaning import clean_data
from utils.limits import CIRCUIT_ENDS

PHYSICAL_PORT_KIND = "DcimPhysicalInterface"
VIRTUAL_PORT_KIND = "DcimVirtualInterface"
# The one carrier type that is delivered over provider ports and appliance NICs, many to a port.
VIRTUAL_CONNECTION = "virtual_connection"


def _port_label(end: dict[str, Any]) -> str:
    """The port as ``device:port`` when both are known, else what identifies it."""
    name = end.get("name")
    device = (end.get("device") or {}).get("name")
    if name and device:
        return f"{device}:{name}"
    return str(name or end.get("id") or "<unknown>")


class CheckCarrierCircuitPorts(InfrahubCheck):
    """Carrier circuits: two physical ends each, no port shared between circuits."""

    query = "carrier_circuit_ports"

    def validate(self, data: Any) -> None:
        circuits = clean_data(data).get("TopologyCarrierCircuit") or []
        circuits_by_port: dict[str, tuple[str, list[str]]] = {}

        for circuit in circuits:
            name = circuit.get("circuit_id") or circuit.get("id") or "<unnamed-circuit>"
            ends = [end for end in circuit.get("capabilities") or [] if isinstance(end, dict)]
            shared_ports = circuit.get("circuit_type") == VIRTUAL_CONNECTION
            allowed = (PHYSICAL_PORT_KIND, VIRTUAL_PORT_KIND) if shared_ports else (PHYSICAL_PORT_KIND,)

            if len(ends) != CIRCUIT_ENDS:
                self._error(
                    circuit,
                    f"Carrier circuit '{name}' has {len(ends)} end(s), a carrier circuit terminates on "
                    f"exactly {CIRCUIT_ENDS} ports.",
                )

            for end in ends:
                if end.get("typename") not in allowed:
                    self._error(
                        circuit,
                        f"Carrier circuit '{name}' has an end that is not a {' or '.join(allowed)}: "
                        f"'{_port_label(end)}' is a {end.get('typename')}.",
                    )
                    continue
                if shared_ports:
                    continue
                port_circuits = circuits_by_port.setdefault(str(end.get("id")), (_port_label(end), []))[1]
                if name not in port_circuits:
                    port_circuits.append(name)

        for port_id, (label, port_circuits) in circuits_by_port.items():
            if len(port_circuits) > 1:
                self.log_error(
                    message=f"Port '{label}' is an end of {len(port_circuits)} carrier circuits "
                    f"({', '.join(sorted(map(str, port_circuits)))}), a port terminates at most one.",
                    object_id=port_id,
                    object_type="DcimPhysicalInterface",
                )

    def _error(self, circuit: dict[str, Any], message: str) -> None:
        """Log one finding against the circuit."""
        self.log_error(message=message, object_id=circuit.get("id"), object_type="TopologyCarrierCircuit")
