"""Unit tests for cabling strategy functions."""

from unittest.mock import Mock

from generators.common import CablingStrategy, build_cabling_plan


class TestCablingStrategy:
    """Test cabling strategy calculations."""

    def _create_mock_device(self, name: str, index: int = 1) -> Mock:
        """Create a mock device with name and index attributes."""
        device = Mock()
        device.display_label = name
        device.index = Mock(value=index)
        return device

    def _create_mock_interface(self, name: str, device: Mock) -> Mock:
        """Create a mock interface with name and device attributes."""
        interface = Mock()
        interface.display_label = f"{device.display_label}:{name}"
        interface.device = device
        return interface

    def test_pod_strategy_single_pair(self) -> None:
        """Test pod strategy with single source and destination device."""
        src_device = self._create_mock_device("spine-01")
        dst_device = self._create_mock_device("leaf-01")

        src_interfaces = [self._create_mock_interface("Eth1", src_device)]
        dst_interfaces = [self._create_mock_interface("Eth1", dst_device)]

        src_interface_map = {src_device: src_interfaces}
        dst_interface_map = {dst_device: dst_interfaces}

        connections = build_cabling_plan(
            index=1,
            src_interface_map=src_interface_map,
            dst_interface_map=dst_interface_map,
            strategy=CablingStrategy.POD,
        )

        assert len(connections) == 1
        assert connections[0] == (src_interfaces[0], dst_interfaces[0])

    def test_pod_strategy_full_mesh(self) -> None:
        """Test pod strategy with multiple source and destination devices."""
        src_device_1 = self._create_mock_device("spine-01", index=1)
        src_device_2 = self._create_mock_device("spine-02", index=2)
        dst_device_1 = self._create_mock_device("leaf-01")
        dst_device_2 = self._create_mock_device("leaf-02")
        dst_device_3 = self._create_mock_device("leaf-03")

        # Each source device has 3 interfaces (one for each destination)
        src_1_interfaces = [
            self._create_mock_interface(f"Eth{i}", src_device_1) for i in range(1, 4)
        ]
        src_2_interfaces = [
            self._create_mock_interface(f"Eth{i}", src_device_2) for i in range(1, 4)
        ]

        # Each destination device needs enough interfaces for the algorithm
        # Index 1 means (1-2) * 3 = -3, so base index wraps/negative - we need to handle this
        # For POD strategy with index=1: dst_interface_base_index = -3, so dst_interface_index = -3, -2
        # This will access negative indices, which Python supports (wraps around)
        # Create more interfaces to avoid index errors
        dst_1_interfaces = [
            self._create_mock_interface(f"Eth{i}", dst_device_1) for i in range(1, 10)
        ]
        dst_2_interfaces = [
            self._create_mock_interface(f"Eth{i}", dst_device_2) for i in range(1, 10)
        ]
        dst_3_interfaces = [
            self._create_mock_interface(f"Eth{i}", dst_device_3) for i in range(1, 10)
        ]

        src_interface_map = {
            src_device_1: src_1_interfaces,
            src_device_2: src_2_interfaces,
        }
        dst_interface_map = {
            dst_device_1: dst_1_interfaces,
            dst_device_2: dst_2_interfaces,
            dst_device_3: dst_3_interfaces,
        }

        connections = build_cabling_plan(
            index=2,  # Use index=2 to avoid negative indexing: (2-2) * 3 = 0
            src_interface_map=src_interface_map,
            dst_interface_map=dst_interface_map,
            strategy=CablingStrategy.POD,
        )

        # 2 source devices × 3 destination devices = 6 connections
        assert len(connections) == 6

    def test_rack_strategy_with_index(self) -> None:
        """Test rack strategy with proper index parameter."""
        src_device = self._create_mock_device("spine-01", index=1)
        dst_device = self._create_mock_device("leaf-01")

        # Need enough interfaces for the range calculation
        src_interfaces = [
            self._create_mock_interface(f"Eth{i}", src_device) for i in range(1, 4)
        ]
        dst_interfaces = [
            self._create_mock_interface(f"Eth{i}", dst_device) for i in range(1, 5)
        ]

        src_interface_map = {src_device: src_interfaces}
        dst_interface_map = {dst_device: dst_interfaces}

        connections = build_cabling_plan(
            index=2,
            src_interface_map=src_interface_map,
            dst_interface_map=dst_interface_map,
            strategy=CablingStrategy.RACK,
        )

        # Should have connections based on rack strategy
        assert len(connections) >= 1

    def test_invalid_strategy_raises_error(self) -> None:
        """Test that invalid strategy raises ValueError."""
        src_device = self._create_mock_device("spine-01")
        dst_device = self._create_mock_device("leaf-01")

        src_interfaces = [self._create_mock_interface("Eth1", src_device)]
        dst_interfaces = [self._create_mock_interface("Eth1", dst_device)]

        src_interface_map = {src_device: src_interfaces}
        dst_interface_map = {dst_device: dst_interfaces}

        invalid_strategy = Mock()
        try:
            build_cabling_plan(
                index=1,
                src_interface_map=src_interface_map,
                dst_interface_map=dst_interface_map,
                strategy=invalid_strategy,
            )
            assert False, "Should have raised ValueError"
        except ValueError as e:
            assert "Unknown cabling strategy" in str(e)

    def test_rack_strategy_device_missing_index_raises_error(self) -> None:
        """Test that RACK strategy raises error if device lacks index attribute."""
        src_device = Mock()
        src_device.display_label = "spine-01"
        # No index attribute
        dst_device = self._create_mock_device("leaf-01")

        src_interfaces = [self._create_mock_interface("Eth1", src_device)]
        dst_interfaces = [
            self._create_mock_interface(f"Eth{i}", dst_device) for i in range(1, 5)
        ]

        src_interface_map = {src_device: src_interfaces}
        dst_interface_map = {dst_device: dst_interfaces}

        try:
            build_cabling_plan(
                index=2,
                src_interface_map=src_interface_map,
                dst_interface_map=dst_interface_map,
                strategy=CablingStrategy.RACK,
            )
            assert False, "Should have raised ValueError for missing index"
        except ValueError as e:
            assert "index.value" in str(e)
