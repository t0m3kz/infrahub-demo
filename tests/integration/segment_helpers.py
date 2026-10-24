"""Segment deployment fetchers (test_19, test_20)."""

import logging
from typing import Any

from infrahub_sdk import InfrahubClient

from .wait_helpers import wait_for_condition

logger = logging.getLogger(__name__)


# ======================================================================
# Segment deployments
# ======================================================================


async def fetch_segment_deployments(
    client: InfrahubClient,
    branch: str,
    expected_count: int = 1,
    deployment_name: str | None = None,
    max_attempts: int = 12,
    poll_interval: int = 5,
) -> dict[str, Any]:
    """Fetch ManagedSegmentDeployment records created by add_segment generator.

    The segment generator runs as an asynchronous Prefect flow that is spawned
    *after* the ``CoreGeneratorDefinitionRun`` trigger task completes, so the
    deployment objects can appear several seconds after ``run_generator`` returns.
    Polls until at least ``expected_count`` records exist rather than fetching
    once, to avoid racing the generator flow — this is waiting for async work
    to settle, not a business assertion; callers must check the returned count.

    Args:
        client: Infrahub async client
        branch: Branch to check
        expected_count: Minimum number of segment deployments to poll for
        deployment_name: Optional deployment name filter (e.g., "DC4")
        max_attempts: Max polling attempts before giving up
        poll_interval: Seconds between polling attempts

    Returns:
        Dictionary with deployment_count and list of deployment detail dicts
    """
    client.default_branch = branch

    # Build filter clause — push deployment filter to the server
    # Declared only with its filter: GraphQL rejects an unused variable.
    var_decl = ""
    dep_filter = ""
    variables: dict[str, Any] = {}
    if deployment_name:
        var_decl = "($deployment_name: String)"
        dep_filter = "(deployment__name__value: $deployment_name)"
        variables["deployment_name"] = deployment_name

    query = f"""
    query GetSegmentDeployments{var_decl} {{
        ManagedSegmentDeployment{dep_filter} {{
            edges {{
                node {{
                    id
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
        # One final fetch so the caller's assertion message reports the real count.
        result = await client.execute_graphql(query=query, variables=variables)
        edges = result.get("ManagedSegmentDeployment", {}).get("edges", [])

    deployment_count = len(edges)

    deployments = [
        {
            "id": e["node"]["id"],
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
            "  - segment=%s, vni=%s, status=%s, deployment=%s",
            d["segment_name"],
            d["vni"],
            d["status"],
            d["deployment_name"],
        )

    return {"deployment_count": deployment_count, "deployments": deployments}


async def fetch_vlan_domain_segments(
    *,
    client: InfrahubClient,
    branch: str,
    expected_count: int = 1,
    segment_id: str | None = None,
    max_attempts: int = 12,
    poll_interval: int = 5,
) -> dict[str, Any]:
    """Poll for ManagedVlanDomainSegment records — per-(segment, VLAN domain)
    local VLAN ID realizations. LOCAL VLAN ID moved off ManagedSegmentDeployment
    (DC-wide) onto this node (per-VLAN-domain: an MLAG pair or standalone
    device) — see schemas/extensions/service/segment.yml.

    Args:
        client: Async Infrahub client
        branch: Branch to query
        expected_count: Minimum record count to poll for
        segment_id: Optional segment id to filter by
        max_attempts: Max polling attempts
        poll_interval: Seconds between polling attempts

    Returns:
        Dictionary with record_count and list of record detail dicts
    """
    client.default_branch = branch

    # GraphQL rejects a declared-but-unused variable, so $segment_id is only
    # declared alongside the filter that uses it.
    var_decl = ""
    seg_filter = ""
    variables: dict[str, Any] = {}
    if segment_id:
        var_decl = "($segment_id: [ID])"
        seg_filter = "(segment__ids: $segment_id)"
        variables["segment_id"] = [segment_id]

    query = f"""
    query GetVlanDomainSegments{var_decl} {{
        ManagedVlanDomainSegment{seg_filter} {{
            edges {{
                node {{
                    id
                    vlan_id {{ value }}
                    segment {{
                        node {{
                            id
                            ... on ManagedVxlanSegment {{
                                name {{ value }}
                            }}
                        }}
                    }}
                    vlan_domain {{
                        node {{
                            id
                            display_label
                        }}
                    }}
                }}
            }}
        }}
    }}
    """

    async def _check() -> tuple[bool, list[dict]]:
        result = await client.execute_graphql(query=query, variables=variables)
        found = result.get("ManagedVlanDomainSegment", {}).get("edges", [])
        return len(found) >= expected_count, found

    try:
        edges = await wait_for_condition(
            check_fn=_check,
            max_attempts=max_attempts,
            poll_interval=poll_interval,
            description=f">= {expected_count} VLAN domain segment(s) on '{branch}' (segment={segment_id})",
        )
    except TimeoutError:
        result = await client.execute_graphql(query=query, variables=variables)
        edges = result.get("ManagedVlanDomainSegment", {}).get("edges", [])

    record_count = len(edges)

    records = [
        {
            "id": e["node"]["id"],
            "vlan_id": e["node"]["vlan_id"]["value"],
            "segment_name": (e["node"].get("segment") or {}).get("node", {}).get("name", {}).get("value"),
            "vlan_domain_id": (e["node"].get("vlan_domain") or {}).get("node", {}).get("id"),
            "vlan_domain_label": (e["node"].get("vlan_domain") or {}).get("node", {}).get("display_label"),
        }
        for e in edges
    ]

    logger.info(
        "Found %d VLAN domain segment(s) on branch '%s' (segment_filter=%s)",
        record_count,
        branch,
        segment_id,
    )
    for r in records:
        logger.info(
            "  - segment=%s, vlan_id=%s, vlan_domain=%s",
            r["segment_name"],
            r["vlan_id"],
            r["vlan_domain_label"],
        )

    return {"record_count": record_count, "records": records}
