"""Mock data helpers for routing planner unit tests.

Provides factory functions to create mock Infrahub objects for testing
routing planner logic without requiring a real InfrahubClient connection.
"""

from typing import Any
from unittest.mock import MagicMock


def create_mock_device(
    name: str,
    device_id: str,
    role: str,
    loopback_ip: str | None = None,
) -> MagicMock:
    """Create a mock device object.

    Args:
        name: Device name (e.g., "spine-1")
        device_id: Device UUID
        role: Device role (spine, leaf, tor, etc.)
        loopback_ip: Optional loopback IP address

    Returns:
        Mock device object with required attributes
    """
    device = MagicMock()
    device.id = device_id
    device.name = MagicMock()
    device.name.value = name
    device.role = MagicMock()
    device.role.value = role

    return device


def create_mock_loopback(
    interface_id: str,
    device_id: str,
    ip_address: str,
) -> MagicMock:
    """Create a mock loopback interface object.

    Args:
        interface_id: Interface UUID
        device_id: Parent device UUID
        ip_address: Loopback IP address (e.g., "10.0.0.1/32")

    Returns:
        Mock loopback interface with IP address
    """
    loopback = MagicMock()
    loopback.id = interface_id
    loopback.name = MagicMock()
    loopback.name.value = "Loopback0"

    # Mock device relationship
    loopback.device = MagicMock()
    loopback.device.id = device_id

    # Mock IP address
    ip_obj = MagicMock()
    ip_obj.display_label = ip_address
    ip_obj.id = f"ip-{interface_id}"

    loopback.ip_addresses = MagicMock()
    loopback.ip_addresses.peers = [ip_obj]

    return loopback


def create_mock_bgp_process(
    process_id: str,
    device_id: str,
    device_name: str,
    local_asn: int,
    router_id: str,
) -> MagicMock:
    """Create a mock BGP process object.

    Args:
        process_id: BGP process UUID
        device_id: Parent device UUID
        device_name: Device name
        local_asn: Local AS number
        router_id: BGP router ID

    Returns:
        Mock BGP process object
    """
    bgp_process = MagicMock()
    bgp_process.id = process_id
    bgp_process.name = MagicMock()
    bgp_process.name.value = f"{device_name}-bgp"

    bgp_process.local_asn = MagicMock()
    bgp_process.local_asn.value = local_asn

    bgp_process.router_id = MagicMock()
    bgp_process.router_id.display_label = router_id

    bgp_process.device = MagicMock()
    bgp_process.device.id = device_id

    return bgp_process


def create_mock_interface(
    interface_id: str,
    device_id: str,
    device_name: str,
    interface_name: str,
    has_cable: bool = False,
) -> MagicMock:
    """Create a mock physical interface object.

    Args:
        interface_id: Interface UUID
        device_id: Parent device UUID
        device_name: Device name
        interface_name: Interface name (e.g., "Ethernet1/1")
        has_cable: Whether interface has a cable attached

    Returns:
        Mock physical interface object
    """
    interface = MagicMock()
    interface.id = interface_id
    interface.name = MagicMock()
    interface.name.value = interface_name

    interface.device = MagicMock()
    interface.device.id = device_id
    interface.device.name = MagicMock()
    interface.device.name.value = device_name

    if has_cable:
        interface.cable = MagicMock()
        interface.cable.id = f"cable-{interface_id}"
    else:
        interface.cable = None

    return interface


def create_mock_cable(
    cable_id: str,
    interface1: MagicMock,
    interface2: MagicMock,
) -> MagicMock:
    """Create a mock cable object connecting two interfaces.

    Args:
        cable_id: Cable UUID
        interface1: First interface endpoint
        interface2: Second interface endpoint

    Returns:
        Mock cable object with endpoints
    """
    cable = MagicMock()
    cable.id = cable_id
    cable.type = MagicMock()
    cable.type.value = "cat6"

    cable.endpoints = MagicMock()
    cable.endpoints.peers = [interface1, interface2]

    # Update interfaces to reference this cable
    interface1.cable = cable
    interface2.cable = cable

    return cable


def create_mock_ospf_area(
    area_id: str,
    area_number: int,
    deployment_name: str,
) -> MagicMock:
    """Create a mock OSPF area object.

    Args:
        area_id: OSPF area UUID
        area_number: OSPF area number (typically 0)
        deployment_name: Deployment name

    Returns:
        Mock OSPF area object
    """
    area = MagicMock()
    area.id = area_id
    area.name = MagicMock()
    area.name.value = f"{deployment_name}-area-{area_number}"
    area.area = MagicMock()
    area.area.value = area_number
    area.area_type = MagicMock()
    area.area_type.value = "standard"

    return area


