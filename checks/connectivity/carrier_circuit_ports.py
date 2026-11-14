"""Validate the ports a carrier circuit ends on: two physical ends, a port in at most one circuit.

Exemptions (virtual connection, internet attachment, THE INTERNET's interface): see AGENTS.md Circuit placement.
"""

from __future__ import annotations

from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from utils.circuit_underlay import INTERNET_INTERFACE_KIND
from utils.data_cleaning import clean_data
from utils.limits import CIRCUIT_ENDS

PHYSICAL_PORT_KIND = "DcimPhysicalInterface"
VIRTUAL_PORT_KIND = "DcimVirtualInterface"
PROVIDER_INTERFACE_KIND = "TopologyProviderInterface"
VIRTUAL_CONNECTION = "virtual_connection"
INTERNET_ACCESS = "internet"


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
            if shared_ports:
                allowed = (PHYSICAL_PORT_KIND, VIRTUAL_PORT_KIND, PROVIDER_INTERFACE_KIND)
            elif circuit.get("circuit_type") == INTERNET_ACCESS:
                allowed = (PHYSICAL_PORT_KIND, PROVIDER_INTERFACE_KIND)
            else:
                allowed = (PHYSICAL_PORT_KIND,)

            if len(ends) != CIRCUIT_ENDS:
                self._error(
                    circuit,
                    f"Carrier circuit '{name}' has {len(ends)} end(s), a carrier circuit terminates on "
                    f"exactly {CIRCUIT_ENDS} ports.",
                )

            for end in ends:
                if end.get("typename") not in (*allowed, INTERNET_INTERFACE_KIND):
                    self._error(
                        circuit,
                        f"Carrier circuit '{name}' has an end that is not a {' or '.join(allowed)}: "
                        f"'{_port_label(end)}' is a {end.get('typename')}.",
                    )
                    continue
                if end.get("typename") == INTERNET_INTERFACE_KIND or (
                    shared_ports and end.get("typename") != PROVIDER_INTERFACE_KIND
                ):
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
