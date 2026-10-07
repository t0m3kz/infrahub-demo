# import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

# from infrahub_sdk import Config, InfrahubClientSync
# from infrahub_sdk.ctl.repository import get_repository_config
# from infrahub_sdk.schema.repository import InfrahubRepositoryConfig
# from infrahub_sdk.yaml import SchemaFile

CURRENT_DIR = Path(__file__).parent

# Add project root to sys.path for imports
sys.path.insert(0, str(CURRENT_DIR.parent))


# ============================================================================
# CablingPlanner Test Utilities
# ============================================================================


class MockInterface:
    """Mock DcimPhysicalInterface for testing."""

    def __init__(self, name: str, device_label: str, interface_type: str = "other") -> None:
        """Initialize mock interface.

        Args:
            name: Interface name (e.g., 'Ethernet1/1')
            device_label: Device display label (e.g., 'spine-01')
            interface_type: Dropdown value — mandatory with a default on the real
                schema (schemas/base/dcim.yml), so every real interface has one.
        """
        self.name: Any = Mock(value=name)
        self.device: Any = Mock(display_label=device_label)
        self.cable: Any = None
        self.interface_type: Any = Mock(value=interface_type)


def create_mock_interfaces(device_label: str, interface_names: list[str]) -> list[MockInterface]:
    """Helper to create multiple mock interfaces for a device.

    Args:
        device_label: Device display label
        interface_names: List of interface names

    Returns:
        List of MockInterface objects
    """
    return [MockInterface(name, device_label) for name in interface_names]


@pytest.fixture(scope="session")
def root_dir() -> Path:
    return Path(__file__).parent.parent.resolve()


@pytest.fixture(scope="session")
def data_dir(root_dir: Path) -> Path:
    return root_dir / "data"
