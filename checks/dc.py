"""Validate Data Center deployment against design pattern constraints."""

from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from .common import get_data


class CheckDataCenterCapacity(InfrahubCheck):
    """Check Data Center deployment against design pattern limits."""

    query = "dc_validation"

    def validate(self, data: Any) -> None:
        """Validate Data Center capacity and design compliance.

        Validates:
        - Super spine count vs design maximum
        - Pod count vs design maximum
        - Per-pod spine count vs design maximum
        - Per-pod leaf count vs design maximum
        - Per-pod ToR count vs design maximum
        """
        data = get_data(data)
        errors: list[str] = []

        if not data:
            self.log_error(message="No data center data found")
            return

        dc_name = data.get("name", "Unknown")
        design_pattern = data.get("design_pattern", {})

        if not design_pattern:
            self.log_error(
                message=f"Data center '{dc_name}' has no design pattern assigned"
            )
            return

        # Get design limits
        max_super_spines = design_pattern.get("maximum_super_spines", 0)
        max_pods = design_pattern.get("maximum_pods", 0)
        max_spines = design_pattern.get("maximum_spines", 0)
        max_leafs = design_pattern.get("maximum_leafs", 0)
        max_tors = design_pattern.get("maximum_tors", 0)

        # Get actual deployment
        actual_super_spines = data.get("amount_of_super_spines", 0)
        pods = data.get("children", [])
        actual_pod_count = len(pods)

        # Validate super spines
        if actual_super_spines > max_super_spines:
            errors.append(
                f"Super spine count ({actual_super_spines}) exceeds design maximum ({max_super_spines})"
            )

        # Validate pod count
        if actual_pod_count > max_pods:
            errors.append(
                f"Pod count ({actual_pod_count}) exceeds design maximum ({max_pods})"
            )

        # Validate each pod
        for pod in pods:
            pod_name = pod.get("name", "Unknown")

            # Get counts directly from GraphQL query aggregation
            spine_count = pod.get("spine_count", 0)
            leaf_count = pod.get("leaf_count", 0)
            tor_count = pod.get("tor_count", 0)

            # Validate against design
            if spine_count > max_spines:
                errors.append(
                    f"Pod '{pod_name}': Spine count ({spine_count}) exceeds design maximum ({max_spines})"
                )

            if leaf_count > max_leafs:
                errors.append(
                    f"Pod '{pod_name}': Leaf count ({leaf_count}) exceeds design maximum ({max_leafs})"
                )

            if tor_count > max_tors:
                errors.append(
                    f"Pod '{pod_name}': ToR count ({tor_count}) exceeds design maximum ({max_tors})"
                )

        # Display all errors
        if errors:
            for error in errors:
                self.log_error(message=error)
