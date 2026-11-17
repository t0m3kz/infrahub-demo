"""Unit tests for the controller that governs a device role and the bootstrap role groups."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import yaml

from generators.dcim.controllers import resolve_role_controller
from utils.limits import ROLE_GROUP_NAMES


def _controller_dict(controller_type: str, platform_id: str | None = None, id_: str = "ctrl-1") -> dict[str, Any]:
    """Shape clean_data() produces for one controllers() query edge."""
    entry: dict[str, Any] = {"id": id_, "controller_type": controller_type}
    if platform_id is not None:
        entry["platform"] = {"id": platform_id}
    return entry


class TestResolveRoleController:
    """resolve_role_controller."""

    def _gen(self, controllers: list[dict[str, Any]] | None = None) -> Any:
        gen = SimpleNamespace(logger=MagicMock(), controllers=controllers or [])
        gen.resolve = lambda *, device_role, template: resolve_role_controller(
            gen.controllers, device_role=device_role, template=template, logger=gen.logger
        )
        return gen

    def test_no_controllers_set_returns_none(self) -> None:
        gen = self._gen()  # _all_controllers never set

        controller = gen.resolve(device_role="internal-firewall", template={})

        assert controller is None

    def test_role_with_no_controller_mapping_returns_none(self) -> None:
        gen = self._gen(controllers=[_controller_dict("aci_apic")])

        controller = gen.resolve(device_role="endpoint", template={})

        assert controller is None

    def test_fabric_role_matches_by_controller_type_alone(self) -> None:
        apic = _controller_dict("aci_apic")
        gen = self._gen(controllers=[apic])

        controller = gen.resolve(device_role="leaf", template={"platform": {"id": "plat-nxos"}})

        assert controller is apic

    def test_campus_role_matches_dna_center_by_controller_type_alone(self) -> None:
        dnac = _controller_dict("dna_center")
        gen = self._gen(controllers=[dnac])

        controller = gen.resolve(device_role="access-switch", template={})

        assert controller is dnac

    def test_firewall_requires_platform_match_not_just_controller_type(self) -> None:
        # A controller of a different platform must not match despite the same controller_type.
        panorama = _controller_dict("security_manager", platform_id="plat-paloalto_panos")
        gen = self._gen(controllers=[panorama])

        controller = gen.resolve(device_role="internal-firewall", template={"platform": {"id": "plat-checkpoint"}})

        assert controller is None
        gen.logger.warning.assert_called_once()

    def test_firewall_matches_when_controller_type_and_platform_both_match(self) -> None:
        cp_sms = _controller_dict("security_manager", platform_id="plat-checkpoint")
        gen = self._gen(controllers=[cp_sms])

        controller = gen.resolve(device_role="internal-firewall", template={"platform": {"id": "plat-checkpoint"}})

        assert controller is cp_sms

    def test_load_balancer_matches_lb_manager_type(self) -> None:
        big_iq = _controller_dict("lb_manager", platform_id="plat-f5")
        gen = self._gen(controllers=[big_iq])

        controller = gen.resolve(device_role="load-balancer", template={"platform": {"id": "plat-f5"}})

        assert controller is big_iq

    def test_empty_controllers_list_returns_none_without_warning(self) -> None:
        """No controllers at all isn't controller-managed."""
        gen = self._gen(controllers=[])

        controller = gen.resolve(device_role="internal-firewall", template={"platform": {"id": "plat-checkpoint"}})

        assert controller is None
        gen.logger.warning.assert_not_called()


class TestRoleGroups:
    """The role groups the device plans put a device in."""

    def test_every_role_group_is_a_bootstrap_group(self, root_dir: Path) -> None:
        """A group the bootstrap does not load collides across branches (see ROLE_GROUP_NAMES)."""
        groups = {
            entry["name"]
            for document in yaml.safe_load_all((root_dir / "data/bootstrap/00_groups.yml").read_text())
            if document and document["spec"]["kind"] == "CoreStandardGroup"
            for entry in document["spec"]["data"]
        }

        assert set(ROLE_GROUP_NAMES.values()) <= groups
