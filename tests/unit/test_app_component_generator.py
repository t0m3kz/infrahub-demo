"""Unit tests for AppComponentGenerator.

Covers:
  - generate()                      — top-level orchestration
  - _assign_segment_to_hosts()      — segment→host uplink wiring
  - _wire_pool_member()             — pool member + pool interface creation

Tests use asyncio.run() directly — same pattern as test_segment_generators.py.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from generators.add.app_component import AppComponentGenerator

# ---------------------------------------------------------------------------
# Harness helpers
# ---------------------------------------------------------------------------


def _make_gen() -> Any:
    """Return an AppComponentGenerator with mocked client and logger."""
    gen = AppComponentGenerator.__new__(AppComponentGenerator)
    gen.client = AsyncMock()
    gen.logger = MagicMock()
    return gen


def _mock_iface(iface_id: str = "iface-1", existing_cap_ids: set | None = None) -> MagicMock:
    """Return a mock physical interface with a controllable interface_capabilities relationship."""
    iface = MagicMock()
    iface.id = iface_id
    iface.save = AsyncMock()

    caps = MagicMock()
    caps.fetch = AsyncMock()
    caps.add = AsyncMock()
    caps.peers = [MagicMock(id=cap_id) for cap_id in (existing_cap_ids or set())]
    iface.interface_capabilities = caps

    return iface


def _mock_vip(
    vip_id: str = "vip-1",
    hostname: str = "vip.example.com",
    protocol: str = "https",
    port: int = 443,
    lb_peer_id: str | None = "lbha-1",
) -> MagicMock:
    """Return a mock VIP SDK object."""
    vip = MagicMock()
    vip.id = vip_id
    vip.resolve = AsyncMock()

    vip.hostname = MagicMock()
    vip.hostname.value = hostname

    vip.protocol = MagicMock()
    vip.protocol.value = protocol

    vip.port = MagicMock()
    vip.port.value = port

    if lb_peer_id:
        lb_rel = MagicMock()
        lb_peer = MagicMock()
        lb_peer.id = lb_peer_id
        lb_rel.peer = lb_peer
        vip.load_balancer = lb_rel
    else:
        vip.load_balancer = None

    return vip


def _mock_pool_member(pm_id: str = "pm-1", name: str = "member-1") -> MagicMock:
    """Return a mock LoadbalancerPoolMember SDK object."""
    pm = MagicMock()
    pm.id = pm_id
    pm.name = MagicMock()
    pm.name.value = name
    pm.save = AsyncMock()
    return pm


# ---------------------------------------------------------------------------
# GQL data builder
# ---------------------------------------------------------------------------


def _comp_response(
    comp_id: str = "comp-1",
    slug: str = "c001-trade-portal-p-web-frontend",
    name: str = "web-frontend",
    backend_port: int | None = 443,
    segment_id: str = "seg-1",
    segment_name: str = "c001-web-frontend-p",
    segment_typename: str = "ManagedVxlanSegment",
    vip_id: str | None = "vip-1",
    capabilities: list | None = None,
    cluster_capabilities: list | None = None,
) -> dict:
    """Build cleaned AppComponent data (no edges/node wrapping — already clean_data'd)."""
    return {
        "AppComponent": [
            {
                "id": comp_id,
                "name": name,
                "slug": slug,
                "backend_port": backend_port,
                "network_segment": {
                    "id": segment_id,
                    "name": segment_name,
                    "typename": segment_typename,
                },
                "vip_service": {"id": vip_id, "typename": "LoadbalancerVIP"} if vip_id else {},
                "capabilities": capabilities or [],
                "cluster_capabilities": cluster_capabilities or [],
            }
        ]
    }


# ===========================================================================
# TestAppComponentGeneratorGenerate
# ===========================================================================


class TestAppComponentGeneratorGenerate:
    """Tests for the top-level generate() method."""

    def test_empty_response_logs_error(self):
        """Empty AppComponent list triggers an error log and early return."""
        gen = _make_gen()
        # Pass data that cleans to an empty AppComponent list
        asyncio.run(gen.generate({"AppComponent": {"edges": []}}))
        gen.logger.error.assert_called_once()
        error_msg = gen.logger.error.call_args[0][0]
        assert "AppComponent" in error_msg

    def test_no_vip_service_skips_lb_wiring(self):
        """Component with no vip_service → client.get is never called for VIP."""
        gen = _make_gen()
        gen._assign_segment_to_hosts = AsyncMock()

        data = _comp_response(vip_id=None)
        asyncio.run(gen.generate(data))

        # No client.get for LoadbalancerVIP
        for call_args in gen.client.get.call_args_list:
            assert call_args.kwargs.get("kind") != "LoadbalancerVIP"
        # Info log about no vip_service
        info_msgs = " ".join(str(c) for c in gen.logger.info.call_args_list)
        assert "vip_service" in info_msgs or "LB wiring" in info_msgs

    def test_cloud_segment_skips_host_assignment(self):
        """CloudNetworkSegment typename → _assign_segment_to_hosts NOT called."""
        gen = _make_gen()
        gen._assign_segment_to_hosts = AsyncMock()
        gen._assign_vip_to_lb = AsyncMock()
        gen._wire_pool_member = AsyncMock()

        data = _comp_response(
            segment_typename="CloudNetworkSegment",
            vip_id=None,
            cluster_capabilities=[{"node_pools": [{"physical_hosts": [{"id": "host-1"}]}]}],
        )
        asyncio.run(gen.generate(data))

        gen._assign_segment_to_hosts.assert_not_called()

    def test_host_ids_from_cluster_capabilities(self):
        """Cluster with node_pool with physical_host → _assign_segment_to_hosts called with that host id."""
        gen = _make_gen()
        gen._assign_segment_to_hosts = AsyncMock()
        gen._assign_vip_to_lb = AsyncMock()
        gen._wire_pool_member = AsyncMock()

        data = _comp_response(
            vip_id=None,
            cluster_capabilities=[{"node_pools": [{"physical_hosts": [{"id": "host-cluster-1"}]}]}],
        )
        asyncio.run(gen.generate(data))

        gen._assign_segment_to_hosts.assert_awaited_once()
        call_kwargs = gen._assign_segment_to_hosts.call_args.kwargs
        assert "host-cluster-1" in call_kwargs["host_ids"]

    def test_host_ids_from_vm_hosting_device(self):
        """VM capability with hosting_device → _assign_segment_to_hosts called with that host id.

        Note: hosting_device must have more than just 'id' in the test data, because clean_data
        reduces a single-key {'id': X} dict to just the string X, causing a crash in the
        generator at `hosting.get('id')`. Using {'id': ..., 'name': ...} preserves the dict.
        """
        gen = _make_gen()
        gen._assign_segment_to_hosts = AsyncMock()
        gen._assign_vip_to_lb = AsyncMock()
        gen._wire_pool_member = AsyncMock()

        data = _comp_response(
            vip_id=None,
            capabilities=[
                {"id": "vm-1", "name": "vm-01", "hosting_device": {"id": "host-vm-1", "name": "hypervisor-01"}}
            ],
        )
        asyncio.run(gen.generate(data))

        gen._assign_segment_to_hosts.assert_awaited_once()
        call_kwargs = gen._assign_segment_to_hosts.call_args.kwargs
        assert "host-vm-1" in call_kwargs["host_ids"]

    def test_both_host_sources_merged(self):
        """Both cluster hosts and VM hosting devices → combined set passed to _assign_segment_to_hosts.

        Note: hosting_device must have more than just 'id' — see test_host_ids_from_vm_hosting_device.
        """
        gen = _make_gen()
        gen._assign_segment_to_hosts = AsyncMock()
        gen._assign_vip_to_lb = AsyncMock()
        gen._wire_pool_member = AsyncMock()

        data = _comp_response(
            vip_id=None,
            cluster_capabilities=[{"node_pools": [{"physical_hosts": [{"id": "host-cluster-1"}]}]}],
            capabilities=[
                {"id": "vm-1", "name": "vm-01", "hosting_device": {"id": "host-vm-1", "name": "hypervisor-01"}}
            ],
        )
        asyncio.run(gen.generate(data))

        gen._assign_segment_to_hosts.assert_awaited_once()
        call_kwargs = gen._assign_segment_to_hosts.call_args.kwargs
        assert "host-cluster-1" in call_kwargs["host_ids"]
        assert "host-vm-1" in call_kwargs["host_ids"]

    def test_segment_kind_passed_correctly(self):
        """ManagedVlanSegment typename → segment_kind='ManagedVlanSegment' passed to _assign_segment_to_hosts."""
        gen = _make_gen()
        gen._assign_segment_to_hosts = AsyncMock()
        gen._assign_vip_to_lb = AsyncMock()
        gen._wire_pool_member = AsyncMock()

        data = _comp_response(
            segment_typename="ManagedVlanSegment",
            vip_id=None,
            cluster_capabilities=[{"node_pools": [{"physical_hosts": [{"id": "host-1"}]}]}],
        )
        asyncio.run(gen.generate(data))

        gen._assign_segment_to_hosts.assert_awaited_once()
        call_kwargs = gen._assign_segment_to_hosts.call_args.kwargs
        assert call_kwargs["segment_kind"] == "ManagedVlanSegment"

    def test_vip_fetch_failure_logs_warning(self):
        """client.get raising for VIP → warning logged, _wire_pool_member not called."""
        gen = _make_gen()
        gen._assign_segment_to_hosts = AsyncMock()
        gen._assign_vip_to_lb = AsyncMock()
        gen._wire_pool_member = AsyncMock()
        gen.client.get = AsyncMock(side_effect=Exception("not found"))

        data = _comp_response(
            capabilities=[{"id": "vm-1", "name": "vm-01"}],
        )
        asyncio.run(gen.generate(data))

        warning_msgs = " ".join(str(c) for c in gen.logger.warning.call_args_list)
        assert "vip" in warning_msgs.lower() or "VIP" in warning_msgs
        gen._wire_pool_member.assert_not_called()

    def test_pool_members_created_for_each_capability(self):
        """2 VMs in capabilities → _wire_pool_member called twice."""
        gen = _make_gen()
        gen._assign_segment_to_hosts = AsyncMock()
        gen._assign_vip_to_lb = AsyncMock()
        gen._wire_pool_member = AsyncMock()

        vip = _mock_vip()
        gen.client.get = AsyncMock(return_value=vip)

        data = _comp_response(
            capabilities=[
                {"id": "vm-1", "name": "vm-01"},
                {"id": "vm-2", "name": "vm-02"},
            ],
        )
        asyncio.run(gen.generate(data))

        assert gen._wire_pool_member.call_count == 2


# ===========================================================================
# TestAssignSegmentToHosts
# ===========================================================================


class TestAssignSegmentToHosts:
    """Tests for _assign_segment_to_hosts()."""

    _CALL = dict(
        segment_id="seg-1",
        segment_name="c001-web-frontend-p",
        segment_kind="ManagedVxlanSegment",
        host_ids={"host-1", "host-2"},
    )

    def _run(self, gen, **overrides) -> None:
        kwargs = {**self._CALL, **overrides}
        asyncio.run(gen._assign_segment_to_hosts(**kwargs))

    def test_segment_fetched_by_correct_kind(self):
        """client.get called with the given segment_kind."""
        gen = _make_gen()
        segment_obj = MagicMock()
        gen.client.get = AsyncMock(return_value=segment_obj)
        gen.client.filters = AsyncMock(return_value=[])

        self._run(gen)

        gen.client.get.assert_awaited_once()
        call_kwargs = gen.client.get.call_args.kwargs
        assert call_kwargs["kind"] == "ManagedVxlanSegment"
        assert call_kwargs["id"] == "seg-1"

    def test_interfaces_queried_with_uplink_role(self):
        """client.filters called with role__value='uplink' and status__value='active'."""
        gen = _make_gen()
        segment_obj = MagicMock()
        gen.client.get = AsyncMock(return_value=segment_obj)
        gen.client.filters = AsyncMock(return_value=[])

        self._run(gen)

        gen.client.filters.assert_awaited_once()
        call_kwargs = gen.client.filters.call_args.kwargs
        assert call_kwargs["role__value"] == "uplink"
        assert call_kwargs["status__value"] == "active"

    def test_segment_added_to_new_interfaces(self):
        """Interface not in existing_ids → iface_caps.add called."""
        gen = _make_gen()
        segment_obj = MagicMock()
        segment_obj.id = "seg-1"
        gen.client.get = AsyncMock(return_value=segment_obj)

        iface = _mock_iface(iface_id="iface-1", existing_cap_ids=set())
        gen.client.filters = AsyncMock(return_value=[iface])

        self._run(gen)

        iface.interface_capabilities.add.assert_awaited_once_with(segment_obj)
        iface.save.assert_awaited_once()

    def test_already_assigned_interface_skipped(self):
        """Interface already has segment in peers → iface_caps.add NOT called, iface.save still called."""
        gen = _make_gen()
        segment_obj = MagicMock()
        gen.client.get = AsyncMock(return_value=segment_obj)

        # Existing caps already include seg-1
        iface = _mock_iface(iface_id="iface-1", existing_cap_ids={"seg-1"})
        gen.client.filters = AsyncMock(return_value=[iface])

        self._run(gen)

        iface.interface_capabilities.add.assert_not_called()
        iface.save.assert_awaited_once()

    def test_segment_fetch_failure_returns_early(self):
        """client.get raising → client.filters never called."""
        gen = _make_gen()
        gen.client.get = AsyncMock(side_effect=Exception("object not found"))

        self._run(gen)

        gen.client.filters.assert_not_called()
        warning_msgs = " ".join(str(c) for c in gen.logger.warning.call_args_list)
        assert "segment" in warning_msgs.lower() or "fetch" in warning_msgs.lower()


# ===========================================================================
# TestWirePoolMember
# ===========================================================================


class TestWirePoolMember:
    """Tests for _wire_pool_member()."""

    _CALL = dict(
        member_name="c001-trade-portal-p-web-frontend-vm-01",
        vm_id="vm-1",
        vm_name="vm-01",
        vip_id="vip-1",
        vip_hostname="vip.example.com",
        vip_proto="https",
        vip_port="443",
        backend_port=443,
    )

    def _run(self, gen, **overrides) -> None:
        kwargs = {**self._CALL, **overrides}
        asyncio.run(gen._wire_pool_member(**kwargs))

    def _make_vm(self, vm_id: str = "vm-1", ip_id: str | None = "ip-1") -> MagicMock:
        """Build a mock DcimVirtualDevice with primary_address."""
        vm = MagicMock()
        vm.id = vm_id

        if ip_id:
            addr_peer = MagicMock()
            addr_peer.id = ip_id
            primary_addr = MagicMock()
            primary_addr.peer = addr_peer
            vm.primary_address = primary_addr
        else:
            vm.primary_address = None

        caps = MagicMock()
        caps.fetch = AsyncMock()
        caps.add = AsyncMock()
        caps.peers = []
        vm.capabilities = caps
        vm.save = AsyncMock()

        return vm

    def _make_pool_member(self, pm_id: str = "pm-1") -> MagicMock:
        pm = MagicMock()
        pm.id = pm_id
        pm.save = AsyncMock()
        return pm

    def _make_pool_iface(self, pi_id: str = "pi-1") -> MagicMock:
        pi = MagicMock()
        pi.id = pi_id
        pi.save = AsyncMock()
        return pi

    def _make_vm_iface(self, iface_id: str = "viface-1") -> MagicMock:
        viface = MagicMock()
        viface.id = iface_id
        viface.save = AsyncMock()
        pi_caps = MagicMock()
        pi_caps.fetch = AsyncMock()
        pi_caps.add = AsyncMock()
        pi_caps.peers = []
        viface.interface_capabilities = pi_caps
        return viface

    def test_idempotent_existing_member_skips_create(self):
        """client.filters returns existing → client.create not called."""
        gen = _make_gen()
        existing = _mock_pool_member("pm-existing", str(self._CALL["member_name"]))
        gen.client.filters = AsyncMock(return_value=[existing])

        self._run(gen)

        gen.client.create.assert_not_called()
        info_msgs = " ".join(str(c) for c in gen.logger.info.call_args_list)
        assert "already exists" in info_msgs

    def test_creates_pool_member_with_correct_data(self):
        """Happy path → client.create called with kind='LoadbalancerPoolMember', correct data."""
        gen = _make_gen()
        gen.client.filters = AsyncMock(
            side_effect=[
                [],  # no existing pool members
                [],  # virtual interfaces query
                [],  # physical interfaces fallback query
            ]
        )

        vm = self._make_vm()
        pool_member = self._make_pool_member()
        pool_iface = self._make_pool_iface()
        gen.client.get = AsyncMock(return_value=vm)
        gen.client.create = AsyncMock(side_effect=[pool_member, pool_iface])

        self._run(gen)

        assert gen.client.create.call_count >= 1
        first_create_kwargs = gen.client.create.call_args_list[0].kwargs
        assert first_create_kwargs["kind"] == "LoadbalancerPoolMember"
        data = first_create_kwargs["data"]
        assert data["name"] == self._CALL["member_name"]
        assert data["vip_service"] == {"id": "vip-1"}
        assert data["weight"] == 1

    def test_backend_port_included_in_pool_interface(self):
        """backend_port=8443 → pool interface data has port=8443."""
        gen = _make_gen()
        gen.client.filters = AsyncMock(
            side_effect=[
                [],
                [],
                [],
            ]
        )

        vm = self._make_vm()
        pool_member = self._make_pool_member()
        pool_iface = self._make_pool_iface()
        gen.client.get = AsyncMock(return_value=vm)
        gen.client.create = AsyncMock(side_effect=[pool_member, pool_iface])

        self._run(gen, backend_port=8443)

        # Second create call is for PoolInterface
        assert gen.client.create.call_count >= 2
        pi_create_kwargs = gen.client.create.call_args_list[1].kwargs
        assert pi_create_kwargs["kind"] == "LoadbalancerPoolInterface"
        assert pi_create_kwargs["data"]["port"] == 8443

    def test_no_backend_port_omitted_from_pool_interface(self):
        """backend_port=None → 'port' key absent from pool interface data."""
        gen = _make_gen()
        gen.client.filters = AsyncMock(
            side_effect=[
                [],
                [],
                [],
            ]
        )

        vm = self._make_vm()
        pool_member = self._make_pool_member()
        pool_iface = self._make_pool_iface()
        gen.client.get = AsyncMock(return_value=vm)
        gen.client.create = AsyncMock(side_effect=[pool_member, pool_iface])

        self._run(gen, backend_port=None)

        assert gen.client.create.call_count >= 2
        pi_create_kwargs = gen.client.create.call_args_list[1].kwargs
        assert pi_create_kwargs["kind"] == "LoadbalancerPoolInterface"
        assert "port" not in pi_create_kwargs["data"]

    def test_pool_member_create_failure_logs_error(self):
        """client.create raises on PoolMember → error logged, no PoolInterface create."""
        gen = _make_gen()
        gen.client.filters = AsyncMock(return_value=[])  # no existing

        vm = self._make_vm()
        gen.client.get = AsyncMock(return_value=vm)
        gen.client.create = AsyncMock(side_effect=Exception("DB constraint violation"))

        self._run(gen)

        gen.logger.error.assert_called()
        error_msgs = " ".join(str(c) for c in gen.logger.error.call_args_list)
        assert "PoolMember" in error_msgs or "pool" in error_msgs.lower()
        # Only one create attempt — no PoolInterface was created
        gen.client.create.assert_awaited_once()
