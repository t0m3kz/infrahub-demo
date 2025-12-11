"""Unit tests for endpoint connectivity generator.

Tests deployment strategies (middle_rack, tor, mixed) with strict interface
type matching and idempotency validation following tests/AGENTS.md patterns.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, Mock, patch

from generators.endpoint_connectivity import EndpointConnectivityGenerator
from generators.models import (
    EndpointDevice,
    EndpointInterface,
    EndpointPod,
    EndpointRack,
    RackDevice,
)


def create_endpoint_device(
    deployment_type: str, rack_devices: list[RackDevice] | None = None
) -> EndpointDevice:
    """Create test endpoint device with specified deployment type.

    Args:
        deployment_type: Type of deployment (middle_rack, tor, mixed)
        rack_devices: Optional list of devices in the rack

    Returns:
        EndpointDevice instance for testing
    """
    return EndpointDevice(
        id="endpoint-1",
        name="server-01",
        role="server",
        interfaces=[
            EndpointInterface(
                id="intf-1",
                name="eth0",
                interface_type="10gbase-x-sfpp",
                role="customer",
                status="active",
                cable=None,
            ),
            EndpointInterface(
                id="intf-2",
                name="eth1",
                interface_type="10gbase-x-sfpp",
                role="customer",
                status="active",
                cable=None,
            ),
        ],
        rack=EndpointRack(
            id="rack-1",
            name="compute-rack-1",
            index=1,
            row_index=1,
            rack_type="compute",
            pod=EndpointPod(
                id="pod-1",
                name="pod-1",
                deployment_type=deployment_type,
                index=1,
            ),
            devices=rack_devices or [],
        ),
    )


class TestMiddleRackDeployment:
    """Test middle_rack deployment primary and error scenarios."""

    async def test_connects_to_tors_in_same_row(self) -> None:
        """Primary path: successfully connects to ToRs in same row."""
        with patch(
            "generators.endpoint_connectivity.EndpointConnectivityGenerator.__init__",
            return_value=None,
        ):
            generator = EndpointConnectivityGenerator()  # type: ignore
            generator.client = Mock()
            generator.client.get = AsyncMock(
                return_value=Mock(deployment=Mock(_peer=None), save=AsyncMock())
            )
            generator.client.filters = AsyncMock(return_value=[])
            generator.logger = Mock()
            generator.branch = "main"
            generator.data = create_endpoint_device("middle_rack")

            # Mock ToR devices in row - need actual strings for regex operations
            mock_tor1 = Mock(spec=["id", "name", "role"])
            mock_tor1.id = "tor-1"
            mock_tor1.name = Mock(value="tor-01")
            mock_tor1.role = Mock(value="tor")

            mock_tor2 = Mock(spec=["id", "name", "role"])
            mock_tor2.id = "tor-2"
            mock_tor2.name = Mock(value="tor-02")
            mock_tor2.role = Mock(value="tor")

            mock_tors = [mock_tor1, mock_tor2]

            # Mock interface query - sortable string values
            mock_intf1 = Mock(spec=["id", "name", "interface_type", "role", "cable"])
            mock_intf1.id = "intf-tor-1"
            mock_intf1.name = Mock(value="Ethernet1")
            mock_intf1.interface_type = Mock(value="10gbase-x-sfpp")
            mock_intf1.role = Mock(value="access")
            mock_intf1.cable = None

            mock_intf2 = Mock(spec=["id", "name", "interface_type", "role", "cable"])
            mock_intf2.id = "intf-tor-2"
            mock_intf2.name = Mock(value="Ethernet2")
            mock_intf2.interface_type = Mock(value="10gbase-x-sfpp")
            mock_intf2.role = Mock(value="access")
            mock_intf2.cable = None

            mock_interfaces = [mock_intf1, mock_intf2]

            async def mock_filters_side_effect(*args: Any, **kwargs: Any) -> list[Any]:
                if "rack__ids" in kwargs or "row_index__value" in kwargs:
                    return mock_tors
                return mock_interfaces

            generator.client.filters = AsyncMock(side_effect=mock_filters_side_effect)
            generator.create_cabling = AsyncMock()  # type: ignore

            await generator._connect_middle_rack_deployment()

            # Verify cabling was created
            generator.create_cabling.assert_called_once()  # type: ignore
            assert any(
                "[middle_rack]" in str(call)
                for call in generator.logger.info.call_args_list
            )

    async def test_handles_no_tors_or_leafs(self) -> None:
        """Error handling: logs warning when no ToRs or Leafs available."""
        with patch(
            "generators.endpoint_connectivity.EndpointConnectivityGenerator.__init__",
            return_value=None,
        ):
            generator = EndpointConnectivityGenerator()  # type: ignore
            generator.client = Mock()
            generator.client.get = AsyncMock(
                return_value=Mock(deployment=Mock(_peer=None), save=AsyncMock())
            )
            generator.client.filters = AsyncMock(return_value=[])
            generator.logger = Mock()
            generator.branch = "main"
            generator.data = create_endpoint_device("middle_rack")
            generator.create_cabling = AsyncMock()  # type: ignore

            await generator._connect_middle_rack_deployment()

            # Verify warning was logged
            assert any(
                "No ToRs or Leafs found" in str(call)
                for call in generator.logger.warning.call_args_list
            )
            generator.create_cabling.assert_not_called()  # type: ignore


class TestTorDeployment:
    """Test tor deployment primary and error scenarios."""

    async def test_connects_to_tors_in_same_rack(self) -> None:
        """Primary path: successfully connects to ToRs in same rack."""
        with patch(
            "generators.endpoint_connectivity.EndpointConnectivityGenerator.__init__",
            return_value=None,
        ):
            # Create endpoint with ToRs in rack
            rack_tors = [
                RackDevice(
                    id="tor-1",
                    name="tor-01",
                    role="tor",
                    rack_row_index=1,
                    interfaces=[],
                ),
                RackDevice(
                    id="tor-2",
                    name="tor-02",
                    role="tor",
                    rack_row_index=1,
                    interfaces=[],
                ),
            ]
            endpoint = create_endpoint_device("tor", rack_tors)

            generator = EndpointConnectivityGenerator()  # type: ignore
            generator.client = Mock()
            generator.client.get = AsyncMock(
                return_value=Mock(deployment=Mock(_peer=None), save=AsyncMock())
            )
            generator.logger = Mock()
            generator.branch = "main"
            generator.data = endpoint

            # Mock compatible interfaces with proper spec
            mock_intf1 = Mock(spec=["id", "name", "interface_type", "role", "cable"])
            mock_intf1.id = "intf-tor-1"
            mock_intf1.name = Mock(value="Ethernet1")
            mock_intf1.interface_type = Mock(value="10gbase-x-sfpp")
            mock_intf1.role = Mock(value="access")
            mock_intf1.cable = None

            mock_intf2 = Mock(spec=["id", "name", "interface_type", "role", "cable"])
            mock_intf2.id = "intf-tor-2"
            mock_intf2.name = Mock(value="Ethernet2")
            mock_intf2.interface_type = Mock(value="10gbase-x-sfpp")
            mock_intf2.role = Mock(value="access")
            mock_intf2.cable = None

            mock_interfaces = [mock_intf1, mock_intf2]
            generator.client.filters = AsyncMock(return_value=mock_interfaces)
            generator.create_cabling = AsyncMock()  # type: ignore

            await generator._connect_tor_deployment()

            # Verify connections created to rack ToRs
            generator.create_cabling.assert_called_once()  # type: ignore
            assert any(
                "[tor]" in str(call) for call in generator.logger.info.call_args_list
            )

    async def test_handles_no_tors_available(self) -> None:
        """Error handling: logs warning when no ToRs available."""
        with patch(
            "generators.endpoint_connectivity.EndpointConnectivityGenerator.__init__",
            return_value=None,
        ):
            generator = EndpointConnectivityGenerator()  # type: ignore
            generator.client = Mock()
            generator.client.get = AsyncMock(
                return_value=Mock(deployment=Mock(_peer=None), save=AsyncMock())
            )
            generator.client.filters = AsyncMock(return_value=[])
            generator.logger = Mock()
            generator.branch = "main"
            generator.data = create_endpoint_device("tor", [])
            generator.create_cabling = AsyncMock()  # type: ignore

            await generator._connect_tor_deployment()

            # Verify warning logged
            assert any(
                "No ToR devices found" in str(call)
                for call in generator.logger.warning.call_args_list
            )
            generator.create_cabling.assert_not_called()  # type: ignore


class TestMixedDeployment:
    """Test mixed deployment primary and error scenarios."""

    async def test_connects_to_tors_in_same_rack(self) -> None:
        """Primary path: successfully connects to ToRs in same rack."""
        with patch(
            "generators.endpoint_connectivity.EndpointConnectivityGenerator.__init__",
            return_value=None,
        ):
            rack_tors = [
                RackDevice(
                    id="tor-1",
                    name="tor-01",
                    role="tor",
                    rack_row_index=1,
                    interfaces=[],
                ),
                RackDevice(
                    id="tor-2",
                    name="tor-02",
                    role="tor",
                    rack_row_index=1,
                    interfaces=[],
                ),
            ]
            endpoint = create_endpoint_device("mixed", rack_tors)

            generator = EndpointConnectivityGenerator()  # type: ignore
            generator.client = Mock()
            generator.client.get = AsyncMock(
                return_value=Mock(deployment=Mock(_peer=None), save=AsyncMock())
            )
            generator.logger = Mock()
            generator.branch = "main"
            generator.data = endpoint

            # Mock compatible interfaces with proper spec
            mock_intf1 = Mock(spec=["id", "name", "interface_type", "role", "cable"])
            mock_intf1.id = "intf-tor-1"
            mock_intf1.name = Mock(value="Ethernet1")
            mock_intf1.interface_type = Mock(value="10gbase-x-sfpp")
            mock_intf1.role = Mock(value="access")
            mock_intf1.cable = None

            mock_intf2 = Mock(spec=["id", "name", "interface_type", "role", "cable"])
            mock_intf2.id = "intf-tor-2"
            mock_intf2.name = Mock(value="Ethernet2")
            mock_intf2.interface_type = Mock(value="10gbase-x-sfpp")
            mock_intf2.role = Mock(value="access")
            mock_intf2.cable = None

            mock_interfaces = [mock_intf1, mock_intf2]
            generator.client.filters = AsyncMock(return_value=mock_interfaces)
            generator.create_cabling = AsyncMock()  # type: ignore

            await generator._connect_mixed_deployment()

            generator.create_cabling.assert_called_once()  # type: ignore

    async def test_handles_no_devices_available(self) -> None:
        """Error handling: logs warning when no ToRs or Leafs available."""
        with patch(
            "generators.endpoint_connectivity.EndpointConnectivityGenerator.__init__",
            return_value=None,
        ):
            generator = EndpointConnectivityGenerator()  # type: ignore
            generator.client = Mock()
            generator.client.get = AsyncMock(
                return_value=Mock(deployment=Mock(_peer=None), save=AsyncMock())
            )
            generator.client.filters = AsyncMock(return_value=[])
            generator.logger = Mock()
            generator.branch = "main"
            generator.data = create_endpoint_device("mixed", [])
            generator.create_cabling = AsyncMock()  # type: ignore

            await generator._connect_mixed_deployment()

            assert any(
                "No ToR or middle rack leaf devices found" in str(call)
                for call in generator.logger.warning.call_args_list
            )
            generator.create_cabling.assert_not_called()  # type: ignore


class TestInterfaceTypeMatching:
    """Test strict interface type matching via query filtering."""

    async def test_query_filters_by_interface_type(self) -> None:
        """Verify query uses interface types for filtering."""
        with patch(
            "generators.endpoint_connectivity.EndpointConnectivityGenerator.__init__",
            return_value=None,
        ):
            generator = EndpointConnectivityGenerator()  # type: ignore
            generator.client = Mock()
            generator.logger = Mock()
            generator.branch = "main"

            endpoint_intfs = [
                Mock(interface_type="10gbase-x-sfpp", name="eth0"),
                Mock(interface_type="25gbase-x-sfp28", name="eth1"),
            ]

            # Mock interfaces matching types
            mock_interfaces = [
                Mock(
                    interface_type=Mock(value="10gbase-x-sfpp"),
                    name=Mock(value="Ethernet1"),
                    cable=None,
                ),
                Mock(
                    interface_type=Mock(value="25gbase-x-sfp28"),
                    name=Mock(value="Ethernet2"),
                    cable=None,
                ),
            ]
            generator.client.filters = AsyncMock(return_value=mock_interfaces)

            result = await generator._query_compatible_interfaces(
                device_names=["tor-01"], endpoint_interfaces=endpoint_intfs
            )

            # Verify filter was called with interface types
            generator.client.filters.assert_called_once()
            call_kwargs = generator.client.filters.call_args.kwargs
            assert "interface_type__values" in call_kwargs
            assert set(call_kwargs["interface_type__values"]) == {
                "10gbase-x-sfpp",
                "25gbase-x-sfp28",
            }
            assert len(result) == 2

    async def test_query_excludes_cabled_interfaces(self) -> None:
        """Verify query filters out already-cabled interfaces."""
        with patch(
            "generators.endpoint_connectivity.EndpointConnectivityGenerator.__init__",
            return_value=None,
        ):
            generator = EndpointConnectivityGenerator()  # type: ignore
            generator.client = Mock()
            generator.logger = Mock()
            generator.branch = "main"

            endpoint_intfs = [Mock(interface_type="10gbase-x-sfpp")]

            # Mock mix of cabled and uncabled interfaces
            mock_interfaces = [
                Mock(
                    interface_type=Mock(value="10gbase-x-sfpp"),
                    cable=Mock(id="cable-1"),
                ),
                Mock(interface_type=Mock(value="10gbase-x-sfpp"), cable=None),
                Mock(interface_type=Mock(value="10gbase-x-sfpp"), cable=Mock(id=None)),
            ]
            generator.client.filters = AsyncMock(return_value=mock_interfaces)

            result = await generator._query_compatible_interfaces(
                device_names=["tor-01"], endpoint_interfaces=endpoint_intfs
            )

            # Should return only uncabled interfaces (last 2)
            assert len(result) == 2
            assert all(not (intf.cable and intf.cable.id) for intf in result)


class TestIdempotency:
    """Test idempotency - running generator multiple times produces same result."""

    async def test_skips_interfaces_with_existing_cables(self) -> None:
        """Verify already-connected interfaces are skipped."""
        with patch(
            "generators.endpoint_connectivity.EndpointConnectivityGenerator.__init__",
            return_value=None,
        ):
            # Create endpoint with already-cabled interfaces
            rack_tors = [
                RackDevice(
                    id="tor-1",
                    name="tor-01",
                    role="tor",
                    rack_row_index=1,
                    interfaces=[],
                ),
                RackDevice(
                    id="tor-2",
                    name="tor-02",
                    role="tor",
                    rack_row_index=1,
                    interfaces=[],
                ),
            ]
            endpoint = create_endpoint_device("tor", rack_tors)
            # Mark interfaces as already connected
            endpoint.interfaces[0].cable = Mock(id="cable-1")
            endpoint.interfaces[1].cable = Mock(id="cable-2")

            generator = EndpointConnectivityGenerator()  # type: ignore
            generator.client = Mock()
            generator.client.get = AsyncMock(
                return_value=Mock(deployment=Mock(_peer=None), save=AsyncMock())
            )
            generator.client.filters = AsyncMock(return_value=[])
            generator.logger = Mock()
            generator.branch = "main"
            generator.data = endpoint
            generator.create_cabling = AsyncMock()  # type: ignore

            await generator._connect_tor_deployment()

            # Verify info message about already-cabled interfaces
            assert any(
                "already have cables" in str(call)
                for call in generator.logger.info.call_args_list
            )
            generator.create_cabling.assert_not_called()  # type: ignore


class TestDeviceSelection:
    """Test device selection logic for dual-homing."""

    def test_selects_consecutive_device_pair(self) -> None:
        """Verify selection of consecutive odd-even device pairs."""
        with patch(
            "generators.endpoint_connectivity.EndpointConnectivityGenerator.__init__",
            return_value=None,
        ):
            generator = EndpointConnectivityGenerator()  # type: ignore
            generator.logger = Mock()

            devices = [
                {"name": {"value": "tor-01"}},
                {"name": {"value": "tor-02"}},
                {"name": {"value": "tor-03"}},
                {"name": {"value": "tor-04"}},
            ]

            pair = generator._select_consecutive_device_pair(devices, "tor")

            # Should select tor-01 and tor-02 (first consecutive odd-even pair)
            assert len(pair) == 2
            assert pair[0]["name"]["value"] == "tor-01"
            assert pair[1]["name"]["value"] == "tor-02"


class TestErrorHandling:
    """Test error handling and data validation."""

    async def test_handles_missing_rack(self) -> None:
        """Error handling: logs error when endpoint has no rack assigned."""
        with patch(
            "generators.endpoint_connectivity.EndpointConnectivityGenerator.__init__",
            return_value=None,
        ):
            endpoint = create_endpoint_device("tor")
            endpoint.rack = None

            generator = EndpointConnectivityGenerator()  # type: ignore
            generator.client = Mock()
            generator.logger = Mock()
            generator.branch = "main"
            generator.data = endpoint

            # Call generate with data dict
            await generator.generate({"DcimGenericDevice": [endpoint.model_dump()]})

            # Verify error logged
            assert any(
                "has no rack assigned" in str(call)
                for call in generator.logger.error.call_args_list
            )
