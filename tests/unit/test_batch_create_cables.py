"""Unit tests for _batch_create_cables method in PodTopologyGenerator."""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from generators.generate_pod import PodTopologyGenerator


def create_pod_generator() -> PodTopologyGenerator:
    """Create a mocked PodTopologyGenerator instance for testing."""
    gen = PodTopologyGenerator.__new__(PodTopologyGenerator)
    gen.client = MagicMock()
    gen.logger = MagicMock(spec=["info", "debug", "warning", "error"])
    gen.branch = "test-branch"
    return gen


class TestBatchCreateCables:
    """Test _batch_create_cables method with minimalistic GraphQL fixture."""

    @pytest.fixture
    def generator(self) -> PodTopologyGenerator:
        """Create PodTopologyGenerator instance for testing."""
        return create_pod_generator()

    @pytest.mark.asyncio
    async def test_batch_create_cables_single_pair(
        self, generator: PodTopologyGenerator
    ) -> None:
        """Test batch cable creation with single spine/super-spine pair."""
        # Setup batch mock
        batch_mock = AsyncMock()
        batch_mock.add = MagicMock()
        batch_mock.execute = AsyncMock()
        batch_mock.execute.return_value = [
            (MagicMock(display_label="cable-dc1-pod-a1-spine-01"), None)
        ]

        generator.client.create_batch = AsyncMock(return_value=batch_mock)
        generator.client.create = AsyncMock()
        generator.client.filters = AsyncMock(return_value=[])

        # Setup store with mock interfaces
        spine_iface = MagicMock(display_label="Ethernet1")
        super_spine_iface = MagicMock(display_label="Ethernet1")

        def store_lookup(key: str) -> MagicMock:
            if "spine-01" in key and "super" not in key:
                return spine_iface
            return super_spine_iface

        generator.client.store.get_by_hfid = MagicMock(side_effect=store_lookup)

        # Mock cable creation
        cable_mock = AsyncMock()
        generator.client.create.return_value = cable_mock

        # Execute test
        await generator._batch_create_cables(
            spines=["dc1-pod-a1-spine-01"],
            super_spines=["dc1-super-spine-01"],
            spine_interface_names=["Ethernet1", "Ethernet2"],
            super_spine_interface_names=["Ethernet1", "Ethernet2"],
            fabric_interface_sorting_method="top_down",
            spine_interface_sorting_method="bottom_up",
        )

        # Assertions
        assert batch_mock.add.called
        assert batch_mock.execute.called

    @pytest.mark.asyncio
    async def test_batch_create_cables_multiple_pairs(
        self, generator: PodTopologyGenerator
    ) -> None:
        """Test batch cable creation with multiple spine/super-spine pairs."""
        # Setup mocks
        batch_mock = AsyncMock()
        batch_mock.add = MagicMock()
        batch_mock.execute = AsyncMock()
        batch_mock.execute.return_value = [
            (MagicMock(display_label="cable-1"), None),
            (MagicMock(display_label="cable-2"), None),
        ]

        generator.client.create_batch = AsyncMock(return_value=batch_mock)
        generator.client.create = AsyncMock()
        generator.client.filters = AsyncMock(return_value=[])
        generator.client.store.get_by_hfid = MagicMock(return_value=MagicMock())

        cable_mock = AsyncMock()
        generator.client.create.return_value = cable_mock

        # Execute with 2 spines and 2 super-spines (4 cables total)
        await generator._batch_create_cables(
            spines=["dc1-pod-a1-spine-01", "dc1-pod-a1-spine-02"],
            super_spines=["dc1-super-spine-01", "dc1-super-spine-02"],
            spine_interface_names=["Ethernet1", "Ethernet2"],
            super_spine_interface_names=["Ethernet1", "Ethernet2"],
            fabric_interface_sorting_method="top_down",
            spine_interface_sorting_method="bottom_up",
        )

        # Should create cables
        assert batch_mock.add.called
        assert batch_mock.execute.called

    @pytest.mark.asyncio
    async def test_batch_create_cables_empty_devices(
        self, generator: PodTopologyGenerator
    ) -> None:
        """Test batch cable creation with empty device lists."""
        batch_mock = AsyncMock()
        batch_mock.add = MagicMock()
        batch_mock.execute = AsyncMock(return_value=[])

        generator.client.create_batch = AsyncMock(return_value=batch_mock)
        generator.client.filters = AsyncMock(return_value=[])

        # Execute with empty lists
        await generator._batch_create_cables(
            spines=[],
            super_spines=[],
            spine_interface_names=[],
            super_spine_interface_names=[],
            fabric_interface_sorting_method="top_down",
            spine_interface_sorting_method="bottom_up",
        )

        # Batch should have been created but no cables added
        assert batch_mock.add.call_count == 0

    @pytest.mark.asyncio
    async def test_batch_create_cables_interface_missing_in_store(
        self, generator: PodTopologyGenerator
    ) -> None:
        """Test batch cable creation when interface not found in store."""
        batch_mock = AsyncMock()
        batch_mock.add = MagicMock()
        batch_mock.execute = AsyncMock(return_value=[])

        generator.client.create_batch = AsyncMock(return_value=batch_mock)
        generator.client.filters = AsyncMock(return_value=[])
        generator.client.store.get_by_hfid = MagicMock(return_value=None)

        # Execute
        await generator._batch_create_cables(
            spines=["spine-01"],
            super_spines=["super-spine-01"],
            spine_interface_names=["Ethernet1"],
            super_spine_interface_names=["Ethernet1"],
            fabric_interface_sorting_method="top_down",
            spine_interface_sorting_method="bottom_up",
        )

        # No cables should be added when interfaces are missing
        assert batch_mock.add.call_count == 0

    @pytest.mark.asyncio
    async def test_batch_create_cables_interface_sorting(
        self, generator: PodTopologyGenerator
    ) -> None:
        """Test interface sorting with different methods."""
        batch_mock = AsyncMock()
        batch_mock.add = MagicMock()
        batch_mock.execute = AsyncMock(return_value=[])

        generator.client.create_batch = AsyncMock(return_value=batch_mock)
        generator.client.create = AsyncMock()
        generator.client.filters = AsyncMock(return_value=[])

        # Setup store with mock interfaces to allow cable creation
        spine_iface = MagicMock()
        super_spine_iface = MagicMock()

        generator.client.store.get_by_hfid = MagicMock(
            side_effect=lambda key: spine_iface
            if "spine-01" in key
            else super_spine_iface
        )

        cable_mock = AsyncMock()
        generator.client.create.return_value = cable_mock

        # Execute with bottom_up sorting
        await generator._batch_create_cables(
            spines=["spine-01"],
            super_spines=["super-spine-01"],
            spine_interface_names=["Ethernet1", "Ethernet2", "Ethernet3"],
            super_spine_interface_names=["Ethernet1", "Ethernet2", "Ethernet3"],
            fabric_interface_sorting_method="bottom_up",
            spine_interface_sorting_method="bottom_up",
        )

        # Batch should be created and executed
        assert batch_mock.execute.called
