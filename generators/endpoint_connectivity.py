"""Generator for automatic endpoint connectivity to leaf switches."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .common import CommonGenerator

if TYPE_CHECKING:
    pass


class EndpointConnectivityGenerator(CommonGenerator):
    """Automatically connect endpoints to leaf switches with dual-homing redundancy.

    Connection logic:
    - Endpoints connect to 2 leaf switches for redundancy
    - Priority 1: Connect to leafs in the same rack
    - Priority 2: Connect to leafs in the same pod (different rack)
    - Use first available access ports on leafs
    - All data fetched in single GraphQL query for efficiency
    """

    async def generate(self, data: dict[str, Any]) -> None:
        """Generate dual-homed endpoint connections to leaf switches.

        Args:
            data: GraphQL query result with endpoint, rack leafs, and pod leafs
        """
        # Extract endpoint from query result
        endpoint_edges = data.get("endpoint", {}).get("edges", [])

        if not endpoint_edges:
            self.logger.warning("No endpoint found in query result")
            return

        endpoint_node = endpoint_edges[0]["node"]
        endpoint_name = endpoint_node.get("name", {}).get("value", "unknown")
        endpoint_role = endpoint_node.get("role", {}).get("value", "")

        # Only process endpoint devices (servers)
        if endpoint_role != "endpoint":
            self.logger.warning(
                f"Device {endpoint_name} is not an endpoint (role: {endpoint_role}), skipping"
            )
            return

        self.logger.info(f"Processing endpoint connectivity for {endpoint_name}")

        # Get endpoint's rack and pod
        rack_data = endpoint_node.get("rack", {}).get("node")
        if not rack_data:
            self.logger.warning(f"Endpoint {endpoint_name} has no rack assignment")
            return

        rack_id = rack_data.get("id")
        rack_name = rack_data.get("name", {}).get("value", "unknown")
        pod_data = rack_data.get("parent", {}).get("node")
        pod_id = pod_data.get("id") if pod_data else None

        # Get available endpoint interfaces (not connected)
        endpoint_interfaces = endpoint_node.get("interfaces", {}).get("edges", [])
        available_endpoint_intfs = []

        for intf_edge in endpoint_interfaces:
            intf = intf_edge.get("node", {})
            if not intf.get("cable"):
                available_endpoint_intfs.append(intf)

        if len(available_endpoint_intfs) < 2:
            self.logger.warning(
                f"Endpoint {endpoint_name} has only {len(available_endpoint_intfs)} available interfaces, need 2 for dual-homing"
            )
            if not available_endpoint_intfs:
                return

        # Sort endpoint interfaces by name for consistent ordering
        available_endpoint_intfs.sort(key=lambda x: x.get("name", {}).get("value", ""))

        # Calculate how many connections per leaf based on endpoint interface count
        # For dual-homing: split interfaces evenly between 2 consecutive leafs
        total_endpoint_intfs = len(available_endpoint_intfs)
        connections_per_leaf = total_endpoint_intfs // 2

        if connections_per_leaf == 0:
            self.logger.warning(
                f"Endpoint {endpoint_name} needs at least 2 interfaces for dual-homing"
            )
            return

        self.logger.info(
            f"Endpoint {endpoint_name} has {total_endpoint_intfs} interfaces, "
            f"will create {connections_per_leaf} connections to each of 2 consecutive leafs"
        )

        # Find 2 consecutive leafs with available ports (e.g., leaf-01 & leaf-02, or leaf-03 & leaf-04)
        rack_leafs = self._extract_rack_leafs(rack_data, rack_name)
        rack_leaf_names = [
            leaf.get("name", {}).get("value")
            for leaf in rack_leafs
            if leaf.get("name", {}).get("value")
        ]

        # Sort rack leafs by name to find consecutive pairs
        rack_leaf_names.sort()

        # Find consecutive leaf pair with enough available ports
        selected_leafs = []
        if len(rack_leaf_names) >= 2:
            selected_pair = await self._find_consecutive_leaf_pair(
                rack_leafs,
                rack_leaf_names,
                required_ports_per_leaf=connections_per_leaf,
            )
            selected_leafs = selected_pair

        # If we didn't find a suitable pair in the rack, check the pod
        if len(selected_leafs) < 2 and pod_id:
            pod_leafs_data = data.get("pod_leafs", {}).get("edges", [])
            pod_leaf_names = []
            pod_leafs_list = []

            for leaf_edge in pod_leafs_data:
                leaf = leaf_edge.get("node", {})
                leaf_rack = leaf.get("rack", {}).get("node", {})
                leaf_rack_id = leaf_rack.get("id")
                leaf_pod_id = leaf_rack.get("parent", {}).get("node", {}).get("id")

                # Include leafs from same pod but different rack
                if leaf_pod_id == pod_id and leaf_rack_id != rack_id:
                    leaf_name = leaf.get("name", {}).get("value")
                    if leaf_name and leaf_name not in rack_leaf_names:
                        pod_leaf_names.append(leaf_name)
                        pod_leafs_list.append(leaf)

            if len(pod_leaf_names) >= 2:
                pod_leaf_names.sort()
                selected_pair = await self._find_consecutive_leaf_pair(
                    pod_leafs_list,
                    pod_leaf_names,
                    required_ports_per_leaf=connections_per_leaf,
                )
                selected_leafs = selected_pair

        if len(selected_leafs) < 2:
            self.logger.warning(
                f"Could not find 2 consecutive leaf switches with {connections_per_leaf} available ports each for {endpoint_name}"
            )
            return

        leaf1, leaf1_ports = selected_leafs[0]
        leaf2, leaf2_ports = selected_leafs[1]
        leaf1_name = leaf1.get("name", {}).get("value", "unknown")
        leaf2_name = leaf2.get("name", {}).get("value", "unknown")

        self.logger.info(
            f"Selected consecutive leaf pair: {leaf1_name} ({len(leaf1_ports)} ports) and {leaf2_name} ({len(leaf2_ports)} ports)"
        )

        # Create connections: first half of endpoint interfaces to leaf1, second half to leaf2
        connection_count = 0

        for i in range(connections_per_leaf):
            # Connect to leaf1
            if i < len(leaf1_ports):
                endpoint_intf = available_endpoint_intfs[i]
                leaf_intf = leaf1_ports[i]

                endpoint_intf_name = endpoint_intf.get("name", {}).get(
                    "value", "unknown"
                )
                leaf_intf_name = leaf_intf.get("name", {}).get("value", "unknown")

                self.logger.info(
                    f"Creating connection: {endpoint_name}:{endpoint_intf_name} <-> {leaf1_name}:{leaf_intf_name}"
                )

                await self._create_cable(endpoint_intf, leaf_intf)
                connection_count += 1

        for i in range(connections_per_leaf):
            # Connect to leaf2
            if i < len(leaf2_ports):
                endpoint_intf = available_endpoint_intfs[connections_per_leaf + i]
                leaf_intf = leaf2_ports[i]

                endpoint_intf_name = endpoint_intf.get("name", {}).get(
                    "value", "unknown"
                )
                leaf_intf_name = leaf_intf.get("name", {}).get("value", "unknown")

                self.logger.info(
                    f"Creating connection: {endpoint_name}:{endpoint_intf_name} <-> {leaf2_name}:{leaf_intf_name}"
                )

                await self._create_cable(endpoint_intf, leaf_intf)
                connection_count += 1

        self.logger.info(
            f"Successfully created {connection_count} connections for {endpoint_name} "
            f"({connections_per_leaf} to {leaf1_name}, {connections_per_leaf} to {leaf2_name})"
        )

    def _extract_rack_leafs(
        self, rack_data: dict[str, Any], rack_name: str
    ) -> list[dict[str, Any]]:
        """Extract leaf devices from rack data.

        Args:
            rack_data: Rack node data containing devices
            rack_name: Name of the rack for logging

        Returns:
            List of leaf device nodes with interfaces
        """
        leafs = []
        devices = rack_data.get("devices", {}).get("edges", [])

        for device_edge in devices:
            device = device_edge.get("node", {})
            if device.get("role", {}).get("value") == "leaf":
                leafs.append(device)

        self.logger.info(f"Found {len(leafs)} leaf switches in rack {rack_name}")
        return leafs

    async def _find_consecutive_leaf_pair(
        self,
        leafs: list[dict[str, Any]],
        leaf_names: list[str],
        required_ports_per_leaf: int,
    ) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
        """Find a pair of consecutive leaf switches with enough available ports.

        Consecutive means adjacent IDs like leaf-01/leaf-02, leaf-03/leaf-04, etc.

        Args:
            leafs: List of leaf device data
            leaf_names: Sorted list of leaf names
            required_ports_per_leaf: Number of available access ports needed per leaf

        Returns:
            List of 2 tuples: [(leaf1, [port1, port2, ...]), (leaf2, [port1, port2, ...])]
        """
        import re

        # Extract numeric IDs from leaf names
        def extract_id(name: str) -> int | None:
            """Extract numeric ID from leaf name (e.g., 'leaf-01' -> 1, 'dc1-pod1-leaf-03' -> 3)."""
            match = re.search(r"leaf-?(\d+)", name, re.IGNORECASE)
            if match:
                return int(match.group(1))
            return None

        # Build map of leaf ID to leaf data
        leaf_map: dict[int, dict[str, Any]] = {}
        for leaf in leafs:
            leaf_name = leaf.get("name", {}).get("value")
            if leaf_name:
                leaf_id = extract_id(leaf_name)
                if leaf_id is not None:
                    leaf_map[leaf_id] = leaf

        # Find consecutive pairs (1-2, 3-4, 5-6, etc.)
        sorted_ids = sorted(leaf_map.keys())

        for i in range(len(sorted_ids) - 1):
            id1 = sorted_ids[i]
            id2 = sorted_ids[i + 1]

            # Check if consecutive (difference of 1) and odd-even pair
            if id2 - id1 == 1 and id1 % 2 == 1:
                leaf1 = leaf_map[id1]
                leaf2 = leaf_map[id2]

                # Get available ports for each leaf
                leaf1_ports = self._get_n_available_ports(
                    leaf1, required_ports_per_leaf
                )
                leaf2_ports = self._get_n_available_ports(
                    leaf2, required_ports_per_leaf
                )

                # Check if both leafs have enough ports
                if (
                    len(leaf1_ports) >= required_ports_per_leaf
                    and len(leaf2_ports) >= required_ports_per_leaf
                ):
                    leaf1_name = leaf1.get("name", {}).get("value", "unknown")
                    leaf2_name = leaf2.get("name", {}).get("value", "unknown")
                    self.logger.info(
                        f"Found consecutive leaf pair: {leaf1_name} (ID {id1}) and {leaf2_name} (ID {id2})"
                    )
                    return [(leaf1, leaf1_ports), (leaf2, leaf2_ports)]

        self.logger.warning(
            f"Could not find consecutive leaf pair with {required_ports_per_leaf} available ports each"
        )
        return []

    def _get_n_available_ports(
        self,
        leaf_device: dict[str, Any],
        count: int,
    ) -> list[dict[str, Any]]:
        """Get N available access ports from a leaf switch.

        Args:
            leaf_device: Leaf device node with interfaces
            count: Number of ports needed

        Returns:
            List of available interface dicts (up to count)
        """
        interfaces = leaf_device.get("interfaces", {}).get("edges", [])
        available_ports: list[dict[str, Any]] = []

        for intf_edge in interfaces:
            if len(available_ports) >= count:
                break

            intf = intf_edge.get("node", {})

            # Check if it's an access port and not connected
            if intf.get("role", {}).get("value") == "access" and not intf.get("cable"):
                available_ports.append(intf)

        return available_ports

    def _find_available_access_port(
        self, leaf_device: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Find first available access port on a leaf switch.

        Args:
            leaf_device: Leaf device node with interfaces

        Returns:
            Available interface dict or None
        """
        interfaces = leaf_device.get("interfaces", {}).get("edges", [])

        for intf_edge in interfaces:
            intf = intf_edge.get("node", {})

            # Check if it's an access port and not connected
            if intf.get("role", {}).get("value") == "access" and not intf.get("cable"):
                return intf

        return None

    async def _create_cable(
        self, interface1: dict[str, Any], interface2: dict[str, Any]
    ) -> None:
        """Create a cable between two interfaces.

        Args:
            interface1: First interface data dict
            interface2: Second interface data dict
        """
        intf1_id = interface1.get("id")
        intf2_id = interface2.get("id")

        if not intf1_id or not intf2_id:
            self.logger.error("Cannot create cable: missing interface IDs")
            return

        # Fetch the actual interface nodes to create relationship
        intf1_node = await self.client.get(kind="DcimPhysicalInterface", id=intf1_id)
        intf2_node = await self.client.get(kind="DcimPhysicalInterface", id=intf2_id)

        # Create cable
        cable = await self.client.create(
            kind="DcimCable",
            data={
                "endpoints": [intf1_node, intf2_node],
            },
        )

        await cable.save()

        self.logger.info(
            f"Created cable {cable.id} between interfaces {intf1_id} and {intf2_id}"
        )  # type: ignore
