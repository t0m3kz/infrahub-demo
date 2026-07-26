"""Unit tests for base AppServicePort generator helpers."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from generators.protocols import AppServicePort
from generators.topology.application_service_ports import BaseAppServicePortGenerator


def _make_gen() -> Any:
    gen = BaseAppServicePortGenerator.__new__(BaseAppServicePortGenerator)
    gen.client = AsyncMock()
    gen.logger = MagicMock()
    return gen


class TestBaseAppServicePortGenerator:
    def test_port_range_str(self) -> None:
        assert BaseAppServicePortGenerator._port_range_str(443, None) == "443"
        assert BaseAppServicePortGenerator._port_range_str(8000, 8080) == "8000-8080"

    def test_get_component_with_ports_returns_state(self) -> None:
        gen = _make_gen()

        rel = MagicMock()
        rel.fetch = AsyncMock()
        rel.peers = [MagicMock(id="p-1"), MagicMock(id="p-2")]

        component_obj = MagicMock()
        component_obj.service_ports = rel
        gen.client.get = AsyncMock(return_value=component_obj)

        state = asyncio.run(gen._get_component_with_ports("comp-1"))

        assert state is not None
        obj, relation, existing_ids = state
        assert obj is component_obj
        assert relation is rel
        assert existing_ids == {"p-1", "p-2"}

    def test_get_component_with_ports_logs_when_component_missing(self) -> None:
        gen = _make_gen()
        gen.client.get = AsyncMock(return_value=None)

        state = asyncio.run(gen._get_component_with_ports("comp-404"))

        assert state is None
        gen.logger.error.assert_called_once()

    def test_upsert_service_port_object_saves_with_port_end(self) -> None:
        gen = _make_gen()

        port_obj = MagicMock()
        port_obj.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=port_obj)

        result = asyncio.run(gen._upsert_service_port_object(port=8080, port_end=8090, protocol="tcp"))

        assert result is port_obj
        gen.client.create.assert_called_once_with(
            kind=AppServicePort,
            data={"port": 8080, "protocol": "tcp", "port_end": 8090},
        )
        port_obj.save.assert_called_once_with(allow_upsert=True)
