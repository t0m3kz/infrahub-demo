"""ColocationZoneGenerator and VirtualColocationGenerator (generators/colocation/colocation_fabric.py): the class contract and
which fabric_templates entries a node acts on."""

from __future__ import annotations

from typing import Any

import pytest

from generators.colocation.colocation_fabric import (
    ColocationFabricGenerator,
    ColocationZoneGenerator,
    VirtualColocationGenerator,
)
from generators.model.colocation_model import COLO_SERVICE_ROLES, COLO_VALID_FABRIC_ROLES
from generators.protocols import TopologyColocationZone, TopologyVirtualColocation
from tests.unit.colocation_zone_generator_helpers import (
    _EDGE_TEMPLATE,
    _EDGE_TEMPLATE_WITH_ALL_SERVICE_PORTS,
    _EDGE_TEMPLATE_WITH_FIREWALL_PORTS,
    _VIRTUAL_EDGE_TEMPLATE,
    _made_virtual,
    _made_zone,
)


class TestSubclassContract:
    """The two registered generators differ by class attributes alone."""

    @pytest.mark.parametrize(
        ("cls", "root_key", "node_kind", "template_kind", "label"),
        [
            (
                ColocationZoneGenerator,
                "TopologyColocationZone",
                TopologyColocationZone,
                "TemplateDcimPhysicalDevice",
                "zone",
            ),
            (
                VirtualColocationGenerator,
                "TopologyVirtualColocation",
                TopologyVirtualColocation,
                "TemplateCloudNetworkAppliance",
                "virtual colocation",
            ),
        ],
    )
    def test_class_attributes(
        self, cls: type[ColocationFabricGenerator], root_key: str, node_kind: type, template_kind: str, label: str
    ) -> None:
        """The GraphQL root, the protocol class pools attach to, the template kind hosted and the log label."""
        assert cls.graphql_root_key == root_key
        assert cls._node_kind is node_kind
        assert cls._template_kind == template_kind
        assert cls._label == label

    def test_ha_roles_are_a_subset_of_the_valid_colocation_roles(self) -> None:
        """A service role the generator would never build is dead code; one it builds but cannot pair is a SPOF."""
        assert COLO_SERVICE_ROLES < COLO_VALID_FABRIC_ROLES


class TestFabricTemplateValidation:
    """_valid_fabric_templates: which entries are acted on, and that a bad one only warns."""

    def test_valid_entry_is_kept(self) -> None:
        gen = _made_zone()

        assert gen._valid_fabric_templates() == gen.data["fabric_templates"]
        gen.logger.warning.assert_not_called()

    @pytest.mark.parametrize(
        "entry",
        [
            # DC-only fabric tiers are meaningless where there is no fabric.
            {"role": "super-spine", "quantity": 2, "template": _EDGE_TEMPLATE},
            {"role": "leaf", "quantity": 2, "template": _EDGE_TEMPLATE},
            {"role": None, "quantity": 2, "template": _EDGE_TEMPLATE},
            {"role": "edge", "quantity": 0, "template": _EDGE_TEMPLATE},
            {"role": "edge", "quantity": 2, "template": None},
        ],
    )
    def test_unusable_entry_warns_and_is_dropped(self, entry: dict[str, Any]) -> None:
        gen = _made_zone(fabric_templates=[entry])

        assert gen._valid_fabric_templates() == []
        gen.logger.warning.assert_called_once()
        # warning(), never error(): one bad entry must not fail the whole run.
        gen.logger.error.assert_not_called()

    def test_one_bad_entry_does_not_drop_the_good_ones(self) -> None:
        good = {"role": "internal-firewall", "quantity": 2, "template": _EDGE_TEMPLATE}
        gen = _made_zone(fabric_templates=[{"role": "border-leaf", "quantity": 2, "template": _EDGE_TEMPLATE}, good])

        assert gen._valid_fabric_templates() == [good]

    def test_every_colocation_role_is_accepted(self) -> None:
        """Every colocation role is valid given an edge template with the matching ports."""
        entries = [
            {"role": "edge", "quantity": 2, "template": _EDGE_TEMPLATE_WITH_ALL_SERVICE_PORTS},
            {"role": "internal-firewall", "quantity": 2, "template": _EDGE_TEMPLATE},
            {"role": "load-balancer", "quantity": 2, "template": _EDGE_TEMPLATE},
        ]
        gen = _made_zone(fabric_templates=entries)

        assert gen._valid_fabric_templates() == entries
        gen.logger.warning.assert_not_called()

    def test_no_fabric_templates_key_is_an_empty_list(self) -> None:
        """A zone without the key (no kit declared) yields nothing and warns about nothing."""
        gen = _made_zone()
        del gen.data["fabric_templates"]

        assert gen._valid_fabric_templates() == []
        gen.logger.warning.assert_not_called()


