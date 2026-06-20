"""Segment deployment verification helpers for integration tests."""

import logging
from typing import Any

from infrahub_sdk import InfrahubClient

from .test_helpers import wait_for_condition

logger = logging.getLogger(__name__)


async def verify_segment_deployments(
    client: InfrahubClient,
    branch: str,
    expected_count: int = 1,
    deployment_name: str | None = None,
    max_attempts: int = 12,
    poll_interval: int = 5,
) -> dict[str, Any]:
    """Verify ManagedSegmentDeployment records created by add_segment generator.

    The segment generator runs as an asynchronous Prefect flow that is spawned
    *after* the ``CoreGeneratorDefinitionRun`` trigger task completes, so the
    deployment objects can appear several seconds after ``run_generator`` returns.
    Poll until at least ``expected_count`` records exist rather than asserting
    once, to avoid racing the generator flow.

    Args:
        client: Infrahub async client
        branch: Branch to check
        expected_count: Minimum number of segment deployments expected
        deployment_name: Optional deployment name filter (e.g., "DC4")
        max_attempts: Max polling attempts before giving up
        poll_interval: Seconds between polling attempts

    Returns:
        Dictionary with deployment_count and list of deployment detail dicts
    """
    client.default_branch = branch

    # Build filter clause — push deployment filter to the server
    dep_filter = ""
    variables: dict[str, Any] = {}
    if deployment_name:
        dep_filter = "(deployment__name__value: $deployment_name)"
        variables["deployment_name"] = deployment_name

    query = f"""
    query GetSegmentDeployments($deployment_name: String) {{
        ManagedSegmentDeployment{dep_filter} {{
            edges {{
                node {{
                    id
                    vlan_id {{ value }}
                    vni {{ value }}
                    status {{ value }}
                    segment {{
                        node {{
                            id
                            ... on ManagedVlanSegment {{
                                name {{ value }}
                            }}
                            ... on ManagedVxlanSegment {{
                                name {{ value }}
                            }}
                        }}
                    }}
                    deployment {{
                        node {{
                            id
                            name {{ value }}
                        }}
                    }}
                }}
            }}
        }}
    }}
    """

    async def _check() -> tuple[bool, list[dict]]:
        result = await client.execute_graphql(query=query, variables=variables)
        found = result.get("ManagedSegmentDeployment", {}).get("edges", [])
        return len(found) >= expected_count, found

    try:
        edges = await wait_for_condition(
            check_fn=_check,
            max_attempts=max_attempts,
            poll_interval=poll_interval,
            description=f">= {expected_count} segment deployment(s) on '{branch}' (filter={deployment_name})",
        )
    except TimeoutError:
        # One final fetch so the assertion message reports the real count.
        result = await client.execute_graphql(query=query, variables=variables)
        edges = result.get("ManagedSegmentDeployment", {}).get("edges", [])

    deployment_count = len(edges)

    assert deployment_count >= expected_count, (
        f"Expected {expected_count} segment deployment(s), found {deployment_count} "
        f"after {max_attempts * poll_interval}s\n"
        f"  Branch: {branch}\n"
        f"  Deployment filter: {deployment_name}"
    )

    deployments = [
        {
            "id": e["node"]["id"],
            "vlan_id": e["node"]["vlan_id"]["value"],
            "vni": (e["node"].get("vni") or {}).get("value"),
            "status": e["node"]["status"]["value"],
            "segment_name": (e["node"].get("segment") or {}).get("node", {}).get("name", {}).get("value"),
            "deployment_name": (e["node"].get("deployment") or {}).get("node", {}).get("name", {}).get("value"),
        }
        for e in edges
    ]

    logger.info(
        "Found %d segment deployment(s) on branch '%s' (deployment_filter=%s)",
        deployment_count,
        branch,
        deployment_name,
    )
    for d in deployments:
        logger.info(
            "  - segment=%s, vlan=%s, vni=%s, status=%s, deployment=%s",
            d["segment_name"],
            d["vlan_id"],
            d["vni"],
            d["status"],
            d["deployment_name"],
        )

    return {"deployment_count": deployment_count, "deployments": deployments}
