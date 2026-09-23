"""Unit tests for DeviceMixin.create_devices()."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from generators.devices import DeviceMixin
from generators.protocols import (
    DcimCable,
    DcimInterface,
    DcimPhysicalDevice,
    DcimPhysicalInterface,
    DcimVirtualDevice,
    DcimVirtualInterface,
    ManagedHAInterface,
    ManagedMLAG,
)


class _DummyBatch:
    def __init__(self) -> None:
        self._nodes: list[Any] = []

    def add(self, *, task: Any, allow_upsert: bool, node: Any) -> None:  # noqa: ARG002
        self._nodes.append(node)

    async def execute(self):
        for node in self._nodes:
            yield node, None


def _make_generator() -> Any:
    gen = DeviceMixin.__new__(DeviceMixin)
    gen.fabric_name = "dc1"
    gen.pod_name = None
    gen.logger = MagicMock()
    gen.client = MagicMock()
    gen.client.get = AsyncMock(return_value=MagicMock(id="group-1"))
    gen.client.filters = AsyncMock(return_value=[])
    gen.client.allocate_next_ip_address = AsyncMock(return_value={"id": "ip-1"})
    gen.client.create_batch = AsyncMock(side_effect=[_DummyBatch(), _DummyBatch()])
    gen._resolve_pool = AsyncMock(return_value=MagicMock(id="pool-1"))
    # PoolMixin.upsert_number_pool — _ensure_mlag_pairs calls this to create
    # each VLAN domain's own local VLAN ID pool; not under test here.
    gen.upsert_number_pool = AsyncMock(return_value=MagicMock(id="vlan-pool-1"))
    # No controllers by default — every existing test here expects normal
    # CoreStandardGroup behavior, not controller routing (see
    # TestCreateDevicesControllerRouting for the controller-routing path).
    gen._all_controllers = []
    return gen


def _mock_created_device(kind_name: str, name: str) -> MagicMock:
    device = MagicMock()
    device.hfid = name
    device.id = f"id-{name}"
    device.get_kind = Mock(return_value=kind_name)
    device.save = AsyncMock()
    return device


class TestDeviceMixinCreateDevices:
    @pytest.mark.asyncio
    async def test_uses_template_owner_when_no_owner_is_passed(self) -> None:
        """Existing callers that rely on the template owner should keep working."""

        gen = _make_generator()
        created_device = _mock_created_device(DcimPhysicalDevice.__name__, "dc1-fw-01")
        gen.client.create = AsyncMock(return_value=created_device)

        await gen.create_devices(
            device_role="firewall",
            quantity=1,
            deployment_id="dep-1",
            template={"device_type": {"id": "dt-1"}, "platform": {"name": "nxos"}, "owner": {"id": "owner-0"}},
        )

        create_kwargs = gen.client.create.call_args.kwargs
        assert create_kwargs["data"]["owner"] == {"id": "owner-0"}

    @pytest.mark.asyncio
    async def test_forwards_owner_for_physical_devices(self) -> None:
        """Physical device creation should include the supplied owner relationship."""

        gen = _make_generator()
        created_device = _mock_created_device(DcimPhysicalDevice.__name__, "dc1-fw-01")
        gen.client.create = AsyncMock(return_value=created_device)

        owner = MagicMock(id="owner-1")
        await gen.create_devices(
            device_role="firewall",
            quantity=1,
            deployment_id="dep-1",
            template={"device_type": {"id": "dt-1"}, "platform": {"name": "nxos"}},
            owner=owner,
        )

        create_kwargs = gen.client.create.call_args.kwargs
        assert create_kwargs["kind"] is DcimPhysicalDevice
        assert create_kwargs["data"]["owner"] == {"id": "owner-1"}
        assert "hosting_device" not in create_kwargs["data"]

    @pytest.mark.asyncio
    async def test_forwards_owner_and_hosting_device_for_virtual_devices(self) -> None:
        """Virtual device creation should include both owner and hosting device."""

        gen = _make_generator()
        created_device = _mock_created_device(DcimVirtualDevice.__name__, "dc1-vm-01")
        gen.client.create = AsyncMock(return_value=created_device)

        owner = MagicMock(id="owner-2")
        hosting_device = MagicMock(id="host-1")
        await gen.create_devices(
            device_role="appliance",
            quantity=1,
            deployment_id="dep-2",
            template={"device_type": {"id": "dt-2"}, "platform": {"name": "linux"}},
            options={"virtual": True},
            owner=owner,
            hosting_device=hosting_device,
        )

        create_kwargs = gen.client.create.call_args.kwargs
        assert create_kwargs["kind"] is DcimVirtualDevice
        assert create_kwargs["data"]["owner"] == {"id": "owner-2"}
        assert create_kwargs["data"]["hosting_device"] == {"id": "host-1"}

    @pytest.mark.asyncio
    async def test_name_override_bypasses_naming_convention(self) -> None:
        gen = _make_generator()
        created_device = _mock_created_device(DcimVirtualDevice.__name__, "fw-01-fw-02-shared-production-01")
        gen.client.create = AsyncMock(return_value=created_device)

        names = await gen.create_devices(
            device_role="firewall",
            quantity=1,
            deployment_id="dep-1",
            template={"device_type": {"id": "dt-1"}, "platform": {"name": "checkpoint_gaia"}},
            options={"virtual": True, "name_override": "fw-01-fw-02-shared-production-01"},
        )

        assert names == ["fw-01-fw-02-shared-production-01"]
        create_kwargs = gen.client.create.call_args.kwargs
        assert create_kwargs["data"]["name"] == "fw-01-fw-02-shared-production-01"

    @pytest.mark.asyncio
    async def test_logs_updated_not_created_for_pre_existing_device(self) -> None:
        """A device already present in existing_devices_map (upsert-by-id path)
        must log "Updated", not "Created" — re-running create_devices() is
        idempotent at the data level, and the log should say so instead of
        always claiming a fresh creation."""
        gen = _make_generator()
        existing_device = MagicMock(member_of_groups=MagicMock(peers=[]))
        existing_device.name.value = "dc1-fw-01"
        gen.client.filters = AsyncMock(return_value=[existing_device])
        created_device = _mock_created_device(DcimPhysicalDevice.__name__, "dc1-fw-01")
        created_device.name.value = "dc1-fw-01"
        gen.client.create = AsyncMock(return_value=created_device)

        await gen.create_devices(
            device_role="firewall",
            quantity=1,
            deployment_id="dep-1",
            template={"device_type": {"id": "dt-1"}, "platform": {"name": "nxos"}},
            options={"name_override": "dc1-fw-01"},
        )

        info_messages = [c.args[0] for c in gen.logger.info.call_args_list]
        assert any("Updated [" in m for m in info_messages)
        assert not any("Created [" in m for m in info_messages)

    @pytest.mark.asyncio
    async def test_name_override_rejects_quantity_other_than_one(self) -> None:
        gen = _make_generator()

        with pytest.raises(ValueError, match="name_override is only valid with quantity=1"):
            await gen.create_devices(
                device_role="firewall",
                quantity=2,
                deployment_id="dep-1",
                template={"device_type": {"id": "dt-1"}, "platform": {"name": "checkpoint_gaia"}},
                options={"virtual": True, "name_override": "fw-shared-production-01"},
            )


def _mock_device(name: str) -> MagicMock:
    dev = MagicMock()
    dev.id = f"id-{name}"
    dev.name = MagicMock(value=name)
    return dev


def _mock_group() -> MagicMock:
    group = MagicMock()
    group.id = "group-1"
    return group


class TestCreateDevicesPairingDispatch:
    """create_devices() pairs devices itself when the role/options call for it —
    firewall/load-balancer via DeviceOptions.ha_kind, leaf/tor/l2-leaf/access-leaf
    via DeviceOptions.mlag_create. Any quantity (not just 2) is paired two-at-a-
    time — see test_rack_mlag_pairs.py for the pairing algorithm itself; this
    covers create_devices()'s dispatch into it."""

    @pytest.mark.asyncio
    async def test_firewall_with_ha_kind_pairs_after_creation(self) -> None:
        gen = _make_generator()
        # HA sync interface/cable creation is covered by TestEnsureHaInterfaces
        # in test_device_mixin.py — stub it here, this test only checks that
        # create_devices() dispatches into HA domain creation at all.
        gen._ensure_ha_interfaces = AsyncMock()
        created = [_mock_created_device(DcimPhysicalDevice.__name__, n) for n in ("dc1-firewall-01", "dc1-firewall-02")]
        gen.client.create = AsyncMock(side_effect=[*created, MagicMock(id="ha-1", save=AsyncMock())])

        async def _filters(*, kind: Any, **kwargs: Any) -> list[Any]:
            if kind is DcimPhysicalDevice:
                return [_mock_device("dc1-firewall-01"), _mock_device("dc1-firewall-02")]
            return []  # no existing devices, no existing HA domain

        gen.client.filters = AsyncMock(side_effect=_filters)
        gen.client.get = AsyncMock(side_effect=[_mock_group(), _mock_group()])

        await gen.create_devices(
            device_role="firewall",
            quantity=2,
            deployment_id="dep-1",
            template={"device_type": {"id": "dt-1"}, "platform": {"name": "nxos"}},
            options={"ha_kind": "ManagedFirewallHA"},
        )

        last_create_kwargs = gen.client.create.call_args_list[-1].kwargs
        assert last_create_kwargs["kind"] == "ManagedFirewallHA"

    @pytest.mark.asyncio
    async def test_load_balancer_without_ha_kind_does_not_pair(self) -> None:
        gen = _make_generator()
        created = [_mock_created_device(DcimPhysicalDevice.__name__, "dc1-load-balancer-01")]
        gen.client.create = AsyncMock(side_effect=created)

        await gen.create_devices(
            device_role="load-balancer",
            quantity=1,
            deployment_id="dep-1",
            template={"device_type": {"id": "dt-1"}, "platform": {"name": "nxos"}},
        )

        gen.client.create.assert_called_once()  # only the device itself, no HA domain

    @pytest.mark.asyncio
    async def test_leaf_with_mlag_create_pairs_after_creation(self) -> None:
        gen = _make_generator()
        # naming_convention="flat" (the default) for fabric_name="dc1"/role="leaf" —
        # devices_by_name (built from these very objects) is keyed by these exact names.
        generated_names = ("dc1lf01", "dc1lf02")
        created = [_mock_created_device(DcimPhysicalDevice.__name__, n) for n in generated_names]
        for node, name in zip(created, generated_names):
            node.name = MagicMock(value=name)
        gen.client.create = AsyncMock(side_effect=[*created, MagicMock(id="mlag-1", save=AsyncMock())])
        gen.client.filters = AsyncMock(return_value=[])  # no existing devices, no existing MLAG domain
        gen.client.get = AsyncMock(side_effect=[_mock_group(), _mock_group()])
        # MLAGWiringMixin.ensure_mlag_wiring — peer-link wiring is covered by
        # tests/unit/test_mlag_wiring_helper.py; not under test here.
        gen.ensure_mlag_wiring = AsyncMock()

        await gen.create_devices(
            device_role="leaf",
            quantity=2,
            deployment_id="dep-1",
            template={"device_type": {"id": "dt-1"}, "platform": {"name": "nxos"}},
            options={"mlag_create": "virtual"},
        )

        last_create_kwargs = gen.client.create.call_args_list[-1].kwargs
        assert last_create_kwargs["kind"] == ManagedMLAG

    @pytest.mark.asyncio
    async def test_mlag_create_no_does_not_pair(self) -> None:
        gen = _make_generator()
        created = [_mock_created_device(DcimPhysicalDevice.__name__, "dc1-leaf-01")]
        gen.client.create = AsyncMock(side_effect=created)

        await gen.create_devices(
            device_role="leaf",
            quantity=1,
            deployment_id="dep-1",
            template={"device_type": {"id": "dt-1"}, "platform": {"name": "nxos"}},
            options={"mlag_create": "no"},
        )

        gen.client.create.assert_called_once()


