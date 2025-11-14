"""Transform for extracting fabric cabling information from topology."""

from infrahub_sdk.transforms import InfrahubTransform


class TopologyCabling(InfrahubTransform):
    """Extract cabling connections from DC and Pod devices.

    This transform processes both DC-level (super-spine) and Pod-level
    (spine/leaf) devices to build a cabling plan CSV output.
    """

    query = "topology_cabling"

    async def transform(self, data: dict) -> str:
        """Transform cabling data into CSV format.

        Processes both DC-level (super-spine) and Pod-level (spine/leaf) devices
        to extract cable connections between devices.

        Query response returns TopologyDeployment (which is TopologyDataCenter):
        - TopologyDeployment.devices -> DC-level devices (super-spines)
        - TopologyDeployment.children -> Pods (each with their own devices)

        Args:
            data: Query response containing TopologyDeployment

        Returns:
            CSV string with cabling information
        """
        # Create a list to hold CSV rows
        csv_rows = []

        # Add CSV header
        csv_rows.append(
            ",".join(
                [
                    "Pod",
                    "Source Rack",
                    "Source Device",
                    "Source Interface",
                    "Destination Rack",
                    "Destination Device",
                    "Destination Interface",
                ]
            )
        )

        seen_connections: set[tuple] = (
            set()
        )  # Track connections we've already processed
        device_info: dict[str, dict] = {}  # Map device names to their properties

        # Query returns TopologyDeployment which can be TopologyDataCenter
        deployment_edges = data.get("TopologyDeployment", {}).get("edges", [])
        print(f"DEBUG: Deployment edges count = {len(deployment_edges)}")

        if not deployment_edges:
            return "\n".join(csv_rows)

        dc_node = deployment_edges[0]["node"]
        dc_name = dc_node.get("name", {}).get("value", "UNKNOWN")
        print(f"DEBUG: DC name = {dc_name}")

        # Phase 1: Collect device info from all devices for later lookup
        # Process DC-level devices (super-spines)
        dc_devices = dc_node.get("devices", {}).get("edges", [])
        print(f"DEBUG: DC devices count = {len(dc_devices)}")

        for device in dc_devices:
            device_name = self._safe_get(device["node"], "name", "value")
            device_rack = self._safe_get(
                device["node"], "rack", "node", "name", "value"
            )

            # Extract device name from cables if not available
            if not device_name:
                device_name = self._extract_device_name_from_cables(device["node"])

            if device_name:
                device_info[device_name] = {
                    "pod": "",  # DC devices have no pod
                    "rack": device_rack,
                }

        # Process pod-level devices (spine, leaf)
        pods = dc_node.get("children", {}).get("edges", [])
        print(f"DEBUG: Pods count = {len(pods)}")

        for pod in pods:
            pod_typename = pod["node"].get("__typename", "MISSING")
            if pod_typename != "TopologyPod":
                continue

            pod_name = pod["node"].get("name", {}).get("value", "UNKNOWN")
            pod_devices = pod["node"].get("devices", {}).get("edges", [])

            for device in pod_devices:
                device_name = self._safe_get(device["node"], "name", "value")
                device_rack = self._safe_get(
                    device["node"], "rack", "node", "name", "value"
                )

                # Extract device name from cables if not available
                if not device_name:
                    device_name = self._extract_device_name_from_cables(device["node"])

                if device_name:
                    device_info[device_name] = {
                        "pod": pod_name,
                        "rack": device_rack,
                    }

        print(f"DEBUG: Collected device info for {len(device_info)} devices")

        # Phase 2: Process cables from all devices
        dc_count = 0
        for device in dc_devices:
            dc_count += 1
            self._process_device(
                device["node"],
                pod_name="",
                csv_rows=csv_rows,
                seen_connections=seen_connections,
                device_info=device_info,
            )

        # Process pod-level devices for cables
        pod_count = 0
        for pod in pods:
            pod_typename = pod["node"].get("__typename")
            if pod_typename != "TopologyPod":
                continue

            pod_devices = pod["node"].get("devices", {}).get("edges", [])
            pod_name = pod["node"].get("name", {}).get("value", "")

            for device in pod_devices:
                pod_count += 1
                self._process_device(
                    device["node"],
                    pod_name=pod_name,
                    csv_rows=csv_rows,
                    seen_connections=seen_connections,
                    device_info=device_info,
                )

        print(f"DEBUG: Processed {dc_count} DC devices and {pod_count} Pod devices")
        print(f"DEBUG: Generated {len(csv_rows) - 1} cable connections")

        # Join all rows with newlines to create CSV string
        csv_data = "\n".join(csv_rows)
        return csv_data

    def _safe_get(self, obj: dict | None, *keys: str) -> str:
        """Safely get nested dict values, returning empty string if any level is None.

        Args:
            obj: Dictionary to traverse
            *keys: Nested keys to access

        Returns:
            String value at path, or empty string if any level is missing/None
        """
        current = obj
        for key in keys:
            if not isinstance(current, dict):
                return ""
            current = current.get(key)
            if current is None:
                return ""
        return str(current) if current is not None else ""

    def _extract_device_name_from_cables(self, device: dict) -> str:
        """Extract device name from cable display labels.

        For devices with null names (like super-spines), extract from cable
        display_label in format: source_device-source_interface__remote_device-remote_interface

        Parses by finding the rightmost hyphen followed by an uppercase letter,
        treating everything before that as the device name.

        Args:
            device: Device node from query

        Returns:
            Device name extracted from cable label, or empty string
        """
        for interface in device.get("interfaces", {}).get("edges", []):
            cable = interface["node"].get("cable", {}).get("node")
            if cable:
                display_label = cable.get("display_label", "")
                if "__" in display_label:
                    source_part, _ = display_label.split("__", 1)
                    # Extract device name from source part (before last hyphen + uppercase letter)
                    for i in range(len(source_part) - 1, 0, -1):
                        if (
                            source_part[i] == "-"
                            and i + 1 < len(source_part)
                            and source_part[i + 1].isupper()
                        ):
                            return source_part[:i]
        return ""

    def _process_device(
        self,
        device: dict,
        pod_name: str,
        csv_rows: list,
        seen_connections: set,
        device_info: dict,
    ) -> None:
        """Process a device and extract cable connections.

        For each interface with a cable, parse the cable display_label to extract
        the remote device and interface, then create a CSV row.

        Args:
            device: Device node to process
            pod_name: Pod name for this device (empty for DC-level devices)
            csv_rows: List to append CSV rows to
            seen_connections: Set to track dedup on normalized keys
            device_info: Map of device names to their metadata (pod, rack)
        """
        # Get source device name and rack
        source_device_name = self._safe_get(device, "name", "value")
        if not source_device_name:
            source_device_name = self._extract_device_name_from_cables(device)

        source_rack = self._safe_get(device, "rack", "node", "name", "value")

        # Get source pod from device_info if built earlier
        source_pod = pod_name
        if source_device_name in device_info:
            source_pod = device_info[source_device_name].get("pod", pod_name)
            if not source_rack:
                source_rack = device_info[source_device_name].get("rack", "")

        # Process interfaces
        for interface in device.get("interfaces", {}).get("edges", []):
            interface_name = self._safe_get(interface["node"], "name", "value")
            cable = interface["node"].get("cable", {}).get("node")

            if not cable:
                continue

            display_label = cable.get("display_label", "")
            if "__" not in display_label:
                continue

            # Parse cable display label: source_device-source_interface__remote_device-remote_interface
            source_part, remote_part = display_label.split("__", 1)

            # Extract remote device and interface
            # remote_part format: "remote_device-remote_interface"
            remote_device_name, remote_interface_name = self._parse_remote_info(
                remote_part
            )

            if not remote_device_name or not remote_interface_name:
                continue

            # Create normalized connection key (sorted for dedup)
            connection_key = tuple(sorted([source_device_name, remote_device_name]))

            if connection_key in seen_connections:
                continue
            seen_connections.add(connection_key)

            # Look up remote device info
            remote_rack = ""
            if remote_device_name in device_info:
                remote_rack = device_info[remote_device_name].get("rack", "")

            # Format display values
            display_pod = source_pod if source_pod else "Not applicable"
            display_source_rack = source_rack if source_rack else "TBD"
            display_remote_rack = remote_rack if remote_rack else "TBD"

            # Create CSV row
            csv_row = ",".join(
                [
                    display_pod,
                    display_source_rack,
                    source_device_name,
                    interface_name,
                    display_remote_rack,
                    remote_device_name,
                    remote_interface_name,
                ]
            )

            csv_rows.append(csv_row)

    @staticmethod
    def _parse_remote_info(remote_part: str) -> tuple[str, str]:
        """Parse remote device and interface from display_label part.

        Format: remote_device-remote_interface
        Must find the boundary where device name ends and interface begins.
        Typically interface starts with uppercase after a hyphen.

        For example:
        - "dc-1-fab1-pod1-spine-01-Ethernet1/31" -> ("dc-1-fab1-pod1-spine-01", "Ethernet1/31")
        - "leaf-01-Eth1/1" -> ("leaf-01", "Eth1/1")

        Args:
            remote_part: String in format "device-interface" or similar

        Returns:
            Tuple of (device_name, interface_name) or ("", "") if parse fails
        """
        if not remote_part or "-" not in remote_part:
            return "", ""

        # Find the last hyphen followed by an uppercase letter (start of interface)
        for i in range(len(remote_part) - 1, 0, -1):
            if (
                remote_part[i] == "-"
                and i + 1 < len(remote_part)
                and remote_part[i + 1].isupper()
            ):
                device = remote_part[:i]
                interface = remote_part[i + 1 :]
                return device, interface

        # Fallback: split on first hyphen
        parts = remote_part.split("-", 1)
        return parts[0], parts[1] if len(parts) > 1 else ""
