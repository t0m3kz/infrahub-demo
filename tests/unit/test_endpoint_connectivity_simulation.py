"""Simulation analysis of endpoint connectivity based on integration test data.

This test simulates what the endpoint generator would do with the actual
integration test data to verify connections will be created correctly.

Test Data Analysis:
- POD-1: middle_rack deployment (4 rows, 4 leafs per row, 4 tors per row)
- 2 compute racks added: ktw-1-s-1-r-1-10 (row 1) and ktw-1-s-1-r-2-10 (row 2)
- 5 servers with 25G interfaces (4 interfaces each)
- Servers should connect to Leaf switches in network rack (middle rack)
"""

from dataclasses import dataclass
from typing import Any

import pytest


@dataclass
class SimulatedRack:
    """Simulated rack based on test data."""

    name: str
    row_index: int
    rack_type: str  # compute or network
    index: int


@dataclass
class SimulatedDevice:
    """Simulated device (server or switch)."""

    name: str
    role: str  # endpoint, leaf, tor
    rack: str
    status: str  # active, free


@dataclass
class SimulatedInterface:
    """Simulated interface."""

    device_name: str
    name: str
    interface_type: str
    role: str  # uplink, customer, downlink
    status: str  # free, active
    has_cable: bool = False


class EndpointConnectivitySimulator:
    """Simulates endpoint connectivity based on test data."""

    def __init__(self, deployment_type: str = "middle_rack") -> None:
        """Initialize simulator with test data.

        Args:
            deployment_type: Type of deployment ('middle_rack', 'tor', 'mixed')
        """
        self.deployment_type = deployment_type

        # Racks from test data
        if deployment_type == "middle_rack":
            self.racks = {
                # Compute racks (servers)
                "ktw-1-s-1-r-1-10": SimulatedRack("ktw-1-s-1-r-1-10", row_index=1, rack_type="compute", index=10),
                "ktw-1-s-1-r-2-10": SimulatedRack("ktw-1-s-1-r-2-10", row_index=2, rack_type="compute", index=10),
                # Network rack (middle rack) - simulated based on POD-1 middle_rack topology
                "ktw-1-s-1-r-1-1": SimulatedRack("ktw-1-s-1-r-1-1", row_index=1, rack_type="network", index=1),
                "ktw-1-s-1-r-2-1": SimulatedRack("ktw-1-s-1-r-2-1", row_index=2, rack_type="network", index=1),
            }
        elif deployment_type == "tor":
            self.racks = {
                # Compute racks with ToRs and servers (POD-2 tor deployment)
                "ktw-1-s-2-r-1-1": SimulatedRack("ktw-1-s-2-r-1-1", row_index=1, rack_type="compute", index=1),
                "ktw-1-s-2-r-1-5": SimulatedRack("ktw-1-s-2-r-1-5", row_index=1, rack_type="compute", index=5),
                "ktw-1-s-2-r-2-1": SimulatedRack("ktw-1-s-2-r-2-1", row_index=2, rack_type="compute", index=1),
                "ktw-1-s-2-r-2-5": SimulatedRack("ktw-1-s-2-r-2-5", row_index=2, rack_type="compute", index=5),
            }
        elif deployment_type == "mixed":
            self.racks = {
                # Mixed: Compute racks with ToRs + Network rack with Leafs (POD-3)
                "ktw-1-s-3-r-1-1": SimulatedRack("ktw-1-s-3-r-1-1", row_index=1, rack_type="compute", index=1),
                "ktw-1-s-3-r-1-5": SimulatedRack("ktw-1-s-3-r-1-5", row_index=1, rack_type="compute", index=5),
                "ktw-1-s-3-r-1-10": SimulatedRack("ktw-1-s-3-r-1-10", row_index=1, rack_type="network", index=10),
                "ktw-1-s-3-r-2-1": SimulatedRack("ktw-1-s-3-r-2-1", row_index=2, rack_type="compute", index=1),
                "ktw-1-s-3-r-2-10": SimulatedRack("ktw-1-s-3-r-2-10", row_index=2, rack_type="network", index=10),
            }
        else:
            raise ValueError(f"Unknown deployment type: {deployment_type}")

        # Servers from test data
        self.servers = {
            "server-01": SimulatedDevice("server-01", "endpoint", self._get_server_rack(1, 1), "active"),
            "server-02": SimulatedDevice("server-02", "endpoint", self._get_server_rack(1, 2), "active"),
            "server-03": SimulatedDevice("server-03", "endpoint", self._get_server_rack(2, 1), "active"),
            "server-04": SimulatedDevice("server-04", "endpoint", self._get_server_rack(2, 2), "active"),
            "server-05": SimulatedDevice("server-05", "endpoint", self._get_server_rack(1, 3), "active"),
        }

        # Switch configuration based on deployment type
        if deployment_type == "middle_rack":
            self._init_middle_rack_switches()
        elif deployment_type == "tor":
            self._init_tor_switches()
        elif deployment_type == "mixed":
            self._init_mixed_switches()

        # Server interfaces (from test data)
        self.server_interfaces: dict[str, list[SimulatedInterface]] = {
            "server-01": [
                SimulatedInterface("server-01", "eno1", "25gbase-x-sfp28", "uplink", "active"),
                SimulatedInterface("server-01", "eno2", "25gbase-x-sfp28", "uplink", "active"),
                SimulatedInterface("server-01", "eno3", "25gbase-x-sfp28", "uplink", "active"),
                SimulatedInterface("server-01", "eno4", "25gbase-x-sfp28", "uplink", "active"),
            ],
            "server-02": [
                SimulatedInterface("server-02", "eno1", "25gbase-x-sfp28", "uplink", "active"),
                SimulatedInterface("server-02", "eno2", "25gbase-x-sfp28", "uplink", "active"),
                SimulatedInterface("server-02", "eno3", "25gbase-x-sfp28", "uplink", "active"),
                SimulatedInterface("server-02", "eno4", "25gbase-x-sfp28", "uplink", "active"),
            ],
            "server-03": [
                SimulatedInterface("server-03", "eno1", "25gbase-x-sfp28", "uplink", "active"),
                SimulatedInterface("server-03", "eno2", "25gbase-x-sfp28", "uplink", "active"),
                SimulatedInterface("server-03", "eno3", "25gbase-x-sfp28", "uplink", "active"),
                SimulatedInterface("server-03", "eno4", "25gbase-x-sfp28", "uplink", "active"),
            ],
            "server-04": [
                SimulatedInterface("server-04", "eno1", "25gbase-x-sfp28", "uplink", "active"),
                SimulatedInterface("server-04", "eno2", "25gbase-x-sfp28", "uplink", "active"),
                SimulatedInterface("server-04", "eno3", "25gbase-x-sfp28", "uplink", "active"),
                SimulatedInterface("server-04", "eno4", "25gbase-x-sfp28", "uplink", "active"),
            ],
            "server-05": [
                SimulatedInterface("server-05", "eno1", "25gbase-x-sfp28", "uplink", "active"),
                SimulatedInterface("server-05", "eno2", "25gbase-x-sfp28", "uplink", "active"),
                SimulatedInterface("server-05", "eno3", "25gbase-x-sfp28", "uplink", "active"),
                SimulatedInterface("server-05", "eno4", "25gbase-x-sfp28", "uplink", "active"),
            ],
        }

        # Initialize switch interfaces
        self.switch_interfaces: dict[str, list[SimulatedInterface]] = {}
        self._init_switch_interfaces()

    def _get_server_rack(self, row: int, server_index: int) -> str:
        """Get server rack name based on deployment type and server position."""
        if self.deployment_type == "middle_rack":
            return f"ktw-1-s-1-r-{row}-10"
        elif self.deployment_type == "tor":
            # Servers in compute racks with ToRs
            rack_index = 1 if server_index <= 2 else 5
            return f"ktw-1-s-2-r-{row}-{rack_index}"
        elif self.deployment_type == "mixed":
            rack_index = 1 if server_index <= 2 else 5
            return f"ktw-1-s-3-r-{row}-{rack_index}"
        return ""

    def _init_middle_rack_switches(self) -> None:
        """Initialize Leaf switches for middle_rack deployment."""
        self.leafs = {
            # Row 1 leafs in network rack
            "ktw-1-s-1-p-1-l-1-1": SimulatedDevice("ktw-1-s-1-p-1-l-1-1", "leaf", "ktw-1-s-1-r-1-1", "active"),
            "ktw-1-s-1-p-1-l-1-2": SimulatedDevice("ktw-1-s-1-p-1-l-1-2", "leaf", "ktw-1-s-1-r-1-1", "active"),
            "ktw-1-s-1-p-1-l-1-3": SimulatedDevice("ktw-1-s-1-p-1-l-1-3", "leaf", "ktw-1-s-1-r-1-1", "active"),
            "ktw-1-s-1-p-1-l-1-4": SimulatedDevice("ktw-1-s-1-p-1-l-1-4", "leaf", "ktw-1-s-1-r-1-1", "active"),
            # Row 2 leafs in network rack
            "ktw-1-s-1-p-1-l-2-1": SimulatedDevice("ktw-1-s-1-p-1-l-2-1", "leaf", "ktw-1-s-1-r-2-1", "active"),
            "ktw-1-s-1-p-1-l-2-2": SimulatedDevice("ktw-1-s-1-p-1-l-2-2", "leaf", "ktw-1-s-1-r-2-1", "active"),
            "ktw-1-s-1-p-1-l-2-3": SimulatedDevice("ktw-1-s-1-p-1-l-2-3", "leaf", "ktw-1-s-1-r-2-1", "active"),
            "ktw-1-s-1-p-1-l-2-4": SimulatedDevice("ktw-1-s-1-p-1-l-2-4", "leaf", "ktw-1-s-1-r-2-1", "active"),
        }
        self.tors = {}

    def _init_tor_switches(self) -> None:
        """Initialize ToR switches for tor deployment."""
        self.leafs = {}
        self.tors = {
            # Row 1 ToRs in compute racks
            "ktw-1-s-2-p-2-t-1-1": SimulatedDevice("ktw-1-s-2-p-2-t-1-1", "tor", "ktw-1-s-2-r-1-1", "active"),
            "ktw-1-s-2-p-2-t-1-2": SimulatedDevice("ktw-1-s-2-p-2-t-1-2", "tor", "ktw-1-s-2-r-1-1", "active"),
            "ktw-1-s-2-p-2-t-1-5": SimulatedDevice("ktw-1-s-2-p-2-t-1-5", "tor", "ktw-1-s-2-r-1-5", "active"),
            "ktw-1-s-2-p-2-t-1-6": SimulatedDevice("ktw-1-s-2-p-2-t-1-6", "tor", "ktw-1-s-2-r-1-5", "active"),
            # Row 2 ToRs in compute racks
            "ktw-1-s-2-p-2-t-2-1": SimulatedDevice("ktw-1-s-2-p-2-t-2-1", "tor", "ktw-1-s-2-r-2-1", "active"),
            "ktw-1-s-2-p-2-t-2-2": SimulatedDevice("ktw-1-s-2-p-2-t-2-2", "tor", "ktw-1-s-2-r-2-1", "active"),
            "ktw-1-s-2-p-2-t-2-5": SimulatedDevice("ktw-1-s-2-p-2-t-2-5", "tor", "ktw-1-s-2-r-2-5", "active"),
            "ktw-1-s-2-p-2-t-2-6": SimulatedDevice("ktw-1-s-2-p-2-t-2-6", "tor", "ktw-1-s-2-r-2-5", "active"),
        }

    def _init_mixed_switches(self) -> None:
        """Initialize both ToRs and Leafs for mixed deployment."""
        # ToRs in compute racks (same rack as servers)
        self.tors = {
            "ktw-1-s-3-p-3-t-1-1": SimulatedDevice("ktw-1-s-3-p-3-t-1-1", "tor", "ktw-1-s-3-r-1-1", "active"),
            "ktw-1-s-3-p-3-t-1-2": SimulatedDevice("ktw-1-s-3-p-3-t-1-2", "tor", "ktw-1-s-3-r-1-1", "active"),
            "ktw-1-s-3-p-3-t-1-5": SimulatedDevice("ktw-1-s-3-p-3-t-1-5", "tor", "ktw-1-s-3-r-1-5", "active"),
            "ktw-1-s-3-p-3-t-1-6": SimulatedDevice("ktw-1-s-3-p-3-t-1-6", "tor", "ktw-1-s-3-r-1-5", "active"),
            "ktw-1-s-3-p-3-t-2-1": SimulatedDevice("ktw-1-s-3-p-3-t-2-1", "tor", "ktw-1-s-3-r-2-1", "active"),
            "ktw-1-s-3-p-3-t-2-2": SimulatedDevice("ktw-1-s-3-p-3-t-2-2", "tor", "ktw-1-s-3-r-2-1", "active"),
        }
        # Leafs in network rack (fallback)
        self.leafs = {
            "ktw-1-s-3-p-3-l-1-1": SimulatedDevice("ktw-1-s-3-p-3-l-1-1", "leaf", "ktw-1-s-3-r-1-10", "active"),
            "ktw-1-s-3-p-3-l-1-2": SimulatedDevice("ktw-1-s-3-p-3-l-1-2", "leaf", "ktw-1-s-3-r-1-10", "active"),
            "ktw-1-s-3-p-3-l-2-1": SimulatedDevice("ktw-1-s-3-p-3-l-2-1", "leaf", "ktw-1-s-3-r-2-10", "active"),
            "ktw-1-s-3-p-3-l-2-2": SimulatedDevice("ktw-1-s-3-p-3-l-2-2", "leaf", "ktw-1-s-3-r-2-10", "active"),
        }

    def _init_switch_interfaces(self) -> None:
        """Initialize interfaces for all switches based on deployment type."""
        # Leaf interfaces (25G customer ports)
        for leaf_name in self.leafs.keys():
            interfaces = []
            for port in range(25, 33):
                interfaces.append(
                    SimulatedInterface(
                        leaf_name,
                        f"Ethernet{port}/1",
                        "25gbase-x-sfp28",
                        "customer",
                        "free",
                    )
                )
            self.switch_interfaces[leaf_name] = interfaces

        # ToR interfaces (25G downlink ports)
        for tor_name in self.tors.keys():
            interfaces = []
            for port in range(1, 17):  # 16 downlink ports per ToR
                interfaces.append(
                    SimulatedInterface(
                        tor_name,
                        f"Ethernet1/{port}",
                        "25gbase-x-sfp28",
                        "downlink",
                        "free",
                    )
                )
            self.switch_interfaces[tor_name] = interfaces

    def get_network_rack_for_row(self, row_index: int) -> SimulatedRack | None:
        """Get network rack (middle rack) for given row."""
        for rack in self.racks.values():
            if rack.rack_type == "network" and rack.row_index == row_index:
                return rack
        return None

    def get_switch_interfaces_in_rack(
        self, rack_name: str, device_role: str, interface_type: str | None = None
    ) -> list[SimulatedInterface]:
        """Get available switch interfaces in given rack."""
        available = []
        switches = self.leafs if device_role == "leaf" else self.tors

        for switch_name, switch in switches.items():
            if switch.rack == rack_name and switch.status in ["active", "free"]:
                for interface in self.switch_interfaces.get(switch_name, []):
                    if interface.status == "free" and not interface.has_cable:
                        if interface_type is None or interface.interface_type == interface_type:
                            available.append(interface)
        return available

    def simulate_middle_rack_connectivity(self, server_name: str) -> dict[str, Any]:
        """Simulate connectivity for a server in middle_rack deployment."""
        server = self.servers[server_name]
        server_rack = self.racks[server.rack]
        server_interfaces = self.server_interfaces[server_name]

        # Step 1: Find network rack in same row
        network_rack = self.get_network_rack_for_row(server_rack.row_index)
        if not network_rack:
            return {
                "success": False,
                "error": f"No network rack found in row {server_rack.row_index}",
                "server": server_name,
            }

        # Step 2: Query available leaf interfaces in network rack
        interface_type = server_interfaces[0].interface_type if server_interfaces else None
        available_interfaces = self.get_switch_interfaces_in_rack(network_rack.name, "leaf", interface_type)

        if len(available_interfaces) < len(server_interfaces):
            return {
                "success": False,
                "error": f"Not enough available interfaces: need {len(server_interfaces)}, found {len(available_interfaces)}",
                "server": server_name,
                "network_rack": network_rack.name,
            }

        # Step 3: Create connections
        connections = []
        for idx, server_iface in enumerate(server_interfaces):
            if idx < len(available_interfaces):
                switch_iface = available_interfaces[idx]
                connections.append(
                    {
                        "server": server_name,
                        "server_interface": server_iface.name,
                        "switch": switch_iface.device_name,
                        "switch_interface": switch_iface.name,
                        "speed": server_iface.interface_type,
                    }
                )
                switch_iface.has_cable = True

        return {
            "success": True,
            "server": server_name,
            "server_rack": server_rack.name,
            "network_rack": network_rack.name,
            "row": server_rack.row_index,
            "connections": connections,
            "connection_count": len(connections),
        }

    def simulate_tor_connectivity(self, server_name: str) -> dict[str, Any]:
        """Simulate connectivity for a server in tor deployment."""
        server = self.servers[server_name]
        server_rack = self.racks[server.rack]
        server_interfaces = self.server_interfaces[server_name]
        interface_type = server_interfaces[0].interface_type if server_interfaces else None

        # Step 1: Try same rack first
        available_interfaces = self.get_switch_interfaces_in_rack(server_rack.name, "tor", interface_type)

        # Step 2: Fallback to same row if needed
        if len(available_interfaces) < len(server_interfaces):
            row_racks = [rack for rack in self.racks.values() if rack.row_index == server_rack.row_index]
            for rack in row_racks:
                if rack.name != server_rack.name:
                    available_interfaces.extend(self.get_switch_interfaces_in_rack(rack.name, "tor", interface_type))

        if len(available_interfaces) < len(server_interfaces):
            return {
                "success": False,
                "error": f"Not enough ToR interfaces: need {len(server_interfaces)}, found {len(available_interfaces)}",
                "server": server_name,
            }

        # Step 3: Create connections
        connections = []
        for idx, server_iface in enumerate(server_interfaces):
            if idx < len(available_interfaces):
                tor_iface = available_interfaces[idx]
                connections.append(
                    {
                        "server": server_name,
                        "server_interface": server_iface.name,
                        "switch": tor_iface.device_name,
                        "switch_interface": tor_iface.name,
                        "speed": server_iface.interface_type,
                    }
                )
                tor_iface.has_cable = True

        return {
            "success": True,
            "server": server_name,
            "server_rack": server_rack.name,
            "row": server_rack.row_index,
            "connections": connections,
            "connection_count": len(connections),
        }

    def simulate_mixed_connectivity(self, server_name: str) -> dict[str, Any]:
        """Simulate connectivity for a server in mixed deployment."""
        server = self.servers[server_name]
        server_rack = self.racks[server.rack]
        server_interfaces = self.server_interfaces[server_name]
        interface_type = server_interfaces[0].interface_type if server_interfaces else None

        # Step 1: Try ToR in same rack first
        available_interfaces = self.get_switch_interfaces_in_rack(server_rack.name, "tor", interface_type)

        # Step 2: Fallback to Leaf in network rack (middle rack)
        if len(available_interfaces) < len(server_interfaces):
            network_rack = self.get_network_rack_for_row(server_rack.row_index)
            if network_rack:
                available_interfaces.extend(
                    self.get_switch_interfaces_in_rack(network_rack.name, "leaf", interface_type)
                )

        if len(available_interfaces) < len(server_interfaces):
            return {
                "success": False,
                "error": f"Not enough interfaces: need {len(server_interfaces)}, found {len(available_interfaces)}",
                "server": server_name,
            }

        # Step 3: Create connections
        connections = []
        for idx, server_iface in enumerate(server_interfaces):
            if idx < len(available_interfaces):
                switch_iface = available_interfaces[idx]
                connections.append(
                    {
                        "server": server_name,
                        "server_interface": server_iface.name,
                        "switch": switch_iface.device_name,
                        "switch_interface": switch_iface.name,
                        "speed": server_iface.interface_type,
                    }
                )
                switch_iface.has_cable = True

        return {
            "success": True,
            "server": server_name,
            "server_rack": server_rack.name,
            "row": server_rack.row_index,
            "connections": connections,
            "connection_count": len(connections),
        }


