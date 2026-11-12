# import json
import shutil
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

CURRENT_DIR = Path(__file__).parent

# Add project root to sys.path for imports
sys.path.insert(0, str(CURRENT_DIR.parent))


@pytest.fixture
def tmp_path() -> Iterator[Path]:
    """A scratch directory inside the repository, instead of pytest's own ``tmp_path``."""
    base = CURRENT_DIR / ".scratch"
    base.mkdir(exist_ok=True)
    path = Path(tempfile.mkdtemp(dir=base))
    yield path
    # Only the test's own directory goes: the shared parent is git-ignored and stays
    shutil.rmtree(path, ignore_errors=True)


# ============================================================================
# CablingPlanner Test Utilities
# ============================================================================


class MockInterface:
    """Mock DcimPhysicalInterface for testing."""

    def __init__(self, name: str, device_label: str, interface_type: str = "other") -> None:
        """Initialize mock interface."""
        self.name: Any = Mock(value=name)
        self.device: Any = Mock(display_label=device_label)
        self.cable: Any = None
        self.interface_type: Any = Mock(value=interface_type)


def create_mock_interfaces(device_label: str, interface_names: list[str]) -> list[MockInterface]:
    """Helper to create multiple mock interfaces for a device."""
    return [MockInterface(name, device_label) for name in interface_names]


@pytest.fixture(scope="session")
def root_dir() -> Path:
    return Path(__file__).parent.parent.resolve()


@pytest.fixture(scope="session")
def data_dir(root_dir: Path) -> Path:
    return root_dir / "data"