class TestEnsureHaPairs:
    """DeviceMixin._ensure_ha_pairs — used by create_devices() for firewall/
    load-balancer roles via DeviceOptions.ha_kind."""

    def _gen(self) -> Any:
        gen = DeviceMixin.__new__(DeviceMixin)
        gen.logger = MagicMock()
        gen.client = MagicMock()
        gen.client.group_context = MagicMock()
        gen.client.group_context.related_node_ids = []
        gen.client.create = AsyncMock()
        gen.client.filters = AsyncMock(return_value=[])
        gen.client.get = AsyncMock()
        # _ensure_ha_interfaces (HA sync interface/cable creation) is covered
        # by its own TestEnsureHaInterfaces below — stub it here so these
        # tests only exercise _ensure_ha_pairs's own domain create/lookup.
        gen._ensure_ha_interfaces = AsyncMock()
        return gen

    @pytest.mark.asyncio
    async def test_single_device_is_a_noop(self) -> None:
        gen = self._gen()

        await gen._ensure_ha_pairs(["fw-01"], ha_kind="ManagedFirewallHA", role_label="firewall")

        gen.client.create.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_creates_domain_for_two_devices(self) -> None:
        gen = self._gen()
        gen.client.filters = AsyncMock(side_effect=[[], [_mock_device("fw-01"), _mock_device("fw-02")]])
        gen.client.get = AsyncMock(return_value=_mock_group())
        ha_obj = MagicMock()
        ha_obj.id = "ha-1"
        ha_obj.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=ha_obj)

        await gen._ensure_ha_pairs(["fw-02", "fw-01"], ha_kind="ManagedFirewallHA", role_label="firewall")

        gen.client.create.assert_awaited_once()
        create_kwargs = gen.client.create.call_args.kwargs
        assert create_kwargs["kind"] == "ManagedFirewallHA"
        assert create_kwargs["data"]["name"] == "fw-01-fw-02-ha"
        assert create_kwargs["data"]["capabilities"] == [{"id": "id-fw-01"}, {"id": "id-fw-02"}]
        ha_obj.save.assert_awaited_once_with(allow_upsert=True)

    @pytest.mark.asyncio
    async def test_fresh_domain_passes_member_ids_skipping_capabilities_fetch(self) -> None:
        """A just-created ha_obj's capabilities peers only carry the id each
        was created with, no __typename — RelatedNode.fetch() requires both
        (raises 'Unable to fetch the peer, id and/or typename are not
        defined' otherwise, caught live on a real run). _ensure_ha_pairs must
        pass the already-known device ids through as member_ids so
        _ensure_ha_interfaces never calls capabilities.fetch() on this path."""
        gen = self._gen()
        gen.client.filters = AsyncMock(side_effect=[[], [_mock_device("fw-01"), _mock_device("fw-02")]])
        gen.client.get = AsyncMock(return_value=_mock_group())
        ha_obj = MagicMock()
        ha_obj.id = "ha-1"
        ha_obj.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=ha_obj)

        await gen._ensure_ha_pairs(["fw-02", "fw-01"], ha_kind="ManagedFirewallHA", role_label="firewall")

        gen._ensure_ha_interfaces.assert_awaited_once_with(
            ha_obj, "fw-01-fw-02-ha", device_kind=DcimPhysicalDevice, member_ids=["id-fw-01", "id-fw-02"]
        )

    @pytest.mark.asyncio
    async def test_existing_domain_passes_no_member_ids_so_capabilities_is_fetched(self) -> None:
        """An existing ha_obj came from client.filters(..., include=[
        "capabilities"]) — real server-side typenames, so letting
        _ensure_ha_interfaces fetch() it itself is safe and correct (also
        covers domains whose membership changed since creation)."""
        gen = self._gen()
        existing = MagicMock(id="existing-ha-1")
        gen.client.filters = AsyncMock(return_value=[existing])

        await gen._ensure_ha_pairs(["fw-01", "fw-02"], ha_kind="ManagedFirewallHA", role_label="firewall")

        gen._ensure_ha_interfaces.assert_awaited_once_with(
            existing, "fw-01-fw-02-ha", device_kind=DcimPhysicalDevice, member_ids=None
        )

    @pytest.mark.asyncio
    async def test_device_kind_defaults_to_physical_but_can_be_overridden(self) -> None:
        """dc.py's shared virtual production/non-production instances pass
        device_kind=DcimVirtualDevice — pair resolution must query that kind,
        not the default DcimPhysicalDevice."""
        gen = self._gen()
        gen.client.filters = AsyncMock(side_effect=[[], [_mock_device("vfw-01"), _mock_device("vfw-02")]])
        gen.client.get = AsyncMock(return_value=_mock_group())
        ha_obj = MagicMock()
        ha_obj.id = "ha-1"
        ha_obj.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=ha_obj)

        await gen._ensure_ha_pairs(
            ["vfw-02", "vfw-01"], ha_kind="ManagedFirewallHA", role_label="firewall", device_kind=DcimVirtualDevice
        )

        pair_lookup_kwargs = gen.client.filters.call_args_list[-1].kwargs
        assert pair_lookup_kwargs["kind"] is DcimVirtualDevice

    @pytest.mark.asyncio
    async def test_pairs_two_at_a_time_for_larger_even_counts(self) -> None:
        """quantity=4/6 must pair all of them, not just the first two."""
        gen = self._gen()
        gen.client.filters = AsyncMock(
            side_effect=[
                [],
                [_mock_device("fw-01"), _mock_device("fw-02")],
                [],
                [_mock_device("fw-03"), _mock_device("fw-04")],
            ]
        )
        gen.client.get = AsyncMock(return_value=_mock_group())
        ha_obj = MagicMock()
        ha_obj.id = "ha-1"
        ha_obj.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=ha_obj)

        await gen._ensure_ha_pairs(
            ["fw-01", "fw-02", "fw-03", "fw-04"], ha_kind="ManagedFirewallHA", role_label="firewall"
        )

        assert gen.client.create.await_count == 2

    @pytest.mark.asyncio
    async def test_existing_domain_is_tracked_not_recreated(self) -> None:
        gen = self._gen()
        existing = MagicMock()
        existing.id = "existing-ha-1"
        gen.client.filters = AsyncMock(return_value=[existing])

        await gen._ensure_ha_pairs(["lb-01", "lb-02"], ha_kind="ManagedLoadbalancerHA", role_label="load-balancer")

        gen.client.create.assert_not_awaited()
        assert "existing-ha-1" in gen.client.group_context.related_node_ids

    @pytest.mark.asyncio
    async def test_unresolvable_devices_errors(self) -> None:
        gen = self._gen()
        gen.client.filters = AsyncMock(side_effect=[[], [_mock_device("fw-01")]])

        await gen._ensure_ha_pairs(["fw-01", "fw-02"], ha_kind="ManagedFirewallHA", role_label="firewall")

        gen.client.create.assert_not_awaited()
        gen.logger.error.assert_called_once()

    @pytest.mark.asyncio
    async def test_tenant_id_sets_tenant_on_created_ha_domain(self) -> None:
        """tenant_id (set by a dedicated customer LB/FW pair) is sent as
        ManagedTenantScoped.tenant on the created HA domain."""
        gen = self._gen()
        gen.client.filters = AsyncMock(side_effect=[[], [_mock_device("lb-01"), _mock_device("lb-02")]])
        gen.client.get = AsyncMock(return_value=_mock_group())
        ha_obj = MagicMock()
        ha_obj.id = "ha-1"
        ha_obj.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=ha_obj)

        await gen._ensure_ha_pairs(
            ["lb-02", "lb-01"],
            ha_kind="ManagedLoadbalancerHA",
            role_label="load-balancer (dedicated C005)",
            device_kind=DcimVirtualDevice,
            tenant_id="cust-1",
        )

        create_kwargs = gen.client.create.call_args.kwargs
        assert create_kwargs["data"]["tenant"] == {"id": "cust-1"}

    @pytest.mark.asyncio
    async def test_no_tenant_id_omits_tenant_from_data(self) -> None:
        """Shared (non-dedicated) HA pairs never send a tenant field."""
        gen = self._gen()
        gen.client.filters = AsyncMock(side_effect=[[], [_mock_device("fw-01"), _mock_device("fw-02")]])
        gen.client.get = AsyncMock(return_value=_mock_group())
        ha_obj = MagicMock()
        ha_obj.id = "ha-1"
        ha_obj.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=ha_obj)

        await gen._ensure_ha_pairs(["fw-02", "fw-01"], ha_kind="ManagedFirewallHA", role_label="firewall")

        create_kwargs = gen.client.create.call_args.kwargs
        assert "tenant" not in create_kwargs["data"]


