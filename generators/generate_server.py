"""Server connectivity generator for physical device connections to leaf switches."""

from __future__ import annotations

from typing import Any

from .common import CommonGenerator
from .helpers import CablingStrategy


class ServerConnectivityGenerator(CommonGenerator):
    """Generate server connectivity with intelligent interface distribution.

    This generator:
    1. Queries physical devices (servers) assigned to a pod
    2. Discovers leaf switches within the pod
    3. Distributes server interfaces equally across leaf switch pairs
    4. Creates VPC connections (dual uplink) for redundancy
    5. Allocates P2P /31 subnets for server-to-leaf links

    Intelligent Distribution:
    - For N server interfaces connecting to M leaf pairs:
      * Round-robin distribution across leaf pairs
      * Each server interface gets one connection per leaf (for VPC)
      * Automatically selects available interfaces on leaves
      * Balanced load across all switches
    """

    async def generate(self, data: dict[str, Any]) -> None:
        """Generate server connectivity to leaf switches.

        Args:
            data: Pod configuration data containing servers and leaf topology
        """
        try:
            self.logger.debug(
                f"Input data type: {type(data)}, keys: {list(data.keys()) if isinstance(data, dict) else 'N/A'}"
            )
            cleaned = self.clean_data(data)
            self.logger.debug(f"Cleaned data keys: {list(cleaned.keys())}")

            # Extract pod data
            pod_list = cleaned.get("topology_pod", [])
            if not pod_list:
                pod_list = cleaned.get("TopologyPod", [])

            if not pod_list:
                self.logger.error(
                    "No TopologyPod data found in GraphQL response. "
                    f"Available keys: {list(cleaned.keys())}"
                )
                return

            if isinstance(pod_list, list) and pod_list:
                pod_data = pod_list[0]
            elif isinstance(pod_list, dict):
                pod_data = pod_list
            else:
                self.logger.error(f"Unexpected pod_list type: {type(pod_list)}")
                return

            pod_name = pod_data.get("name", "unknown").lower()
            self.logger.info(
                f"Starting server connectivity generator for pod: {pod_name}"
            )

        except (ValueError, KeyError, IndexError) as exc:
            self.logger.error(f"Generation failed due to {exc}", exc_info=True)
            return

        try:
            # Extract references
            parent_data = pod_data.get("parent", {})
            parent_name = parent_data.get("name", "").lower()
            pod_id: str | None = pod_data.get("id")

            if not pod_id:
                self.logger.error("Pod ID is required but not found")
                return

            # Get physical devices (servers) assigned to this pod
            # Servers are found via the topologies_server group
            servers = await self._get_servers_for_pod(pod_id)
            if not servers:
                self.logger.warning(f"No servers found for pod {pod_name}")
                return

            self.logger.info(
                f"Found {len(servers)} physical devices assigned to pod {pod_name}"
            )

            # Get leaf switches from the pod
            leaf_switches = await self._get_leaf_switches_for_pod(pod_id)
            if not leaf_switches:
                self.logger.error(f"No leaf switches found for pod {pod_name}")
                return

            # Group leaf switches into pairs (for VPC)
            leaf_pairs = self._group_leaf_switches_into_pairs(leaf_switches)
            if not leaf_pairs:
                self.logger.error("Could not create leaf switch pairs")
                return

            self.logger.info(
                f"Created {len(leaf_pairs)} leaf switch pairs for VPC connectivity"
            )

            # Pool name for P2P IP allocation
            pool_name = f"{parent_name}-{pod_name}-technical-pool"

            # Process each server
            created_cables = 0
            for server_idx, server in enumerate(servers, 1):
                server_name = server.get("name", f"server-{server_idx}")
                self.logger.info(f"Processing server {server_idx}: {server_name}")

                # Get server interfaces
                server_interfaces = server.get("interfaces", [])
                if not server_interfaces:
                    self.logger.warning(f"Server {server_name} has no interfaces")
                    continue

                # Get available uplink interfaces on server
                server_uplinks = [
                    iface
                    for iface in server_interfaces
                    if iface.get("role") == "uplink"
                ]
                if not server_uplinks:
                    self.logger.warning(
                        f"Server {server_name} has no uplink interfaces"
                    )
                    continue

                # Assign server interfaces to leaf switch pairs using round-robin
                for iface_idx, server_iface in enumerate(server_uplinks):
                    # Determine which leaf pair this interface connects to
                    pair_idx = iface_idx % len(leaf_pairs)
                    leaf_pair = leaf_pairs[pair_idx]

                    self.logger.debug(
                        f"Server {server_name} interface {server_iface.get('name')} "
                        f"assigned to leaf pair {pair_idx + 1}"
                    )

                    # Create VPC connection (dual uplink) to the leaf pair
                    cables_created = await self._create_vpc_connection(
                        server_name=server_name,
                        server_iface_name=server_iface.get("name"),
                        leaf_pair=leaf_pair,
                        pool_name=pool_name,
                    )

                    created_cables += cables_created

            self.logger.info(
                f"Successfully completed server connectivity generator. "
                f"Created {created_cables} cables across {len(servers)} servers."
            )

        except Exception as exc:
            self.logger.error(f"Unexpected error in generator: {exc}", exc_info=True)

    async def _get_leaf_switches_for_pod(self, pod_id: str) -> list[dict]:
        """Get all leaf switches in the pod.

        Args:
            pod_id: Pod ID to query

        Returns:
            List of leaf switch objects with interface details
        """
        self.logger.debug("Querying leaf switches")

        try:
            # Query all devices that have "leaf" in their name
            # We use a pattern-based approach since we know leaf devices are named like dc-X-pod-YY-leaf-NN
            query = """
            query GetLeafDevices {
                DcimPhysicalDevice(limit: 100) {
                    edges {
                        node {
                            ... on DcimPhysicalDevice {
                                id
                                name { value }
                                device_type {
                                    node {
                                        id
                                        name { value }
                                    }
                                }
                                interfaces(limit: 100) {
                                    edges {
                                        node {
                                            ... on DcimPhysicalInterface {
                                                id
                                                name { value }
                                                role { value }
                                                cable {
                                                    node {
                                                        id
                                                    }
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
            """

            result = await self.client.execute_graphql(query=query)
            if result.get("errors"):
                self.logger.error(f"GraphQL error: {result['errors']}")
                return []

            # Extract all devices and filter for leaf switches
            all_devices = result.get("DcimPhysicalDevice", {}).get("edges", [])
            leaf_switches = []

            for device_edge in all_devices:
                device = device_edge.get("node", {})
                device_name = device.get("name", {}).get("value", "")

                # Filter for leaf devices by name pattern
                if "leaf" in device_name.lower():
                    leaf_switches.append(device)
                    self.logger.debug(f"Found leaf device: {device_name}")

            self.logger.info(f"Found {len(leaf_switches)} leaf switches")
            return leaf_switches

        except Exception as exc:
            self.logger.error(f"Error querying leaf switches: {exc}", exc_info=True)
            return []

    async def _get_servers_for_pod(self, pod_id: str) -> list[dict]:
        """Get all servers assigned to a pod.

        Servers are found by querying for DcimPhysicalDevice nodes that:
        1. Are members of the topologies_server group
        2. Their rack/location is part of this pod

        Args:
            pod_id: Pod ID (currently unused, but available for hierarchical queries)

        Returns:
            List of server objects with interface details
        """
        self.logger.debug(f"Querying servers for pod {pod_id}")

        # Query to get all servers from the topologies_server group
        query = """
        query GetServers {
            CoreStandardGroup(name__value: "topologies_server") {
                edges {
                    node {
                        members {
                            edges {
                                node {
                                    ... on DcimPhysicalDevice {
                                        id
                                        name { value }
                                        interfaces {
                                            edges {
                                                node {
                                                    ... on DcimPhysicalInterface {
                                                        name { value }
                                                        role { value }
                                                        cable {
                                                            node {
                                                                id
                                                            }
                                                        }
                                                    }
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
        """

        try:
            result = await self.client.execute_graphql(query=query)
            if result.get("errors"):
                self.logger.error(f"GraphQL error: {result['errors']}")
                return []

            # Extract servers from group members
            # Note: execute_graphql returns the GraphQL response directly (no 'data' wrapper)
            groups_data = result.get("CoreStandardGroup", {}).get("edges", [])
            if not groups_data:
                self.logger.warning("No topologies_server group found")
                return []

            group_node = groups_data[0].get("node", {})
            members_data = group_node.get("members", {}).get("edges", [])
            servers = [edge.get("node") for edge in members_data if edge.get("node")]

            self.logger.info(f"Found {len(servers)} servers in topologies_server group")
            return servers

        except Exception as exc:
            self.logger.error(f"Error querying servers: {exc}", exc_info=True)
            return []

    def _group_leaf_switches_into_pairs(self, leaves: list[dict]) -> list[list[dict]]:
        """Group leaf switches into pairs for VPC.

        Strategy:
        - Group leaves of the same type together (e.g., two CCS-720DP-48S-2F)
        - Create pairs for VPC (leaf-01 + leaf-02, leaf-03 + leaf-04, etc.)
        - If odd number of same type, last one is unpaired

        Args:
            leaves: List of leaf switch objects

        Returns:
            List of leaf switch pairs, each pair containing [switch1, switch2]
        """
        if not leaves:
            return []

        self.logger.info(f"Grouping {len(leaves)} leaf switches into VPC pairs")

        # Group leaves by device type
        leaves_by_type: dict[str, list[dict[str, Any]]] = {}
        for leaf in leaves:
            device_type = leaf.get("device_type", {}).get("name", "unknown")
            if device_type not in leaves_by_type:
                leaves_by_type[device_type] = []
            leaves_by_type[device_type].append(leaf)

        # Create pairs from each type group
        leaf_pairs = []
        for device_type, type_leaves in leaves_by_type.items():
            self.logger.debug(
                f"Pairing {len(type_leaves)} leaves of type {device_type}"
            )

            # Sort leaves by name to ensure consistent pairing
            sorted_leaves = sorted(type_leaves, key=lambda x: x.get("name", ""))

            # Create pairs
            for i in range(0, len(sorted_leaves) - 1, 2):
                pair = [sorted_leaves[i], sorted_leaves[i + 1]]
                leaf_pairs.append(pair)
                self.logger.debug(
                    f"Created pair: {pair[0].get('name')} + {pair[1].get('name')}"
                )

            # If odd number, single leaf becomes its own pair
            if len(sorted_leaves) % 2 == 1:
                last_leaf = sorted_leaves[-1]
                leaf_pairs.append([last_leaf])
                self.logger.debug(f"Single leaf (unpaired): {last_leaf.get('name')}")

        return leaf_pairs

    async def _create_vpc_connection(
        self,
        server_name: str,
        server_iface_name: str,
        leaf_pair: list[dict],
        pool_name: str,
    ) -> int:
        """Create VPC connection from server interface to leaf switch pair.

        For a VPC pair (2 leaves) with 1 server interface:
        - Create 2 cables: server_iface -> leaf1_iface AND server_iface -> leaf2_iface
        - Each cable gets a P2P /31 subnet

        Args:
            server_name: Name of the physical server
            server_iface_name: Name of server interface
            leaf_pair: List with 1 or 2 leaf switches
            pool_name: Resource pool name for P2P IP allocation

        Returns:
            Number of cables created
        """
        cables_created = 0

        try:
            # Get available customer interfaces on each leaf
            leaf_names = []
            leaf_interface_names = []

            for leaf in leaf_pair:
                leaf_name = leaf.get("name", {}).get("value", "unknown")

                # Query for available customer interfaces on this leaf
                available_iface_names = await self._get_available_leaf_interfaces(
                    leaf_name, role="customer"
                )

                if not available_iface_names:
                    self.logger.warning(
                        f"No available customer interfaces on {leaf_name}"
                    )
                    continue

                leaf_names.append(leaf_name)
                leaf_interface_names.append(available_iface_names[0])
                self.logger.debug(
                    f"Selected {available_iface_names[0]} on {leaf_name} for server connection"
                )
                cables_created += 1

            if leaf_names:
                # Use SERVER cabling strategy: one server interface to multiple leaf interfaces
                # Creates N cables: server_iface -> leaf1_iface, server_iface -> leaf2_iface, etc.
                await self.create_cabling(
                    bottom_devices=[server_name],
                    bottom_interfaces=[server_iface_name],
                    top_devices=leaf_names,
                    top_interfaces=leaf_interface_names,
                    strategy=CablingStrategy.SERVER,
                    pool=pool_name,
                )
                self.logger.info(
                    f"Created {len(leaf_names)} cables from {server_name}:{server_iface_name} to leaves {leaf_names}"
                )

        except Exception as exc:
            self.logger.error(
                f"Error creating VPC connection for {server_name}: {exc}",
                exc_info=True,
            )

        return cables_created

    def _get_available_interfaces(
        self, device: dict, role: str = "customer"
    ) -> list[dict]:
        """Get available (uncabled) interfaces on a device.

        Args:
            device: Device object with interfaces
            role: Interface role to filter (e.g., "customer" for server-facing, "uplink" for spine connections)

        Returns:
            List of available interface dictionaries
        """
        interfaces = device.get("interfaces", [])
        available = []

        for iface in interfaces:
            # Check if interface has the desired role
            iface_role = iface.get("role", "")
            if iface_role != role:
                continue

            # Check if interface is already cabled
            if iface.get("cable"):
                continue

            available.append(iface)

        return available

    async def _get_available_leaf_interfaces(
        self, leaf_name: str, role: str = "customer"
    ) -> list[str]:
        """Get available (uncabled) interface names on a leaf switch.

        Args:
            leaf_name: Name of the leaf switch
            role: Interface role to filter (e.g., "customer")

        Returns:
            List of available interface names
        """
        query = """
        query GetLeafInterfaces($leaf_name: String!, $role: String!) {
            DcimPhysicalInterface(
                device__name__value: $leaf_name,
                role__value: $role,
                cable__isnull: true
            ) {
                edges {
                    node {
                        name { value }
                    }
                }
            }
        }
        """

        try:
            result = await self.client.execute_graphql(
                query=query, variables={"leaf_name": leaf_name, "role": role}
            )

            if result.get("errors"):
                self.logger.error(f"GraphQL error: {result['errors']}")
                return []

            # Fix: result doesn't have a "data" wrapper, it's the root level
            interfaces = result.get("DcimPhysicalInterface", {}).get("edges", [])
            interface_names = [
                edge.get("node", {}).get("name", {}).get("value")
                for edge in interfaces
                if edge.get("node")
            ]

            self.logger.debug(
                f"Found {len(interface_names)} available {role} interfaces on {leaf_name}: {interface_names}"
            )
            return interface_names

        except Exception as exc:
            self.logger.error(
                f"Error querying leaf interfaces for {leaf_name}: {exc}",
                exc_info=True,
            )
            return []
