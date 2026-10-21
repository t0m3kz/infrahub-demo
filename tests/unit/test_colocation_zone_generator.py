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
from unittest.mock import AsyncMock, MagicMock

import pytest
from infrahub_sdk.exceptions import NodeNotFoundError
from infrahub_sdk.protocols import CoreNumberPool

from generators.firewall_context import FIREWALL_CONTEXT
from generators.helpers.naming import DeviceNameContext, DeviceNamingConfig
from generators.logger import GeneratorError
from generators.protocols import TopologyColocationZone, TopologyVirtualColocation
from generators.topology.colocation_fabric import (
    _COLO_LOOPBACK_PREFIX_LENGTH,
    _COLO_MANAGEMENT_PREFIX_LENGTH,
    _COLO_SERVICE_ROLES,
    _COLO_TECHNICAL_PREFIX_LENGTH,
    _COLO_VALID_FABRIC_ROLES,
    ColocationFabricGenerator,
    ColocationZoneGenerator,
    VirtualColocationGenerator,
)

_EDGE_TEMPLATE = {
    "id": "tpl-edge",
    # The cleaned form of the `template_kind: __typename` alias the query
    # selects (aliased because clean_data() would rewrite __typename to
    # "typename"). This is what tells the generator physical from virtual.
    "template_kind": "TemplateDcimPhysicalDevice",
    "template_name": "N9K-C9316D-GX_EDGE",
    "platform": {"id": "plat-nxos", "name": "cisco_nxos"},
    "device_type": "dt-9316d",
}

# Same edge template with the real N9K-C9316D-GX_EDGE port layout attached
# (data/bootstrap/10_physical_devices_templates_cisco_nxos.yaml): a
# `firewall`-role port block, no `load-balancer`-role ports at all.
_EDGE_TEMPLATE_WITH_FIREWALL_PORTS = {
    **_EDGE_TEMPLATE,
    "id": "tpl-edge-fw-ports",
    "interfaces": [
        {"name": "Ethernet1/15", "role": "firewall"},
        {"name": "Ethernet1/16", "role": "firewall"},
    ],
}

# A hypothetical edge template with BOTH firewall- and load-balancer-role ports,
# for tests that want every colocation role to be cablable.
_EDGE_TEMPLATE_WITH_ALL_SERVICE_PORTS = {
    **_EDGE_TEMPLATE,
    "id": "tpl-edge-all-ports",
    "interfaces": [
        {"name": "Ethernet1/15", "role": "firewall"},
        {"name": "Ethernet1/16", "role": "firewall"},
        {"name": "Ethernet1/17", "role": "load-balancer"},
        {"name": "Ethernet1/18", "role": "load-balancer"},
    ],
}

_VIRTUAL_EDGE_TEMPLATE = {
    "id": "tpl-vedge",
    "template_kind": "TemplateDcimVirtualDevice",
    "template_name": "C8000V_EDGE",
}

_METRO_ASN_POOL = {"id": "asn-pool", "name": "fr-asn-pool"}
_PROCESSES = {"eg-fr201": "bgp-1", "eg-fr202": "bgp-2"}


def _make_generator(cls: type[ColocationFabricGenerator] = ColocationZoneGenerator, *, fabric: str = "fr2") -> Any:
    gen = cls.__new__(cls)
    gen.logger = MagicMock()
    gen.client = MagicMock()
    gen.client.get = AsyncMock()
    gen.client.filters = AsyncMock(return_value=[])
    gen.client.create = AsyncMock()
    gen.client.allocate_next_ip_prefix = AsyncMock(return_value=MagicMock(id="prefix-1"))
    gen.branch = "main"
    gen.fabric_name = fabric
    gen.pod_name = None
    gen.deployment_id = "zone-1"
    setattr(gen, "acquire_resource_lock", AsyncMock(return_value="lock-id"))  # noqa: B010
    setattr(gen, "release_resource_lock", AsyncMock())  # noqa: B010
    return gen


def _zone(**overrides: Any) -> dict[str, Any]:
    """A cleaned zone row as colocation_zone.gql returns it: zone FR2 in metro FR."""
    zone: dict[str, Any] = {
        "id": "zone-1",
        "name": "FR2",
        "parent": {
            "id": "metro-1",
            "name": "FR",
            "naming_convention": "standard",
            "fabric_asn_pool": _METRO_ASN_POOL,
        },
        "fabric_templates": [{"role": "edge", "quantity": 2, "template": _EDGE_TEMPLATE}],
    }
    zone.update(overrides)
    return zone


def _virtual(**overrides: Any) -> dict[str, Any]:
    """A cleaned virtual colocation row as colocation_virtual.gql returns it: AM-NE in metro AM."""
    node: dict[str, Any] = {
        "id": "vc-1",
        "name": "AM-NE",
        "parent": {
            "id": "metro-am",
            "name": "AM",
            "naming_convention": "standard",
            "fabric_asn_pool": {"id": "am-asn-pool", "name": "am-asn-pool"},
        },
        "fabric_templates": [{"role": "edge", "quantity": 2, "template": _VIRTUAL_EDGE_TEMPLATE}],
    }
    node.update(overrides)
    return node


def _made_zone(**overrides: Any) -> Any:
    gen = _make_generator()
    gen.data = _zone(**overrides)
    return gen


def _made_virtual(**overrides: Any) -> Any:
    gen = _make_generator(VirtualColocationGenerator, fabric="am-ne")
    gen.data = _virtual(**overrides)
    return gen


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
                "TemplateDcimVirtualDevice",
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
        assert "TemplateDcimVirtualDevice" in message
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