def _mock_relmgr(peer_ids: list[str]) -> MagicMock:
    """A cardinality-many RelationshipManager stub — .fetch() is a no-op,
    .peers is pre-populated (mirrors client.create()'s own client-side
    initialization from the data dict passed to it, no server round-trip
    needed to see the ids back)."""
    rel = MagicMock()
    rel.fetch = AsyncMock()
    rel.peers = [MagicMock(id=pid) for pid in peer_ids]
    return rel


def _mock_iface(iface_id: str, name: str, *, cable_id: str | None = None, status: str = "free") -> MagicMock:
    iface = MagicMock()
    iface.id = iface_id
    iface.name = MagicMock(value=name)
    # `free` is DcimInterface's schema default, so that is what a sync port
    # inherited from a device's object_template really looks like before
    # _ensure_ha_interfaces flips it.
    iface.status = MagicMock(value=status)
    iface.save = AsyncMock()
    cable = MagicMock()
    cable.initialized = cable_id is not None
    if cable_id is not None:
        cable.id = cable_id
    iface.cable = cable
    return iface


class TestEnsureHaInterfaces:
    """DeviceMixin._ensure_ha_interfaces / _ensure_ha_cable — replaces the old
    standalone add_ha generator (generators/topology/ha.py, deleted). Called
    synchronously from _ensure_ha_pairs right after a domain is created or
    found, so there's no separate trigger-based dispatch to race on."""

    def _gen(self) -> Any:
        gen = DeviceMixin.__new__(DeviceMixin)
        gen.logger = MagicMock()
        gen.client = MagicMock()
        gen.client.filters = AsyncMock(return_value=[])
        gen.client.create = AsyncMock()
        gen.client.get = AsyncMock()
        gen.client.group_context = MagicMock()
        gen.client.group_context.related_node_ids = []
        return gen

    @pytest.mark.asyncio
    async def test_no_members_is_a_noop(self) -> None:
        gen = self._gen()
        ha_obj = MagicMock(id="ha-1", capabilities=_mock_relmgr([]))

        await gen._ensure_ha_interfaces(ha_obj, "fw-01-fw-02-ha")

        gen.client.filters.assert_not_called()

    @pytest.mark.asyncio
    async def test_member_ids_param_skips_capabilities_fetch(self) -> None:
        """Passing member_ids (the fresh-domain path in _ensure_ha_pairs)
        must never touch ha_obj.capabilities.fetch() — a just-created ha_obj's
        capabilities peers have no __typename, so fetch() would raise 'Unable
        to fetch the peer, id and/or typename are not defined' (caught live
        on a real add_dc run before this param existed)."""
        gen = self._gen()
        caps = _mock_relmgr([])
        caps.fetch = AsyncMock(side_effect=AssertionError("capabilities.fetch() must not be called"))
        ha_obj = MagicMock(id="ha-1", capabilities=caps)
        dev_1 = MagicMock(id="dev-1", deployment=MagicMock(initialized=False))
        dev_1.name = MagicMock(value="fw-01")
        dev_2 = MagicMock(id="dev-2", deployment=MagicMock(initialized=False))
        dev_2.name = MagicMock(value="fw-02")
        iface_1 = _mock_iface("iface-1", "sync0")
        iface_2 = _mock_iface("iface-2", "sync0")

        async def _filters(*, kind: Any, **kwargs: Any) -> list[Any]:
            if kind is DcimPhysicalDevice:
                return [dev_1, dev_2]
            if kind is DcimPhysicalInterface:
                return [iface_1] if kwargs.get("device__ids") == ["dev-1"] else [iface_2]
            return []

        gen.client.filters = AsyncMock(side_effect=_filters)
        gen.client.create = AsyncMock(return_value=MagicMock(save=AsyncMock()))

        await gen._ensure_ha_interfaces(ha_obj, "fw-01-fw-02-ha", member_ids=["dev-1", "dev-2"])

        caps.fetch.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_creates_interface_capability_for_each_member_with_existing_sync_iface(self) -> None:
        """Physical devices already have a role=ha interface from their
        template — no on-demand creation, just wire ManagedHAInterface."""
        gen = self._gen()
        ha_obj = MagicMock(id="ha-1", capabilities=_mock_relmgr(["dev-1", "dev-2"]))
        dev_1 = MagicMock(id="dev-1", deployment=MagicMock(initialized=False))
        dev_1.name = MagicMock(value="fw-01")
        dev_2 = MagicMock(id="dev-2", deployment=MagicMock(initialized=False))
        dev_2.name = MagicMock(value="fw-02")
        iface_1 = _mock_iface("iface-1", "sync0")
        iface_2 = _mock_iface("iface-2", "sync0")

        async def _filters(*, kind: Any, **kwargs: Any) -> list[Any]:
            if kind is DcimPhysicalDevice:
                return [dev_1, dev_2]
            if kind is ManagedHAInterface:
                return []  # no existing HAInterface nodes
            if kind is DcimPhysicalInterface:
                return [iface_1] if kwargs.get("device__ids") == ["dev-1"] else [iface_2]
            return []

        gen.client.filters = AsyncMock(side_effect=_filters)
        created_ha_iface = MagicMock(save=AsyncMock())
        gen.client.create = AsyncMock(return_value=created_ha_iface)

        await gen._ensure_ha_interfaces(ha_obj, "fw-01-fw-02-ha")

        ha_iface_calls = [c for c in gen.client.create.call_args_list if c.kwargs["kind"] is ManagedHAInterface]
        assert len(ha_iface_calls) == 2
        assert ha_iface_calls[0].kwargs["data"]["name"] == "fw-01-HA-SYNC"
        assert ha_iface_calls[0].kwargs["data"]["interface_capabilities"] == [{"id": "iface-1"}]

    @pytest.mark.asyncio
    async def test_existing_sync_iface_membership_skips_create(self) -> None:
        """An interface already wired to a ManagedHAInterface in this domain
        must not get a second ManagedHAInterface created for it."""
        gen = self._gen()
        ha_obj = MagicMock(id="ha-1", capabilities=_mock_relmgr(["dev-1", "dev-2"]))
        dev_1 = MagicMock(id="dev-1", deployment=MagicMock(initialized=False))
        dev_1.name = MagicMock(value="fw-01")
        dev_2 = MagicMock(id="dev-2", deployment=MagicMock(initialized=False))
        dev_2.name = MagicMock(value="fw-02")
        iface_1 = _mock_iface("iface-1", "sync0")
        iface_2 = _mock_iface("iface-2", "sync0")
        existing_ha_iface = MagicMock(
            id="existing-ha-iface", interface_capabilities=_mock_relmgr(["iface-1", "iface-2"])
        )

        async def _filters(*, kind: Any, **kwargs: Any) -> list[Any]:
            if kind is DcimPhysicalDevice:
                return [dev_1, dev_2]
            if kind is ManagedHAInterface:
                return [existing_ha_iface]
            if kind is DcimPhysicalInterface:
                return [iface_1] if kwargs.get("device__ids") == ["dev-1"] else [iface_2]
            return []

        gen.client.filters = AsyncMock(side_effect=_filters)

        await gen._ensure_ha_interfaces(ha_obj, "fw-01-fw-02-ha")

        ha_iface_calls = [c for c in gen.client.create.call_args_list if c.kwargs["kind"] is ManagedHAInterface]
        assert len(ha_iface_calls) == 0
        # ... and it must stay in the run's tracking group, or the next run
        # deletes it as unused — same trap as the sync cable, see
        # TestEnsureHaCable.
        assert gen.client.group_context.related_node_ids == ["existing-ha-iface"]
        # The status flip still happens on this path: a pair built before the
        # flip existed has cabled-but-`free` sync ports, and re-running the
        # generator is what repairs them.
        assert iface_1.status.value == "active"
        assert iface_2.status.value == "active"

    @pytest.mark.asyncio
    async def test_sync_ports_are_flipped_to_active(self) -> None:
        """A port carrying the HA sync link must not keep DcimInterface's
        `free` default — it is in service, exactly like both ends of any cable
        create_connections() lays. Caught live on a colocation metro, where
        fw-fr01:HA1 sat at `free` while cabled to fw-fr02:HA1."""
        gen = self._gen()
        ha_obj = MagicMock(id="ha-1", capabilities=_mock_relmgr(["dev-1", "dev-2"]))
        dev_1 = MagicMock(id="dev-1", deployment=MagicMock(initialized=False))
        dev_1.name = MagicMock(value="fw-01")
        dev_2 = MagicMock(id="dev-2", deployment=MagicMock(initialized=False))
        dev_2.name = MagicMock(value="fw-02")
        iface_1 = _mock_iface("iface-1", "HA1")
        # Already active — a re-run must not spend a write reaffirming it.
        iface_2 = _mock_iface("iface-2", "HA1", status="active")

        async def _filters(*, kind: Any, **kwargs: Any) -> list[Any]:
            if kind is DcimPhysicalDevice:
                return [dev_1, dev_2]
            if kind is DcimPhysicalInterface:
                return [iface_1] if kwargs.get("device__ids") == ["dev-1"] else [iface_2]
            return []

        gen.client.filters = AsyncMock(side_effect=_filters)
        gen.client.create = AsyncMock(return_value=MagicMock(save=AsyncMock()))

        await gen._ensure_ha_interfaces(ha_obj, "fw-01-fw-02-ha")

        assert iface_1.status.value == "active"
        # update_group_context=False: the port belongs to the device's
        # object_template, so it must never be a delete_unused_nodes candidate.
        iface_1.save.assert_awaited_once_with(allow_upsert=True, update_group_context=False)
        iface_2.save.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_sync_iface_on_physical_device_logs_error(self) -> None:
        gen = self._gen()
        ha_obj = MagicMock(id="ha-1", capabilities=_mock_relmgr(["dev-1", "dev-2"]))
        dev_1 = MagicMock(id="dev-1", deployment=MagicMock(initialized=False))
        dev_1.name = MagicMock(value="fw-01")
        dev_2 = MagicMock(id="dev-2", deployment=MagicMock(initialized=False))
        dev_2.name = MagicMock(value="fw-02")

        async def _filters(*, kind: Any, **kwargs: Any) -> list[Any]:
            if kind is DcimPhysicalDevice:
                return [dev_1, dev_2]
            return []  # no HA sync interface found for either device

        gen.client.filters = AsyncMock(side_effect=_filters)

        await gen._ensure_ha_interfaces(ha_obj, "fw-01-fw-02-ha")

        assert gen.logger.error.call_count == 2
        gen.client.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_virtual_device_with_no_sync_iface_creates_eth7_on_demand(self) -> None:
        gen = self._gen()
        ha_obj = MagicMock(id="ha-1", capabilities=_mock_relmgr(["dev-1", "dev-2"]))
        dev_1 = MagicMock(id="dev-1", deployment=MagicMock(initialized=False))
        dev_1.name = MagicMock(value="vfw-01")
        dev_2 = MagicMock(id="dev-2", deployment=MagicMock(initialized=False))
        dev_2.name = MagicMock(value="vfw-02")

        created_sync_iface = MagicMock(id="new-iface", save=AsyncMock())
        created_sync_iface.name = MagicMock(value="eth7")

        async def _filters(*, kind: Any, **kwargs: Any) -> list[Any]:
            if kind is DcimVirtualDevice:
                return [dev_1, dev_2]
            return []  # no existing sync interface, no existing HAInterface

        gen.client.filters = AsyncMock(side_effect=_filters)
        gen.client.create = AsyncMock(
            side_effect=[
                created_sync_iface,
                MagicMock(save=AsyncMock()),
                created_sync_iface,
                MagicMock(save=AsyncMock()),
            ]
        )

        await gen._ensure_ha_interfaces(ha_obj, "vfw-01-vfw-02-ha", device_kind=DcimVirtualDevice)

        virtual_iface_calls = [c for c in gen.client.create.call_args_list if c.kwargs["kind"] is DcimVirtualInterface]
        assert len(virtual_iface_calls) == 2
        assert virtual_iface_calls[0].kwargs["data"]["name"] == "eth7"
        assert virtual_iface_calls[0].kwargs["data"]["role"] == "ha"

    @pytest.mark.asyncio
    async def test_virtual_device_with_physical_eth7_reuses_it_no_duplicate(self) -> None:
        """Most virtual firewall/LB templates (CloudGuard/PANOS/NetScaler/etc,
        every template except *_CUSTOMER_*) provision eth7 as a
        TemplateDcimPhysicalInterface even on a virtual device. The lookup
        must find it via the generic DcimInterface kind and reuse it —
        querying DcimVirtualInterface alone would miss it and attempt to
        create a colliding duplicate eth7 (live NODE_NOT_FOUND/uniqueness
        failure on add_dc, since Interface's uniqueness_constraints span
        both Physical and Virtual subtypes)."""
        gen = self._gen()
        ha_obj = MagicMock(id="ha-1", capabilities=_mock_relmgr(["dev-1", "dev-2"]))
        dev_1 = MagicMock(id="dev-1", deployment=MagicMock(initialized=False))
        dev_1.name = MagicMock(value="vfw-01")
        dev_2 = MagicMock(id="dev-2", deployment=MagicMock(initialized=False))
        dev_2.name = MagicMock(value="vfw-02")
        iface_1 = _mock_iface("iface-1", "eth7")
        iface_2 = _mock_iface("iface-2", "eth7")

        async def _filters(*, kind: Any, **kwargs: Any) -> list[Any]:
            if kind is DcimVirtualDevice:
                return [dev_1, dev_2]
            if kind is DcimInterface:
                return [iface_1] if kwargs.get("device__ids") == ["dev-1"] else [iface_2]
            return []  # no existing HAInterface; DcimVirtualInterface must not be queried

        gen.client.filters = AsyncMock(side_effect=_filters)
        created_ha_iface = MagicMock(save=AsyncMock())
        gen.client.create = AsyncMock(return_value=created_ha_iface)

        await gen._ensure_ha_interfaces(ha_obj, "vfw-01-vfw-02-ha", device_kind=DcimVirtualDevice)

        virtual_iface_calls = [c for c in gen.client.create.call_args_list if c.kwargs["kind"] is DcimVirtualInterface]
        assert len(virtual_iface_calls) == 0
        ha_iface_calls = [c for c in gen.client.create.call_args_list if c.kwargs["kind"] is ManagedHAInterface]
        assert len(ha_iface_calls) == 2
        assert ha_iface_calls[0].kwargs["data"]["interface_capabilities"] == [{"id": "iface-1"}]

    @pytest.mark.asyncio
    async def test_virtual_pairs_never_create_a_cable(self) -> None:
        """No physical cabling exists between hosted virtual instances —
        _ensure_ha_cable must never be reached for device_kind=DcimVirtualDevice."""
        gen = self._gen()
        ha_obj = MagicMock(id="ha-1", capabilities=_mock_relmgr(["dev-1", "dev-2"]))
        dev_1 = MagicMock(id="dev-1", deployment=MagicMock(initialized=False))
        dev_1.name = MagicMock(value="vfw-01")
        dev_2 = MagicMock(id="dev-2", deployment=MagicMock(initialized=False))
        dev_2.name = MagicMock(value="vfw-02")
        iface_1 = _mock_iface("iface-1", "eth7")
        iface_2 = _mock_iface("iface-2", "eth7")

        async def _filters(*, kind: Any, **kwargs: Any) -> list[Any]:
            if kind is DcimVirtualDevice:
                return [dev_1, dev_2]
            if kind is DcimVirtualInterface:
                return [iface_1] if kwargs.get("device__ids") == ["dev-1"] else [iface_2]
            return []

        gen.client.filters = AsyncMock(side_effect=_filters)
        gen.client.create = AsyncMock(return_value=MagicMock(save=AsyncMock()))
        gen._ensure_ha_cable = AsyncMock()

        await gen._ensure_ha_interfaces(ha_obj, "vfw-01-vfw-02-ha", device_kind=DcimVirtualDevice)

        gen._ensure_ha_cable.assert_not_awaited()


