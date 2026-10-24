"""Unit tests for MLAGWiringMixin (generators/mlag.py) — shared by
DeviceMixin._ensure_mlag_pairs and MLAGGenerator. Mirrors
test_device_mixin_ha.py's TestEnsureHaInterfaces/TestEnsureHaCable style: a
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

from generators.connections import CablingMixin
from generators.mlag import MLAGWiringMixin
from generators.pools import PoolMixin
from generators.protocols import (
    DcimCable,
    DcimLAGInterface,
    DcimPhysicalDevice,
    DcimPhysicalInterface,
    DcimVirtualInterface,
    TopologyPod,
)


class _WiringHost(MLAGWiringMixin, CablingMixin, PoolMixin):
    """MLAGWiringMixin with the CablingMixin P2P helper and PoolMixin lock it expects from its host."""


def _gen() -> Any:
    gen = _WiringHost.__new__(_WiringHost)
    gen.client = MagicMock()
    gen.logger = MagicMock()
    gen.acquire_resource_lock = AsyncMock(return_value="lock-id")
    gen.release_resource_lock = AsyncMock()
    # The cable length's rack lookup has its own tests (test_cable_attributes_generators.py).
    gen._cable_layouts = AsyncMock(return_value={})
    return gen


def _mock_iface(
    iface_id: str,
    name: str,
    *,
    cable_id: str | None = None,
    status: str = "active",
    device_id: str = "",
) -> MagicMock:
    iface = MagicMock()
    iface.id = iface_id
    iface.name = MagicMock(value=name)
    iface.status = MagicMock(value=status)
    iface.device = MagicMock(id=device_id)
    cable = MagicMock()
    cable.initialized = cable_id is not None
    if cable_id is not None:
        cable.id = cable_id
    iface.cable = cable
    iface.save = AsyncMock()
    return iface


def _pod_with_parent(*, initialized: bool, parent_id: str = "") -> MagicMock:
    """A TopologyPod as get(include=["parent"]) returns it (``parent`` is a reserved MagicMock kwarg, so set after)."""
    pod = MagicMock()
    pod.parent = MagicMock(initialized=initialized, id=parent_id)
    return pod


def _mock_mlag_device(name: str, *, platform: str = "nxos") -> MagicMock:
    """A device as filters(include=["platform"]) returns it: the platform peer
    is already in the store, so fetch() must never be needed."""
    dev = MagicMock()
    dev.id = f"id-{name}"
    dev.name = MagicMock(value=name)
    dev.deployment = MagicMock(initialized=False)
    platform_rel = MagicMock(initialized=True)
    platform_rel.fetch = AsyncMock(side_effect=AssertionError("platform.fetch() must not be called"))
    platform_rel.peer = MagicMock()
    platform_rel.peer.name = MagicMock(value=platform)
    dev.platform = platform_rel
    return dev


def _mock_cable(cable_id: str, name: str) -> MagicMock:
    cable = MagicMock(id=cable_id)
    cable.name = MagicMock(value=name)
    return cable


def _mock_mlag_obj(*, virtual_peer_link: bool = False) -> MagicMock:
    mlag_obj = MagicMock()
    mlag_obj.id = "mlag-1"
    mlag_obj.virtual_peer_link = MagicMock(value=virtual_peer_link)
    mlag_obj.save = AsyncMock()
    return mlag_obj


def _pair_client(gen: Any, devices: list[Any], interfaces: dict[tuple[Any, str], list[Any]]) -> None:
    """gen.client.filters for a whole wiring run: the device pair, no control
    pool, the given ``(kind, role) -> interfaces`` (each tagged with its device
    id) filtered by device__ids, and no cable found by name."""
    by_id = {dev.id: dev for dev in devices}

    async def _filters(*, kind: Any, **kwargs: Any) -> list[Any]:
        if kind is DcimPhysicalDevice:
            return [by_id[device_id] for device_id in kwargs["ids"]]
        if "device__ids" in kwargs:
            matching = interfaces.get((kind, kwargs["role__value"]), [])
            return [iface for iface in matching if iface.device.id in kwargs["device__ids"]]
        return []  # no MLAG control pool, no cable by that name

    gen.client.filters = AsyncMock(side_effect=_filters)
    gen.client.delete = AsyncMock()


def _create_returning_ids(gen: Any) -> None:
    async def _create(*, kind: Any, data: dict[str, Any]) -> MagicMock:
        obj = MagicMock(id=data.get("id", f"new-{len(gen.client.create.call_args_list)}"), save=AsyncMock())
        return obj

    gen.client.create = AsyncMock(side_effect=_create)


def _interface_queries(gen: Any) -> list[dict[str, Any]]:
    return [c.kwargs for c in gen.client.filters.call_args_list if "device__ids" in c.kwargs]


class TestEnsureMlagWiring:
    @pytest.mark.asyncio
    async def test_wiring_runs_under_a_per_domain_lock(self) -> None:
        """The pod/rack generator and the add_mlag trigger can wire the same
        domain concurrently; both would create the same control address
        (Schema Integrity failure at merge), so wiring serializes per domain."""
        gen = _gen()
        events: list[str] = []
        gen.acquire_resource_lock.side_effect = lambda key: events.append(f"acquire:{key}") or "lock-id"
        gen.release_resource_lock.side_effect = lambda lock_id: events.append("release")
        gen._ensure_mlag_wiring_locked = AsyncMock(side_effect=lambda *a, **k: events.append("wire"))

        await gen.ensure_mlag_wiring(_mock_mlag_obj(), "tor-01-tor-02-mlag", member_ids=["a", "b"])

        assert events == ["acquire:mlag-wiring-mlag-1", "wire", "release"]

    @pytest.mark.asyncio
    async def test_platform_comes_from_the_device_query_without_a_fetch(self) -> None:
        """The device query includes platform (its peer lands in the store), so
        the platform name is read without one fetch() per device."""
        gen = _gen()
        dev_1 = _mock_mlag_device("leaf-01", platform="arista_eos")
        dev_2 = _mock_mlag_device("leaf-02", platform="nokia_sros")
        _pair_client(gen, [dev_1, dev_2], {})
        _create_returning_ids(gen)

        await gen.ensure_mlag_wiring(
            _mock_mlag_obj(virtual_peer_link=True), "leaf-01-leaf-02-mlag", member_ids=[dev_1.id, dev_2.id]
        )

        device_query = gen.client.filters.call_args_list[0].kwargs
        assert device_query["kind"] is DcimPhysicalDevice
        assert device_query["include"] == ["deployment", "platform"]
        dev_1.platform.fetch.assert_not_awaited()
        loopback_names = [c.kwargs["data"]["name"] for c in gen.client.create.call_args_list]
        assert loopback_names == ["Loopback100", "loopback-100"]

    @pytest.mark.asyncio
    async def test_wrong_member_count_logs_error_and_noops(self) -> None:
        gen = _gen()
        mlag_obj = _mock_mlag_obj()
        gen.client.filters = AsyncMock(return_value=[])

        await gen.ensure_mlag_wiring(mlag_obj, "tor-01-tor-02-mlag", member_ids=["id-tor-01"])

        gen.logger.error.assert_called_once()
        gen.client.filters.assert_not_called()

    @pytest.mark.asyncio
    async def test_unresolved_device_logs_error_and_noops(self) -> None:
        """Only one of the two ids resolves to a device: error, nothing written."""
        gen = _gen()
        dev_1 = _mock_mlag_device("tor-01")
        gen.client.filters = AsyncMock(return_value=[dev_1])
        gen.client.create = AsyncMock()

        await gen.ensure_mlag_wiring(_mock_mlag_obj(), "tor-01-tor-02-mlag", member_ids=[dev_1.id, "id-gone"])

        gen.logger.error.assert_called_once()
        gen.client.filters.assert_awaited_once()
        gen.client.create.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_back_to_back_wires_lag_and_cable(self) -> None:
        gen = _gen()
        mlag_obj = _mock_mlag_obj(virtual_peer_link=False)
        dev_1, dev_2 = _mock_mlag_device("tor-01"), _mock_mlag_device("tor-02")
        mlag_peer_1 = _mock_iface("iface-1", "Ethernet1/1", device_id=dev_1.id)
        mlag_peer_2 = _mock_iface("iface-2", "Ethernet1/1", device_id=dev_2.id)
        _pair_client(gen, [dev_1, dev_2], {(DcimPhysicalInterface, "mlag-peer"): [mlag_peer_1, mlag_peer_2]})
        _create_returning_ids(gen)

        await gen.ensure_mlag_wiring(mlag_obj, "tor-01-tor-02-mlag", member_ids=[dev_1.id, dev_2.id])

        lag_calls = [c.kwargs["data"] for c in gen.client.create.call_args_list if c.kwargs["kind"] is DcimLAGInterface]
        assert [(d["device"], d["member_interfaces"]) for d in lag_calls] == [
            ({"id": dev_1.id}, [{"id": "iface-1"}]),
            ({"id": dev_2.id}, [{"id": "iface-2"}]),
        ]
        cable_calls = [c.kwargs["data"] for c in gen.client.create.call_args_list if c.kwargs["kind"] is DcimCable]
        assert [d["endpoints"] for d in cable_calls] == [["iface-1", "iface-2"]]

    @pytest.mark.asyncio
    async def test_back_to_back_reads_each_interface_kind_once_for_the_pair(self) -> None:
        """Stale virtual peer-links, existing LAGs and the mlag-peer ports are
        each one query for both devices; the ports read for the LAG feed the
        cables too. 2 devices, 1 port each: 1 device + 1 pool + 3 interface + 1
        cable-by-name = 6 reads."""
        gen = _gen()
        dev_1, dev_2 = _mock_mlag_device("tor-01"), _mock_mlag_device("tor-02")
        ports = [
            _mock_iface("iface-1", "Ethernet1/1", device_id=dev_1.id),
            _mock_iface("iface-2", "Ethernet1/1", device_id=dev_2.id),
        ]
        _pair_client(gen, [dev_1, dev_2], {(DcimPhysicalInterface, "mlag-peer"): ports})
        _create_returning_ids(gen)

        await gen.ensure_mlag_wiring(_mock_mlag_obj(), "tor-01-tor-02-mlag", member_ids=[dev_1.id, dev_2.id])

        queries = _interface_queries(gen)
        assert [(q["kind"], q["role__value"]) for q in queries] == [
            (DcimVirtualInterface, "mlag-peer"),
            (DcimLAGInterface, "mlag-peer"),
            (DcimPhysicalInterface, "mlag-peer"),
        ]
        assert all(q["device__ids"] == [dev_1.id, dev_2.id] for q in queries)
        assert queries[2]["include"] == ["cable"]
        assert gen.client.filters.await_count == 6

    @pytest.mark.asyncio
    async def test_existing_and_stale_interfaces_go_to_their_own_device(self) -> None:
        """Grouping by device: only tor-02 has a LAG to upsert and only tor-01 a
        stale loopback to delete."""
        gen = _gen()
        dev_1, dev_2 = _mock_mlag_device("tor-01"), _mock_mlag_device("tor-02")
        _pair_client(
            gen,
            [dev_1, dev_2],
            {
                (DcimPhysicalInterface, "mlag-peer"): [
                    _mock_iface("iface-1", "Ethernet1/1", device_id=dev_1.id),
                    _mock_iface("iface-2", "Ethernet1/1", device_id=dev_2.id),
                ],
                (DcimLAGInterface, "mlag-peer"): [_mock_iface("lag-2", "port-channel100", device_id=dev_2.id)],
                (DcimVirtualInterface, "mlag-peer"): [_mock_iface("lo-1", "loopback100", device_id=dev_1.id)],
            },
        )
        _create_returning_ids(gen)

        await gen.ensure_mlag_wiring(_mock_mlag_obj(), "tor-01-tor-02-mlag", member_ids=[dev_1.id, dev_2.id])

        lag_calls = [c.kwargs["data"] for c in gen.client.create.call_args_list if c.kwargs["kind"] is DcimLAGInterface]
        assert [(d["device"]["id"], d.get("id")) for d in lag_calls] == [(dev_1.id, None), (dev_2.id, "lag-2")]
        gen.client.delete.assert_awaited_once_with(kind=DcimVirtualInterface, id="lo-1")

    @pytest.mark.asyncio
    async def test_back_to_back_control_svis_read_once_and_upserted_per_device(self) -> None:
        """With a control session, the pair's existing control SVIs are one
        query; each device's SVI is upserted by its own id."""
        gen = _gen()
        dev_1, dev_2 = _mock_mlag_device("leaf-01", platform="sonic"), _mock_mlag_device("leaf-02", platform="sonic")
        gen._allocate_control_ips = AsyncMock(return_value={dev_1.id: "ip-1", dev_2.id: "ip-2"})
        _pair_client(
            gen,
            [dev_1, dev_2],
            {
                (DcimPhysicalInterface, "mlag-peer"): [
                    _mock_iface("iface-1", "Ethernet0", device_id=dev_1.id),
                    _mock_iface("iface-2", "Ethernet0", device_id=dev_2.id),
                ],
                (DcimVirtualInterface, "mlag-control"): [_mock_iface("svi-2", "Vlan4094", device_id=dev_2.id)],
            },
        )
        _create_returning_ids(gen)

        await gen.ensure_mlag_wiring(_mock_mlag_obj(), "leaf-01-leaf-02-mlag", member_ids=[dev_1.id, dev_2.id])

        svi_queries = [q for q in _interface_queries(gen) if q["role__value"] == "mlag-control"]
        assert len(svi_queries) == 1
        svis = [c.kwargs["data"] for c in gen.client.create.call_args_list if c.kwargs["data"]["name"] == "Vlan4094"]
        assert [(d["device"]["id"], d.get("id"), d["ip_address"]) for d in svis] == [
            (dev_1.id, None, {"id": "ip-1"}),
            (dev_2.id, "svi-2", {"id": "ip-2"}),
        ]

    @pytest.mark.asyncio
    async def test_virtual_wires_loopback_and_skips_cable(self) -> None:
        gen = _gen()
        mlag_obj = _mock_mlag_obj(virtual_peer_link=True)
        dev_1, dev_2 = _mock_mlag_device("leaf-01"), _mock_mlag_device("leaf-02")
        _pair_client(gen, [dev_1, dev_2], {})
        _create_returning_ids(gen)

        await gen.ensure_mlag_wiring(mlag_obj, "leaf-01-leaf-02-mlag", member_ids=[dev_1.id, dev_2.id])

        virt_calls = [c for c in gen.client.create.call_args_list if c.kwargs["kind"] is DcimVirtualInterface]
        assert len(virt_calls) == 2
        cable_calls = [c for c in gen.client.create.call_args_list if c.kwargs["kind"] is DcimCable]
        assert len(cable_calls) == 0

    @pytest.mark.asyncio
    async def test_virtual_removes_stale_cables_with_one_lookup(self) -> None:
        """Switched to virtual: the PL<n> cables of the old back-to-back wiring
        (n up to the larger port count) are found in one name__values query."""
        gen = _gen()
        dev_1, dev_2 = _mock_mlag_device("leaf-01"), _mock_mlag_device("leaf-02")
        _pair_client(
            gen,
            [dev_1, dev_2],
            {
                (DcimPhysicalInterface, "mlag-peer"): [
                    _mock_iface("a1", "Ethernet1/1", device_id=dev_1.id),
                    _mock_iface("a2", "Ethernet1/2", device_id=dev_1.id),
                    _mock_iface("b1", "Ethernet1/1", device_id=dev_2.id),
                ]
            },
        )
        _create_returning_ids(gen)
        filters = gen.client.filters.side_effect

        async def _with_cables(*, kind: Any, **kwargs: Any) -> list[Any]:
            if kind is DcimCable:
                return [_mock_cable("cable-1", "CBL-m-PL1")]
            return await filters(kind=kind, **kwargs)

        gen.client.filters.side_effect = _with_cables

        await gen.ensure_mlag_wiring(_mock_mlag_obj(virtual_peer_link=True), "m", member_ids=[dev_1.id, dev_2.id])

        cable_queries = [c.kwargs for c in gen.client.filters.call_args_list if c.kwargs["kind"] is DcimCable]
        assert cable_queries == [{"kind": DcimCable, "name__values": ["CBL-m-PL1", "CBL-m-PL2"]}]
        gen.client.delete.assert_awaited_once_with(kind=DcimCable, id="cable-1")


