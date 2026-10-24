"""Pools, devices, device names and service cabling of ColocationZoneGenerator.

The generator lives in generators/topology/colocation_fabric.py.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from infrahub_sdk.exceptions import NodeNotFoundError

from generators.helpers.naming import DeviceNameContext, DeviceNamingConfig
from generators.logger import GeneratorError
from generators.protocols import TopologyColocationZone, TopologyVirtualColocation
from generators.topology.colocation_fabric import VirtualColocationGenerator
from generators.topology.colocation_pools import (
    _COLO_LOOPBACK_PREFIX_LENGTH,
    _COLO_MANAGEMENT_PREFIX_LENGTH,
    _COLO_TECHNICAL_PREFIX_LENGTH,
)
from tests.unit.colocation_zone_generator_helpers import (
    _EDGE_TEMPLATE,
    _VIRTUAL_EDGE_TEMPLATE,
    _made_virtual,
    _made_zone,
    _make_generator,
    _virtual,
    _zone,
)


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
        # A physical template must not be flipped to a virtual device or an appliance.
        assert "virtual" not in kwargs["options"]
        assert "appliance" not in kwargs["options"]
        # An edge router peers, it does not fail over.
        assert "ha_kind" not in kwargs["options"]

    @pytest.mark.asyncio
    async def test_edge_platform_is_recorded_for_the_evpn_step(self) -> None:
        gen = _made_zone()
        gen.create_devices = AsyncMock(return_value=["eg-fr201", "eg-fr202"])

        await gen._create_fabric_devices(templates=gen.data["fabric_templates"])

        assert gen._edge_devices == {"eg-fr201": "cisco_nxos", "eg-fr202": "cisco_nxos"}

    @pytest.mark.asyncio
    async def test_appliance_template_creates_appliances(self) -> None:
        """A provider-hosted router (Network Edge, MCR) is a CloudNetworkAppliance, selected via options["appliance"]."""
        gen = _made_virtual()
        gen.create_devices = AsyncMock(return_value=["eg-am-ne01", "eg-am-ne02"])

        await gen._create_fabric_devices(templates=gen.data["fabric_templates"])

        kwargs = gen.create_devices.await_args_list[-1].kwargs
        assert kwargs["options"]["appliance"] is True
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
    async def test_appliances_are_created_but_never_registered_for_cabling(self) -> None:
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
        assert all(c.kwargs["options"]["appliance"] for c in gen.create_devices.await_args_list)
        assert physical_names_by_role == {}
        # Still registered for EVPN: a virtual edge runs the routing like a physical one.
        assert set(gen._edge_devices) == {"eg-am-ne01", "eg-am-ne02"}

    @pytest.mark.asyncio
    async def test_firewall_pair_in_a_virtual_colocation_gets_the_appliance_option(self) -> None:
        templates = [{"role": "firewall", "quantity": 2, "template": _VIRTUAL_EDGE_TEMPLATE}]
        gen = _made_virtual(fabric_templates=templates)
        gen.create_devices = AsyncMock(return_value=["fw-am-ne01", "fw-am-ne02"])

        await gen._create_fabric_devices(templates=templates)

        options = gen.create_devices.await_args_list[-1].kwargs["options"]
        assert options["appliance"] is True
        assert options["ha_kind"] == "ManagedFirewallHA"

    @pytest.mark.asyncio
    async def test_edge_routers_of_a_virtual_colocation_are_independent_appliances_ordered_under_the_account(
        self,
    ) -> None:
        """No redundancy representation for routers: no HA kind, no HA data; each router is its own appliance."""
        gen = _made_virtual(account={"id": "acct-1"})
        gen.create_devices = AsyncMock(return_value=["eg-am-ne01", "eg-am-ne02"])

        await gen._create_fabric_devices(templates=gen.data["fabric_templates"])

        options = gen.create_devices.await_args_list[-1].kwargs["options"]
        assert options["appliance"] is True
        assert options["account_id"] == "acct-1"
        assert "ha_kind" not in options

    @pytest.mark.asyncio
    async def test_firewall_pair_of_a_virtual_colocation_is_a_firewall_ha_cluster(self) -> None:
        templates = [{"role": "firewall", "quantity": 2, "template": _VIRTUAL_EDGE_TEMPLATE}]
        gen = _made_virtual(fabric_templates=templates, account={"id": "acct-1"})
        gen.create_devices = AsyncMock(return_value=["fw-am-ne01", "fw-am-ne02"])

        await gen._create_fabric_devices(templates=templates)

        options = gen.create_devices.await_args_list[-1].kwargs["options"]
        # The usual firewall HA domain: its default mode (active-passive) is the Equinix cluster.
        assert options["ha_kind"] == "ManagedFirewallHA"
        assert options["appliance"] is True
        assert options["account_id"] == "acct-1"

    @pytest.mark.asyncio
    async def test_load_balancer_pair_keeps_its_usual_ha_domain(self) -> None:
        """Only the roles Terraform renders get the provider-hosted domain; a load balancer pair is a LoadbalancerHA."""
        templates = [{"role": "load-balancer", "quantity": 2, "template": _VIRTUAL_EDGE_TEMPLATE}]
        gen = _made_virtual(fabric_templates=templates)
        gen.create_devices = AsyncMock(return_value=["lb-am-ne01", "lb-am-ne02"])

        await gen._create_fabric_devices(templates=templates)

        options = gen.create_devices.await_args_list[-1].kwargs["options"]
        assert options["appliance"] is True
        assert options["ha_kind"] == "ManagedLoadbalancerHA"
        assert "ha_data" not in options

    @pytest.mark.asyncio
    async def test_a_presence_without_an_account_orders_its_appliances_under_none(self) -> None:
        gen = _made_virtual(account=None)
        gen.create_devices = AsyncMock(return_value=["eg-am-ne01", "eg-am-ne02"])

        await gen._create_fabric_devices(templates=gen.data["fabric_templates"])

        assert "account_id" not in gen.create_devices.await_args_list[-1].kwargs["options"]

    @pytest.mark.asyncio
    async def test_a_zones_edges_are_never_an_ha_domain(self) -> None:
        """Physical edges are joined by their interlink (_ensure_edge_pair), not by an HA domain."""
        gen = _made_zone()
        gen.create_devices = AsyncMock(return_value=["eg-fr201", "eg-fr202"])

        await gen._create_fabric_devices(templates=gen.data["fabric_templates"])

        options = gen.create_devices.await_args_list[-1].kwargs["options"]
        assert "ha_kind" not in options and "account_id" not in options


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