class TestZonePools:
    """_ensure_fabric_pools: the zone's own pools, their kinds, parents and attach."""

    @staticmethod
    def _pools_generator() -> Any:
        gen = _made_zone()
        gen.ensure_sliced_pool = AsyncMock(side_effect=lambda **kw: MagicMock(id=f"id-{kw['pool_name']}"))
        gen.attach_refs = AsyncMock()
        return gen

    @pytest.mark.asyncio
    async def test_pool_names_kinds_and_parents(self) -> None:
        gen = self._pools_generator()

        await gen._ensure_fabric_pools()

        calls = {c.kwargs["pool_name"]: c.kwargs for c in gen.ensure_sliced_pool.await_args_list}
        assert set(calls) == {
            "fr2-loopback-pool",
            "fr2-loopback-ipv6-pool",
            "fr2-management-pool",
            "fr2-technical-pool",
        }
        # The EVPN Multi-Site VTEP loopback is IPv6, matching the IPv6 DCI.
        assert calls["fr2-loopback-ipv6-pool"]["kind"] == "address"
        assert calls["fr2-loopback-ipv6-pool"]["parent_pool_name"] == "Loopback-IPv6"
        assert calls["fr2-loopback-ipv6-pool"]["prefix_length"] == 120
        # An address pool for loopbacks and management: create_devices() feeds each to allocate_next_ip_address().
        assert calls["fr2-loopback-pool"]["kind"] == "address"
        assert calls["fr2-loopback-pool"]["parent_pool_name"] == "Loopback-IPv4"
        assert calls["fr2-loopback-pool"]["prefix_length"] == _COLO_LOOPBACK_PREFIX_LENGTH
        assert calls["fr2-management-pool"]["kind"] == "address"
        assert calls["fr2-management-pool"]["parent_pool_name"] == "Management-IPv4"
        assert calls["fr2-management-pool"]["prefix_length"] == _COLO_MANAGEMENT_PREFIX_LENGTH
        # P2P links get whole prefixes handed out, so this one is a prefix pool.
        assert calls["fr2-technical-pool"]["kind"] == "prefix"
        assert calls["fr2-technical-pool"]["parent_pool_name"] == "Technical-IPv4"
        assert calls["fr2-technical-pool"]["prefix_length"] == _COLO_TECHNICAL_PREFIX_LENGTH

    @pytest.mark.asyncio
    async def test_three_pools_are_attached_to_the_zone_in_one_call(self) -> None:
        """loopback, management and technical ride one attach_refs on the zone id; the IPv6 VTEP pool is internal."""
        gen = self._pools_generator()

        await gen._ensure_fabric_pools()

        gen.attach_refs.assert_awaited_once_with(
            kind=TopologyColocationZone,
            node_id="zone-1",
            refs={
                "loopback_pool": "id-fr2-loopback-pool",
                "management_pool": "id-fr2-management-pool",
                "technical_pool": "id-fr2-technical-pool",
            },
        )
        assert gen._vtep_pool.id == "id-fr2-loopback-ipv6-pool"

    @pytest.mark.asyncio
    async def test_virtual_colocation_pools_are_named_by_the_virtual_colocation_and_attach_to_it(self) -> None:
        gen = _made_virtual()
        gen.ensure_sliced_pool = AsyncMock(side_effect=lambda **kw: MagicMock(id=f"id-{kw['pool_name']}"))
        gen.attach_refs = AsyncMock()

        await gen._ensure_fabric_pools()

        names = {c.kwargs["pool_name"] for c in gen.ensure_sliced_pool.await_args_list}
        assert names == {
            "am-ne-loopback-pool",
            "am-ne-management-pool",
            "am-ne-technical-pool",
            "am-ne-loopback-ipv6-pool",
        }
        assert gen.attach_refs.await_args_list[-1].kwargs["kind"] is TopologyVirtualColocation
        assert gen.attach_refs.await_args_list[-1].kwargs["node_id"] == "vc-1"

    @pytest.mark.asyncio
    async def test_attach_saves_plainly_and_untracked(self) -> None:
        """The real attach_refs: one fetch by id, setattr per pool, plain save without group-context tracking.

        allow_upsert=True would resend every relationship and re-fire the fabric_templates `updated` trigger."""
        gen = self._pools_generator()
        del gen.attach_refs
        zone_node = AsyncMock()
        gen.client.get = AsyncMock(return_value=zone_node)

        await gen._ensure_fabric_pools()

        assert zone_node.loopback_pool == {"id": "id-fr2-loopback-pool"}
        assert zone_node.management_pool == {"id": "id-fr2-management-pool"}
        assert zone_node.technical_pool == {"id": "id-fr2-technical-pool"}
        zone_node.save.assert_awaited_once_with(update_group_context=False)
        gen.client.get.assert_awaited_once_with(kind=TopologyColocationZone, id="zone-1")

    @pytest.mark.asyncio
    async def test_missing_zone_when_attaching_pools_fails_the_run(self) -> None:
        """The zone is the generator's own target: not finding it is a fault, not a skip."""
        gen = self._pools_generator()
        del gen.attach_refs
        gen.client.get = AsyncMock(
            side_effect=NodeNotFoundError(branch_name="main", node_type="TopologyColocationZone", identifier={})
        )

        with pytest.raises(GeneratorError, match="TopologyColocationZone zone-1: node not found"):
            await gen._ensure_fabric_pools()

    @pytest.mark.asyncio
    async def test_rerun_slices_the_same_pool_names(self) -> None:
        """Idempotent: a second run asks for the same names, so the identifier-keyed slices converge."""
        gen = self._pools_generator()

        await gen._ensure_fabric_pools()
        await gen._ensure_fabric_pools()

        names = [c.kwargs["pool_name"] for c in gen.ensure_sliced_pool.await_args_list]
        assert len(names) == 8
        assert names[:4] == names[4:]

    @pytest.mark.asyncio
    async def test_sliced_address_pool_wraps_the_allocated_prefix(self) -> None:
        gen = _made_zone()
        gen._get_parent_pool_with_retry = AsyncMock(return_value=MagicMock(id="parent-1"))
        pool = AsyncMock()
        gen.client.create = AsyncMock(return_value=pool)

        await gen.ensure_sliced_pool(
            pool_name="fr2-loopback-pool",
            parent_pool_name="Loopback-IPv4",
            prefix_length=28,
            role="loopback",
            kind="address",
        )

        # Sliced out of the global bootstrap pool, keyed by an identifier so the slice is idempotent.
        alloc = gen.client.allocate_next_ip_prefix.await_args_list[-1].kwargs
        assert alloc["identifier"] == "fr2-loopback-pool"
        assert alloc["prefix_length"] == 28
        assert alloc["data"] == {"role": "loopback"}
        create = gen.client.create.await_args_list[-1].kwargs
        assert create["kind"].__name__ == "CoreIPAddressPool"
        assert create["data"]["default_address_type"] == "IpamIPAddress"
        pool.save.assert_awaited_once_with(allow_upsert=True)