class TestTemplateKindGating:
    """A zone hosts physical templates only, a virtual colocation virtual only; the other kind warns and is skipped."""

    def test_zone_skips_a_virtual_template_with_a_warning(self) -> None:
        gen = _made_zone(fabric_templates=[{"role": "edge", "quantity": 2, "template": _VIRTUAL_EDGE_TEMPLATE}])

        assert gen._valid_fabric_templates() == []
        gen.logger.warning.assert_called_once()
        message = gen.logger.warning.call_args.args[0]
        assert "TemplateCloudNetworkAppliance" in message
        assert "TemplateDcimPhysicalDevice" in message
        assert "zone" in message
        gen.logger.error.assert_not_called()

    def test_virtual_colocation_skips_a_physical_template_with_a_warning(self) -> None:
        gen = _made_virtual(fabric_templates=[{"role": "edge", "quantity": 2, "template": _EDGE_TEMPLATE}])

        assert gen._valid_fabric_templates() == []
        gen.logger.warning.assert_called_once()
        message = gen.logger.warning.call_args.args[0]
        assert "TemplateDcimPhysicalDevice" in message
        assert "virtual colocation" in message
        gen.logger.error.assert_not_called()

    def test_virtual_colocation_keeps_a_virtual_template(self) -> None:
        gen = _made_virtual()

        assert gen._valid_fabric_templates() == gen.data["fabric_templates"]
        gen.logger.warning.assert_not_called()

    def test_zone_keeps_the_physical_entry_next_to_a_skipped_virtual_one(self) -> None:
        """Mixed kinds in one zone: the virtual entry is dropped, the physical one survives."""
        physical = {"role": "edge", "quantity": 2, "template": _EDGE_TEMPLATE}
        gen = _made_zone(
            fabric_templates=[
                physical,
                {"role": "internal-firewall", "quantity": 2, "template": _VIRTUAL_EDGE_TEMPLATE},
            ]
        )

        assert gen._valid_fabric_templates() == [physical]
        gen.logger.warning.assert_called_once()

    def test_template_without_a_kind_is_skipped(self) -> None:
        """Guards a query regression: no template_kind means physical cannot be told from virtual, so nothing is built."""
        gen = _made_zone(fabric_templates=[{"role": "edge", "quantity": 2, "template": {"id": "tpl-edge"}}])

        assert gen._valid_fabric_templates() == []
        gen.logger.warning.assert_called_once()
        assert "unknown" in gen.logger.warning.call_args.args[0]
        gen.logger.error.assert_not_called()


class TestUncablableServiceEntries:
    """A service entry no edge template in the zone can cable is dropped before its devices and HA pair are planned."""

    def test_service_role_with_no_matching_edge_port_is_dropped(self) -> None:
        """N9K-C9316D-GX_EDGE has `firewall` ports but no `load-balancer` ports."""
        entries = [
            {"role": "edge", "quantity": 2, "template": _EDGE_TEMPLATE_WITH_FIREWALL_PORTS},
            {"role": "load-balancer", "quantity": 2, "template": _EDGE_TEMPLATE},
        ]
        gen = _made_zone(fabric_templates=entries)

        assert gen._valid_fabric_templates() == [entries[0]]
        gen.logger.warning.assert_called_once()
        gen.logger.error.assert_not_called()

    def test_service_role_with_a_matching_edge_port_is_kept(self) -> None:
        entries = [
            {"role": "edge", "quantity": 2, "template": _EDGE_TEMPLATE_WITH_FIREWALL_PORTS},
            {"role": "internal-firewall", "quantity": 2, "template": _EDGE_TEMPLATE},
        ]
        gen = _made_zone(fabric_templates=entries)

        assert gen._valid_fabric_templates() == entries
        gen.logger.warning.assert_not_called()

    def test_no_physical_edge_entry_at_all_is_not_this_checks_concern(self) -> None:
        """A zone can declare a service role with no edge pair; _cable_services's own no-op guard covers that."""
        entries = [{"role": "internal-firewall", "quantity": 2, "template": _EDGE_TEMPLATE}]
        gen = _made_zone(fabric_templates=entries)

        assert gen._valid_fabric_templates() == entries
        gen.logger.warning.assert_not_called()

    def test_virtual_service_entry_is_exempt(self) -> None:
        """A virtual firewall in a virtual colocation is never cabled, so a missing edge port is irrelevant."""
        entries = [
            {"role": "edge", "quantity": 2, "template": _VIRTUAL_EDGE_TEMPLATE},
            {"role": "internal-firewall", "quantity": 2, "template": _VIRTUAL_EDGE_TEMPLATE},
        ]
        gen = _made_virtual(fabric_templates=entries)

        assert gen._valid_fabric_templates() == entries
        gen.logger.warning.assert_not_called()

    def test_dropped_entry_warning_names_the_role_and_the_available_port_roles(self) -> None:
        entries = [
            {"role": "edge", "quantity": 2, "template": _EDGE_TEMPLATE_WITH_FIREWALL_PORTS},
            {"role": "load-balancer", "quantity": 2, "template": _EDGE_TEMPLATE},
        ]
        gen = _made_zone(fabric_templates=entries)

        gen._valid_fabric_templates()

        message = gen.logger.warning.call_args.args[0]
        assert "load-balancer" in message
        assert "firewall" in message
