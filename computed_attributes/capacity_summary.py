"""Detailed capacity summary computed attributes for DataCenter and Pod."""

from typing import Any

from infrahub_sdk.transforms import InfrahubTransform


class ComputedDCCapacitySummary(InfrahubTransform):
    """Generate detailed capacity summary for DataCenter.

    Shows:
    - Overall super spine utilization
    - Overall pod utilization
    - Per-pod switch utilization breakdown (spine, leaf, tor)
    """

    query = "computed_dc_capacity_summary"

    async def transform(self, data: dict[str, Any]) -> str:
        """Generate capacity summary text.

        Args:
            data: Query result containing TopologyDataCenter with pods and devices

        Returns:
            Formatted multi-line summary showing all capacity metrics
        """
        dc_edges = data.get("TopologyDataCenter", {}).get("edges", [])
        if not dc_edges:
            return ""

        dc_data = dc_edges[0]["node"]
        dc_name = dc_data.get("name", {}).get("value", "Unknown")

        # Get design pattern limits
        design_pattern = dc_data.get("design_pattern", {}).get("node", {})
        if not design_pattern:
            return "No design pattern assigned"

        max_super_spines = design_pattern.get("maximum_super_spines", {}).get("value", 0)
        max_pods = design_pattern.get("maximum_pods", {}).get("value", 0)
        max_spines = design_pattern.get("maximum_spines", {}).get("value", 0)
        max_leafs = design_pattern.get("maximum_leafs", {}).get("value", 0)
        max_tors = design_pattern.get("maximum_tors", {}).get("value", 0)

        # Get actual deployment
        actual_super_spines = dc_data.get("amount_of_super_spines", {}).get("value", 0)
        pods = dc_data.get("children", {}).get("edges", [])
        actual_pod_count = len(pods)

        # Build summary
        lines = []
        lines.append(f"═══ {dc_name} Capacity Summary ═══")
        lines.append("")

        # Super spine utilization
        ss_pct = (actual_super_spines / max_super_spines * 100) if max_super_spines > 0 else 0
        lines.append(f"Super Spines: {actual_super_spines}/{max_super_spines} ({ss_pct:.1f}%)")

        # Pod utilization
        pod_pct = (actual_pod_count / max_pods * 100) if max_pods > 0 else 0
        lines.append(f"Pods: {actual_pod_count}/{max_pods} ({pod_pct:.1f}%)")
        lines.append("")

        # Per-pod breakdown
        if pods:
            lines.append("Pod Switch Utilization:")
            lines.append("─" * 60)

            for pod_edge in pods:
                pod = pod_edge["node"]
                pod_name = pod.get("name", {}).get("value", "Unknown")

                spine_count = pod.get("spine_count", 0)
                leaf_count = pod.get("leaf_count", 0)
                tor_count = pod.get("tor_count", 0)

                spine_pct = (spine_count / max_spines * 100) if max_spines > 0 else 0
                leaf_pct = (leaf_count / max_leafs * 100) if max_leafs > 0 else 0
                tor_pct = (tor_count / max_tors * 100) if max_tors > 0 else 0

                lines.append(f"\n{pod_name}:")
                lines.append(f"  Spines: {spine_count}/{max_spines} ({spine_pct:.1f}%)")
                lines.append(f"  Leafs:  {leaf_count}/{max_leafs} ({leaf_pct:.1f}%)")
                lines.append(f"  ToRs:   {tor_count}/{max_tors} ({tor_pct:.1f}%)")
        else:
            lines.append("No pods deployed")

        return "\n".join(lines)


