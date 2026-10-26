"""Unit tests for DeviceMixin.create_devices() and its pairing dispatch."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from infrahub_sdk.exceptions import NodeNotFoundError
from infrahub_sdk.protocols import CoreStandardGroup

from generators.protocols import (
    CloudNetworkAppliance,
    DcimPhysicalDevice,
    DcimVirtualDevice,
    ManagedMLAG,
)
from tests.unit.device_mixin_helpers import (
    _DummyBatch,
    _make_generator,
    _mock_created_device,
    _mock_device,
    _mock_group,
)


class TestDeviceMixinCreateDevices:
    @pytest.mark.asyncio
    async def test_role_index_start_continues_the_numbering(self) -> None:
        """A role split over several fabric_templates entries: the second entry's devices
        continue at role_index_start instead of re-creating the first entry's names."""
        gen = _make_generator()
        gen.client.create = AsyncMock(
            side_effect=lambda **kw: _mock_created_device(kw["kind"].__name__, kw["data"]["name"])
        )

        first = await gen.create_devices(
            device_role="spine", quantity=2, deployment_id="dep-1", template={}, options={"indexes": [1, 1]}
        )
        gen.client.create_batch = AsyncMock(side_effect=[_DummyBatch(), _DummyBatch()])
        second = await gen.create_devices(
            device_role="spine",
            quantity=2,
            deployment_id="dep-1",
            template={},
            options={"indexes": [1, 1], "role_index_start": 3},
        )

        assert first == ["dc111sp01", "dc111sp02"]
        assert second == ["dc111sp03", "dc111sp04"]

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
        assert create_kwargs["kind"] is DcimPhysicalDevice
        assert "hosting_device" not in create_kwargs["data"]

    @pytest.mark.asyncio
    async def test_forwards_owner_and_hosting_device_for_virtual_devices(self) -> None:
        """Virtual device creation should include both owner and hosting device."""

        gen = _make_generator()
        created_device = _mock_created_device(DcimVirtualDevice.__name__, "dc1-vm-01")
        gen.client.create = AsyncMock(return_value=created_device)
        gen._pin_virtual_nics = AsyncMock()

        hosting_device = MagicMock(id="host-1")
        await gen.create_devices(
            device_role="appliance",
            quantity=1,
            deployment_id="dep-2",
            template={"device_type": {"id": "dt-2"}, "platform": {"name": "linux"}, "owner": {"id": "owner-2"}},
            options={"virtual": True},
            hosting_device=hosting_device,
        )

        create_kwargs = gen.client.create.call_args.kwargs
        assert create_kwargs["kind"] is DcimVirtualDevice
        assert create_kwargs["data"]["owner"] == {"id": "owner-2"}
        assert create_kwargs["data"]["hosting_device"] == {"id": "host-1"}
        gen._pin_virtual_nics.assert_awaited_once_with([created_device], "host-1")

    @pytest.mark.asyncio
    async def test_appliance_option_creates_provider_hosted_appliances(self) -> None:
        """options["appliance"] selects CloudNetworkAppliance: no hosting device, no NIC pinning, no cabling."""

        gen = _make_generator()
        created_device = _mock_created_device(CloudNetworkAppliance.__name__, "eg-am-ne01")
        gen.client.create = AsyncMock(return_value=created_device)
        gen._pin_virtual_nics = AsyncMock()

        names = await gen.create_devices(
            device_role="edge",
            quantity=1,
            deployment_id="vc-1",
            template={"id": "tpl-1", "device_type": {"id": "dt-1"}, "platform": {"name": "cisco_xe"}, "owner": "p027"},
            options={"appliance": True, "indexes": []},
            hosting_device=MagicMock(id="host-1"),
        )

        assert len(names) == 1
        create_kwargs = gen.client.create.call_args.kwargs
        assert create_kwargs["kind"] is CloudNetworkAppliance
        assert create_kwargs["data"]["deployment"] == {"id": "vc-1"}
        assert create_kwargs["data"]["object_template"] == {"id": "tpl-1"}
        gen._pin_virtual_nics.assert_not_awaited()
        assert gen.client.filters.await_args_list[0].kwargs["kind"] is CloudNetworkAppliance

    @pytest.mark.asyncio
    async def test_appliance_wins_over_virtual_and_physical_is_the_default(self) -> None:
        for options, expected in (
            ({"appliance": True, "virtual": True}, CloudNetworkAppliance),
            ({"virtual": True}, DcimVirtualDevice),
            ({}, DcimPhysicalDevice),
        ):
            gen = _make_generator()
            gen.client.create = AsyncMock(return_value=_mock_created_device(expected.__name__, "x-01"))

            await gen.create_devices(
                device_role="firewall",
                quantity=1,
                deployment_id="dep-1",
                template={"device_type": {"id": "dt-1"}, "platform": {"name": "linux"}},
                options={**options, "name_override": "x-01"},  # type: ignore[typeddict-item]
            )

            assert gen.client.create.call_args.kwargs["kind"] is expected

    @pytest.mark.asyncio
    async def test_name_override_bypasses_naming_convention(self) -> None:
        gen = _make_generator()
        created_device = _mock_created_device(DcimVirtualDevice.__name__, "dc1-fw-01-C001-p-dedicated")
        gen.client.create = AsyncMock(return_value=created_device)

        names = await gen.create_devices(
            device_role="firewall",
            quantity=1,
            deployment_id="dep-1",
            template={"device_type": {"id": "dt-1"}, "platform": {"name": "checkpoint_gaia"}},
            options={"virtual": True, "name_override": "dc1-fw-01-C001-p-dedicated"},
        )

        assert names == ["dc1-fw-01-C001-p-dedicated"]
        create_kwargs = gen.client.create.call_args.kwargs
        assert create_kwargs["data"]["name"] == "dc1-fw-01-C001-p-dedicated"

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
                options={"virtual": True, "name_override": "dc1-fw-01-C001-p-dedicated"},
            )


