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
    """Validate a TopologyPhysicalCircuit and its customer/provider interfaces."""

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

        ifaces = circuit.get("customer_interfaces") or []
        if not ifaces:
            self.logger.warning(f"  Circuit {circuit_name}: no customer interfaces assigned")
        for i, iface in enumerate(ifaces):
            if not (iface or {}).get("id"):
                self.logger.warning(f"  Circuit {circuit_name}: customer_interfaces[{i}] has no interface assigned")
                continue
            device_name = (iface.get("device") or {}).get("name", "?")
            side = "A" if i == 0 else "Z" if i == 1 else str(i)
            self.logger.info(f"  {side}-side customer → {device_name} / {iface.get('name', '?')}")

        provider_ifaces = circuit.get("provider_interfaces") or []
        for i, port in enumerate(provider_ifaces):
            if not (port or {}).get("id"):
                continue
            device_name = (port.get("device") or {}).get("name", "?")
            side = "A" if i == 0 else "Z" if i == 1 else str(i)
            self.logger.info(f"  {side}-side provider → {device_name} / {port.get('name', '?')}")

        self.logger.info(f"Physical circuit {circuit_name} — completed")


class VirtualCircuitGenerator(CommonGenerator):
    """Validate and register a TopologyVirtualCircuit.

    Validates a simplified direct model:
    - technical interfaces are attached directly to the virtual circuit
    - underlay physical circuits are attached directly to the virtual circuit
    - transport mode aligns with available underlay mappings
    """

    graphql_root_key = "TopologyVirtualCircuit"

    _INTERNET_LINK_TYPES = {"sd_wan", "vpn_ipsec", "vpn_ssl", "gre", "geneve"}
    _PHYSICAL_BACKED_LINK_TYPES = {
        "direct_connect_aws",
        "express_route_azure",
        "interconnect_gcp",
        "fast_connect_oracle",
        "equinix_fabric",
        "megaport",
        "packetfabric",
        "mpls_l3vpn",
    }

    @classmethod
    def _infer_transport_mode(cls, link_type: str) -> str:
        if link_type in cls._INTERNET_LINK_TYPES:
            return "internet_backed"
        if link_type in cls._PHYSICAL_BACKED_LINK_TYPES:
            return "physical_backed"
        return "provider_virtual_only"

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

        interfaces = circuit.get("interfaces") or []
        if len(interfaces) != 2:
            self.logger.warning(f"  Virtual circuit {circuit_name}: expected 2 interfaces, found {len(interfaces)}")
        if interfaces:
            iface_summary = " | ".join(
                f"{((iface.get('device') or {}).get('name') or '?')}/{iface.get('name', '?')}" for iface in interfaces
            )
            self.logger.info(f"  Interfaces: {iface_summary}")

        link_type = circuit.get("link_type", "")
        transport_mode = circuit.get("transport_mode") or self._infer_transport_mode(link_type)
        if not circuit.get("transport_mode"):
            self.logger.info(
                f"  transport_mode not set, inferred={transport_mode} from link_type={link_type or 'unknown'}"
            )

        physical_circuits = circuit.get("physical_circuits") or []
        if physical_circuits:
            summary = ", ".join(
                f"{(pc.get('circuit_id') or pc.get('name') or '?')}[{(pc.get('circuit_type') or '?')}]"
                for pc in physical_circuits
            )
            self.logger.info(f"  Underlay circuits: {summary}")

        underlay_types = {((pc.get("circuit_type") or "").lower()) for pc in physical_circuits}
        if transport_mode == "internet_backed":
            if not physical_circuits:
                self.logger.warning(
                    f"  Virtual circuit {circuit_name}: internet_backed without physical underlay mapping; "
                    "consider internet_underlay physical underlay for traceability"
                )
            elif "internet_underlay" not in underlay_types:
                self.logger.warning(
                    f"  Virtual circuit {circuit_name}: internet_backed should reference at least one "
                    "underlay circuit_type=internet_underlay"
                )
        elif transport_mode == "physical_backed":
            if not physical_circuits:
                self.logger.warning(f"  Virtual circuit {circuit_name}: physical_backed without physical underlay")
        elif transport_mode == "provider_virtual_only" and physical_circuits:
            self.logger.info(
                f"  Virtual circuit {circuit_name}: provider_virtual_only with optional physical mapping present"
            )

        iface_a = interfaces[0] if interfaces else {}
        iface_z = interfaces[1] if len(interfaces) > 1 else {}
        a_device = (iface_a.get("device") or {}).get("name", "?") if iface_a else "?"
        z_device = (iface_z.get("device") or {}).get("name", "?") if iface_z else "?"

        vni = circuit.get("vni")
        tunnel_id = circuit.get("tunnel_id")
        extra = ", ".join(
            filter(
                None,
                [
                    f"vni={vni}" if vni else "",
                    f"tunnel_id={tunnel_id}" if tunnel_id else "",
                    f"type={link_type}" if link_type else "",
                    f"transport={transport_mode}" if transport_mode else "",
                ],
            )
        )
        self.logger.info(
            f"  Virtual circuit {circuit_name}: {a_device} <-> {z_device}" + (f" ({extra})" if extra else "")
        )

        self.logger.info(f"Virtual circuit {circuit_name} — completed")
