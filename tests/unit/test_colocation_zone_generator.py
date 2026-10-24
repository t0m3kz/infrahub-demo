"""Unit tests for ColocationZoneGenerator and VirtualColocationGenerator
(generators/topology/colocation_fabric.py).

A zone (the cage, TopologyColocationZone) and a virtual colocation (a provider-hosted
presence, TopologyVirtualColocation) declare ``fabric_templates`` and get devices,
pools and EVPN objects. One base class holds the flow; the two subclasses differ in
class attributes only, so the flow is tested once against the zone and each subclass
gets its own tests for what differs (root key, node kind, template gating, no cabling).

What the demo data depends on being exactly right:

- which fabric_templates entries are usable, and that a bad one warns rather than
  failing the whole run (FailOnErrorLogger turns error() into a task failure);
- the pool names and their KINDS, since create_devices() resolves
  "{zone}-loopback-pool" and hands it to allocate_next_ip_address(), which needs an
  address pool, not a prefix pool;
- the device names: data/demos/30_all/08_interconnects/ references "eg-fr201" by name,
  and the zone (or virtual colocation) NAME is in the device name;
- the metro stays the one EVPN Multi-Site site: the AS is shared per metro and
  attached to the METRO, whichever zone or virtual colocation runs.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from generators.firewall_context import FIREWALL_CONTEXT
from generators.protocols import TopologyColocationZone, TopologyVirtualColocation
from generators.topology.colocation_fabric import (
    _COLO_SERVICE_ROLES,
    _COLO_VALID_FABRIC_ROLES,
    ColocationFabricGenerator,
    ColocationZoneGenerator,
    VirtualColocationGenerator,
)
from tests.unit.colocation_zone_generator_helpers import (
    _EDGE_TEMPLATE,
    _EDGE_TEMPLATE_WITH_ALL_SERVICE_PORTS,
    _EDGE_TEMPLATE_WITH_FIREWALL_PORTS,
    _PROCESSES,
    _VIRTUAL_EDGE_TEMPLATE,
    _made_virtual,
    _made_zone,
    _make_generator,
    _virtual,
    _zone,
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
        assert _COLO_SERVICE_ROLES < _COLO_VALID_FABRIC_ROLES


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
        good = {"role": "firewall", "quantity": 2, "template": _EDGE_TEMPLATE}
        gen = _made_zone(fabric_templates=[{"role": "border-leaf", "quantity": 2, "template": _EDGE_TEMPLATE}, good])

        assert gen._valid_fabric_templates() == [good]

    def test_every_colocation_role_is_accepted(self) -> None:
        """Every colocation role is valid given an edge template with the matching ports."""
        entries = [
            {"role": "edge", "quantity": 2, "template": _EDGE_TEMPLATE_WITH_ALL_SERVICE_PORTS},
            {"role": "firewall", "quantity": 2, "template": _EDGE_TEMPLATE},
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
            fabric_templates=[physical, {"role": "firewall", "quantity": 2, "template": _VIRTUAL_EDGE_TEMPLATE}]
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
    """_drop_uncablable_service_entries: a physical firewall/load-balancer entry is only usable if some physical
    edge template in the same zone exposes a same-named port role, else create_devices() would build a real HA
    pair that create_chain_cabling then fails on, with the appliance already left behind."""

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
            {"role": "firewall", "quantity": 2, "template": _EDGE_TEMPLATE},
        ]
        gen = _made_zone(fabric_templates=entries)

        assert gen._valid_fabric_templates() == entries
        gen.logger.warning.assert_not_called()

    def test_no_physical_edge_entry_at_all_is_not_this_checks_concern(self) -> None:
        """A zone can declare a service role with no edge pair; _cable_services's own no-op guard covers that."""
        entries = [{"role": "firewall", "quantity": 2, "template": _EDGE_TEMPLATE}]
        gen = _made_zone(fabric_templates=entries)

        assert gen._valid_fabric_templates() == entries
        gen.logger.warning.assert_not_called()

    def test_virtual_service_entry_is_exempt(self) -> None:
        """A virtual firewall in a virtual colocation is never cabled, so a missing edge port is irrelevant."""
        entries = [
            {"role": "edge", "quantity": 2, "template": _VIRTUAL_EDGE_TEMPLATE},
            {"role": "firewall", "quantity": 2, "template": _VIRTUAL_EDGE_TEMPLATE},
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


class TestGenerateEntry:
    @pytest.mark.asyncio
    async def test_missing_root_key_logs_error(self) -> None:
        gen = _make_generator()

        await gen.generate({})

        gen.logger.error.assert_called_once()

    @pytest.mark.asyncio
    async def test_virtual_colocation_reads_its_own_root_key(self) -> None:
        """A zone payload handed to the virtual colocation generator is 'no data', not silently accepted."""
        gen = _make_generator(VirtualColocationGenerator, fabric="am-ne")

        await gen.generate({"TopologyColocationZone": [_zone()]})

        gen.logger.error.assert_called_once()

    @pytest.mark.asyncio
    async def test_raw_graphql_response_is_cleaned_and_names_derive_from_the_zone(self) -> None:
        """generate() must run clean_data() first; fabric_name is the lower-cased zone name, deployment_id the zone id."""
        gen = _make_generator()
        gen._ensure_fabric_pools = AsyncMock()
        gen._create_fabric_devices = AsyncMock(return_value={"edge": ["eg-fr201", "eg-fr202"]})
        gen._cable_services = AsyncMock()
        gen._ensure_evpn = AsyncMock(return_value=_PROCESSES)
        gen._ensure_edge_pair = AsyncMock()
        raw = {
            "TopologyColocationZone": {
                "edges": [
                    {
                        "node": {
                            "id": "zone-1",
                            "name": {"value": "FR2"},
                            "parent": {
                                "node": {
                                    "id": "metro-1",
                                    "name": {"value": "FR"},
                                    "naming_convention": {"value": "standard"},
                                }
                            },
                            "fabric_templates": {
                                "edges": [
                                    {
                                        "node": {
                                            "role": {"value": "edge"},
                                            "quantity": {"value": 2},
                                            "template": {
                                                "node": {
                                                    "id": "tpl-edge",
                                                    "template_kind": "TemplateDcimPhysicalDevice",
                                                }
                                            },
                                        }
                                    }
                                ]
                            },
                        }
                    }
                ]
            }
        }

        await gen.generate(raw)

        assert gen.fabric_name == "fr2"
        assert gen.pod_name is None
        assert gen.deployment_id == "zone-1"
        gen._ensure_fabric_pools.assert_awaited_once_with()
        assert gen._create_fabric_devices.await_args_list[-1].kwargs["templates"] == [
            {
                "role": "edge",
                "quantity": 2,
                "template": {"id": "tpl-edge", "template_kind": "TemplateDcimPhysicalDevice"},
            }
        ]
        # The names the device pass produced are what the cabling pass acts on: passed through, not re-queried.
        gen._cable_services.assert_awaited_once_with(physical_names_by_role={"edge": ["eg-fr201", "eg-fr202"]})

    @pytest.mark.asyncio
    async def test_zone_without_kit_is_a_no_op(self) -> None:
        """Most zones declare nothing (FR6): no pools, no devices, no EVPN, no error."""
        gen = _make_generator()
        for step in ("_ensure_fabric_pools", "_create_fabric_devices", "_cable_services", "_ensure_evpn"):
            setattr(gen, step, AsyncMock())
        gen._ensure_edge_pair = AsyncMock()
        gen.ensure_context_vlan_pool = AsyncMock()
        gen.attach_refs = AsyncMock()
        zone = _zone(fabric_templates=[])

        await gen.generate({"TopologyColocationZone": [zone]})

        for step in (
            "_ensure_fabric_pools",
            "_create_fabric_devices",
            "_cable_services",
            "_ensure_evpn",
            "_ensure_edge_pair",
            "ensure_context_vlan_pool",
        ):
            getattr(gen, step).assert_not_awaited()
        gen.attach_refs.assert_not_awaited()
        gen.logger.error.assert_not_called()

    @pytest.mark.asyncio
    async def test_zone_whose_only_entries_are_unusable_is_a_no_op(self) -> None:
        """A zone declaring only a virtual template builds nothing: warn per entry, then skip the pool step."""
        gen = _make_generator()
        gen._ensure_fabric_pools = AsyncMock()
        gen._create_fabric_devices = AsyncMock()
        zone = _zone(fabric_templates=[{"role": "edge", "quantity": 2, "template": _VIRTUAL_EDGE_TEMPLATE}])

        await gen.generate({"TopologyColocationZone": [zone]})

        gen.logger.warning.assert_called_once()
        gen._ensure_fabric_pools.assert_not_awaited()
        gen._create_fabric_devices.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_steps_run_in_dependency_order(self) -> None:
        """Ordering is load-bearing: cabling resolves interfaces by device name, EVPN needs the edges' loopbacks,
        and the edge pair needs the overlay processes EVPN returns."""
        gen = _make_generator()
        calls: list[str] = []
        gen._ensure_fabric_pools = AsyncMock(side_effect=lambda **_: calls.append("pools"))
        gen._create_fabric_devices = AsyncMock(side_effect=lambda **_: calls.append("devices") or {})
        gen._cable_services = AsyncMock(side_effect=lambda **_: calls.append("cabling"))
        gen._ensure_evpn = AsyncMock(side_effect=lambda **_: calls.append("evpn") or _PROCESSES)
        gen._ensure_edge_pair = AsyncMock(side_effect=lambda **_: calls.append("pair"))

        await gen.generate({"TopologyColocationZone": [_zone()]})

        assert calls == ["pools", "devices", "cabling", "evpn", "pair"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("physical_names_by_role", "expected"),
        [
            ({"edge": ["eg-fr201", "eg-fr202"], "firewall": ["fw-fr201", "fw-fr202"]}, True),
            # No firewalls, or only virtual ones (never registered as physical): no context can be created.
            ({"edge": ["eg-fr201", "eg-fr202"]}, False),
        ],
    )
    async def test_context_vlan_pool_keyed_by_zone_name_only_with_physical_firewalls(
        self, physical_names_by_role: dict[str, list[str]], expected: bool
    ) -> None:
        """The FirewallContext VLAN pool is `{zone}-fw-context-vlan-pool`, and exists only where a context can be made."""
        gen = _make_generator()
        gen._ensure_fabric_pools = AsyncMock()
        gen._create_fabric_devices = AsyncMock(return_value=physical_names_by_role)
        gen._cable_services = AsyncMock()
        gen._ensure_evpn = AsyncMock(return_value=_PROCESSES)
        gen._ensure_edge_pair = AsyncMock()
        gen.ensure_context_vlan_pool = AsyncMock()

        await gen.generate({"TopologyColocationZone": [_zone()]})

        if expected:
            gen.ensure_context_vlan_pool.assert_awaited_once_with(name="fr2", spec=FIREWALL_CONTEXT)
        else:
            gen.ensure_context_vlan_pool.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_edge_pair_is_ensured_after_evpn_with_its_overlay_process_ids(self) -> None:
        gen = _make_generator()
        gen._ensure_fabric_pools = AsyncMock()
        gen._create_fabric_devices = AsyncMock(return_value={"edge": ["eg-fr201", "eg-fr202"]})
        gen._cable_services = AsyncMock()
        gen._ensure_evpn = AsyncMock(return_value=_PROCESSES)
        gen._ensure_edge_pair = AsyncMock()

        await gen.generate({"TopologyColocationZone": [_zone()]})

        gen._ensure_edge_pair.assert_awaited_once_with(
            physical_edge_names=["eg-fr201", "eg-fr202"], overlay_process_ids=_PROCESSES
        )

    @pytest.mark.asyncio
    async def test_virtual_edges_are_not_counted_for_the_pair(self) -> None:
        """generate() hands over only the physical names: 1 physical + 1 virtual is a single edge."""
        gen = _make_generator()
        gen._ensure_fabric_pools = AsyncMock()
        gen._create_fabric_devices = AsyncMock(return_value={"edge": ["eg-fr201"]})
        gen._cable_services = AsyncMock()
        gen._ensure_evpn = AsyncMock(return_value=_PROCESSES)
        gen._ensure_edge_pair = AsyncMock()

        await gen.generate({"TopologyColocationZone": [_zone()]})

        gen._ensure_edge_pair.assert_awaited_once_with(physical_edge_names=["eg-fr201"], overlay_process_ids=_PROCESSES)


class TestVirtualColocationFlow:
    """VirtualColocationGenerator.generate end to end with the step methods mocked at their seams."""

    @staticmethod
    def _steps(gen: Any) -> None:
        gen._ensure_fabric_pools = AsyncMock()
        gen._create_fabric_devices = AsyncMock(return_value={})
        gen._cable_services = AsyncMock()
        gen._ensure_evpn = AsyncMock(return_value={"eg-am-ne01": "b-1", "eg-am-ne02": "b-2"})
        gen._ensure_edge_pair = AsyncMock()
        gen.ensure_context_vlan_pool = AsyncMock()

    @pytest.mark.asyncio
    async def test_edges_are_virtual_evpn_still_runs_and_no_pair_or_vlan_pool_is_made(self) -> None:
        """AM-NE: virtual kit registers no physical names, so no VLAN pool and an empty edge list for the pair;
        EVPN still runs."""
        gen = _make_generator(VirtualColocationGenerator, fabric="am-ne")
        self._steps(gen)

        await gen.generate({"TopologyVirtualColocation": [_virtual()]})

        gen._ensure_evpn.assert_awaited_once_with()
        gen._ensure_edge_pair.assert_awaited_once_with(
            physical_edge_names=[], overlay_process_ids={"eg-am-ne01": "b-1", "eg-am-ne02": "b-2"}
        )
        gen.ensure_context_vlan_pool.assert_not_awaited()
        assert gen.fabric_name == "am-ne"

    @pytest.mark.asyncio
    async def test_virtual_colocation_without_kit_is_a_no_op(self) -> None:
        """A virtual colocation in group but with no fabric_templates (the 30_all AM-NE) builds nothing."""
        gen = _make_generator(VirtualColocationGenerator, fabric="am-ne")
        self._steps(gen)

        await gen.generate({"TopologyVirtualColocation": [_virtual(fabric_templates=[])]})

        gen._ensure_fabric_pools.assert_not_awaited()
        gen._create_fabric_devices.assert_not_awaited()
        gen._ensure_evpn.assert_not_awaited()
        gen.logger.error.assert_not_called()