class TestFabricDevices:
    """_create_fabric_devices: what a zone / virtual colocation instantiates and how it is deployed."""

    @pytest.mark.asyncio
    async def test_edge_devices_deploy_to_the_zone_with_flat_naming(self) -> None:
        gen = _made_zone()
        gen.create_devices = AsyncMock(return_value=["eg-fr201", "eg-fr202"])

        await gen._create_fabric_devices(templates=gen.data["fabric_templates"])

        kwargs = gen.create_devices.await_args_list[-1].kwargs
        assert kwargs["device_role"] == "edge"
        assert kwargs["quantity"] == 2
        # The kit is the zone's: deployment is the zone id, never the metro.
        assert kwargs["deployment_id"] == "zone-1"
        assert kwargs["naming_convention"] == "standard"
        # An on-ramp is flat: no fabric/pod/suite/row/rack path to encode.
        assert kwargs["options"]["indexes"] == []
        assert kwargs["options"]["allocate_loopback"] is True
        assert kwargs["options"]["loopback_prefix_length"] == 32
        # A physical template must not be flipped to DcimVirtualDevice.
        assert "virtual" not in kwargs["options"]
        # An edge router peers, it does not fail over.
        assert "ha_kind" not in kwargs["options"]

    @pytest.mark.asyncio
    async def test_edge_platform_is_recorded_for_the_evpn_step(self) -> None:
        gen = _made_zone()
        gen.create_devices = AsyncMock(return_value=["eg-fr201", "eg-fr202"])

        await gen._create_fabric_devices(templates=gen.data["fabric_templates"])

        assert gen._edge_devices == {"eg-fr201": "cisco_nxos", "eg-fr202": "cisco_nxos"}

    @pytest.mark.asyncio
    async def test_virtual_template_creates_virtual_devices(self) -> None:
        """A provider-hosted router (Network Edge, MCR) is a DcimVirtualDevice, selected via options["virtual"]."""
        gen = _made_virtual()
        gen.create_devices = AsyncMock(return_value=["eg-am-ne01", "eg-am-ne02"])

        await gen._create_fabric_devices(templates=gen.data["fabric_templates"])

        kwargs = gen.create_devices.await_args_list[-1].kwargs
        assert kwargs["options"]["virtual"] is True
        # Deployed to the virtual colocation, with loopback addressing unchanged: an MCR still peers over one.
        assert kwargs["deployment_id"] == "vc-1"
        assert kwargs["options"]["allocate_loopback"] is True

    @pytest.mark.parametrize(
        ("role", "expected_ha_kind"),
        [
            ("firewall", "ManagedFirewallHA"),
            ("load-balancer", "ManagedLoadbalancerHA"),
        ],
    )
    @pytest.mark.asyncio
    async def test_service_appliances_pair_into_an_ha_domain_without_a_loopback(
        self, role: str, expected_ha_kind: str
    ) -> None:
        """The HA domain is built inside create_devices() off options["ha_kind"]; no loopback, since only edges route."""
        templates = [{"role": role, "quantity": 2, "template": _EDGE_TEMPLATE}]
        gen = _made_zone(fabric_templates=templates)
        gen.create_devices = AsyncMock(return_value=["a-fr201", "a-fr202"])

        await gen._create_fabric_devices(templates=templates)

        kwargs = gen.create_devices.await_args_list[-1].kwargs
        assert kwargs["options"]["ha_kind"] == expected_ha_kind
        assert "allocate_loopback" not in kwargs["options"]
        assert "loopback_prefix_length" not in kwargs["options"]
        assert kwargs["deployment_id"] == "zone-1"

    @pytest.mark.asyncio
    async def test_naming_convention_is_read_from_the_parent_metro_lowercased_and_defaulted(self) -> None:
        templates = [{"role": "edge", "quantity": 2, "template": _EDGE_TEMPLATE}]
        gen = _made_zone(fabric_templates=templates)
        gen.create_devices = AsyncMock(return_value=[])

        gen.data["parent"]["naming_convention"] = "Standard"
        await gen._create_fabric_devices(templates=templates)
        assert gen.create_devices.await_args_list[-1].kwargs["naming_convention"] == "standard"

        gen.data["parent"]["naming_convention"] = None
        await gen._create_fabric_devices(templates=templates)
        assert gen.create_devices.await_args_list[-1].kwargs["naming_convention"] == "standard"

        gen.data["parent"]["naming_convention"] = "Hierarchical"
        await gen._create_fabric_devices(templates=templates)
        assert gen.create_devices.await_args_list[-1].kwargs["naming_convention"] == "hierarchical"

    @pytest.mark.asyncio
    async def test_every_role_gets_its_own_create_devices_call(self) -> None:
        templates = [
            {"role": "edge", "quantity": 2, "template": _EDGE_TEMPLATE},
            {"role": "firewall", "quantity": 2, "template": _EDGE_TEMPLATE},
        ]
        gen = _made_zone(fabric_templates=templates)
        gen.create_devices = AsyncMock(return_value=[])

        await gen._create_fabric_devices(templates=templates)

        assert [c.kwargs["device_role"] for c in gen.create_devices.await_args_list] == ["edge", "firewall"]

    @pytest.mark.asyncio
    async def test_created_names_are_returned_keyed_by_role(self) -> None:
        """_cable_services needs to know which names are edges and which are appliances; nothing else records it."""
        templates = [
            {"role": "edge", "quantity": 2, "template": _EDGE_TEMPLATE},
            {"role": "firewall", "quantity": 2, "template": _EDGE_TEMPLATE},
        ]
        gen = _made_zone(fabric_templates=templates)
        gen.create_devices = AsyncMock(side_effect=[["eg-fr201", "eg-fr202"], ["fw-fr201", "fw-fr202"]])

        physical_names_by_role = await gen._create_fabric_devices(templates=templates)

        assert physical_names_by_role == {"edge": ["eg-fr201", "eg-fr202"], "firewall": ["fw-fr201", "fw-fr202"]}

    @pytest.mark.asyncio
    async def test_two_entries_for_one_role_accumulate(self) -> None:
        """Two firewall entries (two models): the second must not overwrite the first."""
        templates = [
            {"role": "firewall", "quantity": 2, "template": _EDGE_TEMPLATE},
            {"role": "firewall", "quantity": 2, "template": _EDGE_TEMPLATE},
        ]
        gen = _made_zone(fabric_templates=templates)
        gen.create_devices = AsyncMock(side_effect=[["fw-fr201", "fw-fr202"], ["fw-fr203", "fw-fr204"]])

        physical_names_by_role = await gen._create_fabric_devices(templates=templates)

        assert physical_names_by_role == {"firewall": ["fw-fr201", "fw-fr202", "fw-fr203", "fw-fr204"]}

    @pytest.mark.asyncio
    async def test_virtual_devices_are_created_but_never_registered_for_cabling(self) -> None:
        """A DcimCable is fibre between two chassis: provider-hosted instances are created, then withheld.

        Not hypothetical: PA-VM_EDGE_CUSTOMER_* exposes `uplink` on eth[1-6], the role _cable_border_services claims
        on the service side, so without this a virtual edge+firewall pair would reach create_chain_cabling."""
        templates = [
            {"role": "edge", "quantity": 2, "template": _VIRTUAL_EDGE_TEMPLATE},
            {"role": "firewall", "quantity": 2, "template": _VIRTUAL_EDGE_TEMPLATE},
        ]
        gen = _made_virtual(fabric_templates=templates)
        gen.create_devices = AsyncMock(side_effect=[["eg-am-ne01", "eg-am-ne02"], ["fw-am-ne01", "fw-am-ne02"]])

        physical_names_by_role = await gen._create_fabric_devices(templates=templates)

        # Created: withholding them from cabling must not mean skipping them.
        assert [c.kwargs["device_role"] for c in gen.create_devices.await_args_list] == ["edge", "firewall"]
        assert all(c.kwargs["options"]["virtual"] for c in gen.create_devices.await_args_list)
        assert physical_names_by_role == {}
        # Still registered for EVPN: a virtual edge runs the routing like a physical one.
        assert set(gen._edge_devices) == {"eg-am-ne01", "eg-am-ne02"}

    @pytest.mark.asyncio
    async def test_virtual_firewall_pair_in_a_virtual_colocation_gets_the_virtual_option(self) -> None:
        templates = [{"role": "firewall", "quantity": 2, "template": _VIRTUAL_EDGE_TEMPLATE}]
        gen = _made_virtual(fabric_templates=templates)
        gen.create_devices = AsyncMock(return_value=["fw-am-ne01", "fw-am-ne02"])

        await gen._create_fabric_devices(templates=templates)

        options = gen.create_devices.await_args_list[-1].kwargs["options"]
        assert options["virtual"] is True
        assert options["ha_kind"] == "ManagedFirewallHA"


