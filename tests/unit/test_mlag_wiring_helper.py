"""Unit tests for MLAGWiringMixin (generators/mlag.py) — shared by
DeviceMixin._ensure_mlag_pairs and MLAGGenerator. Mirrors
test_device_mixin.py's TestEnsureHaInterfaces/TestEnsureHaCable style: a
bare mixin instance with mocked client/logger, asserting on
.filters/.create/.save/.delete call args.

Every peer-link interface/cable is always create()+save()'d with the full
desired state (existing id passed when found) — mirrors create_devices()'s
own device/loopback pattern — so there's no "already wired, skip" branch
left to test; these tests instead assert the upsert-with-id-if-found shape."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from infrahub_sdk.protocols import CoreIPPrefixPool

from generators.mlag import MLAGWiringMixin
from generators.protocols import (
    DcimCable,
    DcimLAGInterface,
    DcimPhysicalDevice,
    DcimPhysicalInterface,
    DcimVirtualInterface,
)


def _gen() -> Any:
    gen = MLAGWiringMixin.__new__(MLAGWiringMixin)
    gen.client = MagicMock()
    gen.logger = MagicMock()
    return gen


def _mock_relmgr(peer_ids: list[str]) -> MagicMock:
    rel = MagicMock()
    rel.fetch = AsyncMock()
    rel.peers = [MagicMock(id=pid) for pid in peer_ids]
    return rel


def _mock_iface(iface_id: str, name: str, *, cable_id: str | None = None, status: str = "active") -> MagicMock:
    iface = MagicMock()
    iface.id = iface_id
    iface.name = MagicMock(value=name)
    iface.status = MagicMock(value=status)
    cable = MagicMock()
    cable.initialized = cable_id is not None
    if cable_id is not None:
        cable.id = cable_id
    iface.cable = cable
    iface.save = AsyncMock()
    return iface


def _mock_mlag_device(name: str, *, platform: str = "nxos") -> MagicMock:
    dev = MagicMock()
    dev.id = f"id-{name}"
    dev.name = MagicMock(value=name)
    dev.deployment = MagicMock(initialized=False)
    platform_rel = MagicMock(initialized=True, fetch=AsyncMock())
    platform_rel.peer = MagicMock()
    platform_rel.peer.name = MagicMock(value=platform)
    dev.platform = platform_rel
    return dev


def _mock_mlag_obj(*, virtual_peer_link: bool = False) -> MagicMock:
    mlag_obj = MagicMock()
    mlag_obj.id = "mlag-1"
    mlag_obj.virtual_peer_link = MagicMock(value=virtual_peer_link)
    mlag_obj.save = AsyncMock()
    return mlag_obj


class TestEnsureMlagWiring:
    @pytest.mark.asyncio
    async def test_member_ids_param_skips_capabilities_fetch(self) -> None:
        gen = _gen()
        mlag_obj = _mock_mlag_obj()
        mlag_obj.capabilities = _mock_relmgr([])
        mlag_obj.capabilities.fetch = AsyncMock(side_effect=AssertionError("capabilities.fetch() must not be called"))
        dev_1, dev_2 = _mock_mlag_device("tor-01"), _mock_mlag_device("tor-02")

        async def _filters(*, kind: Any, **kwargs: Any) -> list[Any]:
            if kind is DcimPhysicalDevice and "ids" in kwargs:
                return [dev_1, dev_2]
            return []

        gen.client.filters = AsyncMock(side_effect=_filters)

        await gen.ensure_mlag_wiring(mlag_obj, "tor-01-tor-02-mlag", member_ids=["id-tor-01", "id-tor-02"])

        mlag_obj.capabilities.fetch.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_wrong_member_count_logs_error_and_noops(self) -> None:
        gen = _gen()
        mlag_obj = _mock_mlag_obj()
        gen.client.filters = AsyncMock(return_value=[])

        await gen.ensure_mlag_wiring(mlag_obj, "tor-01-tor-02-mlag", member_ids=["id-tor-01"])

        gen.logger.error.assert_called_once()
        gen.client.filters.assert_not_called()

    @pytest.mark.asyncio
    async def test_back_to_back_wires_lag_and_cable(self) -> None:
        gen = _gen()
        mlag_obj = _mock_mlag_obj(virtual_peer_link=False)
        dev_1, dev_2 = _mock_mlag_device("tor-01"), _mock_mlag_device("tor-02")
        mlag_peer_1 = _mock_iface("iface-1", "Ethernet1/1")
        mlag_peer_2 = _mock_iface("iface-2", "Ethernet1/1")

        async def _filters(*, kind: Any, **kwargs: Any) -> list[Any]:
            if kind is DcimPhysicalDevice and "ids" in kwargs:
                return [dev_1, dev_2]
            if kind is DcimPhysicalInterface and kwargs.get("device__ids") == [dev_1.id]:
                return [mlag_peer_1]
            if kind is DcimPhysicalInterface and kwargs.get("device__ids") == [dev_2.id]:
                return [mlag_peer_2]
            return []  # no stale peer-links, no existing LAG, no existing cable

        gen.client.filters = AsyncMock(side_effect=_filters)
        lag_1 = MagicMock(id="lag-1", save=AsyncMock())
        lag_2 = MagicMock(id="lag-2", save=AsyncMock())
        cable_obj = MagicMock(save=AsyncMock())
        gen.client.create = AsyncMock(side_effect=[lag_1, lag_2, cable_obj])

        await gen.ensure_mlag_wiring(mlag_obj, "tor-01-tor-02-mlag", member_ids=["id-tor-01", "id-tor-02"])

        lag_calls = [c for c in gen.client.create.call_args_list if c.kwargs["kind"] is DcimLAGInterface]
        assert len(lag_calls) == 2
        cable_calls = [c for c in gen.client.create.call_args_list if c.kwargs["kind"] is DcimCable]
        assert len(cable_calls) == 1

    @pytest.mark.asyncio
    async def test_virtual_wires_loopback_and_skips_cable(self) -> None:
        gen = _gen()
        mlag_obj = _mock_mlag_obj(virtual_peer_link=True)
        dev_1, dev_2 = _mock_mlag_device("leaf-01"), _mock_mlag_device("leaf-02")

        async def _filters(*, kind: Any, **kwargs: Any) -> list[Any]:
            if kind is DcimPhysicalDevice and "ids" in kwargs:
                return [dev_1, dev_2]
            return []

        gen.client.filters = AsyncMock(side_effect=_filters)
        virt_1 = MagicMock(id="virt-1", save=AsyncMock())
        virt_2 = MagicMock(id="virt-2", save=AsyncMock())
        gen.client.create = AsyncMock(side_effect=[virt_1, virt_2])

        await gen.ensure_mlag_wiring(mlag_obj, "leaf-01-leaf-02-mlag", member_ids=["id-leaf-01", "id-leaf-02"])

        virt_calls = [c for c in gen.client.create.call_args_list if c.kwargs["kind"] is DcimVirtualInterface]
        assert len(virt_calls) == 2
        cable_calls = [c for c in gen.client.create.call_args_list if c.kwargs["kind"] is DcimCable]
        assert len(cable_calls) == 0


class TestDisconnectStalePeerLink:
    @pytest.mark.asyncio
    async def test_removes_stale_virtual_interface_when_switching_to_back_to_back(self) -> None:
        gen = _gen()
        device_obj = _mock_mlag_device("leaf-01")
        stale = _mock_iface("stale-virt", "Loopback100")
        gen.client.filters = AsyncMock(return_value=[stale])
        gen.client.delete = AsyncMock()

        await gen._disconnect_stale_peer_link(device_obj, "leaf-01-leaf-02-mlag", virtual_peer_link=False)

        gen.client.delete.assert_awaited_once_with(kind=DcimVirtualInterface, id="stale-virt")

    @pytest.mark.asyncio
    async def test_removes_stale_lag_when_switching_to_virtual(self) -> None:
        gen = _gen()
        device_obj = _mock_mlag_device("leaf-01")
        stale = _mock_iface("stale-lag", "Port-Channel100")
        gen.client.filters = AsyncMock(return_value=[stale])
        gen.client.delete = AsyncMock()

        await gen._disconnect_stale_peer_link(device_obj, "leaf-01-leaf-02-mlag", virtual_peer_link=True)

        gen.client.delete.assert_awaited_once_with(kind=DcimLAGInterface, id="stale-lag")

    @pytest.mark.asyncio
    async def test_noop_when_nothing_stale(self) -> None:
        gen = _gen()
        device_obj = _mock_mlag_device("leaf-01")
        gen.client.filters = AsyncMock(return_value=[])
        gen.client.delete = AsyncMock()

        await gen._disconnect_stale_peer_link(device_obj, "leaf-01-leaf-02-mlag", virtual_peer_link=False)

        gen.client.delete.assert_not_awaited()


class TestEnsureLagPeerLink:
    @pytest.mark.asyncio
    async def test_no_mlag_peer_interfaces_logs_error_returns_none(self) -> None:
        gen = _gen()
        device_obj = _mock_mlag_device("tor-01")
        mlag_obj = _mock_mlag_obj()
        gen.client.filters = AsyncMock(return_value=[])

        result = await gen._ensure_lag_peer_link(device_obj, mlag_obj, "nxos")

        assert result is None
        gen.logger.error.assert_called_once()

    @pytest.mark.asyncio
    async def test_creates_new_lag_with_full_state(self) -> None:
        gen = _gen()
        device_obj = _mock_mlag_device("tor-01")
        mlag_obj = _mock_mlag_obj()
        iface_1 = _mock_iface("iface-1", "Ethernet1/1")
        iface_2 = _mock_iface("iface-2", "Ethernet1/2")

        async def _filters(*, kind: Any, **kwargs: Any) -> list[Any]:
            if kind is DcimPhysicalInterface:
                return [iface_1, iface_2]
            if kind is DcimLAGInterface:
                return []  # no existing LAG
            return []

        gen.client.filters = AsyncMock(side_effect=_filters)
        lag_obj = MagicMock(id="lag-1", save=AsyncMock())
        gen.client.create = AsyncMock(return_value=lag_obj)

        result = await gen._ensure_lag_peer_link(device_obj, mlag_obj, "nxos")

        assert result is lag_obj
        create_kwargs = gen.client.create.call_args.kwargs
        assert "id" not in create_kwargs["data"]
        assert create_kwargs["data"]["member_interfaces"] == [{"id": "iface-1"}, {"id": "iface-2"}]
        assert create_kwargs["data"]["mlag_domain"] == {"id": "mlag-1"}
        assert create_kwargs["data"]["interface_capabilities"] == [{"id": "mlag-1"}]
        lag_obj.save.assert_awaited_once_with(allow_upsert=True)

    @pytest.mark.asyncio
    async def test_existing_lag_gets_upserted_by_id_with_current_domain(self) -> None:
        """Always create()+save()'d with the full desired state, existing id
        passed when found — no "already correct, skip" branch — so a
        previously-wired LAG whose domain changed (or nothing changed at
        all) both converge through the same single upsert call."""
        gen = _gen()
        device_obj = _mock_mlag_device("tor-01")
        mlag_obj = _mock_mlag_obj()
        mlag_obj.id = "mlag-new"
        iface_1 = _mock_iface("iface-1", "Ethernet1/1")
        existing_lag = MagicMock(id="lag-1")

        async def _filters(*, kind: Any, **kwargs: Any) -> list[Any]:
            if kind is DcimPhysicalInterface:
                return [iface_1]
            if kind is DcimLAGInterface:
                return [existing_lag]
            return []

        gen.client.filters = AsyncMock(side_effect=_filters)
        lag_obj = MagicMock(id="lag-1", save=AsyncMock())
        gen.client.create = AsyncMock(return_value=lag_obj)

        result = await gen._ensure_lag_peer_link(device_obj, mlag_obj, "nxos")

        assert result is lag_obj
        create_kwargs = gen.client.create.call_args.kwargs
        assert create_kwargs["data"]["id"] == "lag-1"
        assert create_kwargs["data"]["mlag_domain"] == {"id": "mlag-new"}
        assert create_kwargs["data"]["interface_capabilities"] == [{"id": "mlag-new"}]
        lag_obj.save.assert_awaited_once_with(allow_upsert=True)


class TestEnsureVirtualPeerLink:
    @pytest.mark.asyncio
    async def test_creates_new_loopback_with_full_state(self) -> None:
        gen = _gen()
        device_obj = _mock_mlag_device("leaf-01")
        mlag_obj = _mock_mlag_obj()
        gen.client.filters = AsyncMock(return_value=[])
        virt_obj = MagicMock(id="virt-1", save=AsyncMock())
        gen.client.create = AsyncMock(return_value=virt_obj)

        result = await gen._ensure_virtual_peer_link(device_obj, mlag_obj, "leaf-01-leaf-02-mlag", "nxos")

        assert result is virt_obj
        create_kwargs = gen.client.create.call_args.kwargs
        assert "id" not in create_kwargs["data"]
        assert create_kwargs["data"]["role"] == "mlag-peer"
        assert create_kwargs["data"]["device"] == {"id": device_obj.id}
        assert create_kwargs["data"]["interface_capabilities"] == [{"id": "mlag-1"}]
        virt_obj.save.assert_awaited_once_with(allow_upsert=True)

    @pytest.mark.asyncio
    async def test_existing_loopback_gets_upserted_by_id(self) -> None:
        gen = _gen()
        device_obj = _mock_mlag_device("leaf-01")
        mlag_obj = _mock_mlag_obj()
        existing = MagicMock(id="virt-1")
        gen.client.filters = AsyncMock(return_value=[existing])
        virt_obj = MagicMock(id="virt-1", save=AsyncMock())
        gen.client.create = AsyncMock(return_value=virt_obj)

        result = await gen._ensure_virtual_peer_link(device_obj, mlag_obj, "leaf-01-leaf-02-mlag", "nxos")

        assert result is virt_obj
        create_kwargs = gen.client.create.call_args.kwargs
        assert create_kwargs["data"]["id"] == "virt-1"
        virt_obj.save.assert_awaited_once_with(allow_upsert=True)


class TestEnsurePeerLinkCables:
    @pytest.mark.asyncio
    async def test_pairs_multiple_mlag_peer_interfaces_index_wise(self) -> None:
        gen = _gen()
        dev_a, dev_b = _mock_mlag_device("tor-01"), _mock_mlag_device("tor-02")
        iface_a1, iface_a2 = _mock_iface("a1", "Ethernet1/1"), _mock_iface("a2", "Ethernet1/2")
        iface_b1, iface_b2 = _mock_iface("b1", "Ethernet1/1"), _mock_iface("b2", "Ethernet1/2")

        async def _filters(*, kind: Any, **kwargs: Any) -> list[Any]:
            if kind is DcimPhysicalInterface and kwargs.get("device__ids") == [dev_a.id]:
                return [iface_a2, iface_a1]  # unsorted on purpose
            if kind is DcimPhysicalInterface and kwargs.get("device__ids") == [dev_b.id]:
                return [iface_b2, iface_b1]
            if kind is DcimCable:
                return []
            return []

        gen.client.filters = AsyncMock(side_effect=_filters)
        cable_1 = MagicMock(save=AsyncMock())
        cable_2 = MagicMock(save=AsyncMock())
        gen.client.create = AsyncMock(side_effect=[cable_1, cable_2])

        await gen._ensure_peer_link_cables("tor-01-tor-02-mlag", dev_a, dev_b)

        assert gen.client.create.await_count == 2
        first_call = gen.client.create.call_args_list[0].kwargs
        assert "id" not in first_call["data"]
        assert first_call["data"]["name"] == "CBL-tor-01-tor-02-mlag-PL1"
        assert first_call["data"]["endpoints"] == [iface_a1.id, iface_b1.id]

    @pytest.mark.asyncio
    async def test_orphan_cabled_interface_gets_adopted_by_id(self) -> None:
        gen = _gen()
        dev_a, dev_b = _mock_mlag_device("tor-01"), _mock_mlag_device("tor-02")
        iface_a = _mock_iface("a1", "Ethernet1/1", cable_id="orphan-cable")
        iface_b = _mock_iface("b1", "Ethernet1/1")
        orphan_obj = MagicMock(id="orphan-cable")

        async def _filters(*, kind: Any, **kwargs: Any) -> list[Any]:
            if kind is DcimPhysicalInterface and kwargs.get("device__ids") == [dev_a.id]:
                return [iface_a]
            if kind is DcimPhysicalInterface and kwargs.get("device__ids") == [dev_b.id]:
                return [iface_b]
            if kind is DcimCable:
                return []
            return []

        gen.client.filters = AsyncMock(side_effect=_filters)
        gen.client.get = AsyncMock(return_value=orphan_obj)
        cable_obj = MagicMock(save=AsyncMock())
        gen.client.create = AsyncMock(return_value=cable_obj)

        await gen._ensure_peer_link_cables("tor-01-tor-02-mlag", dev_a, dev_b)

        gen.client.get.assert_awaited_once_with(kind=DcimCable, id="orphan-cable")
        create_kwargs = gen.client.create.call_args.kwargs
        assert create_kwargs["data"]["id"] == "orphan-cable"
        assert create_kwargs["data"]["name"] == "CBL-tor-01-tor-02-mlag-PL1"
        cable_obj.save.assert_awaited_once_with(allow_upsert=True)

    @pytest.mark.asyncio
    async def test_existing_cable_gets_upserted_by_id(self) -> None:
        gen = _gen()
        dev_a, dev_b = _mock_mlag_device("tor-01"), _mock_mlag_device("tor-02")
        iface_a = _mock_iface("a1", "Ethernet1/1")
        iface_b = _mock_iface("b1", "Ethernet1/1")
        existing_cable = MagicMock(id="cable-existing")

        async def _filters(*, kind: Any, **kwargs: Any) -> list[Any]:
            if kind is DcimPhysicalInterface and kwargs.get("device__ids") == [dev_a.id]:
                return [iface_a]
            if kind is DcimPhysicalInterface and kwargs.get("device__ids") == [dev_b.id]:
                return [iface_b]
            if kind is DcimCable:
                return [existing_cable]
            return []

        gen.client.filters = AsyncMock(side_effect=_filters)
        cable_obj = MagicMock(save=AsyncMock())
        gen.client.create = AsyncMock(return_value=cable_obj)

        await gen._ensure_peer_link_cables("tor-01-tor-02-mlag", dev_a, dev_b)

        create_kwargs = gen.client.create.call_args.kwargs
        assert create_kwargs["data"]["id"] == "cable-existing"
        cable_obj.save.assert_awaited_once_with(allow_upsert=True)


class TestDisconnectStalePeerLinkCables:
    @pytest.mark.asyncio
    async def test_removes_stale_cables_up_to_max_count(self) -> None:
        gen = _gen()
        dev_a, dev_b = _mock_mlag_device("leaf-01"), _mock_mlag_device("leaf-02")
        iface_a1, iface_a2 = _mock_iface("a1", "Ethernet1/1"), _mock_iface("a2", "Ethernet1/2")
        existing_cable = MagicMock(id="cable-1")

        async def _filters(*, kind: Any, **kwargs: Any) -> list[Any]:
            if kind is DcimPhysicalInterface and kwargs.get("device__ids") == [dev_a.id]:
                return [iface_a1, iface_a2]
            if kind is DcimPhysicalInterface and kwargs.get("device__ids") == [dev_b.id]:
                return []
            if kind is DcimCable:
                return [existing_cable]
            return []

        gen.client.filters = AsyncMock(side_effect=_filters)
        gen.client.delete = AsyncMock()

        await gen._disconnect_stale_peer_link_cables("leaf-01-leaf-02-mlag", dev_a, dev_b)

        assert gen.client.delete.await_count == 2


def _control_client(gen: Any, *, prefix: str, devices: list[Any], existing_svi: Any = None) -> MagicMock:
    """Wire gen.client for _allocate_control_ips: one pool, one allocated prefix, no existing IPs."""
    pool = MagicMock(id="pool-1")
    allocated = MagicMock(display_label=prefix)
    allocated.prefix = MagicMock(value=prefix)
    allocated.ip_namespace = MagicMock(id="ns-default")

    async def _filters(*, kind: Any, **kwargs: Any) -> list[Any]:
        if kind is CoreIPPrefixPool:
            return [pool]
        if kind is DcimVirtualInterface and kwargs.get("role__value") == "mlag-control":
            return [existing_svi] if existing_svi is not None else []
        return []

    created: list[MagicMock] = []

    async def _create(*, kind: Any, data: dict[str, Any]) -> MagicMock:
        obj = MagicMock(id=f"ip-{len(created)}", save=AsyncMock())
        obj.kind, obj.data = kind, data
        created.append(obj)
        return obj

    gen.client.filters = AsyncMock(side_effect=_filters)
    gen.client.get = AsyncMock(return_value=None)
    gen.client.create = AsyncMock(side_effect=_create)
    gen.client.delete = AsyncMock()
    gen.client.allocate_next_ip_prefix = AsyncMock(return_value=allocated)
    gen._created = created
    return pool


class TestAllocateControlIps:
    @pytest.mark.asyncio
    async def test_sonic_gets_ipv4_slash31_lower_address_to_first_device(self) -> None:
        """SONiC pairs draw a /31 from MLAG-Control-IPv4; addrs[0] → device sorting first by name."""
        gen = _gen()
        dev_b, dev_a = _mock_mlag_device("leaf-02"), _mock_mlag_device("leaf-01")
        _control_client(gen, prefix="10.254.0.4/31", devices=[dev_a, dev_b])

        ip_ids = await gen._allocate_control_ips(_mock_mlag_obj(), "m", [dev_b, dev_a], {"sonic"}, False)

        assert gen.client.filters.await_args_list[0].kwargs["name__value"] == "MLAG-Control-IPv4"
        alloc = gen.client.allocate_next_ip_prefix.await_args.kwargs
        assert alloc["prefix_length"] == 31
        assert alloc["identifier"] == "mlag-control__mlag-1"
        addresses = [obj.data["address"] for obj in gen._created]
        assert addresses == ["10.254.0.4/31", "10.254.0.5/31"]
        assert ip_ids == {"id-leaf-01": "ip-0", "id-leaf-02": "ip-1"}

    @pytest.mark.asyncio
    async def test_other_platforms_get_ipv6_slash127(self) -> None:
        """Non-SONiC platforms draw a /127 from MLAG-Control-IPv6."""
        gen = _gen()
        devs = [_mock_mlag_device("leaf-01"), _mock_mlag_device("leaf-02")]
        _control_client(gen, prefix="fd00:2400::/127", devices=devs)

        await gen._allocate_control_ips(_mock_mlag_obj(), "m", devs, {"arista_eos"}, False)

        assert gen.client.filters.await_args_list[0].kwargs["name__value"] == "MLAG-Control-IPv6"
        assert gen.client.allocate_next_ip_prefix.await_args.kwargs["prefix_length"] == 127
        assert [obj.data["address"] for obj in gen._created] == ["fd00:2400::/127", "fd00:2400::1/127"]

    @pytest.mark.asyncio
    async def test_virtual_peer_link_uses_host_routes_and_drops_stale_svi(self) -> None:
        """A virtual peer-link gets /128 loopback addresses and removes any leftover control SVI."""
        gen = _gen()
        devs = [_mock_mlag_device("leaf-01"), _mock_mlag_device("leaf-02")]
        stale = _mock_iface("svi-old", "Vlan4094")
        _control_client(gen, prefix="fd00:2400::2/127", devices=devs, existing_svi=stale)

        await gen._allocate_control_ips(_mock_mlag_obj(virtual_peer_link=True), "m", devs, {"arista_eos"}, True)

        assert [obj.data["address"] for obj in gen._created] == ["fd00:2400::2/128", "fd00:2400::3/128"]
        assert gen.client.delete.await_count == 2

    @pytest.mark.asyncio
    async def test_vpc_skips_allocation(self) -> None:
        """Cisco vPC has no control session — nothing is allocated."""
        gen = _gen()
        devs = [_mock_mlag_device("nx-01"), _mock_mlag_device("nx-02")]
        _control_client(gen, prefix="fd00:2400::/127", devices=devs)

        assert await gen._allocate_control_ips(_mock_mlag_obj(), "m", devs, {"cisco_nxos"}, False) == {}
        gen.client.allocate_next_ip_prefix.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_pool_warns_and_skips(self) -> None:
        """Without the pool loaded, allocation is skipped with a warning (not an error)."""
        gen = _gen()
        gen.client.filters = AsyncMock(return_value=[])
        gen.client.allocate_next_ip_prefix = AsyncMock()
        devs = [_mock_mlag_device("leaf-01"), _mock_mlag_device("leaf-02")]

        assert await gen._allocate_control_ips(_mock_mlag_obj(), "m", devs, {"sonic"}, False) == {}
        gen.logger.warning.assert_called_once()
        gen.client.allocate_next_ip_prefix.assert_not_awaited()


class TestEnsureControlSvi:
    @pytest.mark.asyncio
    async def test_creates_vlan4094_with_control_ip(self) -> None:
        """A new Vlan4094 SVI is created with role=mlag-control and the control IP."""
        gen = _gen()
        dev = _mock_mlag_device("leaf-01")
        _control_client(gen, prefix="10.254.0.0/31", devices=[dev])

        await gen._ensure_control_svi(dev, _mock_mlag_obj(), "m", "ip-7")

        (svi,) = gen._created
        assert svi.kind is DcimVirtualInterface
        assert svi.data["name"] == "Vlan4094"
        assert svi.data["role"] == "mlag-control"
        assert svi.data["ip_address"] == {"id": "ip-7"}
        assert "id" not in svi.data

    @pytest.mark.asyncio
    async def test_existing_svi_upserted_by_id(self) -> None:
        """An existing control SVI is upserted by its id."""
        gen = _gen()
        dev = _mock_mlag_device("leaf-01")
        _control_client(gen, prefix="10.254.0.0/31", devices=[dev], existing_svi=_mock_iface("svi-1", "Vlan4094"))

        await gen._ensure_control_svi(dev, _mock_mlag_obj(), "m", "ip-7")

        assert gen._created[0].data["id"] == "svi-1"
