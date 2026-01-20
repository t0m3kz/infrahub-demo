"""Reusable workflow helpers for integration tests.

These helpers encapsulate common patterns used across all test scenarios:
- Running generators and waiting for completion
- Creating and merging proposed changes
- Verifying artifacts (devices, cables, configurations)
"""

import asyncio
import logging
from typing import Any, cast

from infrahub_sdk import InfrahubClient, InfrahubClientSync
from infrahub_sdk.graphql import Mutation
from infrahub_sdk.task.models import TaskState

from .test_constants import (
    DATA_PROPAGATION_DELAY,
    DIFF_TASK_TIMEOUT,
    GENERATOR_TASK_TIMEOUT,
    MERGE_PROPAGATION_DELAY,
    MERGE_TASK_TIMEOUT,
    VALIDATION_MAX_ATTEMPTS,
    VALIDATION_POLL_INTERVAL,
)

logger = logging.getLogger(__name__)


async def run_generator(
    client: InfrahubClient,
    generator_name: str,
    node_ids: list[str],
    branch: str,
) -> dict[str, Any]:
    """Run a generator and wait for completion.

    Args:
        client: Infrahub async client
        generator_name: Name of the generator to run (e.g., "add_dc", "add_rack")
        node_ids: List of node IDs to pass to the generator
        branch: Branch to run the generator on

    Returns:
        Dictionary with task_id, task_state, and success flag
    """
    logger.info("Running generator '%s' on branch '%s' with %d node(s)", generator_name, branch, len(node_ids))

    # Get generator definition
    from infrahub_sdk.protocols import CoreGeneratorDefinition

    try:
        definition = await client.get(
            CoreGeneratorDefinition,
            name__value=generator_name,
            branch="main",
        )
    except Exception as e:
        all_generators = await client.all(kind=CoreGeneratorDefinition, branch="main")
        available = [g.name.value if hasattr(g, "name") else str(g) for g in all_generators]
        raise AssertionError(
            f"Generator '{generator_name}' not found.\n"
            f"  Available generators: {available}\n"
            f"  Repository may not have synced properly."
        ) from e

    # Switch to target branch
    original_branch = client.default_branch
    client.default_branch = branch

    # Run the generator
    mutation = Mutation(
        mutation="CoreGeneratorDefinitionRun",
        input_data={
            "data": {
                "id": definition.id,
                "nodes": node_ids,
            },
            "wait_until_completion": False,
        },
        query={"ok": None, "task": {"id": None}},
    )

    response = await client.execute_graphql(query=mutation.render())
    task_id = response["CoreGeneratorDefinitionRun"]["task"]["id"]
    logger.info("Generator task started: %s", task_id)

    # Wait for completion
    task = await client.task.wait_for_completion(id=task_id, timeout=GENERATOR_TASK_TIMEOUT)

    # Restore original branch
    client.default_branch = original_branch

    success = task.state == TaskState.COMPLETED
    if not success:
        logger.warning("Generator task %s finished with state %s", task_id, task.state)
    else:
        logger.info("Generator task %s completed successfully", task_id)

    return {
        "task_id": task_id,
        "task_state": str(task.state),
        "success": success,
    }


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
        device_types: Optional list of device types to check for (e.g., ["spine", "leaf"])

    Returns:
        Dictionary with device count and breakdown by type
    """
    from generators.protocols import DcimDevice

    client.default_branch = branch
    await asyncio.sleep(DATA_PROPAGATION_DELAY)

    devices = await client.all(kind=DcimDevice)
    device_count = len(devices)

    assert device_count >= expected_min_count, (
        f"Expected at least {expected_min_count} device(s), found {device_count}\n  Branch: {branch}"
    )

    logger.info("Found %d devices on branch '%s'", device_count, branch)

    # Count by type if specified
    breakdown = {}
    if device_types:
        device_names = [d.name.value for d in devices]
        for dtype in device_types:
            count = sum(1 for name in device_names if dtype.lower() in name.lower())
            breakdown[dtype] = count
            logger.info("  - %s: %d", dtype, count)

    return {
        "device_count": device_count,
        "breakdown": breakdown,
    }


async def verify_cables_created(
    client: InfrahubClient,
    branch: str,
    expected_min_count: int = 1,
) -> dict[str, Any]:
    """Verify cables were created by generator.

    Args:
        client: Infrahub async client
        branch: Branch to check
        expected_min_count: Minimum number of cables expected

    Returns:
        Dictionary with cable count
    """
    client.default_branch = branch
    await asyncio.sleep(DATA_PROPAGATION_DELAY)

    # Query for cables using GraphQL
    query = """
    query GetCables {
        DcimCable {
            count
        }
    }
    """
    result = await client.execute_graphql(query=query)
    cable_count = result.get("DcimCable", {}).get("count", 0)

    assert cable_count >= expected_min_count, (
        f"Expected at least {expected_min_count} cable(s), found {cable_count}\n  Branch: {branch}"
    )

    logger.info("Found %d cables on branch '%s'", cable_count, branch)

    return {"cable_count": cable_count}


async def verify_device_interfaces(
    client: InfrahubClient,
    branch: str,
    device_name: str,
    expected_interface_count: int,
    expected_interface_types: list[str] | None = None,
) -> dict[str, Any]:
    """Verify a specific device has the expected number and types of interfaces.

    Args:
        client: Infrahub async client
        branch: Branch to check
        device_name: Name of the device to verify
        expected_interface_count: Expected number of interfaces
        expected_interface_types: Optional list of expected interface types

    Returns:
        Dictionary with interface details
    """
    client.default_branch = branch
    await asyncio.sleep(DATA_PROPAGATION_DELAY)

    query = """
    query GetDeviceInterfaces($device_name: String!) {
        DcimDevice(name__value: $device_name) {
            edges {
                node {
                    id
                    name {
                        value
                    }
                    interfaces {
                        count
                        edges {
                            node {
                                id
                                name {
                                    value
                                }
                                interface_type {
                                    value
                                }
                                role {
                                    value
                                }
                            }
                        }
                    }
                }
            }
        }
    }
    """

    result = await client.execute_graphql(query=query, variables={"device_name": device_name})
    edges = result.get("DcimDevice", {}).get("edges", [])

    assert edges, f"Device '{device_name}' not found on branch '{branch}'"

    device = edges[0]["node"]
    interface_count = device["interfaces"]["count"]
    interfaces = [edge["node"] for edge in device["interfaces"]["edges"]]

    assert interface_count == expected_interface_count, (
        f"Device '{device_name}' has {interface_count} interfaces, expected {expected_interface_count}\n"
        f"  Branch: {branch}"
    )

    logger.info("Device '%s' has %d interfaces (as expected)", device_name, interface_count)

    # Verify interface types if specified
    if expected_interface_types:
        interface_types = [iface["interface_type"]["value"] for iface in interfaces]
        for expected_type in expected_interface_types:
            type_count = interface_types.count(expected_type)
            logger.info("  - Interface type '%s': %d", expected_type, type_count)

    return {
        "device_name": device_name,
        "interface_count": interface_count,
        "interfaces": interfaces,
    }


async def verify_device_role(
    client: InfrahubClient,
    branch: str,
    device_name: str,
    expected_role: str,
    expected_device_type: str | None = None,
) -> dict[str, Any]:
    """Verify a device has the expected role and device type.

    Args:
        client: Infrahub async client
        branch: Branch to check
        device_name: Name of the device
        expected_role: Expected device role (e.g., "spine", "leaf", "tor")
        expected_device_type: Optional expected device type

    Returns:
        Dictionary with device details
    """
    client.default_branch = branch
    await asyncio.sleep(DATA_PROPAGATION_DELAY)

    query = """
    query GetDeviceDetails($device_name: String!) {
        DcimDevice(name__value: $device_name) {
            edges {
                node {
                    id
                    name {
                        value
                    }
                    role {
                        value
                    }
                    device_type {
                        node {
                            id
                            name {
                                value
                            }
                        }
                    }
                    rack {
                        node {
                            id
                            name {
                                value
                            }
                        }
                    }
                }
            }
        }
    }
    """

    result = await client.execute_graphql(query=query, variables={"device_name": device_name})
    edges = result.get("DcimDevice", {}).get("edges", [])

    assert edges, f"Device '{device_name}' not found on branch '{branch}'"

    device = edges[0]["node"]
    actual_role = device["role"]["value"]
    actual_device_type = device["device_type"]["node"]["name"]["value"] if device.get("device_type") else None

    assert actual_role == expected_role, (
        f"Device '{device_name}' has role '{actual_role}', expected '{expected_role}'\n  Branch: {branch}"
    )

    logger.info("Device '%s' has role '%s' (as expected)", device_name, actual_role)

    if expected_device_type:
        assert actual_device_type == expected_device_type, (
            f"Device '{device_name}' has device_type '{actual_device_type}', expected '{expected_device_type}'\n"
            f"  Branch: {branch}"
        )
        logger.info("Device '%s' has device_type '%s' (as expected)", device_name, actual_device_type)

    return {
        "device_name": device_name,
        "role": actual_role,
        "device_type": actual_device_type,
        "rack": device.get("rack", {}).get("node", {}).get("name", {}).get("value"),
    }


async def verify_cable_connections(
    client: InfrahubClient,
    branch: str,
    device_name: str,
    expected_connections: int,
    connected_to_roles: list[str] | None = None,
) -> dict[str, Any]:
    """Verify a device has the expected number of cable connections.

    Args:
        client: Infrahub async client
        branch: Branch to check
        device_name: Name of the device
        expected_connections: Expected number of cable connections
        connected_to_roles: Optional list of roles the device should connect to

    Returns:
        Dictionary with connection details
    """
    client.default_branch = branch
    await asyncio.sleep(DATA_PROPAGATION_DELAY)

    query = """
    query GetDeviceCables($device_name: String!) {
        DcimDevice(name__value: $device_name) {
            edges {
                node {
                    id
                    name {
                        value
                    }
                    interfaces {
                        edges {
                            node {
                                id
                                name {
                                    value
                                }
                                cable {
                                    node {
                                        id
                                        endpoints {
                                            edges {
                                                node {
                                                    id
                                                    device {
                                                        node {
                                                            id
                                                            name {
                                                                value
                                                            }
                                                            role {
                                                                value
                                                            }
                                                        }
                                                    }
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }
    """

    result = await client.execute_graphql(query=query, variables={"device_name": device_name})
    edges = result.get("DcimDevice", {}).get("edges", [])

    assert edges, f"Device '{device_name}' not found on branch '{branch}'"

    device = edges[0]["node"]
    interfaces = [edge["node"] for edge in device["interfaces"]["edges"]]

    # Count interfaces with cables
    connected_interfaces = [iface for iface in interfaces if iface.get("cable") and iface["cable"].get("node")]
    connection_count = len(connected_interfaces)

    assert connection_count >= expected_connections, (
        f"Device '{device_name}' has {connection_count} connections, expected at least {expected_connections}\n"
        f"  Branch: {branch}"
    )

    logger.info(
        "Device '%s' has %d cable connections (expected: %d)", device_name, connection_count, expected_connections
    )

    # Verify connected device roles if specified
    if connected_to_roles:
        connected_roles = []
        for iface in connected_interfaces:
            cable = iface["cable"]["node"]
            for endpoint_edge in cable.get("endpoints", {}).get("edges", []):
                endpoint = endpoint_edge["node"]
                if endpoint.get("device") and endpoint["device"].get("node"):
                    remote_device = endpoint["device"]["node"]
                    if remote_device["name"]["value"] != device_name:  # Skip self
                        connected_roles.append(remote_device["role"]["value"])

        for expected_role in connected_to_roles:
            role_count = connected_roles.count(expected_role)
            assert role_count > 0, (
                f"Device '{device_name}' should connect to role '{expected_role}' but doesn't\n"
                f"  Connected to roles: {connected_roles}\n"
                f"  Branch: {branch}"
            )
            logger.info("  - Connected to %d device(s) with role '%s'", role_count, expected_role)

    return {
        "device_name": device_name,
        "connection_count": connection_count,
        "connected_interfaces": [iface["name"]["value"] for iface in connected_interfaces],
    }


def create_proposed_change(
    client: InfrahubClientSync,
    name: str,
    source_branch: str,
    destination_branch: str = "main",
) -> str:
    """Create a proposed change.

    Args:
        client: Infrahub sync client
        name: Name for the proposed change
        source_branch: Source branch with changes
        destination_branch: Destination branch (usually main)

    Returns:
        Proposed change ID
    """
    logger.info("Creating proposed change: %s (%s → %s)", name, source_branch, destination_branch)

    # Create diff first
    diff_mutation = Mutation(
        mutation="DiffUpdate",
        input_data={
            "data": {
                "name": f"diff-{source_branch}",
                "branch": source_branch,
                "wait_for_completion": False,
            }
        },
        query={"ok": None, "task": {"id": None}},
    )

    diff_response = client.execute_graphql(query=diff_mutation.render())
    diff_task_id = diff_response["DiffUpdate"]["task"]["id"]
    diff_task = client.task.wait_for_completion(id=diff_task_id, timeout=DIFF_TASK_TIMEOUT)

    assert diff_task.state == TaskState.COMPLETED, (
        f"Diff creation failed.\n  Task ID: {diff_task_id}\n  Task state: {diff_task.state}"
    )

    logger.info("Diff created successfully")

    # Create proposed change
    pc_mutation = Mutation(
        mutation="CoreProposedChangeCreate",
        input_data={
            "data": {
                "name": {"value": name},
                "source_branch": {"value": source_branch},
                "destination_branch": {"value": destination_branch},
            }
        },
        query={"ok": None, "object": {"id": None}},
    )

    pc_response = client.execute_graphql(query=pc_mutation.render())
    pc_id = pc_response["CoreProposedChangeCreate"]["object"]["id"]

    logger.info("Proposed change created with ID: %s", pc_id)
    return pc_id


def wait_for_validations(
    client: InfrahubClientSync,
    pc_name: str,
) -> list[Any]:
    """Wait for proposed change validations to complete.

    Args:
        client: Infrahub sync client
        pc_name: Name of the proposed change

    Returns:
        List of validation results
    """
    import time

    logger.info("Waiting for validations to complete for PC: %s", pc_name)

    validation_results: list[Any] = []
    validations_completed = False

    for attempt in range(1, VALIDATION_MAX_ATTEMPTS + 1):
        pc = client.get(
            "CoreProposedChange",
            name__value=pc_name,
            include=["validations"],
            exclude=["reviewers", "approved_by", "created_by"],
            prefetch_relationships=True,
            populate_store=True,
        )

        if hasattr(pc.validations, "peers") and pc.validations.peers:
            peers_list = cast(list, pc.validations.peers)
            validations_completed = all(
                (validation.peer.state.value if hasattr(validation.peer.state, "value") else str(validation.peer.state))
                == "completed"
                for validation in peers_list
            )

            if validations_completed:
                validation_results = [validation.peer for validation in peers_list]
                break

        logger.info("Waiting for validations... attempt %d/%d", attempt, VALIDATION_MAX_ATTEMPTS)
        time.sleep(VALIDATION_POLL_INTERVAL)

    assert validations_completed, (
        f"Validations did not complete in time.\n"
        f"  Proposed change: {pc_name}\n"
        f"  Timeout: {VALIDATION_MAX_ATTEMPTS * VALIDATION_POLL_INTERVAL}s"
    )

    # Log results
    for result in validation_results:
        name = result.name.value if hasattr(result, "name") else str(result.id)
        conclusion = result.conclusion.value if hasattr(result, "conclusion") else "unknown"
        logger.info("  Validation: %s - %s", name, conclusion)

    return validation_results


def merge_proposed_change(
    client: InfrahubClientSync,
    pc_id: str,
) -> dict[str, Any]:
    """Merge a proposed change.

    Args:
        client: Infrahub sync client
        pc_id: Proposed change ID

    Returns:
        Dictionary with merge task info and success status
    """
    logger.info("Merging proposed change ID: %s", pc_id)

    pc = client.get("CoreProposedChange", id=pc_id)
    pc_state_before = pc.state.value if hasattr(pc.state, "value") else pc.state

    mutation = Mutation(
        mutation="CoreProposedChangeMerge",
        input_data={
            "data": {
                "id": pc_id,
            },
            "wait_until_completion": False,
        },
        query={"ok": None, "task": {"id": None}},
    )

    response = client.execute_graphql(query=mutation.render())
    task_id = response["CoreProposedChangeMerge"]["task"]["id"]
    task = client.task.wait_for_completion(id=task_id, timeout=MERGE_TASK_TIMEOUT)

    logger.info("Merge task %s finished with state: %s", task_id, task.state)

    # Check final PC state
    pc_after = client.get("CoreProposedChange", id=pc_id)
    pc_state_after = pc_after.state.value if hasattr(pc_after.state, "value") else pc_after.state

    success = pc_state_after in ["merged", "closed"]

    if not success:
        logger.error("Merge failed. PC state: %s → %s", pc_state_before, pc_state_after)
        if hasattr(task, "state_message") and task.state_message:
            logger.error("Task message: %s", task.state_message)

    return {
        "task_id": task_id,
        "task_state": str(task.state),
        "pc_state_before": pc_state_before,
        "pc_state_after": pc_state_after,
        "success": success,
    }


async def verify_merged_to_main(
    client: InfrahubClient,
    expected_object_kind: str,
    expected_object_name: str,
) -> bool:
    """Verify that an object exists in main branch after merge.

    Args:
        client: Infrahub async client
        expected_object_kind: Kind of object to verify (e.g., "TopologyDataCenter")
        expected_object_name: Name of the object to find

    Returns:
        True if object found in main
    """
    client.default_branch = "main"
    await asyncio.sleep(MERGE_PROPAGATION_DELAY)

    logger.info("Verifying '%s' named '%s' exists in main", expected_object_kind, expected_object_name)

    try:
        obj = await client.get(
            kind=expected_object_kind,
            name__value=expected_object_name,
            raise_when_missing=False,
        )

        if obj:
            logger.info("✓ Found '%s' in main branch", expected_object_name)
            return True
        else:
            logger.error("✗ '%s' not found in main branch", expected_object_name)
            return False

    except Exception as e:
        logger.error("Error querying for '%s': %s", expected_object_name, e)
        return False