class TestGeneratedDeviceNames:
    """data/demos/30_all/08_interconnects/ references these names literally: the zone or virtual colocation NAME is
    in the device name, so they are part of the generator's contract."""

    @pytest.mark.parametrize(
        ("fabric", "role", "index", "expected"),
        [
            ("fr2", "edge", 1, "eg-fr201"),
            ("fr2", "edge", 2, "eg-fr202"),
            ("pa4", "edge", 1, "eg-pa401"),
            ("fr2", "firewall", 1, "fw-fr201"),
            ("fr2", "firewall", 2, "fw-fr202"),
            ("fr2", "load-balancer", 1, "lb-fr201"),
            # A virtual colocation: the name carries the dash.
            ("am-ne", "edge", 1, "eg-am-ne01"),
        ],
    )
    def test_standard_strategy_with_no_location_path(self, fabric: str, role: str, index: int, expected: str) -> None:
        name = DeviceNamingConfig(strategy="standard").format_device_name(
            DeviceNameContext.from_indexes(fabric_name=fabric, device_role=role, role_index=index, indexes=[])
        )
        assert name == expected

    @pytest.mark.parametrize(
        ("zone_name", "expected_fabric_name"),
        [("FR2", "fr2"), ("PA4", "pa4"), ("CH1", "ch1")],
    )
    @pytest.mark.asyncio
    async def test_fabric_name_is_the_lower_cased_zone_name(self, zone_name: str, expected_fabric_name: str) -> None:
        gen = _make_generator()
        gen._ensure_fabric_pools = AsyncMock()
        gen._create_fabric_devices = AsyncMock(return_value={})
        gen._cable_services = AsyncMock()
        gen._ensure_evpn = AsyncMock(return_value={})
        gen._ensure_edge_pair = AsyncMock()

        await gen.generate({"TopologyColocationZone": [_zone(name=zone_name)]})

        assert gen.fabric_name == expected_fabric_name

    @pytest.mark.asyncio
    async def test_virtual_colocation_fabric_name_is_the_lower_cased_name(self) -> None:
        gen = _make_generator(VirtualColocationGenerator, fabric="x")
        gen._ensure_fabric_pools = AsyncMock()
        gen._create_fabric_devices = AsyncMock(return_value={})
        gen._cable_services = AsyncMock()
        gen._ensure_evpn = AsyncMock(return_value={})
        gen._ensure_edge_pair = AsyncMock()

        await gen.generate({"TopologyVirtualColocation": [_virtual()]})

        assert gen.fabric_name == "am-ne"
        assert gen.pod_name is None
        assert gen.deployment_id == "vc-1"