class TestEnsureHaCable:
    def _gen(self) -> Any:
        gen = DeviceMixin.__new__(DeviceMixin)
        gen.logger = MagicMock()
        gen.client = MagicMock()
        gen.client.filters = AsyncMock(return_value=[])
        gen.client.create = AsyncMock()
        gen.client.group_context = MagicMock()
        gen.client.group_context.related_node_ids = []
        return gen

    @pytest.mark.asyncio
    async def test_existing_named_cable_skips_create_but_stays_tracked(self) -> None:
        """Skipping the create is only half of idempotency: the generator body
        runs inside client.start_tracking(delete_unused_nodes=True), so a cable
        created by an earlier run and left unregistered here is deleted as
        "no longer required" at the end of this one — caught live on a
        colocation metro, where the HA sync cable appeared and disappeared on
        alternate generator runs."""
        gen = self._gen()
        gen.client.filters = AsyncMock(return_value=[MagicMock(id="existing-cbl")])
        dev_1 = MagicMock()
        dev_1.name = MagicMock(value="fw-01")
        dev_2 = MagicMock()
        dev_2.name = MagicMock(value="fw-02")
        iface_1 = _mock_iface("iface-1", "sync0")
        iface_2 = _mock_iface("iface-2", "sync0")

        await gen._ensure_ha_cable("fw-01-fw-02-ha", [(dev_1, iface_1), (dev_2, iface_2)])

        gen.client.create.assert_not_called()
        assert gen.client.group_context.related_node_ids == ["existing-cbl"]

    @pytest.mark.asyncio
    async def test_orphan_cabled_interface_skips_create(self) -> None:
        """Either endpoint already has a real cable (just not under the
        expected name) — treat as already-cabled, don't create a duplicate."""
        gen = self._gen()
        dev_1 = MagicMock()
        dev_1.name = MagicMock(value="fw-01")
        dev_2 = MagicMock()
        dev_2.name = MagicMock(value="fw-02")
        iface_1 = _mock_iface("iface-1", "sync0", cable_id="orphan-cbl")
        iface_2 = _mock_iface("iface-2", "sync0")

        await gen._ensure_ha_cable("fw-01-fw-02-ha", [(dev_1, iface_1), (dev_2, iface_2)])

        gen.client.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_creates_new_cable_with_deployment(self) -> None:
        gen = self._gen()
        created = MagicMock(save=AsyncMock())
        gen.client.create = AsyncMock(return_value=created)
        dev_1 = MagicMock(deployment=MagicMock(initialized=True))
        dev_1.name = MagicMock(value="fw-01")
        dev_1.deployment.peer = MagicMock(id="dep-1")
        dev_2 = MagicMock()
        dev_2.name = MagicMock(value="fw-02")
        iface_1 = _mock_iface("iface-1", "sync0")
        iface_2 = _mock_iface("iface-2", "sync0")

        await gen._ensure_ha_cable("fw-01-fw-02-ha", [(dev_2, iface_2), (dev_1, iface_1)])

        call_kwargs = gen.client.create.call_args.kwargs
        assert call_kwargs["kind"] is DcimCable
        assert call_kwargs["data"]["name"] == "CBL-fw-01-fw-02-ha-SYNC"
        assert call_kwargs["data"]["endpoints"] == ["iface-1", "iface-2"]
        assert call_kwargs["data"]["deployment"] == {"id": "dep-1"}
        created.save.assert_awaited_once_with(allow_upsert=True)


