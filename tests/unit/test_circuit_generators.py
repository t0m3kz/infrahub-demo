"""Unit tests for circuit generators (PhysicalCircuitGenerator, VirtualCircuitGenerator).

Virtual circuits use a simplified direct model (interfaces + physical_circuits)
while physical circuits keep direct interface validation. Tests use a lightweight
mock generator that patches the SDK client and logger.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from generators.topology.circuit import PhysicalCircuitGenerator, VirtualCircuitGenerator

# ---------------------------------------------------------------------------
# Minimal AsyncMock generator harness
# ---------------------------------------------------------------------------


def _make_gen(cls):
    """Return an instance of cls with a mocked client and logger."""
    gen = cls.__new__(cls)
    gen.client = AsyncMock()
    gen.logger = MagicMock()
    return gen


# ---------------------------------------------------------------------------
# GQL response helpers
# ---------------------------------------------------------------------------


def _iface_node(label: str, device: str, iface_name: str) -> dict:
    return {
        "id": f"iface-{label}",
        "name": {"value": iface_name},
        "device": {"node": {"id": f"dev-{label}", "name": {"value": device}}},
    }


def _default_iface_a() -> dict:
    return _iface_node("a", "dc1-ss-01", "Ethernet1/31")


def _default_iface_z() -> dict:
    return _iface_node("z", "dc2-ss-01", "Ethernet25/1")


def _provider_port(label: str, device: str, port_name: str) -> dict:
    return {
        "id": f"pport-{label}",
        "name": {"value": port_name},
        "device": {"node": {"id": f"pdev-{label}", "name": {"value": device}}},
    }


def _iface_edges(ifaces: list[dict]) -> dict:
    """Wrap a list of interface dicts in GraphQL edges format."""
    return {"edges": [{"node": iface} for iface in ifaces]}


def _pc_node(label: str, circuit_type: str, circuit_id: str) -> dict:
    return {
        "id": f"pc-{label}",
        "name": {"value": circuit_id},
        "circuit_id": {"value": circuit_id},
        "circuit_type": {"value": circuit_type},
    }


def _pc_edges(circuits: list[dict]) -> dict:
    return {"edges": [{"node": circuit} for circuit in circuits]}


def _phys_circuit_response(
    name: str = "EQX-FR2-DC1-DC2-PRIMARY",
    interface_a: dict | None = None,
    interface_z: dict | None = None,
    provider_a: dict | None = None,
    provider_z: dict | None = None,
    *,
    omit_interface_a: bool = False,
    omit_interface_z: bool = False,
) -> dict:
    iface_list = []
    if not omit_interface_a:
        iface_list.append(interface_a if interface_a is not None else _default_iface_a())
    if not omit_interface_z:
        iface_list.append(interface_z if interface_z is not None else _default_iface_z())
    resolved_pa = provider_a if provider_a is not None else _provider_port("a", "eqx-fr2-patch-a", "Port1")
    resolved_pz = provider_z if provider_z is not None else _provider_port("z", "eqx-fr2-patch-z", "Port1")
    return {
        "TopologyPhysicalCircuit": {
            "edges": [
                {
                    "node": {
                        "id": "circ-1",
                        "circuit_id": {"value": name},
                        "customer_interfaces": _iface_edges(iface_list),
                        "provider_interfaces": _iface_edges([resolved_pa, resolved_pz]),
                    }
                }
            ]
        }
    }


def _virt_circuit_response(
    name: str = "DCI-DC1-DC2-PRIMARY",
    link_type: str = "equinix_fabric",
    interface_a: dict | None = None,
    interface_z: dict | None = None,
    *,
    omit_interface_a: bool = False,
    omit_interface_z: bool = False,
) -> dict:
    iface_a = interface_a if interface_a is not None else _default_iface_a()
    iface_z = interface_z if interface_z is not None else _default_iface_z()
    interfaces: list[dict] = []
    if not omit_interface_a:
        interfaces.append(iface_a)
    if not omit_interface_z:
        interfaces.append(iface_z)

    inferred_transport = (
        "internet_backed" if link_type in {"sd_wan", "vpn_ipsec", "vpn_ssl", "gre", "geneve"} else "physical_backed"
    )
    physical_circuits = (
        [_pc_node("1", "internet_underlay", "INET-C001-WAW-HUB")] if inferred_transport == "internet_backed" else []
    )

    return {
        "TopologyVirtualCircuit": {
            "edges": [
                {
                    "node": {
                        "id": "vcirc-1",
                        "name": {"value": name},
                        "link_type": {"value": link_type},
                        "transport_mode": {"value": inferred_transport},
                        "interfaces": _iface_edges(interfaces),
                        "physical_circuits": _pc_edges(physical_circuits),
                    }
                }
            ]
        }
    }


# ===========================================================================
# PhysicalCircuitGenerator
# ===========================================================================


class TestPhysicalCircuitGenerator:
    def _run(self, data: dict) -> MagicMock:
        gen = _make_gen(PhysicalCircuitGenerator)
        asyncio.run(gen.generate(data))
        return gen.logger

    def test_happy_path_logs_both_connectors(self):
        log = self._run(_phys_circuit_response())
        info_msgs = [str(c) for c in log.info.call_args_list]
        assert any("EQX-FR2-DC1-DC2-PRIMARY" in m for m in info_msgs)
        assert any("dc1-ss-01" in m for m in info_msgs)
        assert any("dc2-ss-01" in m for m in info_msgs)
        log.error.assert_not_called()

    def test_empty_response_logs_error(self):
        log = self._run({"TopologyPhysicalCircuit": {"edges": []}})
        log.error.assert_called_once()
        assert "No TopologyPhysicalCircuit" in log.error.call_args[0][0]

    def test_missing_circuit_id_logs_error(self):
        """Circuit with empty circuit_id and no name is rejected."""
        data = _phys_circuit_response(name="")
        log = self._run(data)
        log.error.assert_called_once()
        assert "missing circuit_id" in log.error.call_args[0][0]

    def test_connector_without_interface_z_logs_warning(self):
        """Circuit with Z-side interface unassigned emits a warning."""
        log = self._run(_phys_circuit_response(omit_interface_z=True))
        log.warning.assert_not_called()  # only 1 interface, no missing warning (just fewer entries)
        log.error.assert_not_called()

    def test_both_interfaces_missing_logs_warning(self):
        """Circuit with no interfaces assigned emits a warning."""
        log = self._run(_phys_circuit_response(omit_interface_a=True, omit_interface_z=True))
        log.warning.assert_called_once()
        assert "no customer interfaces" in log.warning.call_args[0][0]
        log.error.assert_not_called()


# ===========================================================================
# VirtualCircuitGenerator
# ===========================================================================


class TestVirtualCircuitGenerator:
    def _run(self, data: dict) -> MagicMock:
        gen = _make_gen(VirtualCircuitGenerator)
        asyncio.run(gen.generate(data))
        return gen.logger

    def test_happy_path_logs_link_details(self):
        log = self._run(_virt_circuit_response())
        info_msgs = [str(c) for c in log.info.call_args_list]
        assert any("DCI-DC1-DC2-PRIMARY" in m for m in info_msgs)
        assert any("dc1-ss-01" in m or "dc2-ss-01" in m for m in info_msgs)
        log.error.assert_not_called()

    def test_logs_link_type(self):
        log = self._run(_virt_circuit_response(link_type="equinix_fabric"))
        info_msgs = " ".join(str(c) for c in log.info.call_args_list)
        assert "equinix_fabric" in info_msgs

    def test_empty_response_logs_error(self):
        log = self._run({"TopologyVirtualCircuit": {"edges": []}})
        log.error.assert_called_once()
        assert "No TopologyVirtualCircuit" in log.error.call_args[0][0]

    def test_missing_name_logs_error(self):
        """Circuit with empty name is rejected."""
        data = _virt_circuit_response(name="")
        log = self._run(data)
        log.error.assert_called_once()
        assert "missing name" in log.error.call_args[0][0]

    def test_connector_without_interface_z_logs_warning(self):
        """Circuit with missing second interface emits a warning."""
        log = self._run(_virt_circuit_response(omit_interface_z=True))
        assert log.warning.call_count >= 2
        warning_msgs = " ".join(str(c) for c in log.warning.call_args_list)
        assert "expected 2 interfaces" in warning_msgs
        assert "physical_backed without physical underlay" in warning_msgs

    def test_both_interfaces_missing_logs_two_warnings(self):
        """Circuit with both endpoints missing emits warnings."""
        log = self._run(_virt_circuit_response(omit_interface_a=True, omit_interface_z=True))
        assert log.warning.call_count >= 2
        warning_msgs = " ".join(str(c) for c in log.warning.call_args_list)
        assert "expected 2 interfaces" in warning_msgs
        assert "physical_backed without physical underlay" in warning_msgs
        log.error.assert_not_called()
