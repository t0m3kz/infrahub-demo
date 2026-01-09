"""Transform for extracting fabric cabling information from topology."""

from typing import Any

from infrahub_sdk.transforms import InfrahubTransform

from .common import clean_data


class TopologyCabling(InfrahubTransform):
    """Extract cabling connections from DC and Pod devices.

    This transform processes topology data to extract cable connections
    between devices and generates a CSV cable matrix output.
    """

    query = "topology_cabling"

    async def transform(self, data: dict[str, Any]) -> str:
        """Transform cabling data into CSV format.

        Processes TopologyDeployment data to extract cable connections
        from all pods and devices within the topology. Deduplicates cables
        by cable ID to ensure each connection appears only once.

        Args:
            data: Query response containing TopologyDeployment

        Returns:
            CSV string with cabling information

        Raises:
            ValueError: If data extraction or validation fails
        """
        try:
            # Clean the raw GraphQL response
            cleaned_data = clean_data(data)

            # Extract cables with deduplication by cable ID
            cables = self._extract_unique_cables(cleaned_data)

            # Generate CSV output
            return self._generate_csv(cables)

        except (ValueError, KeyError, TypeError) as e:
            raise ValueError(f"Failed to transform cabling data: {e}") from e

    def _extract_unique_cables(self, topology_data: dict[str, Any]) -> list[dict[str, Any]]:
        """Extract unique cables from topology data, avoiding duplicates.

        Each cable appears twice in the GraphQL response (once from each endpoint),
        so we deduplicate by cable ID.

        Args:
            topology_data: Cleaned topology deployment data from GraphQL

        Returns:
            List of unique cable dictionaries
        """
        cables_by_id: dict[str, dict[str, Any]] = {}

        # Normalize root (may be raw or wrapped in TopologyDeployment)
        root = topology_data
        if "TopologyDeployment" in topology_data:
            deployments = topology_data.get("TopologyDeployment", [])
            if deployments and isinstance(deployments, list):
                root = deployments[0]

        # Prefer pod children when present; otherwise use deployment-level devices
        pods = root.get("children", []) if isinstance(root, dict) else []
        if pods:
            for pod in pods:
                if not isinstance(pod, dict):
                    continue
                pod_name = self._get_safe_value(pod, "name", "Not applicable")
                devices = pod.get("devices", [])
                self._collect_cables_from_devices(devices, pod_name, cables_by_id)
        else:
            pod_name = self._get_safe_value(root, "name", "Not applicable") if isinstance(root, dict) else ""
            devices = root.get("devices", []) if isinstance(root, dict) else []
            self._collect_cables_from_devices(devices, pod_name, cables_by_id)

        return list(cables_by_id.values())

    def _collect_cables_from_devices(
        self,
        devices: list[dict[str, Any]],
        pod_name: str,
        cables_by_id: dict[str, dict[str, Any]],
    ) -> None:
        """Extract cables from a list of devices for a given pod/context."""

        for device in devices:
            if not isinstance(device, dict):
                continue

            for interface in device.get("interfaces", []):
                if not isinstance(interface, dict):
                    continue

                cable_info = interface.get("cable")
                if not cable_info or not isinstance(cable_info, dict):
                    continue

                cable_id = cable_info.get("id")
                if not cable_id or cable_id in cables_by_id:
                    continue

                cable_data = self._extract_cable_data(cable_info, pod_name)
                if cable_data:
                    cables_by_id[cable_id] = cable_data

    def _extract_cable_data(self, cable_info: dict[str, Any], pod_name: str) -> dict[str, Any] | None:
        """Extract cable connection data from cable information.

        Parses cable endpoints to extract source and destination device/interface
        information, including rack details.

        Args:
            cable_info: Cable information containing type and endpoints
            pod_name: Pod name for the cable

        Returns:
            Cable record dictionary or None if extraction fails
        """
        endpoints = cable_info.get("endpoints", [])

        if len(endpoints) < 2:
            return None

        # Extract source and destination endpoints
        source_endpoint = endpoints[0]
        dest_endpoint = endpoints[1]

        # Parse endpoint HFID to extract device and interface names
        source_device, source_interface = self._parse_hfid(source_endpoint.get("hfid", []))
        dest_device, dest_interface = self._parse_hfid(dest_endpoint.get("hfid", []))

        if not all([source_device, source_interface, dest_device, dest_interface]):
            return None

        # Extract rack information from endpoints
        source_rack = self._extract_rack_from_endpoint(source_endpoint)
        dest_rack = self._extract_rack_from_endpoint(dest_endpoint)

        # Extract cable type
        cable_type = cable_info.get("type", "Unknown")
        if isinstance(cable_type, dict):
            cable_type = cable_type.get("value", "Unknown")

        return {
            "pod": pod_name,
            "source_rack": source_rack,
            "source_device": source_device,
            "source_interface": source_interface,
            "destination_rack": dest_rack,
            "destination_device": dest_device,
            "destination_interface": dest_interface,
            "cable_type": cable_type,
        }

    @staticmethod
    def _parse_hfid(hfid: list[str] | str) -> tuple[str, str]:
        """Parse HFID to extract device name and interface name.

        HFID format: [device_name, interface_name]

        Args:
            hfid: HFID as list or string

        Returns:
            Tuple of (device_name, interface_name)
        """
        if isinstance(hfid, list) and len(hfid) >= 2:
            return hfid[0], hfid[1]
        elif isinstance(hfid, str):
            parts = hfid.split("/")
            if len(parts) >= 2:
                return parts[0], "/".join(parts[1:])
        return "", ""

    @staticmethod
    def _extract_rack_from_endpoint(endpoint: dict[str, Any]) -> str:
        """Extract rack name from endpoint device information.

        Args:
            endpoint: Endpoint information containing device and rack details

        Returns:
            Rack name or "TBD" if not found
        """
        device_info = endpoint.get("device", {})
        if isinstance(device_info, dict) and device_info.get("rack"):
            rack_info = device_info["rack"]
            if isinstance(rack_info, dict) and rack_info.get("name"):
                return rack_info["name"]
        return "TBD"

    @staticmethod
    def _get_safe_value(data: dict[str, Any], key: str, default: str = "") -> str:
        """Safely extract value from nested data structure.

        Args:
            data: Data dictionary
            key: Key to extract
            default: Default value if key not found

        Returns:
            Extracted value or default
        """
        value = data.get(key)
        if value is None:
            return default
        if isinstance(value, dict):
            return value.get("value", default)
        return str(value) if value else default

    def _generate_csv(self, cables: list[dict[str, Any]]) -> str:
        """Generate CSV output from cable data.

        Args:
            cables: List of cable dictionaries

        Returns:
            CSV string with proper formatting
        """
        # CSV header
        header = ",".join(
            [
                "Pod",
                "Source Rack",
                "Source Device",
                "Source Interface",
                "Destination Rack",
                "Destination Device",
                "Destination Interface",
                "Cable type",
            ]
        )

        csv_rows = [header]

        for cable in cables:
            row = ",".join(
                [
                    cable.get("pod", "Not applicable"),
                    cable.get("source_rack", "TBD"),
                    cable.get("source_device", "Unknown"),
                    cable.get("source_interface", "Unknown"),
                    cable.get("destination_rack", "TBD"),
                    cable.get("destination_device", "Unknown"),
                    cable.get("destination_interface", "Unknown"),
                    cable.get("cable_type", "Unknown"),
                ]
            )
            csv_rows.append(row)

        return "\n".join(csv_rows) + "\n"