class TestServiceCabling:
    """The leg from the zone's edge pair to its firewall/load-balancer pair, inside one zone."""

    @pytest.mark.asyncio
    async def test_firewall_pair_is_cabled_to_the_edge_pair(self) -> None:
        gen = _made_zone()
        gen._cable_border_services = AsyncMock()

        await gen._cable_services(
            physical_names_by_role={"edge": ["eg-fr201", "eg-fr202"], "firewall": ["fw-fr201", "fw-fr202"]}
        )

        kwargs = gen._cable_border_services.await_args_list[-1].kwargs
        # The edges are the "border" side: the tier in the underlay that the appliance hangs off.
        assert kwargs["border_names"] == ["eg-fr201", "eg-fr202"]
        assert kwargs["firewall_names"] == ["fw-fr201", "fw-fr202"]
        assert kwargs["load_balancer_names"] == []
        # Same role names dc.py uses: a `firewall`-role port faces a firewall.
        assert kwargs["border_role_for"] == {"firewall": "firewall", "load-balancer": "load-balancer"}

    @pytest.mark.asyncio
    async def test_mode_is_always_pbr(self) -> None:
        """Inline would chain edge->fw->lb->edge, which needs a tier that takes traffic back; an on-ramp forwards on."""
        gen = _made_zone()
        gen._cable_border_services = AsyncMock()

        await gen._cable_services(
            physical_names_by_role={"edge": ["eg-fr201"], "firewall": ["fw-fr201"], "load-balancer": ["lb-fr201"]}
        )

        assert gen._cable_border_services.await_args_list[-1].kwargs["connectivity_mode"] == "pbr"

    @pytest.mark.parametrize(
        "physical_names_by_role",
        [
            # An on-ramp with no appliances at all.
            {"edge": ["eg-pa401", "eg-pa402"]},
            # Appliances declared with no edge pair to hang them off.
            {"firewall": ["fw-pa401", "fw-pa402"]},
            # Nothing declared.
            {},
        ],
    )
    @pytest.mark.asyncio
    async def test_nothing_to_cable_is_a_no_op(self, physical_names_by_role: dict[str, list[str]]) -> None:
        """One side missing must not reach create_chain_cabling: it logs a missing-ports error (a task failure)."""
        gen = _made_zone()
        gen._cable_border_services = AsyncMock()

        await gen._cable_services(physical_names_by_role=physical_names_by_role)

        gen._cable_border_services.assert_not_awaited()
        gen.logger.error.assert_not_called()

    @pytest.mark.asyncio
    async def test_virtual_colocation_never_cables(self) -> None:
        """All virtual kit is withheld from the registry, so a virtual colocation's cabling step is a no-op."""
        templates = [
            {"role": "edge", "quantity": 2, "template": _VIRTUAL_EDGE_TEMPLATE},
            {"role": "firewall", "quantity": 2, "template": _VIRTUAL_EDGE_TEMPLATE},
        ]
        gen = _made_virtual(fabric_templates=templates)
        gen.create_devices = AsyncMock(side_effect=[["eg-am-ne01", "eg-am-ne02"], ["fw-am-ne01", "fw-am-ne02"]])
        gen._cable_border_services = AsyncMock()

        names = await gen._create_fabric_devices(templates=templates)
        await gen._cable_services(physical_names_by_role=names)

        gen._cable_border_services.assert_not_awaited()


def _loopback(device: str, role: str, *, addressed: bool = True, iface_id: str | None = None) -> MagicMock:
    loopback = MagicMock()
    loopback.id = iface_id or f"{device}-{role}"
    loopback.role.value = role
    loopback.device.peer.name.value = device
    loopback.device.peer.id = f"dev-{device}"
    loopback.ip_address.initialized = addressed
    loopback.ip_address.id = f"ip-{device}-{role}"
    return loopback


def _evpn_generator(*, existing_as: bool = True, loopbacks: list | None = None, virtual: bool = False) -> Any:
    """A generator whose EVPN step runs for real; only the SDK edges are mocked.

    client.filters is called twice: the shared-AS lookup by description, then the edges' loopbacks."""
    if virtual:
        gen = _made_virtual()
        gen._edge_devices = {"eg-am-ne01": "cisco_iosxe"}
        edge = "eg-am-ne01"
    else:
        gen = _made_zone()
        gen._edge_devices = {"eg-fr201": "cisco_nxos"}
        edge = "eg-fr201"
    gen._vtep_pool = MagicMock(id="vtep-pool")
    site_as = MagicMock(id="as-site")
    site_as.save = AsyncMock()
    metro_node = MagicMock()
    metro_node.save = AsyncMock()

    gen.client.get = AsyncMock(return_value=metro_node)
    gen.client.filters = AsyncMock(
        side_effect=[
            [site_as] if existing_as else [],
            [_loopback(edge, "loopback")] if loopbacks is None else loopbacks,
        ]
    )
    created = MagicMock()
    created.save = AsyncMock()
    gen.client.create = AsyncMock(return_value=created)
    gen.client.allocate_next_ip_address = AsyncMock(return_value=MagicMock(id="ip-vtep-new"))
    gen.client.group_context.related_node_ids = []
    gen._metro_node = metro_node
    return gen


def _created(gen: Any, kind: str) -> list[dict[str, Any]]:
    """Data of every client.create() call for `kind` (a protocol class or its name)."""
    return [
        c.kwargs["data"]
        for c in gen.client.create.await_args_list
        if getattr(c.kwargs["kind"], "__name__", c.kwargs["kind"]) == kind
    ]