class TestDisconnectStalePeerLink:
    @pytest.mark.asyncio
    async def test_removes_stale_virtual_interface_when_switching_to_back_to_back(self) -> None:
        gen = _gen()
        device_obj = _mock_mlag_device("leaf-01")
        stale = _mock_iface("stale-virt", "Loopback100")
        gen.client.delete = AsyncMock()

        await gen._disconnect_stale_peer_link(device_obj, [stale], "leaf-01-leaf-02-mlag", virtual_peer_link=False)

        gen.client.delete.assert_awaited_once_with(kind=DcimVirtualInterface, id="stale-virt")

    @pytest.mark.asyncio
    async def test_removes_stale_lag_when_switching_to_virtual(self) -> None:
        gen = _gen()
        device_obj = _mock_mlag_device("leaf-01")
        stale = _mock_iface("stale-lag", "Port-Channel100")
        gen.client.delete = AsyncMock()

        await gen._disconnect_stale_peer_link(device_obj, [stale], "leaf-01-leaf-02-mlag", virtual_peer_link=True)

        gen.client.delete.assert_awaited_once_with(kind=DcimLAGInterface, id="stale-lag")

    @pytest.mark.asyncio
    async def test_noop_when_nothing_stale(self) -> None:
        gen = _gen()
        device_obj = _mock_mlag_device("leaf-01")
        gen.client.delete = AsyncMock()

        await gen._disconnect_stale_peer_link(device_obj, [], "leaf-01-leaf-02-mlag", virtual_peer_link=False)

        gen.client.delete.assert_not_awaited()


