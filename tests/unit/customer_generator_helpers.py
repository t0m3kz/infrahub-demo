"""The generator builder of the customer pool boarding tests: a footprint generator on a mocked client."""

from __future__ import annotations

from typing import Any, TypeVar
from unittest.mock import AsyncMock, MagicMock

from generators.base import BaseGenerator

_T = TypeVar("_T", bound=BaseGenerator)


def make_generator(cls: type[_T]) -> Any:
    """A footprint generator of ``cls`` with a mocked client and no in-flight parent generator."""
    gen = cls.__new__(cls)
    gen.logger = MagicMock()
    gen.client = MagicMock()
    gen.client.filters = AsyncMock(return_value=[])
    gen.client.get = AsyncMock()
    gen.client.create = AsyncMock()
    gen.client.execute_graphql = AsyncMock()
    # generate() waits for an in-flight generator of the parent (the DC, or the colocation zone serving the footprint)
    # before reading its devices: none in flight in these unit tests, so no-op.
    gen.wait_for_parent_generator_and_refetch = AsyncMock(return_value=None)
    # resolve_context_target waits for the parent's generators when the cluster is missing, without a re-collect.
    gen.wait_for_parent_generators = AsyncMock(return_value=False)
    # The shared FirewallContext is provisioned under a resource lock (the BaseGenerator's, tested in
    # test_dc_firewall_lb.py): stub it out. setattr: not every generator built here mixes in BaseGenerator.
    setattr(gen, "acquire_resource_lock", AsyncMock(return_value="lock-id"))  # noqa: B010
    setattr(gen, "release_resource_lock", AsyncMock())  # noqa: B010
    # The customer's pools are carved when the footprint is boarded: ``boarded_pools`` (tests/conftest.py) stubs that.
    return gen