class TestEvpn:
    """_ensure_evpn: the metro's shared site AS, VTEP loopback and overlay BGP per edge."""

    @pytest.mark.asyncio
    async def test_no_edges_is_a_no_op(self) -> None:
        """A zone without edges makes no EVPN objects and touches nothing."""
        gen = _evpn_generator()
        gen._edge_devices = {}

        assert await gen._ensure_evpn() == {}

        gen.client.create.assert_not_awaited()
        gen.client.filters.assert_not_awaited()
        gen.client.get.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_edge_attribute_at_all_is_a_no_op(self) -> None:
        """A zone whose device step never ran (no kit) has no _edge_devices: nothing to do."""
        gen = _made_zone()

        assert await gen._ensure_evpn() == {}

    @pytest.mark.asyncio
    async def test_existing_site_as_is_reused_by_metro_description_and_attached_to_the_metro(self) -> None:
        """The AS is found by '<metro> EVPN site ASN', never re-drawn; evpn_rt_as lands on the METRO, not the zone."""
        gen = _evpn_generator()

        await gen._ensure_evpn()

        assert gen.client.filters.await_args_list[0].kwargs["description__value"] == "fr EVPN site ASN"
        assert _created(gen, "RoutingAutonomousSystem") == []
        gen.client.get.assert_awaited_once_with(kind="TopologySegmentHosting", id="metro-1")
        assert gen._metro_node.evpn_rt_as == {"id": "as-site"}
        gen._metro_node.save.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_site_as_is_drawn_under_a_lock_keyed_by_the_metro_description(self) -> None:
        """Zones of one metro running concurrently must not draw two ASNs: the lock key carries the metro's name."""
        gen = _evpn_generator()

        await gen._ensure_evpn()

        gen.acquire_resource_lock.assert_awaited_once_with("shared-as-fr EVPN site ASN")
        gen.release_resource_lock.assert_awaited_once_with("lock-id")

    @pytest.mark.asyncio
    async def test_site_as_is_never_tracked_by_a_zone_run(self) -> None:
        """Every zone and virtual colocation of the metro finds the same AS, so none of their tracking groups may
        own it: a zone that stopped declaring edges would otherwise delete an AS its sibling's overlay BGP uses."""
        for existing_as in (True, False):
            gen = _evpn_generator(existing_as=existing_as)

            await gen._ensure_evpn()

            assert gen.client.group_context.related_node_ids == []

    @pytest.mark.asyncio
    async def test_new_site_as_is_drawn_from_the_metro_asn_pool(self) -> None:
        gen = _evpn_generator(existing_as=False)

        await gen._ensure_evpn()

        (as_data,) = _created(gen, "RoutingAutonomousSystem")
        assert as_data["asn"] == {"from_pool": {"id": "asn-pool"}}
        assert as_data["description"] == "fr EVPN site ASN"

    @pytest.mark.asyncio
    async def test_two_zones_of_one_metro_share_the_same_as_description(self) -> None:
        """FR2 and FR6 both look up the same description, which is why the metro has exactly one site AS."""
        first = _evpn_generator()
        second = _evpn_generator()
        second.data = _zone(name="FR6", id="zone-6")
        second.fabric_name = "fr6"

        await first._ensure_evpn()
        await second._ensure_evpn()

        descriptions = {g.client.filters.await_args_list[0].kwargs["description__value"] for g in (first, second)}
        assert descriptions == {"fr EVPN site ASN"}
        assert second._metro_node.evpn_rt_as == {"id": "as-site"}

    @pytest.mark.asyncio
    async def test_new_vtep_is_allocated_and_overlay_bgp_created(self) -> None:
        gen = _evpn_generator()

        await gen._ensure_evpn()

        assert gen.client.allocate_next_ip_address.await_args_list[-1].kwargs["identifier"] == "eg-fr201-vtep"
        assert gen.client.allocate_next_ip_address.await_args_list[-1].kwargs["prefix_length"] == 128
        assert gen.client.allocate_next_ip_address.await_args_list[-1].kwargs["resource_pool"].id == "vtep-pool"
        (vtep,) = _created(gen, "DcimVirtualInterface")
        assert vtep["role"] == "loopback-vtep"
        assert vtep["name"] == "loopback1"
        assert "id" not in vtep
        (bgp,) = _created(gen, "ManagedBGP")
        assert bgp["name"] == "eg-fr201-bgp-overlay"
        assert bgp["local_as"] == {"id": "as-site"}
        assert bgp["router_id"] == {"id": "ip-eg-fr201-loopback"}
        assert bgp["process_role"] == "overlay"
        assert bgp["capabilities"] == [{"id": "dev-eg-fr201"}]

    @pytest.mark.asyncio
    async def test_existing_vtep_is_reused_idempotently(self) -> None:
        """A re-run keeps the VTEP's id and address: no new allocation."""
        loopbacks = [_loopback("eg-fr201", "loopback"), _loopback("eg-fr201", "loopback-vtep", iface_id="vtep-1")]
        gen = _evpn_generator(loopbacks=loopbacks)

        await gen._ensure_evpn()

        gen.client.allocate_next_ip_address.assert_not_awaited()
        (vtep,) = _created(gen, "DcimVirtualInterface")
        assert vtep["id"] == "vtep-1"
        assert vtep["ip_address"] == {"id": "ip-eg-fr201-loopback-vtep"}

    @pytest.mark.asyncio
    async def test_edge_without_addressed_loopback_is_skipped(self) -> None:
        gen = _evpn_generator(loopbacks=[_loopback("eg-fr201", "loopback", addressed=False)])

        result = await gen._ensure_evpn()

        gen.logger.error.assert_called_once()
        assert _created(gen, "ManagedBGP") == []
        assert result == {}

    @pytest.mark.asyncio
    async def test_overlay_process_ids_are_returned_by_edge_name(self) -> None:
        gen = _evpn_generator()
        gen.client.create.return_value.id = "bgp-saved"

        assert await gen._ensure_evpn() == {"eg-fr201": "bgp-saved"}

    @pytest.mark.asyncio
    async def test_virtual_colocation_does_evpn_for_its_metro(self) -> None:
        """A virtual edge is a border gateway too: same shared AS description, attached to ITS metro (AM)."""
        gen = _evpn_generator(virtual=True)

        result = await gen._ensure_evpn()

        assert gen.client.filters.await_args_list[0].kwargs["description__value"] == "am EVPN site ASN"
        gen.client.get.assert_awaited_once_with(kind="TopologySegmentHosting", id="metro-am")
        assert gen._metro_node.evpn_rt_as == {"id": "as-site"}
        (bgp,) = _created(gen, "ManagedBGP")
        assert bgp["name"] == "eg-am-ne01-bgp-overlay"
        assert result == {"eg-am-ne01": gen.client.create.return_value.id}


