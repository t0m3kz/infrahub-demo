"""Unit tests for EndpointConnectivityGenerator (generators/connectivity/endpoint.py)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from generators.protocols import DcimPhysicalDevice
from tests.unit.endpoint_generator_helpers import _endpoint_data, _make_generator

# generate() — guard clauses and dispatch


class TestGenerateGuardClauses:
    @pytest.mark.asyncio
    async def test_no_device_data_logs_error_and_returns(self) -> None:
        gen = _make_generator()

        await gen.generate({"DcimDevice": []})

        gen.logger.error.assert_called_once()
        gen.client.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_invalid_model_data_logs_error_and_returns(self) -> None:
        gen = _make_generator()

        await gen.generate({"DcimDevice": [{"id": "ep-1"}]})  # missing required "name"

        gen.logger.error.assert_called_once()
        gen.client.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_rack_logs_error_and_returns(self) -> None:
        gen = _make_generator()

        await gen.generate(_endpoint_data(rack=None))

        gen.logger.error.assert_called_once()
        gen.client.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_colocation_rack_returns_without_touching_the_device(self) -> None:
        """A host in a rack that does not hang off a DC-fabric pod is a no-op."""
        gen = _make_generator()

        await gen.generate(_endpoint_data(rack={"id": "rack-1", "name": "NY1-NY1-R1-1", "deployment": {}}))

        gen.client.get.assert_not_awaited()
        gen.client.filters.assert_not_awaited()
        gen.logger.error.assert_not_called()
        assert any("does not hang off a DC-fabric pod" in str(call.args[0]) for call in gen.logger.info.call_args_list)

    @pytest.mark.asyncio
    async def test_rack_without_pod_returns_without_error(self) -> None:
        """Same guard covers a rack whose `deployment` relationship is unset."""
        gen = _make_generator()

        await gen.generate(_endpoint_data(rack={"id": "rack-1", "name": "ORPHAN-RACK"}))

        gen.client.get.assert_not_awaited()
        gen.logger.error.assert_not_called()

    @pytest.mark.asyncio
    async def test_filters_empty_interface_nodes(self) -> None:
        """Empty {} entries in `interfaces`."""
        gen = _make_generator()
        gen.client.filters = AsyncMock(side_effect=[[]])  # no uplink interfaces

        data = _endpoint_data()
        data["DcimDevice"][0]["interfaces"] = [{}, {"id": "i1", "name": "eth0"}]

        await gen.generate(data)

        assert len(gen.data["interfaces"]) == 1
        assert gen.data["interfaces"][0]["name"] == "eth0"


def _record_writes(gen: Any) -> list[tuple[Any, dict[str, Any], dict[str, Any]]]:
    """Make the client's create return nodes whose save is recorded as (kind, data, save kwargs), in order."""
    writes: list[tuple[Any, dict[str, Any], dict[str, Any]]] = []

    async def _create(*, kind: Any, data: dict[str, Any]) -> Any:
        node = MagicMock(id=data.get("id", "new"))
        node.save = AsyncMock(side_effect=lambda **kwargs: writes.append((kind, data, kwargs)))
        return node

    gen.client.create = AsyncMock(side_effect=_create)
    return writes


class TestGenerateDeploymentUpdate:
    @pytest.mark.asyncio
    async def test_deployment_updated_when_mismatched(self) -> None:
        gen = _make_generator()
        writes = _record_writes(gen)
        gen.client.filters = AsyncMock(side_effect=[[]])

        await gen.generate(_endpoint_data(deployment="some-other-pod"))

        # Untracked: the data-loaded server is this run's target, not its output; only the deployment is sent.
        assert writes == [
            (
                DcimPhysicalDevice,
                {"id": "ep-1", "deployment": {"id": "pod-1"}},
                {"allow_upsert": True, "update_group_context": False},
            )
        ]

    @pytest.mark.asyncio
    async def test_deployment_updated_when_unset(self) -> None:
        gen = _make_generator()
        writes = _record_writes(gen)
        gen.client.filters = AsyncMock(side_effect=[[]])

        await gen.generate(_endpoint_data(deployment=None))

        assert [(data, kwargs["update_group_context"]) for _, data, kwargs in writes] == [
            ({"id": "ep-1", "deployment": {"id": "pod-1"}}, False)
        ]

    @pytest.mark.asyncio
    async def test_deployment_already_correct_needs_neither_read_nor_write(self) -> None:
        """The server is never tracked, so a deployment that already matches."""
        gen = _make_generator()
        gen.client.filters = AsyncMock(side_effect=[[]])

        await gen.generate(_endpoint_data())

        gen.client.get.assert_not_awaited()
