"""Unit tests for refactored endpoint connectivity generator.

Tests the interface-first query approach with proper status filtering:
- Device status filtering (active, free, provisioning)
- Interface status filtering (free only)
- All deployment strategies (middle_rack, tor, mixed)
- _query_interfaces_by_location method
- _process_endpoint_connections method
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from generators.add.endpoint import EndpointConnectivityGenerator


class MockDevice:
    """Mock physical device with basic attributes."""

    def __init__(self, id: str, name: str, role: str = "leaf", status: str = "active") -> None:
        """Initialize mock device."""
        self.id = id
        self.name = name
        self.role = role
        self.status = status


class MockInterface:
    """Mock physical interface with all required attributes."""

    def __init__(
        self,
        name: str,
        device_name: str,
        device_role: str = "leaf",
        interface_type: str = "100gbase-x-qsfp28",
        interface_role: str = "customer",
        status: str = "free",
        has_cable: bool = False,
    ) -> None:
        """Initialize mock interface."""
        self.id = f"{device_name}:{name}"
        self.name = Mock(value=name)
        self.interface_type = Mock(value=interface_type) if interface_type else None
        self.role = Mock(value=interface_role)
        self.status = Mock(value=status)
        self.device = Mock(
            name=Mock(value=device_name),
            role=Mock(value=device_role),
            display_label=device_name,
        )
        self.cable = Mock(id="cable-123") if has_cable else Mock(id=None)


class MockRack:
    """Mock rack with location hierarchy."""

    def __init__(self, rack_id: str, pod_id: str, row_index: int, rack_type: str = "compute") -> None:
        """Initialize mock rack."""
        self.id = rack_id
        self.rack_type = rack_type
        self.row_index = row_index
        self.pod = Mock(id=pod_id)
        self.devices = []


class MockEndpointData:
    """Mock endpoint device data."""

    def __init__(self, name: str, rack: MockRack | None = None) -> None:
        """Initialize mock endpoint."""
        self.name = name
        self.rack = rack
        self.interfaces = []
        self._free_interfaces = []  # Dynamic attribute added during idempotency check


@pytest.fixture
def mock_client() -> MagicMock:
    """Create a mock Infrahub client with async filters method."""
    client = MagicMock()

    # Create an AsyncMock for the filters method itself
    client.filters = AsyncMock()

    return client


@pytest.fixture
def mock_logger() -> Mock:
    """Create a mock logger."""
    return Mock()


@pytest.fixture
def generator(mock_client: AsyncMock, mock_logger: Mock) -> EndpointConnectivityGenerator:
    """Create endpoint generator instance with mocked dependencies."""
    gen = EndpointConnectivityGenerator(
        client=mock_client,
        branch="main",
        query="test_query",
        infrahub_node="DcimDevice",
    )
    gen.logger = mock_logger
    gen.data = MockEndpointData("test-server")
    gen.speed_aware = True
    gen.planned_connections = set()
    # Directly patch the _client attribute to bypass the property
    gen._client = mock_client
    return gen


class TestQueryInterfacesByLocation:
    """Test _query_interfaces_by_location method."""

    @pytest.mark.asyncio
    async def test_query_with_device_role_and_location(
        self, generator: EndpointConnectivityGenerator, mock_client: AsyncMock
    ) -> None:
        """Test querying interfaces filters by device role, rack, and statuses."""
        # Setup
        rack_ids = ["rack-1", "rack-2"]
        endpoint_interfaces = []

        # Mock devices query (first call)
        mock_devices = [
            MockDevice(id="device-1", name="leaf-01"),
            MockDevice(id="device-2", name="leaf-02"),
        ]

        # Mock return: 4 free interfaces on Leaf devices (second call)
        mock_interfaces = [
            MockInterface("Ethernet1/1", "leaf-01", device_role="leaf", status="free"),
            MockInterface("Ethernet1/2", "leaf-01", device_role="leaf", status="free"),
            MockInterface("Ethernet1/1", "leaf-02", device_role="leaf", status="free"),
            MockInterface("Ethernet1/2", "leaf-02", device_role="leaf", status="free"),
        ]

        # Side effect: first call returns devices, second returns interfaces
        mock_client.filters.side_effect = [mock_devices, mock_interfaces]

        # Execute
        result = await generator._query_interfaces_by_location(
            rack_ids=rack_ids,
            device_role="leaf",
            endpoint_interfaces=endpoint_interfaces,
        )

        # Verify
        assert len(result) == 4
        assert all(intf.status.value == "free" for intf in result)
        assert all(hasattr(intf.device, "role") and intf.device.role.value == "leaf" for intf in result)  # type: ignore[attr-defined]

        # Verify client.filters was called twice
        assert mock_client.filters.call_count == 2

        # First call: query devices
        first_call_kwargs = mock_client.filters.call_args_list[0].kwargs
        assert first_call_kwargs["role__value"] == "leaf"
        assert first_call_kwargs["rack__ids"] == rack_ids
        assert first_call_kwargs["status__values"] == ["active", "free", "provisioning"]

        # Second call: query interfaces
        second_call_kwargs = mock_client.filters.call_args_list[1].kwargs
        assert second_call_kwargs["device__ids"] == ["device-1", "device-2"]
        assert second_call_kwargs["status__value"] == "free"
        assert second_call_kwargs["role__values"] == ["downlink", "customer"]

    @pytest.mark.asyncio
    async def test_query_filters_out_cabled_interfaces(
        self, generator: EndpointConnectivityGenerator, mock_client: AsyncMock
    ) -> None:
        """Test that interfaces with cables are excluded."""
        # Setup
        rack_ids = ["rack-1"]
        endpoint_interfaces = []

        # Mock devices (first call)
        mock_devices = [MockDevice(id="device-1", name="leaf-01")]

        # Mock return: mix of cabled and uncabled interfaces (second call)
        mock_interfaces = [
            MockInterface("Ethernet1/1", "leaf-01", status="free", has_cable=False),
            MockInterface("Ethernet1/2", "leaf-01", status="free", has_cable=True),  # Cabled
            MockInterface("Ethernet1/3", "leaf-01", status="free", has_cable=False),
            MockInterface("Ethernet1/4", "leaf-01", status="free", has_cable=True),  # Cabled
        ]

        mock_client.filters.side_effect = [mock_devices, mock_interfaces]

        # Execute
        result = await generator._query_interfaces_by_location(
            rack_ids=rack_ids,
            device_role="leaf",
            endpoint_interfaces=endpoint_interfaces,
        )

        # Verify: Only uncabled interfaces returned
        assert len(result) == 2
        assert result[0].name.value == "Ethernet1/1"
        assert result[1].name.value == "Ethernet1/3"

    @pytest.mark.asyncio
    async def test_query_matches_interface_types(
        self, generator: EndpointConnectivityGenerator, mock_client: AsyncMock
    ) -> None:
        """Test that interface type filter is added when endpoint specifies types."""
        # Setup
        rack_ids = ["rack-1"]

        # Endpoint has 25G interfaces
        endpoint_interface = Mock()
        endpoint_interface.interface_type = "25gbase-x-sfp28"
        endpoint_interfaces = [endpoint_interface]

        mock_interfaces = [
            MockInterface("Ethernet1/9", "leaf-01", interface_type="25gbase-x-sfp28", status="free"),
            MockInterface("Ethernet1/10", "leaf-01", interface_type="25gbase-x-sfp28", status="free"),
        ]

        mock_client.filters.return_value = mock_interfaces

        # Execute
        result = await generator._query_interfaces_by_location(
            rack_ids=rack_ids,
            device_role="leaf",
            endpoint_interfaces=endpoint_interfaces,
        )

        # Verify
        assert len(result) == 2

        # Verify interface_type filter was added
        call_kwargs = mock_client.filters.call_args.kwargs
        assert "interface_type__values" in call_kwargs
        assert "25gbase-x-sfp28" in call_kwargs["interface_type__values"]

    @pytest.mark.asyncio
    async def test_query_no_results(self, generator: EndpointConnectivityGenerator, mock_client: AsyncMock) -> None:
        """Test handling of no matching interfaces."""
        # Setup
        rack_ids = ["rack-1"]
        endpoint_interfaces = []

        mock_client.filters.return_value = []

        # Execute
        result = await generator._query_interfaces_by_location(
            rack_ids=rack_ids,
            device_role="tor",
            endpoint_interfaces=endpoint_interfaces,
        )

        # Verify
        assert len(result) == 0


class TestMiddleRackDeployment:
    """Test middle_rack deployment strategy."""

    @pytest.mark.asyncio
    async def test_middle_rack_queries_network_rack(
        self, generator: EndpointConnectivityGenerator, mock_client: AsyncMock
    ) -> None:
        """Test that middle_rack deployment queries network rack for Leaf interfaces."""
        # Setup: Server in compute rack
        rack = MockRack("rack-1", "pod-1", row_index=1, rack_type="compute")
        generator.data.rack = rack  # type: ignore[assignment]
        generator.data.interfaces = []

        # Mock network rack query
        network_rack = MockRack("rack-network", "pod-1", row_index=1, rack_type="network")
        mock_devices = [MockDevice(id="device-1", name="leaf-01", role="leaf")]
        mock_client.filters.side_effect = [
            [network_rack],  # First call: find network rack
            mock_devices,  # Second call: find devices in rack
            [  # Third call: query interfaces on Leaf devices
                MockInterface("Ethernet1/1", "leaf-01", device_role="leaf", status="free"),
                MockInterface("Ethernet1/2", "leaf-01", device_role="leaf", status="free"),
            ],
        ]

        # Mock the _process_endpoint_connections to avoid full execution
        with patch.object(generator, "_process_endpoint_connections", new=AsyncMock()) as mock_process:
            # Execute
            await generator._connect_middle_rack_deployment()

            # Verify: network rack was queried
            first_call = mock_client.filters.call_args_list[0]
            assert first_call.kwargs["row_index__value"] == 1
            assert first_call.kwargs["rack_type__value"] == "network"

            # Verify: _process_endpoint_connections was called with interfaces
            mock_process.assert_called_once()
            interfaces_arg = mock_process.call_args[0][0]
            assert len(interfaces_arg) == 2
            assert all(intf.device.role.value == "leaf" for intf in interfaces_arg)

    @pytest.mark.asyncio
    async def test_middle_rack_no_network_rack_raises_error(
        self, generator: EndpointConnectivityGenerator, mock_client: AsyncMock
    ) -> None:
        """Test that missing network rack raises RuntimeError."""
        # Setup
        rack = MockRack("rack-1", "pod-1", row_index=1, rack_type="compute")
        generator.data.rack = rack  # type: ignore[assignment]

        # Mock: No network rack found
        mock_client.filters.return_value = []

        # Execute & Verify
        with pytest.raises(RuntimeError, match="no network rack"):
            await generator._connect_middle_rack_deployment()


class TestToRDeployment:
    """Test tor deployment strategy."""

    @pytest.mark.asyncio
    async def test_tor_queries_same_rack_first(
        self, generator: EndpointConnectivityGenerator, mock_client: AsyncMock
    ) -> None:
        """Test that ToR deployment tries same rack first."""
        # Setup
        rack = MockRack("rack-1", "pod-1", row_index=1, rack_type="compute")
        generator.data.rack = rack  # type: ignore[assignment]
        generator.data.interfaces = []

        # Mock: devices and interfaces found in same rack
        mock_devices = [MockDevice(id="device-1", name="tor-01", role="tor")]
        mock_interfaces = [
            MockInterface("Ethernet1/1", "tor-01", device_role="tor", status="free"),
            MockInterface("Ethernet1/2", "tor-01", device_role="tor", status="free"),
        ]

        mock_client.filters.side_effect = [mock_devices, mock_interfaces]

        with patch.object(generator, "_process_endpoint_connections", new=AsyncMock()) as mock_process:
            # Execute
            await generator._connect_tor_deployment()

            # Verify: Two queries (devices + interfaces in same rack)
            assert mock_client.filters.call_count == 2

            # Verify interfaces passed to processor
            mock_process.assert_called_once()
            interfaces_arg = mock_process.call_args[0][0]
            assert len(interfaces_arg) == 2

    @pytest.mark.asyncio
    async def test_tor_fallback_to_same_row(
        self, generator: EndpointConnectivityGenerator, mock_client: AsyncMock
    ) -> None:
        """Test ToR deployment fallback to same row when same rack has no interfaces."""
        # Setup
        rack = MockRack("rack-1", "pod-1", row_index=1, rack_type="compute")
        generator.data.rack = rack  # type: ignore[assignment]
        generator.data.interfaces = []

        # Mock: First query (same rack) returns empty, second query (same row) returns interfaces
        row_rack = MockRack("rack-2", "pod-1", row_index=1, rack_type="compute")
        mock_devices = [MockDevice(id="device-2", name="tor-02", role="tor")]
        mock_client.filters.side_effect = [
            [],  # First call: no devices in same rack (skip interface query)
            [row_rack],  # Second call: find racks in same row
            mock_devices,  # Third call: find devices in row racks
            [  # Fourth call: query interfaces in row racks
                MockInterface("Ethernet1/1", "tor-02", device_role="tor", status="free"),
                MockInterface("Ethernet1/2", "tor-02", device_role="tor", status="free"),
            ],
        ]

        with patch.object(generator, "_process_endpoint_connections", new=AsyncMock()) as mock_process:
            # Execute
            await generator._connect_tor_deployment()

            # Verify: Fallback occurred (4 calls: no devices in rack, find row racks, find devices, get interfaces)
            assert mock_client.filters.call_count == 4

            # Verify interfaces from row were used
            mock_process.assert_called_once()


class TestMixedDeployment:
    """Test mixed deployment strategy."""

    @pytest.mark.asyncio
    async def test_mixed_prefers_tor_in_same_rack(
        self, generator: EndpointConnectivityGenerator, mock_client: AsyncMock
    ) -> None:
        """Test that mixed deployment prefers ToR in same rack."""
        # Setup
        rack = MockRack("rack-1", "pod-1", row_index=1, rack_type="compute")
        generator.data.rack = rack  # type: ignore[assignment]
        generator.data.interfaces = []

        # Mock: ToR devices and interfaces found in same rack
        mock_devices = [MockDevice(id="device-1", name="tor-01", role="tor")]
        mock_interfaces = [
            MockInterface("Ethernet1/1", "tor-01", device_role="tor", status="free"),
            MockInterface("Ethernet1/2", "tor-01", device_role="tor", status="free"),
        ]

        mock_client.filters.side_effect = [mock_devices, mock_interfaces]

        with patch.object(generator, "_process_endpoint_connections", new=AsyncMock()) as mock_process:
            # Execute
            await generator._connect_mixed_deployment()

            # Verify: Two queries (devices + interfaces in same rack)
            assert mock_client.filters.call_count == 2

            # Verify ToR interfaces used
            interfaces_arg = mock_process.call_args[0][0]
            assert all(intf.device.role.value == "tor" for intf in interfaces_arg)

    @pytest.mark.asyncio
    async def test_mixed_fallback_to_leaf_in_middle_rack(
        self, generator: EndpointConnectivityGenerator, mock_client: AsyncMock
    ) -> None:
        """Test mixed deployment fallback to Leaf in middle rack."""
        # Setup
        rack = MockRack("rack-1", "pod-1", row_index=1, rack_type="compute")
        generator.data.rack = rack  # type: ignore[assignment]
        generator.data.interfaces = []

        # Mock: No ToR in same rack, but Leaf in network rack
        network_rack = MockRack("rack-network", "pod-1", row_index=1, rack_type="network")
        mock_devices = [MockDevice(id="device-1", name="leaf-01", role="leaf")]
        mock_client.filters.side_effect = [
            [],  # First call: no ToR devices in same rack (skip interface query)
            [network_rack],  # Second call: find network rack
            mock_devices,  # Third call: find Leaf devices
            [  # Fourth call: query Leaf interfaces in network rack
                MockInterface("Ethernet1/1", "leaf-01", device_role="leaf", status="free"),
                MockInterface("Ethernet1/2", "leaf-01", device_role="leaf", status="free"),
            ],
        ]

        with patch.object(generator, "_process_endpoint_connections", new=AsyncMock()) as mock_process:
            # Execute
            await generator._connect_mixed_deployment()

            # Verify: Fallback occurred (4 calls: no devices, find rack, find devices, get interfaces)
            assert mock_client.filters.call_count == 4

            # Verify Leaf interfaces used
            interfaces_arg = mock_process.call_args[0][0]
            assert all(intf.device.role.value == "leaf" for intf in interfaces_arg)


class TestStatusFiltering:
    """Test status filtering for devices and interfaces."""

    @pytest.mark.asyncio
    async def test_device_status_includes_active_free_provisioning(
        self, generator: EndpointConnectivityGenerator, mock_client: AsyncMock
    ) -> None:
        """Test that device queries filter for active, free, and provisioning statuses."""
        rack_ids = ["rack-1"]
        endpoint_interfaces = []

        mock_client.filters.side_effect = [[], []]

        # Execute
        await generator._query_interfaces_by_location(
            rack_ids=rack_ids,
            device_role="leaf",
            endpoint_interfaces=endpoint_interfaces,
        )

        # Verify device status filter (first call)
        first_call_kwargs = mock_client.filters.call_args_list[0].kwargs
        assert set(first_call_kwargs["status__values"]) == {"active", "free", "provisioning"}

    @pytest.mark.asyncio
    async def test_interface_status_only_free(
        self, generator: EndpointConnectivityGenerator, mock_client: AsyncMock
    ) -> None:
        """Test that interface queries filter for free status only."""
        rack_ids = ["rack-1"]
        endpoint_interfaces = []

        mock_client.filters.side_effect = [[MockDevice(id="d1", name="leaf-01")], []]

        # Execute
        await generator._query_interfaces_by_location(
            rack_ids=rack_ids,
            device_role="leaf",
            endpoint_interfaces=endpoint_interfaces,
        )

        # Verify interface status filter (second call)
        second_call_kwargs = mock_client.filters.call_args_list[1].kwargs
        assert second_call_kwargs["status__value"] == "free"