class TestEnsureLagPeerLink:
    @pytest.mark.asyncio
    async def test_no_mlag_peer_interfaces_logs_error_returns_none(self) -> None:
        gen = _gen()
        device_obj = _mock_mlag_device("tor-01")
        mlag_obj = _mock_mlag_obj()
        gen.client.create = AsyncMock()

        result = await gen._ensure_lag_peer_link(device_obj, mlag_obj, "nxos", [], None)

        assert result is None
        gen.logger.error.assert_called_once()
        gen.client.create.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_creates_new_lag_with_full_state(self) -> None:
        gen = _gen()
        device_obj = _mock_mlag_device("tor-01")
        mlag_obj = _mock_mlag_obj()
        iface_1 = _mock_iface("iface-1", "Ethernet1/1")
        iface_2 = _mock_iface("iface-2", "Ethernet1/2")
        lag_obj = MagicMock(id="lag-1", save=AsyncMock())
        gen.client.create = AsyncMock(return_value=lag_obj)

        result = await gen._ensure_lag_peer_link(device_obj, mlag_obj, "nxos", [iface_1, iface_2], None)

        assert result is lag_obj
        create_kwargs = gen.client.create.call_args.kwargs
        assert "id" not in create_kwargs["data"]
        assert create_kwargs["data"]["member_interfaces"] == [{"id": "iface-1"}, {"id": "iface-2"}]
        assert create_kwargs["data"]["mlag_domain"] == {"id": "mlag-1"}
        assert "capabilities" not in create_kwargs["data"]
        lag_obj.save.assert_awaited_once_with(allow_upsert=True)

    @pytest.mark.asyncio
    async def test_inactive_member_is_activated_untracked(self) -> None:
        """A member port not yet active is saved active, outside this run's group."""
        gen = _gen()
        iface = _mock_iface("iface-1", "Ethernet1/1", status="free")
        gen.client.create = AsyncMock(return_value=MagicMock(id="lag-1", save=AsyncMock()))

        await gen._ensure_lag_peer_link(_mock_mlag_device("tor-01"), _mock_mlag_obj(), "nxos", [iface], None)

        assert iface.status.value == "active"
        iface.save.assert_awaited_once_with(allow_upsert=True, update_group_context=False)

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
        lag_obj = MagicMock(id="lag-1", save=AsyncMock())
        gen.client.create = AsyncMock(return_value=lag_obj)

        result = await gen._ensure_lag_peer_link(device_obj, mlag_obj, "nxos", [iface_1], existing_lag)

        assert result is lag_obj
        create_kwargs = gen.client.create.call_args.kwargs
        assert create_kwargs["data"]["id"] == "lag-1"
        assert create_kwargs["data"]["mlag_domain"] == {"id": "mlag-new"}
        assert "capabilities" not in create_kwargs["data"]
        lag_obj.save.assert_awaited_once_with(allow_upsert=True)