def _controller_dict(controller_type: str, platform_id: str | None = None, id_: str = "ctrl-1") -> dict[str, Any]:
    """Shape clean_data() produces for one controllers() query edge — see
    queries/topology/add/dc.gql's fabric_controllers/security_manager_
    controllers/lb_manager_controllers aliases."""
    entry: dict[str, Any] = {"id": id_, "controller_type": controller_type}
    if platform_id is not None:
        entry["platform"] = {"id": platform_id}
    return entry


class TestResolveRoleController:
    """DeviceMixin._resolve_role_controller — synchronous, reads
    self._all_controllers (pre-fetched once per generator run by dc.py/
    pod.py/rack.py's generate(), merging their own query's role-bucketed
    controller aliases — no runtime query here)."""

    def _gen(self, controllers: list[dict[str, Any]] | None = None) -> Any:
        gen = DeviceMixin.__new__(DeviceMixin)
        gen.logger = MagicMock()
        if controllers is not None:
            gen._all_controllers = controllers
        return gen

    def test_no_controllers_set_returns_none(self) -> None:
        gen = self._gen()  # _all_controllers never set

        controller = gen._resolve_role_controller(device_role="firewall", template={})

        assert controller is None

    def test_role_with_no_controller_mapping_returns_none(self) -> None:
        gen = self._gen(controllers=[_controller_dict("aci_apic")])

        controller = gen._resolve_role_controller(device_role="endpoint", template={})

        assert controller is None

    def test_fabric_role_matches_by_controller_type_alone(self) -> None:
        apic = _controller_dict("aci_apic")
        gen = self._gen(controllers=[apic])

        controller = gen._resolve_role_controller(device_role="leaf", template={"platform": {"id": "plat-nxos"}})

        assert controller is apic

    def test_campus_role_matches_dna_center_by_controller_type_alone(self) -> None:
        dnac = _controller_dict("dna_center")
        gen = self._gen(controllers=[dnac])

        controller = gen._resolve_role_controller(device_role="access-switch", template={})

        assert controller is dnac

    def test_firewall_requires_platform_match_not_just_controller_type(self) -> None:
        # security_manager controller for a DIFFERENT platform (panos) — must
        # not match a checkpoint_gaia firewall despite the same controller_type.
        panorama = _controller_dict("security_manager", platform_id="plat-panos")
        gen = self._gen(controllers=[panorama])

        controller = gen._resolve_role_controller(
            device_role="firewall", template={"platform": {"id": "plat-checkpoint"}}
        )

        assert controller is None
        gen.logger.warning.assert_called_once()

    def test_firewall_matches_when_controller_type_and_platform_both_match(self) -> None:
        cp_sms = _controller_dict("security_manager", platform_id="plat-checkpoint")
        gen = self._gen(controllers=[cp_sms])

        controller = gen._resolve_role_controller(
            device_role="firewall", template={"platform": {"id": "plat-checkpoint"}}
        )

        assert controller is cp_sms

    def test_load_balancer_matches_lb_manager_type(self) -> None:
        big_iq = _controller_dict("lb_manager", platform_id="plat-f5")
        gen = self._gen(controllers=[big_iq])

        controller = gen._resolve_role_controller(device_role="load-balancer", template={"platform": {"id": "plat-f5"}})

        assert controller is big_iq

    def test_empty_controllers_list_returns_none_without_warning(self) -> None:
        """No controllers at all isn't controller-managed — shouldn't warn,
        that's just the normal fully_managed case."""
        gen = self._gen(controllers=[])

        controller = gen._resolve_role_controller(
            device_role="firewall", template={"platform": {"id": "plat-checkpoint"}}
        )

        assert controller is None
        gen.logger.warning.assert_not_called()