class TestCreateDevicesPairingDispatch:
    """create_devices() pairs devices itself when the role/options call for it —
    firewall/load-balancer via DeviceOptions.ha_kind, leaf/tor/l2-leaf/access-leaf
    via DeviceOptions.mlag_create. Any quantity (not just 2) is paired two-at-a-
    time — see test_rack_mlag_pairs.py for the pairing algorithm itself; this
    covers create_devices()'s dispatch into it."""

    @pytest.mark.asyncio
    async def test_an_edge_is_never_paired_into_an_ha_domain(self) -> None:
        """An edge router is joined by its interlink (physical) or stands alone (provider-hosted), whatever ha_kind says."""
        gen = _make_generator()
        gen._ensure_ha_pairs = AsyncMock()
        gen.client.create = AsyncMock(side_effect=lambda **kw: _mock_created_device(kw["kind"].__name__, "x"))

        await gen.create_devices(
            device_role="edge",
            quantity=2,
            deployment_id="dep-1",
            template={"device_type": {"id": "dt-1"}, "platform": {"name": "cisco_xe"}},
            options={"indexes": [], "ha_kind": "ManagedFirewallHA"},
        )

        gen._ensure_ha_pairs.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_appliance_account_is_sent_and_cleared_with_the_presence_account(self) -> None:
        for account_id, expected in (("acct-1", {"id": "acct-1"}), (None, None)):
            gen = _make_generator()
            gen.client.create = AsyncMock(return_value=_mock_created_device(CloudNetworkAppliance.__name__, "eg-01"))
            options: Any = {"appliance": True, "name_override": "eg-01"}
            if account_id:
                options["account_id"] = account_id

            await gen.create_devices(
                device_role="edge",
                quantity=1,
                deployment_id="vc-1",
                template={"device_type": {"id": "dt-1"}, "platform": {"name": "cisco_xe"}},
                options=options,
            )

            assert gen.client.create.call_args.kwargs["data"]["account"] == expected

    @pytest.mark.asyncio
    async def test_a_device_that_is_not_an_appliance_never_gets_an_account(self) -> None:
        gen = _make_generator()
        gen.client.create = AsyncMock(return_value=_mock_created_device(DcimPhysicalDevice.__name__, "sw-01"))

        await gen.create_devices(
            device_role="firewall",
            quantity=1,
            deployment_id="dep-1",
            template={"device_type": {"id": "dt-1"}, "platform": {"name": "nxos"}},
            options={"name_override": "sw-01", "account_id": "acct-1"},
        )

        assert "account" not in gen.client.create.call_args.kwargs["data"]

    @pytest.mark.asyncio
    async def test_firewall_with_ha_kind_pairs_after_creation(self) -> None:
        gen = _make_generator()
        # HA sync interface/cable creation is covered by TestEnsureHaInterfaces
        # in test_device_mixin_ha.py — stub it here, this test only checks that
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
        """No MLAG domain, and no record for the unpaired leaf: it is its own VLAN domain."""
        gen = _make_generator()
        gen.client.create = AsyncMock(side_effect=[_mock_created_device(DcimPhysicalDevice.__name__, "dc1-leaf-01")])

        await gen.create_devices(
            device_role="leaf",
            quantity=1,
            deployment_id="dep-1",
            template={"device_type": {"id": "dt-1"}, "platform": {"name": "nxos"}},
            options={"mlag_create": "no"},
        )

        kinds = [call.kwargs["kind"] for call in gen.client.create.call_args_list]
        assert kinds == [DcimPhysicalDevice]