class TestEnsureVirtualPeerLink:
    @pytest.mark.asyncio
    async def test_creates_new_loopback_with_full_state(self) -> None:
        gen = _gen()
        device_obj = _mock_mlag_device("leaf-01")
        virt_obj = MagicMock(id="virt-1", save=AsyncMock())
        gen.client.create = AsyncMock(return_value=virt_obj)

        result = await gen._ensure_virtual_peer_link(device_obj, "leaf-01-leaf-02-mlag", "nxos", None)

        assert result is virt_obj
        create_kwargs = gen.client.create.call_args.kwargs
        assert "id" not in create_kwargs["data"]
        assert create_kwargs["data"]["role"] == "mlag-peer"
        assert create_kwargs["data"]["device"] == {"id": device_obj.id}
        assert "capabilities" not in create_kwargs["data"]
        virt_obj.save.assert_awaited_once_with(allow_upsert=True)

    @pytest.mark.asyncio
    async def test_existing_loopback_gets_upserted_by_id(self) -> None:
        gen = _gen()
        device_obj = _mock_mlag_device("leaf-01")
        existing = MagicMock(id="virt-1")
        virt_obj = MagicMock(id="virt-1", save=AsyncMock())
        gen.client.create = AsyncMock(return_value=virt_obj)

        result = await gen._ensure_virtual_peer_link(device_obj, "leaf-01-leaf-02-mlag", "nxos", existing)

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
        gen.client.filters = AsyncMock(return_value=[])  # no cable by that name yet
        cable_1 = MagicMock(save=AsyncMock())
        cable_2 = MagicMock(save=AsyncMock())
        gen.client.create = AsyncMock(side_effect=[cable_1, cable_2])

        # unsorted on purpose
        await gen._ensure_peer_link_cables(
            "tor-01-tor-02-mlag", dev_a, dev_b, [iface_a2, iface_a1], [iface_b2, iface_b1]
        )

        assert gen.client.create.await_count == 2
        first_call = gen.client.create.call_args_list[0].kwargs
        assert "id" not in first_call["data"]
        assert first_call["data"]["name"] == "CBL-tor-01-tor-02-mlag-PL1"
        assert first_call["data"]["endpoints"] == [iface_a1.id, iface_b1.id]
        names = [c.kwargs["name__value"] for c in gen.client.filters.call_args_list]
        assert names == ["CBL-tor-01-tor-02-mlag-PL1", "CBL-tor-01-tor-02-mlag-PL2"]

    @pytest.mark.asyncio
    async def test_no_ports_on_one_side_creates_nothing(self) -> None:
        gen = _gen()
        gen.client.filters = AsyncMock()
        gen.client.create = AsyncMock()

        await gen._ensure_peer_link_cables(
            "m", _mock_mlag_device("tor-01"), _mock_mlag_device("tor-02"), [_mock_iface("a1", "Ethernet1/1")], []
        )

        gen.client.filters.assert_not_awaited()
        gen.client.create.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_orphan_cabled_interface_gets_adopted_by_id(self) -> None:
        gen = _gen()
        dev_a, dev_b = _mock_mlag_device("tor-01"), _mock_mlag_device("tor-02")
        iface_a = _mock_iface("a1", "Ethernet1/1", cable_id="orphan-cable")
        iface_b = _mock_iface("b1", "Ethernet1/1")
        gen.client.filters = AsyncMock(return_value=[])
        gen.client.get = AsyncMock()
        cable_obj = MagicMock(save=AsyncMock())
        gen.client.create = AsyncMock(return_value=cable_obj)

        await gen._ensure_peer_link_cables("tor-01-tor-02-mlag", dev_a, dev_b, [iface_a], [iface_b])

        gen.client.get.assert_not_awaited()
        create_kwargs = gen.client.create.call_args.kwargs
        assert create_kwargs["data"]["id"] == "orphan-cable"
        assert create_kwargs["data"]["name"] == "CBL-tor-01-tor-02-mlag-PL1"
        cable_obj.save.assert_awaited_once_with(allow_upsert=True)

    @pytest.mark.asyncio
    async def test_peer_link_cables_are_filed_under_the_pods_parent_read_once_per_run(self) -> None:
        """Every pair of a run sits in one pod: its parent is read once, then served from the memo."""
        gen = _gen()
        gen.client.filters = AsyncMock(return_value=[])
        gen.client.get = AsyncMock(return_value=_pod_with_parent(initialized=True, parent_id="dc-1"))
        gen.client.create = AsyncMock(return_value=MagicMock(save=AsyncMock()))
        for pair in ("a", "b"):
            dev_a, dev_b = _mock_mlag_device(f"{pair}-01"), _mock_mlag_device(f"{pair}-02")
            dev_a.deployment = MagicMock(initialized=True, typename="TopologyPod", id="pod-1")
            await gen._ensure_peer_link_cables(
                f"{pair}-mlag", dev_a, dev_b, [_mock_iface("a1", "Ethernet1")], [_mock_iface("b1", "Ethernet1")]
            )

        gen.client.get.assert_awaited_once_with(kind=TopologyPod, id="pod-1", include=["parent"])
        deployments = [c.kwargs["data"]["deployment"] for c in gen.client.create.await_args_list]
        assert deployments == [{"id": "dc-1"}, {"id": "dc-1"}]

    @pytest.mark.asyncio
    async def test_pod_without_a_parent_files_no_deployment_and_is_remembered(self) -> None:
        """A pod whose parent is unset yields no deployment, and that answer is memoised too."""
        gen = _gen()
        gen.client.get = AsyncMock(return_value=_pod_with_parent(initialized=False))
        dev = _mock_mlag_device("a-01")
        dev.deployment = MagicMock(initialized=True, typename="TopologyPod", id="pod-1")

        assert await gen._resolve_peer_link_deployment_id(dev) is None
        assert await gen._resolve_peer_link_deployment_id(dev) is None
        gen.client.get.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_existing_cable_gets_upserted_by_id(self) -> None:
        gen = _gen()
        dev_a, dev_b = _mock_mlag_device("tor-01"), _mock_mlag_device("tor-02")
        iface_a = _mock_iface("a1", "Ethernet1/1")
        iface_b = _mock_iface("b1", "Ethernet1/1")
        existing_cable = MagicMock(id="cable-existing")
        gen.client.filters = AsyncMock(return_value=[existing_cable])
        cable_obj = MagicMock(save=AsyncMock())
        gen.client.create = AsyncMock(return_value=cable_obj)

        await gen._ensure_peer_link_cables("tor-01-tor-02-mlag", dev_a, dev_b, [iface_a], [iface_b])

        create_kwargs = gen.client.create.call_args.kwargs
        assert create_kwargs["data"]["id"] == "cable-existing"
        cable_obj.save.assert_awaited_once_with(allow_upsert=True)


