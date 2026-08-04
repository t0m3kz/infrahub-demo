"""Unit tests for DeviceMixin.create_devices()."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from generators.devices import DeviceMixin
from generators.protocols import DcimPhysicalDevice, DcimVirtualDevice, ManagedMLAG


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
        created = [_mock_created_device(DcimPhysicalDevice.__name__, n) for n in ("dc1-firewall-01", "dc1-firewall-02")]
        gen.client.create = AsyncMock(side_effect=[*created, MagicMock(id="ha-1", save=AsyncMock())])

        async def _filters(*, kind: Any, **kwargs: Any) -> list[Any]:
            if kind is DcimPhysicalDevice:
                return [_mock_device("dc1-firewall-01"), _mock_device("dc1-firewall-02")]
            return []  # no existing devices, no existing HA domain

        gen.client.filters = AsyncMock(side_effect=_filters)
        gen.client.get = AsyncMock(side_effect=[MagicMock(id="group-1"), _mock_group()])

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
        created = [_mock_created_device(DcimPhysicalDevice.__name__, n) for n in ("dc1-leaf-01", "dc1-leaf-02")]
        gen.client.create = AsyncMock(side_effect=[*created, MagicMock(id="mlag-1", save=AsyncMock())])

        async def _filters(*, kind: Any, **kwargs: Any) -> list[Any]:
            if kind is DcimPhysicalDevice:
                return [_mock_device("dc1-leaf-01"), _mock_device("dc1-leaf-02")]
            return []  # no existing devices, no existing MLAG domain

        gen.client.filters = AsyncMock(side_effect=_filters)
        gen.client.get = AsyncMock(side_effect=[MagicMock(id="group-1"), _mock_group()])

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
