"""Unit tests for generators.topology.loadbalancer.LoadbalancerBackendNexthopGenerator."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from generators.topology.loadbalancer import LoadbalancerBackendNexthopGenerator, _dev_id, _dev_name


def _gen() -> Any:
    gen = LoadbalancerBackendNexthopGenerator.__new__(LoadbalancerBackendNexthopGenerator)
    gen.client = AsyncMock()
    gen.logger = MagicMock()
    return gen


def _vlan_segment(
    *, seg_id: str = "seg-1", vlan_id: int | None = 100, gateway_prefix_id: str | None = "prefix-1"
) -> dict:
    seg: dict = {"id": seg_id, "vlan_id": vlan_id}
    if gateway_prefix_id:
        seg["gateway"] = {"ip_prefix": {"id": gateway_prefix_id}}
    return seg


def _vip(
    *,
    vip_id: str = "vip-1",
    hostname: str = "app.example.com",
    snat_enabled: bool = False,
    backend_segment: dict | None = None,
    devices: list[dict] | None = None,
) -> dict:
    return {
        "id": vip_id,
        "hostname": hostname,
        "snat_enabled": snat_enabled,
        "backend_segment": backend_segment if backend_segment is not None else _vlan_segment(),
        "load_balancer": {"id": "lbha-1", "capabilities": devices or [{"id": "lb-01", "name": "lb-01"}]},
    }


class TestHelpers:
    def test_dev_id_from_dict(self) -> None:
        assert _dev_id({"id": "d1", "name": "x"}) == "d1"

    def test_dev_name_from_dict(self) -> None:
        assert _dev_name({"id": "d1", "name": "x"}) == "x"


class TestGenerateNoOps:
    def test_no_vip_data_logs_info_and_returns(self) -> None:
        gen = _gen()
        asyncio.run(gen.generate({}))
        gen.client.get.assert_not_called()

    def test_missing_id_logs_error(self) -> None:
        gen = _gen()
        data = {"LoadbalancerVIP": [{"hostname": "x"}]}
        asyncio.run(gen.generate(data))
        gen.logger.error.assert_called()

    def test_snat_enabled_is_noop(self) -> None:
        gen = _gen()
        data = {"LoadbalancerVIP": [_vip(snat_enabled=True)]}
        asyncio.run(gen.generate(data))
        gen.client.get.assert_not_called()
        gen.client.filters.assert_not_called()

    def test_default_snat_enabled_true_is_noop(self) -> None:
        """snat_enabled missing from the query response defaults to True (safe default)."""
        gen = _gen()
        vip = _vip(snat_enabled=True)
        del vip["snat_enabled"]
        data = {"LoadbalancerVIP": [vip]}
        asyncio.run(gen.generate(data))
        gen.client.get.assert_not_called()

    def test_no_backend_segment_is_noop(self) -> None:
        gen = _gen()
        data = {"LoadbalancerVIP": [_vip(backend_segment={})]}
        asyncio.run(gen.generate(data))
        gen.client.get.assert_not_called()

    def test_no_lb_devices_logs_error(self) -> None:
        gen = _gen()
        data = {"LoadbalancerVIP": [_vip(devices=[])]}
        asyncio.run(gen.generate(data))
        gen.logger.error.assert_called()

    def test_no_vlan_id_logs_error(self) -> None:
        gen = _gen()
        seg = _vlan_segment(vlan_id=None)
        data = {"LoadbalancerVIP": [_vip(backend_segment=seg)]}
        asyncio.run(gen.generate(data))
        gen.logger.error.assert_called()


class TestGenerateHappyPath:
    def test_wires_subinterface_on_each_device(self) -> None:
        gen = _gen()
        devices = [{"id": "lb-01", "name": "lb-01"}, {"id": "lb-02", "name": "lb-02"}]
        data = {"LoadbalancerVIP": [_vip(devices=devices)]}

        gen.client.filters = AsyncMock(return_value=[])
        pool_obj = MagicMock(id="pool-1")
        gen.client.create = AsyncMock(return_value=pool_obj)
        pool_obj.save = AsyncMock()

        vip_obj = MagicMock(id="vip-1")
        gen.client.get = AsyncMock(return_value=vip_obj)

        ip_obj = MagicMock(id="ip-1")
        gen.client.allocate_next_ip_address = AsyncMock(return_value=ip_obj)

        trunk_iface = MagicMock()
        gen.find_role_interface = AsyncMock(return_value=trunk_iface)
        gen.ensure_vlan_subinterface = AsyncMock(return_value=MagicMock())

        asyncio.run(gen.generate(data))

        assert gen.ensure_vlan_subinterface.await_count == 2
        for call in gen.ensure_vlan_subinterface.await_args_list:
            assert call.kwargs["vlan_id_value"] == 100
            assert call.kwargs["capability_obj"] is vip_obj

    def test_no_gateway_prefix_skips_pool_but_still_wires(self) -> None:
        """A backend_segment with no gateway (L2-only) still gets a
        sub-interface, just without an IP address."""
        gen = _gen()
        seg = _vlan_segment(gateway_prefix_id=None)
        data = {"LoadbalancerVIP": [_vip(backend_segment=seg)]}

        trunk_iface = MagicMock()
        gen.find_role_interface = AsyncMock(return_value=trunk_iface)
        gen.ensure_vlan_subinterface = AsyncMock(return_value=MagicMock())
        gen.client.get = AsyncMock(return_value=MagicMock(id="vip-1"))

        asyncio.run(gen.generate(data))

        gen.client.create.assert_not_called()
        gen.ensure_vlan_subinterface.assert_awaited_once()
        assert gen.ensure_vlan_subinterface.call_args.kwargs["ip_address_id"] is None

    def test_no_trunk_interface_logs_error_and_continues(self) -> None:
        gen = _gen()
        data = {"LoadbalancerVIP": [_vip(backend_segment=_vlan_segment(gateway_prefix_id=None))]}
        gen.find_role_interface = AsyncMock(return_value=None)
        gen.ensure_vlan_subinterface = AsyncMock()
        gen.client.get = AsyncMock(return_value=MagicMock(id="vip-1"))

        asyncio.run(gen.generate(data))

        gen.logger.error.assert_called()
        gen.ensure_vlan_subinterface.assert_not_called()

    def test_vxlan_backend_segment_logs_error_and_skips(self) -> None:
        """ManagedVxlanSegment has no plain vlan_id — its LOCAL VLAN ID is
        per VLAN domain (ManagedVlanDomainSegment), not resolvable here (no
        device to resolve a domain against) — a known, logged gap, not a
        crash."""
        gen = _gen()
        seg = {
            "id": "seg-2",
            "segment_deployments": [{"vni": 10200}],
        }
        data = {"LoadbalancerVIP": [_vip(backend_segment=seg)]}

        gen.find_role_interface = AsyncMock()
        gen.ensure_vlan_subinterface = AsyncMock()
        gen.client.get = AsyncMock(return_value=MagicMock(id="vip-1"))

        asyncio.run(gen.generate(data))

        gen.logger.error.assert_called()
        gen.ensure_vlan_subinterface.assert_not_called()


class TestEnsureBackendPool:
    def test_existing_pool_reused(self) -> None:
        gen = _gen()
        existing_pool = MagicMock()
        gen.client.filters = AsyncMock(return_value=[existing_pool])

        result = asyncio.run(gen._ensure_backend_pool(vip_id="vip-1", vip_hostname="x", segment_prefix_id="prefix-1"))

        assert result is existing_pool
        gen.client.create.assert_not_called()

    def test_new_pool_created_wrapping_existing_prefix(self) -> None:
        gen = _gen()
        gen.client.filters = AsyncMock(return_value=[])
        new_pool = MagicMock()
        new_pool.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=new_pool)

        result = asyncio.run(gen._ensure_backend_pool(vip_id="vip-1", vip_hostname="x", segment_prefix_id="prefix-1"))

        assert result is new_pool
        create_kwargs = gen.client.create.call_args.kwargs
        assert create_kwargs["data"]["resources"] == ["prefix-1"]
        new_pool.save.assert_awaited_once()

    def test_pool_creation_failure_returns_none(self) -> None:
        gen = _gen()
        gen.client.filters = AsyncMock(return_value=[])
        gen.client.create = AsyncMock(side_effect=RuntimeError("boom"))

        result = asyncio.run(gen._ensure_backend_pool(vip_id="vip-1", vip_hostname="x", segment_prefix_id="prefix-1"))

        assert result is None
        gen.logger.error.assert_called()