class TestMetroAsnPool:
    """_metro_asn_pool_id: the metro's ASN pool, waiting for add_colocation_metro when it is not visible yet."""

    @pytest.mark.asyncio
    async def test_pool_in_the_query_result_is_used_without_waiting(self) -> None:
        gen = _made_zone()
        gen.wait_for_parent_generators = AsyncMock()
        gen._get_parent_pool_with_retry = AsyncMock()

        assert await gen._metro_asn_pool_id() == "asn-pool"

        gen.wait_for_parent_generators.assert_not_awaited()
        gen._get_parent_pool_with_retry.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("missing", [None, {}])
    async def test_missing_pool_waits_for_the_metro_generator_then_looks_it_up_by_name(
        self, missing: dict[str, Any] | None
    ) -> None:
        """A zone's created-trigger can fire before add_colocation_metro attached the pool."""
        gen = _made_zone()
        gen.data["parent"]["fabric_asn_pool"] = missing
        gen.wait_for_parent_generators = AsyncMock(return_value=True)
        gen._get_parent_pool_with_retry = AsyncMock(return_value=MagicMock(id="late-pool"))

        assert await gen._metro_asn_pool_id() == "late-pool"

        gen.wait_for_parent_generators.assert_awaited_once_with(("add_colocation_metro",), "metro-1")
        gen._get_parent_pool_with_retry.assert_awaited_once_with("fr-asn-pool", kind=CoreNumberPool)

    @pytest.mark.asyncio
    async def test_pool_that_never_appears_fails_the_run(self) -> None:
        gen = _made_zone()
        gen.data["parent"]["fabric_asn_pool"] = None
        gen.wait_for_parent_generators = AsyncMock(return_value=False)
        gen._get_parent_pool_with_retry = AsyncMock(
            side_effect=NodeNotFoundError(branch_name="main", node_type="CoreNumberPool", identifier={})
        )

        with pytest.raises(GeneratorError, match="fr-asn-pool"):
            await gen._metro_asn_pool_id()

        gen.logger.error.assert_called_once()

    @pytest.mark.asyncio
    async def test_evpn_waits_then_draws_from_the_late_pool(self) -> None:
        """End to end through _ensure_evpn: the AS is drawn from the pool found after the wait."""
        gen = _evpn_generator(existing_as=False)
        gen.data["parent"]["fabric_asn_pool"] = None
        gen.wait_for_parent_generators = AsyncMock(return_value=True)
        gen._get_parent_pool_with_retry = AsyncMock(return_value=MagicMock(id="late-pool"))

        await gen._ensure_evpn()

        (as_data,) = _created(gen, "RoutingAutonomousSystem")
        assert as_data["asn"] == {"from_pool": {"id": "late-pool"}}


def _pair_generator(*, legs: list | None = None) -> Any:
    """A generator whose pair step runs for real; only the SDK edges are mocked."""
    gen = _made_zone()
    pool = MagicMock(id="pool-ipv6")
    gen.ensure_sliced_pool = AsyncMock(return_value=pool)
    iface_a, iface_b = MagicMock(id="if-a"), MagicMock(id="if-b")
    gen.create_chain_cabling = AsyncMock(return_value=[[(iface_a, iface_b)]] if legs is None else legs)
    gen._ensure_routing_password = AsyncMock(return_value="key-1")
    gen.ensure_address_family = AsyncMock(side_effect=lambda afi, safi: f"af-{afi}-{safi}")
    created = MagicMock()
    created.save = AsyncMock()
    gen.client.create = AsyncMock(return_value=created)
    gen._pool = pool
    return gen


