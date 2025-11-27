"""Validation helpers for datacenter topology generators."""

from typing import Any

from infrahub_sdk.exceptions import ValidationError


def validate_dc_capacity(
    dc_name: str,
    design_pattern: dict[str, Any],
    super_spine_count: int,
    pod_count: int,
) -> list[str]:
    """Validate datacenter capacity before generation.

    Args:
        dc_name: Data center name for error messages
        design_pattern: Design pattern with maximum limits
        super_spine_count: Requested super spine count
        pod_count: Requested pod count

    Raises:
        ValidationError: If any capacity limits exceeded
    """
    errors = []

    if not design_pattern:
        raise ValidationError(
            f"Data center '{dc_name}' has no design pattern assigned. "
            "Cannot validate capacity."
        )

    # Get design limits (already extracted as integers by Pydantic)
    max_super_spines = design_pattern.get("maximum_super_spines", 0)
    max_pods = design_pattern.get("maximum_pods", 0)

    # Validate super spine count
    if super_spine_count > max_super_spines:
        errors.append(
            f"Requested {super_spine_count} super spines exceeds design pattern "
            f"maximum of {max_super_spines}"
        )

    # Validate pod count
    if pod_count > max_pods:
        errors.append(
            f"Requested {pod_count} pods exceeds design pattern maximum of {max_pods}"
        )

    return errors


def validate_pod_capacity(
    pod_name: str,
    design_pattern: dict[str, Any],
    spine_count: int,
    switch_count: int,
) -> list[str]:
    """Validate pod capacity before generation."""
    errors = []

    if not design_pattern:
        raise ValidationError(
            f"Pod '{pod_name}' has no design pattern. Cannot validate capacity."
        )

    # Get design limits (already extracted as integers by Pydantic)
    max_spines = design_pattern.get("maximum_spines", 0)
    max_switches = design_pattern.get("maximum_switches", 0)

    # Validate spine count
    if spine_count > max_spines:
        errors.append(
            f"Requested {spine_count} spines exceeds design pattern maximum of {max_spines}"
        )

    # Validate leaf count
    if switch_count > max_switches:
        errors.append(
            f"Requested {switch_count} leafs exceeds design pattern maximum of {max_switches}"
        )

    return errors