class TestDisconnectStalePeerLinkCables:
    @pytest.mark.asyncio
    async def test_removes_stale_cables_up_to_port_count(self) -> None:
        """One name__values query for CBL-…-PL1..PL<port_count>; every one found is deleted."""
        gen = _gen()
        gen.client.filters = AsyncMock(
            return_value=[_mock_cable("cable-2", "CBL-m-PL2"), _mock_cable("cable-1", "CBL-m-PL1")]
        )
        gen.client.delete = AsyncMock()

        await gen._disconnect_stale_peer_link_cables("m", 2)

        gen.client.filters.assert_awaited_once_with(kind=DcimCable, name__values=["CBL-m-PL1", "CBL-m-PL2"])
        assert [c.kwargs["id"] for c in gen.client.delete.await_args_list] == ["cable-1", "cable-2"]

    @pytest.mark.asyncio
    async def test_no_ports_queries_nothing(self) -> None:
        gen = _gen()
        gen.client.filters = AsyncMock()
        gen.client.delete = AsyncMock()

        await gen._disconnect_stale_peer_link_cables("m", 0)

        gen.client.filters.assert_not_awaited()
        gen.client.delete.assert_not_awaited()


def _control_client(gen: Any, *, prefix: str, devices: list[Any], existing_svis: list[Any] | None = None) -> MagicMock:
    """Wire gen.client for _allocate_control_ips: one pool, one allocated prefix, no existing IPs."""
    pool = MagicMock(id="pool-1")
    allocated = MagicMock(display_label=prefix)
    allocated.prefix = MagicMock(value=prefix)
    allocated.ip_namespace = MagicMock(id="ns-default")

    async def _filters(*, kind: Any, **kwargs: Any) -> list[Any]:
        if kind is CoreIPPrefixPool:
            return [pool]
        if kind is DcimVirtualInterface and kwargs.get("role__value") == "mlag-control":
            return [svi for svi in existing_svis or [] if svi.device.id in kwargs["device__ids"]]
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
        stale = [_mock_iface(f"svi-old-{dev.id}", "Vlan4094", device_id=dev.id) for dev in devs]
        _control_client(gen, prefix="fd00:2400::2/127", devices=devs, existing_svis=stale)

        await gen._allocate_control_ips(_mock_mlag_obj(virtual_peer_link=True), "m", devs, {"arista_eos"}, True)

        assert [obj.data["address"] for obj in gen._created] == ["fd00:2400::2/128", "fd00:2400::3/128"]
        assert [c.kwargs["id"] for c in gen.client.delete.await_args_list] == [s.id for s in stale]
        svi_queries = [c.kwargs for c in gen.client.filters.await_args_list if "device__ids" in c.kwargs]
        assert [q["device__ids"] for q in svi_queries] == [[dev.id for dev in devs]]

    @pytest.mark.asyncio
    async def test_control_pool_is_read_once_per_run(self) -> None:
        """Two domains of one run draw from the same pool, read a single time."""
        gen = _gen()
        devs = [_mock_mlag_device("leaf-01"), _mock_mlag_device("leaf-02")]
        pool = _control_client(gen, prefix="fd00:2400::/127", devices=devs)

        await gen._allocate_control_ips(_mock_mlag_obj(), "m1", devs, {"arista_eos"}, False)
        await gen._allocate_control_ips(_mock_mlag_obj(), "m2", devs, {"arista_eos"}, False)

        pool_reads = [c for c in gen.client.filters.await_args_list if c.kwargs["kind"] is CoreIPPrefixPool]
        assert len(pool_reads) == 1
        assert [c.kwargs["resource_pool"] for c in gen.client.allocate_next_ip_prefix.await_args_list] == [pool, pool]

    @pytest.mark.asyncio
    async def test_missing_pool_is_not_remembered(self) -> None:
        """A pool that was not found is looked up again by the next domain."""
        gen = _gen()
        gen.client.filters = AsyncMock(return_value=[])
        devs = [_mock_mlag_device("leaf-01"), _mock_mlag_device("leaf-02")]

        await gen._allocate_control_ips(_mock_mlag_obj(), "m1", devs, {"sonic"}, False)
        await gen._allocate_control_ips(_mock_mlag_obj(), "m2", devs, {"sonic"}, False)

        assert gen.client.filters.await_count == 2

    @pytest.mark.asyncio
    async def test_vpc_skips_allocation(self) -> None:
        """Cisco vPC has no control session — nothing is allocated."""
        gen = _gen()
        devs = [_mock_mlag_device("nx-01"), _mock_mlag_device("nx-02")]
        stale = _mock_iface("svi-old", "Vlan4094", device_id=devs[1].id)
        _control_client(gen, prefix="fd00:2400::/127", devices=devs, existing_svis=[stale])

        assert await gen._allocate_control_ips(_mock_mlag_obj(), "m", devs, {"cisco_nxos"}, False) == {}
        gen.client.allocate_next_ip_prefix.assert_not_awaited()
        gen.client.delete.assert_awaited_once_with(kind=DcimVirtualInterface, id="svi-old")

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

        await gen._ensure_control_svi(dev, "m", "ip-7", None)

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
        _control_client(gen, prefix="10.254.0.0/31", devices=[dev])

        await gen._ensure_control_svi(dev, "m", "ip-7", _mock_iface("svi-1", "Vlan4094"))

        assert gen._created[0].data["id"] == "svi-1"
