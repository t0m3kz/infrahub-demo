# import json
import shutil
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest

CURRENT_DIR = Path(__file__).parent

# Add project root to sys.path for imports
sys.path.insert(0, str(CURRENT_DIR.parent))

from tests.unit.generator_steps import generator_steps  # noqa: E402, F401  (fixture)


@pytest.fixture
def tmp_path() -> Iterator[Path]:
    """A scratch directory inside the repository, instead of pytest's own ``tmp_path``."""
    base = CURRENT_DIR / ".scratch"
    base.mkdir(exist_ok=True)
    path = Path(tempfile.mkdtemp(dir=base))
    yield path
    # Only the test's own directory goes: the shared parent is git-ignored and stays
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture(autouse=True)
def no_held_allocations(request: pytest.FixtureRequest) -> Iterator[None]:
    """A fake client has no ``InfrahubResourcePoolAllocated``: for every test but ``test_ipam_allocation`` the branch
    holds nothing under any identifier, so ``generators.ipam.allocation`` goes to the fake allocator as the direct call
    did. ``test_ipam_allocation`` serves the allocations itself.
    """
    if request.module.__name__.endswith("test_ipam_allocation"):
        yield
        return

    async def held(client: Any, pool: Any, identifier: str) -> None:  # noqa: ARG001
        return None

    async def remember(client: Any, pool: Any, identifier: str, node: Any) -> None:  # noqa: ARG001
        return None

    with (
        patch("generators.ipam.allocation._reserved", held),
        patch("generators.ipam.allocation._remember", remember),
    ):
        yield


_BOARDING_TESTS = ("test_customer_pool_boarding", "test_cloud_range")
"""The test modules that run the real boarding of the customer's pools."""


@pytest.fixture(autouse=True)
def boarded_pools(request: pytest.FixtureRequest) -> Iterator[None]:
    """A footprint generator under test boards the customer's pools (``generators/ipam/customer_pool.py``) through the
    reconciler. The fake clients of the customer generator tests answer the reads of their own flow only (no
    ``CoreIPPrefixPool``), and the pools have their own tests: here the boarding is a no-op.
    """
    if request.module.__name__.endswith(_BOARDING_TESTS):
        yield
        return
    with patch("generators.reconciler.ensure_customer_pools", AsyncMock()):
        yield


_RECORDING_POOL_TESTS = (
    "test_segment_customer_pool",
    "test_segment_ipv6_addressing",
    "test_segment_plans",
    "test_customer_pool_boarding",
    "test_cloud_range",
    "test_device_plan_helpers",
)
"""The test modules whose customer pools are carved from the recording ``Client`` of segment_customer_pool_helpers."""


@pytest.fixture(autouse=True)
def recording_customer_pools(request: pytest.FixtureRequest) -> Iterator[None]:
    """The carving of a customer's pool and the read of its tenant parent answer from the recording client
    (``tests/unit/segment_customer_pool_helpers.py``)."""
    if not request.module.__name__.endswith(_RECORDING_POOL_TESTS):
        yield
        return
    from tests.unit.segment_customer_pool_helpers import recording_pool_functions

    with recording_pool_functions():
        yield


@pytest.fixture(autouse=True)
def no_schema_check(request: pytest.FixtureRequest) -> Iterator[None]:
    """The schema pre-check of the reconciler reads ``client.schema``, which the fake clients of most tests do not serve:
    it passes every plan, except in the modules named ``*schema_check*``, which serve a schema (``schema_fakes.py``).
    """
    if "schema_check" in request.module.__name__:
        yield
        return

    async def unchecked(reconciler: Any, plan: Any) -> None:  # noqa: ARG001
        return None

    with patch("generators.reconciler.PlanReconciler._check_schema", unchecked):
        yield


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