class TestCreateDevicesControllerRouting:
    """End-to-end: create_devices() itself skips group membership and adds
    to controller.managed_devices when a matching controller is resolved."""

    @pytest.mark.asyncio
    async def test_routes_to_controller_instead_of_group(self) -> None:
        gen = _make_generator()
        gen._all_controllers = [_controller_dict("security_manager", platform_id="plat-checkpoint", id_="cp-sms-1")]

        controller_obj = MagicMock()
        controller_obj.hfid = "cp-sms-1"
        controller_obj.managed_devices.fetch = AsyncMock()
        controller_obj.managed_devices.peers = []
        controller_obj.managed_devices.add = MagicMock()
        controller_obj.save = AsyncMock()
        gen.client.get = AsyncMock(return_value=controller_obj)

        created_device = _mock_created_device(DcimPhysicalDevice.__name__, "dc1-firewall-01")
        gen.client.create = AsyncMock(return_value=created_device)

        await gen.create_devices(
            device_role="firewall",
            quantity=1,
            deployment_id="dc-1",
            template={"device_type": {"id": "dt-1"}, "platform": {"id": "plat-checkpoint"}},
        )

        create_kwargs = gen.client.create.call_args.kwargs
        assert create_kwargs["data"]["member_of_groups"] == []
        controller_obj.managed_devices.add.assert_called_once_with(created_device)
        controller_obj.save.assert_awaited_once_with(allow_upsert=True)

    @pytest.mark.asyncio
    async def test_no_matching_controller_falls_back_to_group(self) -> None:
        gen = _make_generator()  # _all_controllers defaults to []
        created_device = _mock_created_device(DcimPhysicalDevice.__name__, "dc1-firewall-01")
        gen.client.create = AsyncMock(return_value=created_device)

        await gen.create_devices(
            device_role="firewall",
            quantity=1,
            deployment_id="dc-1",
            template={"device_type": {"id": "dt-1"}, "platform": {"id": "plat-checkpoint"}},
        )

        create_kwargs = gen.client.create.call_args.kwargs
        assert create_kwargs["data"]["member_of_groups"] == [{"id": "group-1"}]