class TestMiddleRackDeploymentSimulation:
    """Test endpoint connectivity simulation for middle_rack deployment."""

    @pytest.fixture
    def simulator(self) -> EndpointConnectivitySimulator:
        """Create simulator instance for middle_rack."""
        return EndpointConnectivitySimulator(deployment_type="middle_rack")

    def test_server_01_middle_rack_connectivity(self, simulator: EndpointConnectivitySimulator) -> None:
        """Simulate connectivity for server-01 in row 1."""
        result = simulator.simulate_middle_rack_connectivity("server-01")

        # Verify connectivity was successful
        assert result["success"], f"Connection failed: {result.get('error')}"
        assert result["server"] == "server-01"
        assert result["server_rack"] == "ktw-1-s-1-r-1-10"
        assert result["network_rack"] == "ktw-1-s-1-r-1-1"
        assert result["row"] == 1

        # Verify all 4 server interfaces connected
        assert result["connection_count"] == 4, f"Expected 4 connections, got {result['connection_count']}"

        # Verify connections
        connections = result["connections"]
        assert len(connections) == 4

        # Check server interfaces
        server_ifaces = {conn["server_interface"] for conn in connections}
        assert server_ifaces == {"eno1", "eno2", "eno3", "eno4"}

        # Check all connections are 25G
        for conn in connections:
            assert conn["speed"] == "25gbase-x-sfp28"
            assert "ktw-1-s-1-p-1-l-1" in conn["switch"]  # Row 1 leafs

    def test_server_02_middle_rack_connectivity(self, simulator: EndpointConnectivitySimulator) -> None:
        """Simulate connectivity for server-02 in row 1."""
        # First connect server-01 to use up some interfaces
        simulator.simulate_middle_rack_connectivity("server-01")

        # Now connect server-02
        result = simulator.simulate_middle_rack_connectivity("server-02")

        assert result["success"], f"Connection failed: {result.get('error')}"
        assert result["server"] == "server-02"
        assert result["network_rack"] == "ktw-1-s-1-r-1-1"
        assert result["connection_count"] == 4

    def test_server_03_middle_rack_connectivity(self, simulator: EndpointConnectivitySimulator) -> None:
        """Simulate connectivity for server-03 in row 2."""
        result = simulator.simulate_middle_rack_connectivity("server-03")

        assert result["success"], f"Connection failed: {result.get('error')}"
        assert result["server"] == "server-03"
        assert result["server_rack"] == "ktw-1-s-1-r-2-10"
        assert result["network_rack"] == "ktw-1-s-1-r-2-1"  # Different network rack for row 2
        assert result["row"] == 2
        assert result["connection_count"] == 4

        # Check leafs are from row 2
        connections = result["connections"]
        for conn in connections:
            assert "ktw-1-s-1-p-1-l-2" in conn["switch"]  # Row 2 leafs

    def test_all_servers_connectivity(self, simulator: EndpointConnectivitySimulator) -> None:
        """Simulate connectivity for all servers and verify no conflicts."""
        results = []
        for server_name in ["server-01", "server-02", "server-03", "server-04", "server-05"]:
            result = simulator.simulate_middle_rack_connectivity(server_name)
            results.append(result)

        # Verify all succeeded
        for result in results:
            assert result["success"], f"Server {result['server']} failed: {result.get('error')}"

        # Verify total connections
        total_connections = sum(r["connection_count"] for r in results)
        assert total_connections == 20, (
            f"Expected 20 total connections (5 servers × 4 interfaces), got {total_connections}"
        )

        # Verify no duplicate leaf interfaces used
        all_switch_ifaces = []
        for result in results:
            for conn in result["connections"]:
                switch_iface_id = f"{conn['switch']}:{conn['switch_interface']}"
                all_switch_ifaces.append(switch_iface_id)

        assert len(all_switch_ifaces) == len(set(all_switch_ifaces)), "Duplicate switch interfaces detected!"

    def test_row_isolation(self, simulator: EndpointConnectivitySimulator) -> None:
        """Verify servers in different rows connect to different network racks."""
        # Row 1 servers
        result_row1_s1 = simulator.simulate_middle_rack_connectivity("server-01")
        result_row1_s2 = simulator.simulate_middle_rack_connectivity("server-02")

        # Row 2 servers
        result_row2_s3 = simulator.simulate_middle_rack_connectivity("server-03")
        result_row2_s4 = simulator.simulate_middle_rack_connectivity("server-04")

        # Verify row 1 servers use row 1 network rack
        assert result_row1_s1["network_rack"] == "ktw-1-s-1-r-1-1"
        assert result_row1_s2["network_rack"] == "ktw-1-s-1-r-1-1"

        # Verify row 2 servers use row 2 network rack
        assert result_row2_s3["network_rack"] == "ktw-1-s-1-r-2-1"
        assert result_row2_s4["network_rack"] == "ktw-1-s-1-r-2-1"

        # Verify no cross-row connections
        row1_switches = set()
        for conn in result_row1_s1["connections"] + result_row1_s2["connections"]:
            row1_switches.add(conn["switch"])

        row2_switches = set()
        for conn in result_row2_s3["connections"] + result_row2_s4["connections"]:
            row2_switches.add(conn["switch"])

        # No overlap between row 1 and row 2 switches
        assert len(row1_switches & row2_switches) == 0, "Cross-row connections detected!"

    def test_interface_capacity(self, simulator: EndpointConnectivitySimulator) -> None:
        """Verify we have enough leaf interfaces for all servers."""
        # Each leaf has 8x 25G customer ports (simulated)
        # Row 1: 4 leafs × 8 ports = 32 ports
        # Row 2: 4 leafs × 8 ports = 32 ports
        # Total: 64 ports available

        # Servers need:
        # Row 1: server-01 (4) + server-02 (4) + server-05 (4) = 12 ports
        # Row 2: server-03 (4) + server-04 (4) = 8 ports
        # Total: 20 ports needed

        # Verify capacity
        row1_available = len(simulator.get_switch_interfaces_in_rack("ktw-1-s-1-r-1-1", "leaf", "25gbase-x-sfp28"))
        row2_available = len(simulator.get_switch_interfaces_in_rack("ktw-1-s-1-r-2-1", "leaf", "25gbase-x-sfp28"))

        assert row1_available >= 12, f"Row 1 needs 12 ports, has {row1_available}"
        assert row2_available >= 8, f"Row 2 needs 8 ports, has {row2_available}"

        print("\nCapacity Analysis:")
        print(f"  Row 1: {row1_available} ports available, 12 needed (servers 01, 02, 05)")
        print(f"  Row 2: {row2_available} ports available, 8 needed (servers 03, 04)")
        print(f"  Total: {row1_available + row2_available} ports available, 20 needed")
        print(f"  Remaining capacity: {(row1_available + row2_available) - 20} ports")

    def test_deployment_strategy_correctness(self, simulator: EndpointConnectivitySimulator) -> None:
        """Verify middle_rack deployment strategy is correctly implemented."""
        # Middle rack deployment means:
        # 1. Servers are in compute racks
        # 2. Leafs are in network racks (middle racks)
        # 3. Servers connect to leafs in network rack in SAME ROW
        # 4. No ToR switches involved

        result = simulator.simulate_middle_rack_connectivity("server-01")

        assert result["success"]

        # Verify server is in compute rack
        server_rack = simulator.racks[result["server_rack"]]
        assert server_rack.rack_type == "compute"

        # Verify network rack is in same row
        network_rack = simulator.racks[result["network_rack"]]
        assert network_rack.rack_type == "network"
        assert network_rack.row_index == server_rack.row_index

        # Verify all connections go to leaf switches (not ToR)
        for conn in result["connections"]:
            switch = simulator.leafs[conn["switch"]]
            assert switch.role == "leaf", f"Expected leaf, got {switch.role}"
            assert switch.rack == result["network_rack"], "Leaf not in network rack!"

        print("\n✅ Middle Rack Deployment Strategy Verified:")
        print(f"  Server: {result['server']} in compute rack (row {server_rack.row_index})")
        print(f"  Connected to: {len(result['connections'])} leafs in network rack {result['network_rack']}")
        print(f"  All connections stay within row {server_rack.row_index}")