def create_mock_ospf_process(
    process_id: str,
    device_id: str,
    device_name: str,
    router_id: str,
) -> MagicMock:
    """Create a mock OSPF process object.

    Args:
        process_id: OSPF process UUID
        device_id: Parent device UUID
        device_name: Device name
        router_id: OSPF router ID

    Returns:
        Mock OSPF process object
    """
    ospf_process = MagicMock()
    ospf_process.id = process_id
    ospf_process.name = MagicMock()
    ospf_process.name.value = f"{device_name}-ospf-underlay"

    ospf_process.process_id = MagicMock()
    ospf_process.process_id.value = "1"

    ospf_process.router_id = MagicMock()
    ospf_process.router_id.value = router_id

    ospf_process.device = MagicMock()
    ospf_process.device.id = device_id

    return ospf_process


def create_device_data_dict(
    name: str,
    device_id: str,
    role: str,
    loopback_ip: str,
) -> dict[str, Any]:
    """Create a device data dictionary for BGPSessionPlanner.

    Args:
        name: Device name
        device_id: Device UUID
        role: Device role
        loopback_ip: Loopback IP address (without prefix)

    Returns:
        Device data dict with required keys
    """
    return {
        "name": name,
        "id": device_id,
        "role": role,
        "loopback_ip": loopback_ip,
    }


def create_standard_spine_leaf_topology() -> tuple[list[MagicMock], dict[str, MagicMock]]:
    """Create a standard spine-leaf topology for testing.

    Returns:
        Tuple of (devices list, device_loopbacks dict)
    """
    devices = [
        create_mock_device("spine-1", "s1", "spine", "10.0.0.1"),
        create_mock_device("spine-2", "s2", "spine", "10.0.0.2"),
        create_mock_device("leaf-1", "l1", "leaf", "10.0.1.1"),
        create_mock_device("leaf-2", "l2", "leaf", "10.0.1.2"),
    ]

    device_loopbacks = {
        "s1": create_mock_loopback("lo-s1", "s1", "10.0.0.1/32"),
        "s2": create_mock_loopback("lo-s2", "s2", "10.0.0.2/32"),
        "l1": create_mock_loopback("lo-l1", "l1", "10.0.1.1/32"),
        "l2": create_mock_loopback("lo-l2", "l2", "10.0.1.2/32"),
    }

    return devices, device_loopbacks


def create_hierarchical_topology() -> tuple[list[MagicMock], dict[str, MagicMock]]:
    """Create a hierarchical topology with super-spines for testing.

    Returns:
        Tuple of (devices list, device_loopbacks dict)
    """
    devices = [
        create_mock_device("super-spine-1", "ss1", "super-spine", "10.0.0.1"),
        create_mock_device("super-spine-2", "ss2", "super-spine", "10.0.0.2"),
        create_mock_device("spine-1", "s1", "spine", "10.0.1.1"),
        create_mock_device("spine-2", "s2", "spine", "10.0.1.2"),
        create_mock_device("leaf-1", "l1", "leaf", "10.0.2.1"),
        create_mock_device("leaf-2", "l2", "leaf", "10.0.2.2"),
    ]

    device_loopbacks = {
        "ss1": create_mock_loopback("lo-ss1", "ss1", "10.0.0.1/32"),
        "ss2": create_mock_loopback("lo-ss2", "ss2", "10.0.0.2/32"),
        "s1": create_mock_loopback("lo-s1", "s1", "10.0.1.1/32"),
        "s2": create_mock_loopback("lo-s2", "s2", "10.0.1.2/32"),
        "l1": create_mock_loopback("lo-l1", "l1", "10.0.2.1/32"),
        "l2": create_mock_loopback("lo-l2", "l2", "10.0.2.2/32"),
    }

    return devices, device_loopbacks


def create_topology_with_tors() -> tuple[list[MagicMock], dict[str, MagicMock]]:
    """Create a topology with ToRs for testing.

    Returns:
        Tuple of (devices list, device_loopbacks dict)
    """
    devices = [
        create_mock_device("spine-1", "s1", "spine", "10.0.0.1"),
        create_mock_device("spine-2", "s2", "spine", "10.0.0.2"),
        create_mock_device("leaf-1", "l1", "leaf", "10.0.1.1"),
        create_mock_device("leaf-2", "l2", "leaf", "10.0.1.2"),
        create_mock_device("tor-1", "t1", "tor", "10.0.2.1"),
        create_mock_device("tor-2", "t2", "tor", "10.0.2.2"),
    ]

    device_loopbacks = {
        "s1": create_mock_loopback("lo-s1", "s1", "10.0.0.1/32"),
        "s2": create_mock_loopback("lo-s2", "s2", "10.0.0.2/32"),
        "l1": create_mock_loopback("lo-l1", "l1", "10.0.1.1/32"),
        "l2": create_mock_loopback("lo-l2", "l2", "10.0.1.2/32"),
        "t1": create_mock_loopback("lo-t1", "t1", "10.0.2.1/32"),
        "t2": create_mock_loopback("lo-t2", "t2", "10.0.2.2/32"),
    }

    return devices, device_loopbacks
