"""Unit tests for DeviceMixin's controller routing and campus roles."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml
from infrahub_sdk.protocols import CoreStandardGroup

from generators.devices import ROLE_GROUP_NAMES, DeviceMixin
from generators.protocols import DcimPhysicalDevice, ManagedController, ManagedStandaloneVlanDomain
from tests.unit.device_mixin_helpers import _make_generator, _mock_created_device


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
        controller_obj.managed_devices.peers = []
        controller_obj.add_relationships = AsyncMock()
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
        # Only the missing device is added, atomically: a save of the whole peer list lets
        # concurrent racks of one controller overwrite each other's devices.
        controller_obj.add_relationships.assert_awaited_once_with(
            relation_to_update="managed_devices", related_nodes=[created_device.id]
        )
        controller_obj.save.assert_not_awaited()
        # The peer ids arrive with the controller itself (include=), never via a
        # managed_devices.fetch() re-get plus full-node read of every managed device.
        gen.client.get.assert_awaited_once_with(kind=ManagedController, id="cp-sms-1", include=["managed_devices"])

    @pytest.mark.asyncio
    async def test_already_attached_device_is_not_added_again(self) -> None:
        """A rerun over a device the controller already manages adds nothing."""
        gen = _make_generator()
        gen._all_controllers = [_controller_dict("security_manager", platform_id="plat-checkpoint", id_="cp-sms-1")]
        created_device = _mock_created_device(DcimPhysicalDevice.__name__, "dc1-firewall-01")

        controller_obj = MagicMock()
        controller_obj.hfid = "cp-sms-1"
        controller_obj.managed_devices.peers = [MagicMock(id=created_device.id)]
        controller_obj.add_relationships = AsyncMock()
        gen.client.get = AsyncMock(return_value=controller_obj)
        gen.client.create = AsyncMock(return_value=created_device)

        await gen.create_devices(
            device_role="firewall",
            quantity=1,
            deployment_id="dc-1",
            template={"device_type": {"id": "dt-1"}, "platform": {"id": "plat-checkpoint"}},
        )

        controller_obj.add_relationships.assert_not_awaited()

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

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("role", "group"),
        [
            ("load-balancer", "loadbalancers"),
            ("firewall", "firewalls"),
            ("access-switch", "access-switches"),
            ("distribution-switch", "distribution-switches"),
        ],
    )
    async def test_role_joins_its_bootstrap_group(self, role: str, group: str) -> None:
        """Every caller lands a role in its bootstrap group — load-balancer is
        "loadbalancers", not the derived "load-balancers" a branch would create."""
        gen = _make_generator()
        gen.client.create = AsyncMock(return_value=_mock_created_device(DcimPhysicalDevice.__name__, f"dc1-{role}-01"))

        await gen.create_devices(
            device_role=role,
            quantity=1,
            deployment_id="dc-1",
            template={"device_type": {"id": "dt-1"}, "platform": {"id": "plat-1"}},
        )

        group_lookup = next(
            call for call in gen.client.get.call_args_list if call.kwargs.get("kind") is CoreStandardGroup
        )
        assert group_lookup.kwargs["name__value"] == group


class TestCampusRoles:
    """The office fabric's roles: only the access-switch (the switch hosts attach to) is its own VLAN
    domain, and neither role joins a derived "access-switchs"-style group."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("role", "kinds"),
        [
            ("access-switch", [DcimPhysicalDevice, ManagedStandaloneVlanDomain]),
            ("distribution-switch", [DcimPhysicalDevice]),
        ],
    )
    async def test_only_the_access_switch_gets_a_standalone_vlan_domain(self, role: str, kinds: list[Any]) -> None:
        gen = _make_generator()
        gen.client.create = AsyncMock(
            side_effect=[
                _mock_created_device(DcimPhysicalDevice.__name__, f"c005-office-{role}-01"),
                _mock_created_device(ManagedStandaloneVlanDomain.__name__, f"c005-office-{role}-01-vlan-domain"),
            ]
        )

        await gen.create_devices(
            device_role=role,
            quantity=1,
            deployment_id="office-1",
            template={"device_type": {"id": "dt-1"}, "platform": {"name": "cisco_xe"}},
        )

        assert [call.kwargs["kind"] for call in gen.client.create.call_args_list] == kinds

    def test_every_role_group_is_a_bootstrap_group(self, root_dir: Path) -> None:
        """A group the bootstrap does not load is created on whichever branch needs it first and then
        collides with every other branch that creates it (see ROLE_GROUP_NAMES)."""
        groups = {
            entry["name"]
            for document in yaml.safe_load_all((root_dir / "data/bootstrap/00_groups.yml").read_text())
            if document and document["spec"]["kind"] == "CoreStandardGroup"
            for entry in document["spec"]["data"]
        }

        assert set(ROLE_GROUP_NAMES.values()) <= groups