class TestEdgePair:
    """_ensure_edge_pair: interlink cable + /127 + FABRIC iBGP session, inside one zone."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("edges", [[], ["eg-fr201"], ["eg-fr201", "eg-fr202", "eg-fr203"]])
    async def test_anything_but_two_physical_edges_gets_no_pair(self, edges: list[str]) -> None:
        gen = _pair_generator()

        await gen._ensure_edge_pair(physical_edge_names=edges, overlay_process_ids=_PROCESSES)

        gen.ensure_sliced_pool.assert_not_awaited()
        gen.create_chain_cabling.assert_not_awaited()
        gen.client.create.assert_not_awaited()
        gen.acquire_resource_lock.assert_not_awaited()
        gen.logger.info.assert_called()
        gen.logger.error.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_whole_step_runs_under_the_zone_keyed_lock(self) -> None:
        """One lock per zone for the whole step: overlapping runs of one zone never both create any of it."""
        gen = _pair_generator()

        await gen._ensure_edge_pair(physical_edge_names=["eg-fr201", "eg-fr202"], overlay_process_ids=_PROCESSES)

        gen.acquire_resource_lock.assert_awaited_once_with("edge-pair-fr2")
        gen.release_resource_lock.assert_awaited_once_with("lock-id")

    @pytest.mark.asyncio
    async def test_the_lock_is_released_when_the_step_fails(self) -> None:
        gen = _pair_generator()
        gen.create_chain_cabling = AsyncMock(side_effect=RuntimeError("boom"))

        with pytest.raises(RuntimeError, match="boom"):
            await gen._ensure_edge_pair(physical_edge_names=["eg-fr201", "eg-fr202"], overlay_process_ids=_PROCESSES)

        gen.release_resource_lock.assert_awaited_once_with("lock-id")

    @pytest.mark.asyncio
    async def test_ipv6_technical_pool_is_a_zone_owned_slice(self) -> None:
        gen = _pair_generator()

        await gen._ensure_edge_pair(physical_edge_names=["eg-fr202", "eg-fr201"], overlay_process_ids=_PROCESSES)

        kwargs = gen.ensure_sliced_pool.await_args_list[-1].kwargs
        assert kwargs["pool_name"] == "fr2-technical-ipv6-pool"
        assert kwargs["parent_pool_name"] == "Technical-IPv6"
        assert kwargs["kind"] == "prefix"
        assert kwargs["role"] == "technical"

    @pytest.mark.asyncio
    async def test_cabled_by_interlink_role_with_a_127_from_that_pool(self) -> None:
        gen = _pair_generator()

        await gen._ensure_edge_pair(physical_edge_names=["eg-fr202", "eg-fr201"], overlay_process_ids=_PROCESSES)

        kwargs = gen.create_chain_cabling.await_args_list[-1].kwargs
        assert kwargs["hops"] == [
            {"devices": ["eg-fr201"], "down_role": "interlink"},
            {"devices": ["eg-fr202"], "up_role": "interlink"},
        ]
        assert kwargs["options"]["pool"] is gen._pool
        assert kwargs["options"]["p2p_prefix_length"] == 127

    @pytest.mark.asyncio
    async def test_peering_name_password_roles_and_families(self) -> None:
        gen = _pair_generator()

        await gen._ensure_edge_pair(physical_edge_names=["eg-fr202", "eg-fr201"], overlay_process_ids=_PROCESSES)

        (peering,) = _created(gen, "ManagedBGPPeering")
        assert peering["name"] == "FABRIC-eg-fr201-eg-fr202"
        assert peering["session_type"] == "IBGP"
        assert peering["peering_role"] == "fabric"
        assert peering["ttl"] == 1
        assert peering["password"] == {"id": "key-1"}
        assert peering["address_families"] == [{"id": "af-ipv6-unicast"}, {"id": "af-l2vpn-evpn"}]
        assert peering["capabilities"] == [{"id": "if-a"}, {"id": "if-b"}]
        assert gen._ensure_routing_password.await_args_list[-1].kwargs["name"] == "fr2-fabric-key"

    @pytest.mark.asyncio
    async def test_process_ids_come_from_the_evpn_step_not_a_name_query(self) -> None:
        gen = _pair_generator()
        gen.client.get = AsyncMock(side_effect=AssertionError("no lookup by name"))
        gen.client.filters = AsyncMock(side_effect=AssertionError("no lookup by name"))

        await gen._ensure_edge_pair(physical_edge_names=["eg-fr201", "eg-fr202"], overlay_process_ids=_PROCESSES)

        (peering,) = _created(gen, "ManagedBGPPeering")
        assert peering["bgp_processes"] == [{"id": "bgp-1"}, {"id": "bgp-2"}]

    @pytest.mark.asyncio
    async def test_missing_overlay_process_skips_the_pair(self) -> None:
        gen = _pair_generator()

        await gen._ensure_edge_pair(
            physical_edge_names=["eg-fr201", "eg-fr202"], overlay_process_ids={"eg-fr201": "bgp-1"}
        )

        gen.create_chain_cabling.assert_not_awaited()
        gen.logger.warning.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_interlink_ports_writes_no_peering(self) -> None:
        gen = _pair_generator(legs=[[]])

        await gen._ensure_edge_pair(physical_edge_names=["eg-fr201", "eg-fr202"], overlay_process_ids=_PROCESSES)

        gen.client.create.assert_not_awaited()
        gen.logger.warning.assert_called_once()

    @pytest.mark.asyncio
    async def test_rerun_converges_on_the_same_keys(self) -> None:
        """Idempotent: a second run issues the same deterministic names and identifiers."""
        gen = _pair_generator()

        for _ in range(2):
            await gen._ensure_edge_pair(physical_edge_names=["eg-fr201", "eg-fr202"], overlay_process_ids=_PROCESSES)

        first, second = _created(gen, "ManagedBGPPeering")
        assert first == second
        assert gen.ensure_sliced_pool.await_count == 2
        assert {c.kwargs["pool_name"] for c in gen.ensure_sliced_pool.await_args_list} == {"fr2-technical-ipv6-pool"}
        assert gen.release_resource_lock.await_count == 2

    @pytest.mark.asyncio
    async def test_two_zones_of_one_metro_get_independent_pairs(self) -> None:
        """The pair stays inside one zone: FR2 and FR6 key their lock, pool and key by their own names."""
        fr2 = _pair_generator()
        fr6 = _pair_generator()
        fr6.fabric_name = "fr6"

        await fr2._ensure_edge_pair(physical_edge_names=["eg-fr201", "eg-fr202"], overlay_process_ids=_PROCESSES)
        await fr6._ensure_edge_pair(
            physical_edge_names=["eg-fr601", "eg-fr602"],
            overlay_process_ids=_PROCESSES
            | {
                "eg-fr601": "b-1",
                "eg-fr602": "b-2",
            },
        )

        assert fr2.acquire_resource_lock.await_args_list[-1].args == ("edge-pair-fr2",)
        assert fr6.acquire_resource_lock.await_args_list[-1].args == ("edge-pair-fr6",)
        assert fr6.ensure_sliced_pool.await_args_list[-1].kwargs["pool_name"] == "fr6-technical-ipv6-pool"

    @pytest.mark.asyncio
    async def test_virtual_colocation_never_forms_a_pair(self) -> None:
        """Two virtual edges are not physical: generate() hands the pair step an empty list, so it logs and returns."""
        gen = _made_virtual()
        gen.create_devices = AsyncMock(return_value=["eg-am-ne01", "eg-am-ne02"])
        gen.create_chain_cabling = AsyncMock()
        gen.ensure_sliced_pool = AsyncMock()

        names = await gen._create_fabric_devices(templates=gen.data["fabric_templates"])
        await gen._ensure_edge_pair(
            physical_edge_names=names.get("edge", []),
            overlay_process_ids={"eg-am-ne01": "b-1", "eg-am-ne02": "b-2"},
        )

        gen.create_chain_cabling.assert_not_awaited()
        gen.ensure_sliced_pool.assert_not_awaited()
        gen.acquire_resource_lock.assert_not_awaited()


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
