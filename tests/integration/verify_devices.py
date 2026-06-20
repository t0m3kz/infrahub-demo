"""Device verification helpers for integration tests."""

import asyncio
import logging
from typing import Any

from infrahub_sdk import InfrahubClient

from .test_constants import DATA_PROPAGATION_DELAY

logger = logging.getLogger(__name__)


async def verify_devices_created(
    client: InfrahubClient,
    branch: str,
    expected_min_count: int = 1,
    device_types: list[str] | None = None,
) -> dict[str, Any]:
    """Verify devices were created by generator.

    Args:
        client: Infrahub async client
        branch: Branch to check
        expected_min_count: Minimum number of devices expected
        device_types: Optional list of device roles to check for (e.g., ["spine", "leaf"])

    Returns:
        Dictionary with device count and breakdown by role
    """
    client.default_branch = branch
    await asyncio.sleep(DATA_PROPAGATION_DELAY)

    # Build a single GraphQL query — optionally with per-role counts
    role_aliases = {}
    role_fragments = ""
    if device_types:
        for role in device_types:
            alias = role.replace("-", "_")
            role_aliases[role] = alias
            role_fragments += f'    {alias}: DcimDevice(role__value: "{role}") {{ count }}\n'

    query = f"""
    query {{
        all: DcimDevice {{ count }}
{role_fragments}    }}
    """

    result = await client.execute_graphql(query=query)
    device_count = result.get("all", {}).get("count", 0)

    assert device_count >= expected_min_count, (
        f"Expected at least {expected_min_count} device(s), found {device_count}\n  Branch: {branch}"
    )

    logger.info("Found %d devices on branch '%s'", device_count, branch)

    breakdown = {}
    if device_types:
        for role in device_types:
            alias = role_aliases[role]
            count = result.get(alias, {}).get("count", 0)
            breakdown[role] = count
            logger.info("  - %s: %d", role, count)

    return {
        "device_count": device_count,
        "breakdown": breakdown,
    }


async def snapshot_device_counts_by_role(
    client: InfrahubClient,
    branch: str,
    roles: list[str],
) -> dict[str, int]:
    """Snapshot current device counts by role for a branch.

    Args:
        client: Infrahub async client
        branch: Branch to query
        roles: Device roles to count (e.g., ["spine", "leaf", "tor"])

    Returns:
        Mapping role -> count
    """
    result = await verify_devices_created(
        client=client,
        branch=branch,
        expected_min_count=0,
        device_types=roles,
    )
    return {role: int(result["breakdown"].get(role, 0)) for role in roles}


async def verify_device_counts_growth(
    client: InfrahubClient,
    branch: str,
    baseline_counts: dict[str, int],
    min_growth_by_role: dict[str, int],
) -> dict[str, Any]:
    """Verify per-role device counts grew by expected minimum deltas.

    Args:
        client: Infrahub async client
        branch: Branch to query
        baseline_counts: Baseline mapping role -> count
        min_growth_by_role: Minimum required increase for each role

    Returns:
        Dictionary with current counts and computed deltas
    """
    roles = list(baseline_counts.keys())
    current = await snapshot_device_counts_by_role(client=client, branch=branch, roles=roles)

    deltas = {role: current[role] - baseline_counts[role] for role in roles}
    failures = [
        f"{role}: expected +{min_growth_by_role.get(role, 0)}, got {deltas[role]}"
        for role in roles
        if deltas[role] < min_growth_by_role.get(role, 0)
    ]

    assert not failures, f"Device count growth check failed on branch '{branch}':\n" + "\n".join(
        f"  - {line}" for line in failures
    )

    logger.info("Per-role device growth verified on branch '%s': deltas=%s", branch, deltas)
    return {"current": current, "deltas": deltas}