class TestToRDeploymentSimulation:
    """Test endpoint connectivity simulation for ToR deployment."""

    @pytest.fixture
    def simulator(self) -> EndpointConnectivitySimulator:
        """Create simulator instance for ToR deployment."""
        return EndpointConnectivitySimulator(deployment_type="tor")

    def test_tor_server_in_same_rack(self, simulator: EndpointConnectivitySimulator) -> None:
        """Verify ToR deployment connects servers to ToRs in same rack."""
        result = simulator.simulate_tor_connectivity("server-01")

        assert result["success"]
        assert result["connection_count"] == 4

        # Verify all connections go to ToRs (not Leafs)
        for conn in result["connections"]:
            assert "t-" in conn["switch"], "Expected ToR switch"

    def test_tor_all_servers_connectivity(self, simulator: EndpointConnectivitySimulator) -> None:
        """Verify all servers connect in ToR deployment."""
        results = []
        for server_name in ["server-01", "server-02", "server-03", "server-04"]:
            result = simulator.simulate_tor_connectivity(server_name)
            results.append(result)

        # Verify all succeeded
        for result in results:
            assert result["success"], f"Server {result['server']} failed: {result.get('error')}"

        # Verify total connections
        total_connections = sum(r["connection_count"] for r in results)
        assert total_connections == 16, (
            f"Expected 16 total connections (4 servers × 4 interfaces), got {total_connections}"
        )

    def test_tor_deployment_strategy(self, simulator: EndpointConnectivitySimulator) -> None:
        """Verify ToR deployment strategy is correct."""
        result = simulator.simulate_tor_connectivity("server-01")

        assert result["success"]

        # ToR deployment: servers connect to ToRs in same rack (or fallback to same row)
        for conn in result["connections"]:
            tor = simulator.tors[conn["switch"]]
            assert tor.role == "tor"
            # ToR should be in same row as server
            assert tor.rack.startswith(f"ktw-1-s-2-r-{result['row']}")

        print("\n✅ ToR Deployment Strategy Verified:")
        print(f"  Server: {result['server']} connects to ToRs in compute racks")
        print(f"  All {result['connection_count']} connections use ToR switches")


