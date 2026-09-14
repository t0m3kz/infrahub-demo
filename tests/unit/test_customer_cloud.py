"""Unit tests for CustomerDeploymentCloudExchangeGenerator
(generators/topology/customer_cloud.py).

Cloud has no ManagedFirewallHA at all (cloud-native security groups instead
— see CloudSecurityGroup), so this generator's only job is the
hub-and-spoke exchange path — identical logic to
customer_colocation.py/customer_office.py's own exchange path.
"""

from __future__ import annotations

from typing import Any, TypeVar
from unittest.mock import AsyncMock, MagicMock

import pytest

from generators.common import CommonGenerator
from generators.topology.customer_cloud import CustomerDeploymentCloudExchangeGenerator

_T = TypeVar("_T", bound=CommonGenerator)


def _make_generator(cls: type[_T]) -> Any:
    gen = cls.__new__(cls)
    gen.logger = MagicMock()
    gen.client = MagicMock()
    gen.client.filters = AsyncMock(return_value=[])
    gen.client.get = AsyncMock()
    gen.client.create = AsyncMock()
    gen.client.execute_graphql = AsyncMock()
    return gen


class TestCloudDeployment:
    @pytest.mark.asyncio
    async def test_no_circuits_is_noop(self) -> None:
        gen = _make_generator(CustomerDeploymentCloudExchangeGenerator)
        payload = {
            "TopologyCustomerCloud": [
                {
                    "id": "cust-1",
                    "name": "C003-P-AWS",
                    "environment": "p",
                    "owner": {"org_id": "C003", "name": "Vaultex Inc."},
                    "circuits": [],
                }
            ]
        }

        await gen.generate(payload)

        gen.client.filters.assert_not_called()

    @pytest.mark.asyncio
    async def test_never_provisions_firewall_context(self) -> None:
        """TopologyCustomerCloud has no ManagedFirewallHA at all — this
        generator has no FirewallContext logic to begin with."""
        gen = _make_generator(CustomerDeploymentCloudExchangeGenerator)
        payload = {
            "TopologyCustomerCloud": [
                {"id": "cust-1", "name": "C003-P-AWS", "owner": {}, "parent": {"id": "region-1"}, "circuits": []}
            ]
        }

        await gen.generate(payload)

        gen.client.filters.assert_not_called()
