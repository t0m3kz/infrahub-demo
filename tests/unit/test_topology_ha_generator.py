"""Unit tests for generators.topology.ha."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from generators.protocols import DcimCable, ManagedGeneric
from generators.topology.ha import HAGenerator, _device_kind, _iface_kind, _is_sync_iface


def _gen() -> Any:
    gen = HAGenerator.__new__(HAGenerator)
    gen.client = AsyncMock()
    gen.logger = MagicMock()
    return gen


def _device(name: str, ifaces: list[dict[str, Any]], *, deployment: dict[str, Any] | None = None) -> dict[str, Any]:
    data: dict[str, Any] = {
        "name": name,
        "interfaces": ifaces,
    }
    if deployment is not None:
        data["deployment"] = deployment
    return data


class TestHAGeneratorHelpers:
    def test_is_sync_iface_true_for_ha_role(self) -> None:
        assert _is_sync_iface({"role": "ha"}) is True

    def test_is_sync_iface_false_for_other_role(self) -> None:
        assert _is_sync_iface({"role": "uplink"}) is False

    def test_iface_kind_defaults_to_physical(self) -> None:
        assert _iface_kind("") == "DcimPhysicalInterface"

    def test_device_kind_defaults_to_physical(self) -> None:
        assert _device_kind("") == "DcimPhysicalDevice"


class TestHAGeneratorGenerate:
    def test_generate_logs_error_when_no_domains(self) -> None:
        gen = _gen()
        gen._process_ha_domain = AsyncMock()

        asyncio.run(gen.generate({}))

        gen.logger.error.assert_called_once()
        gen._process_ha_domain.assert_not_called()

    def test_generate_processes_fw_and_lb_domains(self) -> None:
        gen = _gen()
        gen._process_ha_domain = AsyncMock()

        data = {
            "ManagedFirewallHA": [{"id": "ha-fw", "name": "FW-HA", "capabilities": []}],
            "ManagedLoadbalancerHA": [{"id": "ha-lb", "name": "LB-HA", "capabilities": []}],
        }

        asyncio.run(gen.generate(data))

        assert gen._process_ha_domain.await_count == 2


class TestEnsureHACables:
    def test_skips_when_not_exactly_two_devices(self) -> None:
        gen = _gen()

        asyncio.run(gen._ensure_ha_cables("HA-1", [{"name": "a", "interfaces": []}]))

        gen.client.filters.assert_not_called()

    def test_skips_when_no_ha_interfaces(self) -> None:
        gen = _gen()
        devices = [
            _device("fw-1", [{"id": "i1", "name": "eth1", "role": "uplink"}]),
            _device("fw-2", [{"id": "i2", "name": "eth1", "role": "uplink"}]),
        ]

        asyncio.run(gen._ensure_ha_cables("HA-1", devices))

        gen.client.filters.assert_not_called()

    def test_existing_named_cable_is_reused(self) -> None:
        gen = _gen()
        existing = MagicMock()
        existing.save = AsyncMock()
        gen.client.filters = AsyncMock(return_value=[existing])

        devices = [
            _device("fw-1", [{"id": "i1", "name": "sync0", "role": "ha"}]),
            _device("fw-2", [{"id": "i2", "name": "sync0", "role": "ha"}]),
        ]

        asyncio.run(gen._ensure_ha_cables("HA-1", devices))

        existing.save.assert_awaited_once()
        assert existing.save.call_args.kwargs["allow_upsert"] is True

    def test_orphan_cable_is_adopted_and_renamed(self) -> None:
        gen = _gen()
        gen.client.filters = AsyncMock(return_value=[])

        orphan = MagicMock()
        orphan.name = SimpleNamespace(value="OLD-NAME")
        orphan.save = AsyncMock()
        gen.client.get = AsyncMock(return_value=orphan)

        devices = [
            _device(
                "fw-1",
                [{"id": "i1", "name": "sync0", "role": "ha", "cable": {"id": "cbl-1"}}],
            ),
            _device("fw-2", [{"id": "i2", "name": "sync0", "role": "ha"}]),
        ]

        asyncio.run(gen._ensure_ha_cables("HA-1", devices))

        assert orphan.name.value == "CBL-HA-1-SYNC"
        orphan.save.assert_awaited_once()

    def test_creates_new_cable_with_deployment(self) -> None:
        gen = _gen()
        gen.client.filters = AsyncMock(return_value=[])

        created = MagicMock()
        created.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=created)

        devices = [
            _device(
                "fw-1",
                [{"id": "i1", "name": "sync0", "role": "ha"}],
                deployment={"parent": {"id": "dep-parent"}, "id": "dep-child"},
            ),
            _device("fw-2", [{"id": "i2", "name": "sync0", "role": "ha"}]),
        ]

        asyncio.run(gen._ensure_ha_cables("HA-1", devices))

        call_kwargs = gen.client.create.call_args.kwargs
        assert call_kwargs["kind"] == DcimCable
        assert call_kwargs["data"]["name"] == "CBL-HA-1-SYNC"
        assert call_kwargs["data"]["endpoints"] == ["i1", "i2"]
        assert call_kwargs["data"]["deployment"] == {"id": "dep-parent"}
        created.save.assert_awaited_once()


class TestProcessHADomain:
    def test_process_empty_domain_calls_ensure_cables(self) -> None:
        gen = _gen()

        ha_obj = MagicMock()
        gen.client.get = AsyncMock(return_value=ha_obj)
        gen.client.filters = AsyncMock(return_value=[])
        gen._ensure_ha_cables = AsyncMock()

        ha = {"id": "ha-1", "name": "HA-1", "capabilities": []}
        asyncio.run(gen._process_ha_domain(ha))

        gen.client.get.assert_awaited_once_with(kind=ManagedGeneric, id="ha-1")
        gen._ensure_ha_cables.assert_awaited_once_with("HA-1", [])

    def test_device_load_error_is_tolerated(self) -> None:
        gen = _gen()

        ha_obj = MagicMock()
        gen.client.get = AsyncMock(side_effect=[ha_obj, Exception("boom")])
        gen.client.filters = AsyncMock(return_value=[])
        gen._ensure_ha_cables = AsyncMock()

        ha = {
            "id": "ha-1",
            "name": "HA-1",
            "capabilities": [
                {
                    "id": "dev-1",
                    "name": "fw-1",
                    "typename": "DcimPhysicalDevice",
                    "interfaces": [],
                }
            ],
        }

        asyncio.run(gen._process_ha_domain(ha))

        assert gen.logger.warning.called
        gen._ensure_ha_cables.assert_awaited_once_with("HA-1", ha["capabilities"])
