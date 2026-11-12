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
    """Fetch ManagedSegmentDeployment records created by add_segment generator."""
    client.default_branch = branch

    # Build filter clause — push deployment filter to the server Declared only with its filter
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


async def fetch_segment_vlans(
    *,
    client: InfrahubClient,
    branch: str,
    expected_count: int = 1,
    segment_id: str | None = None,
    max_attempts: int = 12,
    poll_interval: int = 5,
) -> dict[str, Any]:
    """Poll for ManagedSegmentVlan records — per-(segment deployment, device) local VLAN ID realizations."""
    client.default_branch = branch

    # GraphQL rejects a declared-but-unused variable
    var_decl = ""
    seg_filter = ""
    variables: dict[str, Any] = {}
    if segment_id:
        var_decl = "($segment_id: [ID])"
        seg_filter = "(segment__ids: $segment_id)"
        variables["segment_id"] = [segment_id]

    query = f"""
    query GetSegmentVlans{var_decl} {{
        ManagedSegmentDeployment{seg_filter} {{
            edges {{
                node {{
                    id
                    vni {{ value }}
                    segment {{
                        node {{
                            id
                            ... on ManagedVxlanSegment {{
                                name {{ value }}
                            }}
                        }}
                    }}
                    vlans {{
                        edges {{
                            node {{
                                id
                                vlan_id {{ value }}
                                device {{
                                    node {{
                                        id
                                        display_label
                                    }}
                                }}
                            }}
                        }}
                    }}
                }}
            }}
        }}
    }}
    """

    def _rows(result: dict[str, Any]) -> list[dict[str, Any]]:
        """The rows of every segment deployment, each with its deployment's segment name and VNI."""
        return [
            {**row["node"], "segment": deployment["node"]["segment"], "vni": deployment["node"]["vni"]}
            for deployment in result.get("ManagedSegmentDeployment", {}).get("edges", [])
            for row in deployment["node"]["vlans"]["edges"]
        ]

    async def _check() -> tuple[bool, list[dict]]:
        found = _rows(await client.execute_graphql(query=query, variables=variables))
        return len(found) >= expected_count, found

    try:
        rows = await wait_for_condition(
            check_fn=_check,
            max_attempts=max_attempts,
            poll_interval=poll_interval,
            description=f">= {expected_count} VLAN domain segment(s) on '{branch}' (segment={segment_id})",
        )
    except TimeoutError:
        rows = _rows(await client.execute_graphql(query=query, variables=variables))

    record_count = len(rows)

    records = [
        {
            "id": row["id"],
            "vlan_id": row["vlan_id"]["value"],
            "vni": (row.get("vni") or {}).get("value"),
            "segment_name": ((row.get("segment") or {}).get("node") or {}).get("name", {}).get("value"),
            "device_id": (row.get("device") or {}).get("node", {}).get("id"),
            "device_label": (row.get("device") or {}).get("node", {}).get("display_label"),
        }
        for row in rows
    ]

    logger.info(
        "Found %d VLAN domain segment(s) on branch '%s' (segment_filter=%s)",
        record_count,
        branch,
        segment_id,
    )
    for r in records:
        logger.info(
            "  - segment=%s, vlan_id=%s, device=%s",
            r["segment_name"],
            r["vlan_id"],
            r["device_label"],
        )

    return {"record_count": record_count, "records": records}


# ======================================================================
# Switch ports carrying a segment
# ======================================================================

# A port carries a segment as a member of its switch's VLAN ROW
SEGMENT_ROW_IDS_QUERY = """
query SegmentRowIds($segment_id: ID!) {
  ManagedSegmentDeployment(segment__ids: [$segment_id]) { edges { node { id vlans { edges { node { id } } } } } }
}
"""

_SEGMENT_PORT_FIELDS = """
        __typename
        id
        name { value }
        role { value }
        device { node { name { value } ... on DcimDeviceManaged { role { value } } } }
        capabilities(ids: $row_ids) { edges { node { id } } }
"""

SEGMENT_PORTS_QUERY = f"""
query SegmentPorts($row_ids: [ID]) {{
  DcimPhysicalInterface(capabilities__ids: $row_ids) {{ edges {{ node {{ {_SEGMENT_PORT_FIELDS} }} }} }}
  DcimLAGInterface(capabilities__ids: $row_ids) {{ edges {{ node {{ {_SEGMENT_PORT_FIELDS} }} }} }}
}}
"""


async def fetch_segment_row_ids(client: InfrahubClient, branch: str, segment_id: str) -> dict[str, str]:
    """{row id: id of its deployment} of the segment's VLAN rows (one per switch it is realized on)."""
    result = await client.execute_graphql(
        query=SEGMENT_ROW_IDS_QUERY, variables={"segment_id": segment_id}, branch_name=branch
    )
    return {
        row["node"]["id"]: edge["node"]["id"]
        for edge in (result.get("ManagedSegmentDeployment") or {}).get("edges") or []
        for row in ((edge["node"].get("vlans") or {}).get("edges") or [])
    }


async def fetch_segment_ports(client: InfrahubClient, branch: str, segment_id: str) -> list[dict[str, Any]]:
    """The switch ports (physical ports and port-channels) carrying the segment: the members of its VLAN rows."""
    rows = await fetch_segment_row_ids(client, branch, segment_id)
    if not rows:
        return []
    result = await client.execute_graphql(
        query=SEGMENT_PORTS_QUERY, variables={"row_ids": sorted(rows)}, branch_name=branch
    )
    ports: list[dict[str, Any]] = []
    for kind in ("DcimPhysicalInterface", "DcimLAGInterface"):
        for port_edge in (result.get(kind) or {}).get("edges") or []:
            node = port_edge["node"]
            device = (node.get("device") or {}).get("node") or {}
            for edge in node["capabilities"]["edges"]:
                if edge["node"]["id"] not in rows:
                    continue
                ports.append(
                    {
                        "id": node["id"],
                        "name": node["name"]["value"],
                        "role": (node.get("role") or {}).get("value"),
                        "device": (device.get("name") or {}).get("value"),
                        "device_role": (device.get("role") or {}).get("value"),
                        "row_id": edge["node"]["id"],
                        "deployment_id": rows[edge["node"]["id"]],
                    }
                )
    return sorted(ports, key=lambda port: (str(port["device"]), port["name"]))
