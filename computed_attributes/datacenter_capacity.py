"""Computed attributes for TopologyDataCenter capacity metrics."""

from typing import Any

from infrahub_sdk.transforms import InfrahubTransform


class ComputedSuperSpineCapacity(InfrahubTransform):
    """Calculate super spine capacity utilization percentage.

    Returns the percentage of super spine slots used compared to the
    design pattern maximum. For example, if a DC has 3 super spines
    and the design allows 4, this returns "75.0".
    """

    query = "computed_super_spine_capacity"

    async def transform(self, data: dict[str, Any]) -> str:
        """Compute super spine capacity percentage.

        Args:
            data: Query result containing TopologyDataCenter data

        Returns:
            String representation of percentage (0-100),
            or empty string if design pattern not configured
        """
        # Extract DC data from query result
        dc_edges = data.get("TopologyDataCenter", {}).get("edges", [])
        if not dc_edges:
            return ""

        dc_data = dc_edges[0]["node"]

        # Get actual super spine count
        actual = dc_data.get("amount_of_super_spines", {}).get("value")
        if actual is None:
            return ""

        # Get design pattern maximum
        design_pattern = dc_data.get("design_pattern", {}).get("node", {})
        max_super_spines = design_pattern.get("maximum_super_spines", {}).get("value")

        if not max_super_spines or max_super_spines == 0:
            return ""

        # Calculate percentage
        percentage = (float(actual) / float(max_super_spines)) * 100
        return str(round(percentage, 1))


class ComputedPodCapacity(InfrahubTransform):
    """Calculate pod capacity utilization percentage.

    Returns the percentage of pod slots used compared to the
    design pattern maximum. For example, if a DC has 2 pods
    and the design allows 8, this returns "25.0".
    """

    query = "computed_pod_capacity"

    async def transform(self, data: dict[str, Any]) -> str:
        """Compute pod capacity percentage.

        Args:
            data: Query result containing TopologyDataCenter data

        Returns:
            String representation of percentage (0-100),
            or empty string if design pattern not configured
        """
        # Extract DC data from query result
        dc_edges = data.get("TopologyDataCenter", {}).get("edges", [])
        if not dc_edges:
            return ""

        dc_data = dc_edges[0]["node"]

        # Get actual pod count from children
        children = dc_data.get("children", {}).get("edges", [])
        actual_pods = len(children)

        # Get design pattern maximum
        design_pattern = dc_data.get("design_pattern", {}).get("node", {})
        max_pods = design_pattern.get("maximum_pods", {}).get("value")

        if not max_pods or max_pods == 0:
            return ""

        # Calculate percentage
        percentage = (float(actual_pods) / float(max_pods)) * 100
        return str(round(percentage, 1))
