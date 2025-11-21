"""Validation helpers for datacenter topology generators."""

from typing import Any

from infrahub_sdk.exceptions import ValidationError


def validate_dc_capacity(
    dc_name: str,
    design_pattern: dict[str, Any],
    super_spine_count: int,
    pod_count: int,
) -> None:
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

    # Get design limits
    max_super_spines = design_pattern.get("maximum_super_spines", {}).get("value", 0)
    max_pods = design_pattern.get("maximum_pods", {}).get("value", 0)

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

    if errors:
        error_msg = f"Data center '{dc_name}' capacity validation failed:\n"
        error_msg += "\n".join(f"  - {err}" for err in errors)
        raise ValidationError(error_msg)


def validate_pod_capacity(
    pod_name: str,
    design_pattern: dict[str, Any],
    spine_count: int,
    leaf_count: int,
    tor_count: int,
) -> None:
    """Validate pod capacity before generation.

    Args:
        pod_name: Pod name for error messages
        design_pattern: Design pattern with maximum limits
        spine_count: Requested spine count
        leaf_count: Requested leaf count
        tor_count: Requested ToR count

    Raises:
        ValidationError: If any capacity limits exceeded
    """
    errors = []

    if not design_pattern:
        raise ValidationError(
            f"Pod '{pod_name}' has no design pattern. Cannot validate capacity."
        )

    # Get design limits
    max_spines = design_pattern.get("maximum_spines", {}).get("value", 0)
    max_leafs = design_pattern.get("maximum_leafs", {}).get("value", 0)
    max_tors = design_pattern.get("maximum_tors", {}).get("value", 0)

    # Validate spine count
    if spine_count > max_spines:
        errors.append(
            f"Requested {spine_count} spines exceeds design pattern maximum of {max_spines}"
        )

    # Validate leaf count
    if leaf_count > max_leafs:
        errors.append(
            f"Requested {leaf_count} leafs exceeds design pattern maximum of {max_leafs}"
        )

    # Validate ToR count
    if tor_count > max_tors:
        errors.append(
            f"Requested {tor_count} ToRs exceeds design pattern maximum of {max_tors}"
        )

    if errors:
        error_msg = f"Pod '{pod_name}' capacity validation failed:\n"
        error_msg += "\n".join(f"  - {err}" for err in errors)
        raise ValidationError(error_msg)