class TestRoleGroupLookup:
    """create_devices() reads a role's CoreStandardGroup once per run, however many entries use the role."""

    @staticmethod
    async def _create_twice(gen: Any) -> None:
        """Two fabric_templates entries of one role: two create_devices() calls on the same generator."""
        gen.client.create = AsyncMock(
            side_effect=lambda **kw: _mock_created_device(kw["kind"].__name__, kw["data"]["name"])
        )
        for first_index in (1, 2):
            gen.client.create_batch = AsyncMock(side_effect=[_DummyBatch(), _DummyBatch()])
            await gen.create_devices(
                device_role="spine",
                quantity=1,
                deployment_id="dep-1",
                template={},
                options={"indexes": [1, 1], "role_index_start": first_index},
            )

    @pytest.mark.asyncio
    async def test_group_is_read_once_for_repeated_calls(self) -> None:
        """The second call for the same role finds the group in the run's memo: one get, not two."""
        gen = _make_generator()

        await self._create_twice(gen)

        group_gets = [c for c in gen.client.get.await_args_list if c.kwargs["name__value"] == "spines"]
        assert len(group_gets) == 1

    @pytest.mark.asyncio
    async def test_missing_group_is_created_untracked_once(self) -> None:
        """A branch without the bootstrap group gets it created (untracked) on the first call and reuses it after."""
        gen = _make_generator()
        group = MagicMock(id="group-new", save=AsyncMock())
        gen.client.get = AsyncMock(side_effect=NodeNotFoundError(identifier={"name": ["spines"]}, node_type="x"))

        async def _create(**kw: Any) -> Any:
            if kw["kind"] is CoreStandardGroup:
                return group
            return _mock_created_device(kw["kind"].__name__, kw["data"]["name"])

        gen.client.create = AsyncMock(side_effect=_create)
        for first_index in (1, 2):
            gen.client.create_batch = AsyncMock(side_effect=[_DummyBatch(), _DummyBatch()])
            await gen.create_devices(
                device_role="spine",
                quantity=1,
                deployment_id="dep-1",
                template={},
                options={"indexes": [1, 1], "role_index_start": first_index},
            )

        group.save.assert_awaited_once_with(allow_upsert=True, update_group_context=False)
        assert gen.client.get.await_count == 1
        group_creates = [c for c in gen.client.create.await_args_list if c.kwargs["kind"] is CoreStandardGroup]
        assert len(group_creates) == 1
        assert group_creates[0].kwargs["data"]["name"] == "spines"

    @pytest.mark.asyncio
    async def test_ha_and_mlag_groups_are_not_created_when_missing(self) -> None:
        """Only role groups carry the create fallback: a missing ha_domains/mlag_domains group still raises."""
        gen = _make_generator()
        gen.client.get = AsyncMock(side_effect=NodeNotFoundError(identifier={"name": ["ha_domains"]}, node_type="x"))

        with pytest.raises(NodeNotFoundError):
            await gen._standard_group("ha_domains")
