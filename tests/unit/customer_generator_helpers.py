"""The generator builder the DC and colocation customer footprint tests share."""

from __future__ import annotations

from typing import Any, TypeVar
from unittest.mock import AsyncMock, MagicMock

from generators.common import CommonGenerator

_T = TypeVar("_T", bound=CommonGenerator)


def make_generator(cls: type[_T]) -> Any:
    """A footprint generator of ``cls`` with a mocked client and no in-flight parent generator."""
    gen = cls.__new__(cls)
    gen.logger = MagicMock()
    gen.client = MagicMock()
    gen.client.filters = AsyncMock(return_value=[])
    gen.client.get = AsyncMock()
    gen.client.create = AsyncMock()
    gen.client.execute_graphql = AsyncMock()
    # generate() waits for an in-flight generator of the parent (the DC, or the colocation zone serving the
    # footprint) before reading its devices: none in flight in these unit tests, so no-op.
    gen.wait_for_parent_generator_and_refetch = AsyncMock(return_value=None)
    # _resolve_parent_cluster waits for the parent's generators when the cluster is missing, without a re-collect.
    gen.wait_for_parent_generators = AsyncMock(return_value=False)
    # The shared FirewallContext is provisioned under a resource lock (the PoolMixin's, tested in
    # test_dc_firewall_lb.py): stub it out. setattr: not every generator built here mixes in PoolMixin.
    setattr(gen, "acquire_resource_lock", AsyncMock(return_value="lock-id"))  # noqa: B010
    setattr(gen, "release_resource_lock", AsyncMock())  # noqa: B010
    # The border devices' VRF instances follow the legs (tests/unit/test_vrf_callers.py): not these tests'.
    setattr(gen, "_reconcile_border_vrfs", AsyncMock())  # noqa: B010
    return gen
