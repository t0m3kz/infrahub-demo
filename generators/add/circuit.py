"""Circuit generators for physical and virtual topology circuits.

PhysicalCircuitGenerator
    For each TopologyPhysicalCircuit, validates that customer interfaces are assigned
    (stored in the cardinality-many `interfaces` relationship).

VirtualCircuitGenerator
    For each TopologyVirtualCircuit, validates that endpoint interfaces are assigned
    and logs VNI / tunnel metadata.
"""

from __future__ import annotations

from typing import Any

from utils.data_cleaning import clean_data

from ..common import CommonGenerator


class PhysicalCircuitGenerator(CommonGenerator):
    """Validate a TopologyPhysicalCircuit and its interface bindings."""

    graphql_root_key = "TopologyPhysicalCircuit"

    async def generate(self, data: dict[str, Any]) -> None:
        cleaned = clean_data(data)
        circuits = cleaned.get(self.graphql_root_key, [])
        if not circuits:
            self.logger.error("No TopologyPhysicalCircuit data in GraphQL response")
            return

        circuit = circuits[0]
        circuit_name: str = circuit.get("circuit_id") or circuit.get("name") or ""
        if not circuit_name:
            self.logger.error("TopologyPhysicalCircuit missing circuit_id/name")
            return

        self.logger.info(f"Processing physical circuit: {circuit_name}")

        ifaces = circuit.get("interfaces") or []
        if not ifaces:
            self.logger.warning(f"  Circuit {circuit_name}: no customer interfaces assigned")
        for i, iface in enumerate(ifaces):
            if not (iface or {}).get("id"):
                self.logger.warning(f"  Circuit {circuit_name}: interfaces[{i}] has no interface assigned")
                continue
            device_name = (iface.get("device") or {}).get("name", "?")
            side = "A" if i == 0 else "Z" if i == 1 else str(i)
            self.logger.info(f"  {side}-side customer → {device_name} / {iface.get('name', '?')}")

        for side, key in (("A", "provider_a"), ("Z", "provider_z")):
            port = circuit.get(key) or {}
            if not port.get("id"):
                continue
            device_name = (port.get("device") or {}).get("name", "?")
            self.logger.info(f"  {side}-side provider → {device_name} / {port.get('name', '?')}")

        self.logger.info(f"Physical circuit {circuit_name} — completed")


class VirtualCircuitGenerator(CommonGenerator):
    """Validate and register a TopologyVirtualCircuit."""

    graphql_root_key = "TopologyVirtualCircuit"

    async def generate(self, data: dict[str, Any]) -> None:
        cleaned = clean_data(data)
        circuits = cleaned.get(self.graphql_root_key, [])
        if not circuits:
            self.logger.error("No TopologyVirtualCircuit data in GraphQL response")
            return

        circuit = circuits[0]
        circuit_name: str = circuit.get("name") or ""
        if not circuit_name:
            self.logger.error("TopologyVirtualCircuit missing name")
            return

        self.logger.info(f"Processing virtual circuit: {circuit_name}")

        ifaces = circuit.get("interfaces") or []
        iface_a = ifaces[0] if len(ifaces) > 0 else {}
        iface_z = ifaces[1] if len(ifaces) > 1 else {}

        if not (iface_a or {}).get("id"):
            self.logger.warning(f"  Virtual circuit {circuit_name}: A-side interface not assigned")
        if not (iface_z or {}).get("id"):
            self.logger.warning(f"  Virtual circuit {circuit_name}: Z-side interface not assigned")

        a_device = (iface_a.get("device") or {}).get("name", "?") if iface_a else "?"
        z_device = (iface_z.get("device") or {}).get("name", "?") if iface_z else "?"

        vni = circuit.get("vni")
        tunnel_id = circuit.get("tunnel_id")
        link_type = circuit.get("link_type", "")
        extra = ", ".join(
            filter(
                None,
                [
                    f"vni={vni}" if vni else "",
                    f"tunnel_id={tunnel_id}" if tunnel_id else "",
                    f"type={link_type}" if link_type else "",
                ],
            )
        )
        self.logger.info(
            f"  Virtual circuit {circuit_name}: {a_device} <-> {z_device}" + (f" ({extra})" if extra else "")
        )

        self.logger.info(f"Virtual circuit {circuit_name} — completed")
