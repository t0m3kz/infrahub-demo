"""Unit tests for generators.topology.loadbalancer.LoadbalancerBackendNexthopGenerator."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from generators.topology.loadbalancer import LoadbalancerBackendNexthopGenerator


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
        # `devices is None` (the default) means "give me one device"; an explicitly
        # passed [] must stay empty, or the no-devices branch is never exercised.
        "load_balancer": {
            "id": "lbha-1",
            "capabilities": [{"id": "lb-01", "name": "lb-01"}] if devices is None else devices,
        },
    }


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
        # Bail out at the devices check, before any write: an HA domain with no
        # members has nothing to wire, and creating the backend pool anyway would
        # leave an orphan pool behind on every run.
        gen.client.create.assert_not_called()

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

    def test_shared_subinterface_is_written_untracked(self) -> None:
        """<downlink>.<vlan> is shared by every no-SNAT VIP on the segment, so no VIP's run claims it."""
        gen = _gen()
        data = {"LoadbalancerVIP": [_vip(devices=[{"id": "lb-01", "name": "lb-01"}])]}
        pool_obj = MagicMock(id="pool-1")
        pool_obj.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=pool_obj)
        gen.client.get = AsyncMock(return_value=MagicMock(id="vip-1"))
        gen.client.allocate_next_ip_address = AsyncMock(return_value=MagicMock(id="ip-1"))
        gen.find_role_interface = AsyncMock(return_value=MagicMock())
        gen.ensure_vlan_subinterface = AsyncMock(return_value=MagicMock())

        asyncio.run(gen.generate(data))

        assert gen.ensure_vlan_subinterface.call_args.kwargs["track"] is False
        assert gen.ensure_vlan_subinterface.call_args.kwargs["ip_address_id"] == "ip-1"

    def test_backend_ip_identifier_is_per_vip_prefix_and_device(self) -> None:
        """The /32 reservation is the VIP's own and follows a moved backend prefix."""
        gen = _gen()
        pool_obj = MagicMock(id="pool-1")
        gen.client.allocate_next_ip_address = AsyncMock(return_value=MagicMock(id="ip-1"))

        result = asyncio.run(
            gen._allocate_backend_ip(pool=pool_obj, vip_id="vip-1", segment_prefix_id="prefix-1", device_name="lb-01")
        )

        assert result == "ip-1"
        kwargs = gen.client.allocate_next_ip_address.call_args.kwargs
        assert kwargs["identifier"] == "vip-1-prefix-1-lb-01-lb-backend"
        assert kwargs["resource_pool"] is pool_obj

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
    """One pool per VIP, owned and tracked by that VIP's run."""

    def test_pool_is_named_from_the_vip_and_upserted_tracked(self) -> None:
        """The name comes from the VIP's id, not the shared segment prefix, and
        every run upserts it (tracked): it belongs to this VIP alone."""
        gen = _gen()
        pool = MagicMock()
        pool.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=pool)

        result = asyncio.run(gen._ensure_backend_pool(vip_id="vip-1", vip_hostname="x", segment_prefix_id="prefix-1"))

        assert result is pool
        data = gen.client.create.call_args.kwargs["data"]
        assert data["name"] == "lb-backend-vip-1-pool"
        assert data["identifier"] == "lb-backend-vip-1-pool"
        assert data["resources"] == ["prefix-1"]
        pool.save.assert_awaited_once_with(allow_upsert=True)
        gen.client.filters.assert_not_called()

    def test_two_vips_on_one_segment_get_distinct_pools(self) -> None:
        """No-SNAT VIPs sharing a backend prefix no longer share (and delete) one pool."""
        gen = _gen()
        gen.client.create = AsyncMock(side_effect=lambda **_: MagicMock(save=AsyncMock()))

        for vip_id in ("vip-1", "vip-2"):
            asyncio.run(gen._ensure_backend_pool(vip_id=vip_id, vip_hostname=vip_id, segment_prefix_id="prefix-1"))

        names = [call.kwargs["data"]["name"] for call in gen.client.create.call_args_list]
        assert names == ["lb-backend-vip-1-pool", "lb-backend-vip-2-pool"]

    def test_moved_backend_prefix_repoints_the_same_pool(self) -> None:
        """A VIP whose backend_segment moved upserts the same pool over the new prefix."""
        gen = _gen()
        gen.client.create = AsyncMock(return_value=MagicMock(save=AsyncMock()))

        asyncio.run(gen._ensure_backend_pool(vip_id="vip-1", vip_hostname="x", segment_prefix_id="prefix-2"))

        data = gen.client.create.call_args.kwargs["data"]
        assert (data["name"], data["resources"]) == ("lb-backend-vip-1-pool", ["prefix-2"])

    def test_pool_creation_failure_returns_none(self) -> None:
        gen = _gen()
        gen.client.create = AsyncMock(side_effect=RuntimeError("boom"))

        result = asyncio.run(gen._ensure_backend_pool(vip_id="vip-1", vip_hostname="x", segment_prefix_id="prefix-1"))

        assert result is None
        gen.logger.error.assert_called()


class TestHostClassComposition:
    """CablingMixin documents ``deployment_id``, ``_resolve_pool`` and
    ``_retry_delay`` as host-class requirements that CommonGenerator supplies.
    This generator only calls ``find_role_interface`` and
    ``ensure_vlan_subinterface``, so inheriting InfrahubGenerator directly
    happened to work — but any later call into ``create_cabling`` would have
    raised AttributeError at runtime, in a generator, against live data.
    """

    def test_composes_common_generator(self) -> None:
        from generators.common import CommonGenerator

        assert issubclass(LoadbalancerBackendNexthopGenerator, CommonGenerator)

    def test_composes_the_cabling_mixin(self) -> None:
        from generators.connections import CablingMixin

        assert issubclass(LoadbalancerBackendNexthopGenerator, CablingMixin)

    def test_cabling_mixin_host_requirements_are_satisfied(self) -> None:
        for attribute in ("_resolve_pool", "_retry_delay"):
            assert hasattr(LoadbalancerBackendNexthopGenerator, attribute), attribute