class ComputedPodCapacitySummary(InfrahubTransform):
    """Generate detailed capacity summary for Pod.

    Shows:
    - Pod-level switch utilization (spine, leaf, tor)
    - Per-device port utilization
    - Overall statistics
    """

    query = "computed_pod_capacity_summary"

    async def transform(self, data: dict[str, Any]) -> str:
        """Generate capacity summary with switch counts and port utilization.

        Args:
            data: Query result containing TopologyPod with devices and interfaces

        Returns:
            Formatted multi-line summary showing switch counts and port utilization
        """
        pod_edges = data.get("TopologyPod", {}).get("edges", [])
        if not pod_edges:
            return ""

        pod_data = pod_edges[0]["node"]
        pod_name = pod_data.get("name", {}).get("value", "Unknown")

        # Get design pattern limits
        parent_dc = pod_data.get("parent", {}).get("node", {})
        design_pattern = parent_dc.get("design_pattern", {}).get("node", {})

        lines = []
        lines.append(f"═══ {pod_name} Capacity Summary ═══")
        lines.append("")

        # Add switch utilization if design pattern available
        if design_pattern:
            max_spines = design_pattern.get("maximum_spines", {}).get("value", 0)
            max_leafs = design_pattern.get("maximum_leafs", {}).get("value", 0)
            max_tors = design_pattern.get("maximum_tors", {}).get("value", 0)

            spine_count = pod_data.get("spine_count", 0)
            leaf_count = pod_data.get("leaf_count", 0)
            tor_count = pod_data.get("tor_count", 0)

            spine_pct = (spine_count / max_spines * 100) if max_spines > 0 else 0
            leaf_pct = (leaf_count / max_leafs * 100) if max_leafs > 0 else 0
            tor_pct = (tor_count / max_tors * 100) if max_tors > 0 else 0

            lines.append("SWITCH UTILIZATION:")
            lines.append(f"  Spines: {spine_count}/{max_spines} ({spine_pct:.1f}%)")
            lines.append(f"  Leafs:  {leaf_count}/{max_leafs} ({leaf_pct:.1f}%)")
            lines.append(f"  ToRs:   {tor_count}/{max_tors} ({tor_pct:.1f}%)")
            lines.append("")

        devices = pod_data.get("devices", {}).get("edges", [])

        if not devices:
            return "No devices deployed in this pod"

        lines = []
        lines.append(f"═══ {pod_name} Port Utilization ═══")
        lines.append("")

        # Group by role for better organization
        devices_by_role: dict[str, list[dict[str, Any]]] = {}
        for device_edge in devices:
            device = device_edge["node"]
            role = device.get("role", {}).get("value", "unknown")
            if role not in devices_by_role:
                devices_by_role[role] = []
            devices_by_role[role].append(device)

        # Process each role group
        for role in sorted(devices_by_role.keys()):
            role_devices = devices_by_role[role]
            lines.append(f"{role.upper()} SWITCHES ({len(role_devices)}):")
            lines.append("─" * 70)

            for device in sorted(role_devices, key=lambda d: d.get("name", {}).get("value", "")):
                device_name = device.get("name", {}).get("value", "Unknown")

                # Count total and used interfaces
                interfaces = device.get("interfaces", {}).get("edges", [])
                total_ports = len(interfaces)
                used_ports = 0

                for intf_edge in interfaces:
                    intf = intf_edge["node"]
                    # Port is used if it has a cable or IP address
                    has_cable = intf.get("cable", {}).get("node") is not None
                    has_ip = intf.get("ip_address", {}).get("node") is not None
                    if has_cable or has_ip:
                        used_ports += 1

                free_ports = total_ports - used_ports
                utilization = (used_ports / total_ports * 100) if total_ports > 0 else 0

                lines.append(
                    f"  {device_name:30} "
                    f"Total: {total_ports:3} | "
                    f"Used: {used_ports:3} | "
                    f"Free: {free_ports:3} | "
                    f"{utilization:5.1f}%"
                )

            lines.append("")

        # Summary statistics
        total_ports_all = sum(
            len(d.get("interfaces", {}).get("edges", []))
            for role_devs in devices_by_role.values()
            for d in role_devs
        )

        total_used_all = 0
        for role_devs in devices_by_role.values():
            for device in role_devs:
                for intf_edge in device.get("interfaces", {}).get("edges", []):
                    intf = intf_edge["node"]
                    has_cable = intf.get("cable", {}).get("node") is not None
                    has_ip = intf.get("ip_address", {}).get("node") is not None
                    if has_cable or has_ip:
                        total_used_all += 1

        total_free_all = total_ports_all - total_used_all
        overall_util = (total_used_all / total_ports_all * 100) if total_ports_all > 0 else 0

        lines.append("═" * 70)
        lines.append(
            f"OVERALL: Total: {total_ports_all} | "
            f"Used: {total_used_all} | "
            f"Free: {total_free_all} | "
            f"{overall_util:.1f}%"
        )

        return "\n".join(lines)