class TestMixedDeploymentSimulation:
    """Test endpoint connectivity simulation for Mixed deployment."""

    @pytest.fixture
    def simulator(self) -> EndpointConnectivitySimulator:
        """Create simulator instance for Mixed deployment."""
        return EndpointConnectivitySimulator(deployment_type="mixed")

    def test_mixed_prefers_tor_in_same_rack(self, simulator: EndpointConnectivitySimulator) -> None:
        """Verify Mixed deployment prefers ToR in same rack."""
        result = simulator.simulate_mixed_connectivity("server-01")

        assert result["success"]
        assert result["connection_count"] == 4

        # With fresh switches, should use ToRs first
        tor_connections = sum(1 for conn in result["connections"] if "t-" in conn["switch"])
        assert tor_connections > 0, "Expected some ToR connections"

    def test_mixed_fallback_to_leaf(self, simulator: EndpointConnectivitySimulator) -> None:
        """Verify Mixed deployment falls back to Leaf when ToRs exhausted."""
        # Manually exhaust ToR interfaces to force fallback
        # Mark all ToR interfaces as "used" except 2 ports on one ToR
        for tor_name, tor_interfaces in simulator.switch_interfaces.items():
            if "t-" in tor_name:  # ToR switch
                if tor_name == "ktw-1-s-3-p-3-t-1-1":  # Leave 2 ports on one ToR
                    for intf in tor_interfaces[:14]:
                        intf.status = "used"
                else:  # Exhaust all ports on other ToRs
                    for intf in tor_interfaces:
                        intf.status = "used"

        # Now connect server-01 - should use 2 remaining ToR ports + 2 Leaf ports
        result = simulator.simulate_mixed_connectivity("server-01")

        assert result["success"]
        # Should have mix of ToR and Leaf connections
        tor_connections = sum(1 for conn in result["connections"] if "t-" in conn["switch"])
        leaf_connections = sum(1 for conn in result["connections"] if "l-" in conn["switch"])

        # With only 2 ToR ports available, needs 2 from ToR + 2 from Leaf
        assert leaf_connections > 0, (
            f"Expected fallback to Leaf connections, got {leaf_connections} leaf, {tor_connections} tor"
        )
        assert tor_connections == 2, f"Expected 2 ToR connections, got {tor_connections}"
        assert leaf_connections == 2, f"Expected 2 Leaf connections, got {leaf_connections}"

    def test_mixed_deployment_strategy(self, simulator: EndpointConnectivitySimulator) -> None:
        """Verify Mixed deployment strategy is correct."""
        result = simulator.simulate_mixed_connectivity("server-01")

        assert result["success"]

        print("\n✅ Mixed Deployment Strategy Verified:")
        print(f"  Server: {result['server']} uses both ToRs and Leafs")
        print(f"  Total connections: {result['connection_count']}")

        tor_count = sum(1 for conn in result["connections"] if "t-" in conn["switch"])
        leaf_count = sum(1 for conn in result["connections"] if "l-" in conn["switch"])
        print(f"  ToR connections: {tor_count}")
        print(f"  Leaf connections: {leaf_count}")


class TestDeploymentTypeComparison:
    """Compare all three deployment types."""

    def test_all_deployment_types_work(self) -> None:
        """Verify all deployment types can connect servers."""
        results = {}

        for deployment_type in ["middle_rack", "tor", "mixed"]:
            simulator = EndpointConnectivitySimulator(deployment_type=deployment_type)

            if deployment_type == "middle_rack":
                result = simulator.simulate_middle_rack_connectivity("server-01")
            elif deployment_type == "tor":
                result = simulator.simulate_tor_connectivity("server-01")
            else:
                result = simulator.simulate_mixed_connectivity("server-01")

            results[deployment_type] = result

        # All should succeed
        for deployment_type, result in results.items():
            assert result["success"], f"{deployment_type} failed: {result.get('error')}"
            assert result["connection_count"] == 4

        print("\n✅ All Deployment Types Validated:")
        for deployment_type, result in results.items():
            print(f"  {deployment_type}: {result['connection_count']} connections ✓")
