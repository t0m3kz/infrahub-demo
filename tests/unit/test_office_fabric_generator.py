"""Unit tests for OfficeFabricGenerator (generators/topology/office_fabric.py).

The generator turns an office's ``fabric_templates`` into an SD-Access style LISP
site: pools, a distribution pair and an access pair, their cabling, an OSPF
underlay and the site key. The mixins that talk to Infrahub are replaced by
mocks, so the tests pin what this file decides:

- which fabric_templates entries are usable (and that a bad one warns, never
  fails the office — FailOnErrorLogger turns error() into a task failure);
- the pools it asks for, and their kinds and namespaces;
- which ports it cables and how, and when it skips a link;
- the order of the steps and what it hands the routing planner: an underlay-only
  plan over the cabled links, in an area the generator itself created;
- the names the devices get.

The WAN exit to the office's SD-WAN edge is covered in test_office_fabric_generator_wan_exit.py.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from generators.helpers.naming import DeviceNameContext, DeviceNamingConfig
from generators.protocols import (
    TopologyCustomerOffice,
)
from generators.topology.office_fabric import (
    _OFFICE_LOOPBACK_PREFIX_LENGTH,
    _OFFICE_MANAGEMENT_PREFIX_LENGTH,
    _OFFICE_TECHNICAL_PREFIX_LENGTH,
    _OFFICE_VALID_FABRIC_ROLES,
)
from tests.unit.office_fabric_generator_helpers import (
    _ACCESS_ENTRY,
    _ACCESS_TEMPLATE,
    _DISTRIBUTION_ENTRY,
    _DISTRIBUTION_TEMPLATE,
    _VIRTUAL_TEMPLATE,
    _make_generator,
    _office,
)
from utils.fabric_roles import CAMPUS_FABRIC_ROLES, LISP_BORDER_ROLES


def _payload(*offices: dict[str, Any]) -> dict[str, Any]:
    """The raw GraphQL response clean_data() unwraps (attributes as {"value": ...}, relations as edges)."""

    def _entry(entry: dict[str, Any]) -> dict[str, Any]:
        return {
            "node": {
                "role": {"value": entry["role"]},
                "quantity": {"value": entry["quantity"]},
                "template": {"node": entry["template"]},
            }
        }

    return {
        "TopologyCustomerOffice": {
            "edges": [
                {
                    "node": {
                        "id": office["id"],
                        "name": {"value": office["name"]},
                        "naming_convention": {"value": office["naming_convention"]},
                        "sdwan_gateway": {"node": office["sdwan_gateway"]},
                        "fabric_templates": {"edges": [_entry(e) for e in office["fabric_templates"]]},
                    }
                }
                for office in offices
            ]
        }
    }


class TestRoles:
    def test_the_office_fabric_has_exactly_the_two_campus_roles(self) -> None:
        assert _OFFICE_VALID_FABRIC_ROLES == {"access-switch", "distribution-switch"}
        assert CAMPUS_FABRIC_ROLES == {"access-switch"}
        assert LISP_BORDER_ROLES == {"distribution-switch"}

    @pytest.mark.parametrize(
        ("role", "expected"),
        [("distribution-switch", "ds-c005-office01"), ("access-switch", "as-c005-office01")],
    )
    def test_standard_names_carry_the_role_code_and_the_office(self, role: str, expected: str) -> None:
        """create_devices() names a flat office device "{role code}-{office}{NN}"."""
        name = DeviceNamingConfig(strategy="standard").format_device_name(
            DeviceNameContext.from_indexes(fabric_name="c005-office", device_role=role, role_index=1, indexes=[])
        )
        assert name == expected


class TestFabricTemplateValidation:
    def _gen(self, entries: list[dict[str, Any]]) -> Any:
        gen = _make_generator()
        gen.fabric_name = "c005-office"
        gen.data = _office(fabric_templates=entries)
        return gen

    def test_valid_entries_are_kept(self) -> None:
        gen = self._gen([_DISTRIBUTION_ENTRY, _ACCESS_ENTRY])

        assert gen._valid_fabric_templates() == [_DISTRIBUTION_ENTRY, _ACCESS_ENTRY]
        gen.logger.warning.assert_not_called()

    @pytest.mark.parametrize(
        "entry",
        [
            # not a campus role: an office has no leaf/spine/edge tier of its own
            {"role": "leaf", "quantity": 2, "template": _ACCESS_TEMPLATE},
            {"role": "border-leaf", "quantity": 2, "template": _DISTRIBUTION_TEMPLATE},
            {"role": None, "quantity": 2, "template": _ACCESS_TEMPLATE},
            {"role": "access-switch", "quantity": 0, "template": _ACCESS_TEMPLATE},
            {"role": "access-switch", "quantity": 2, "template": None},
            # a campus switch is hardware
            {"role": "access-switch", "quantity": 2, "template": _VIRTUAL_TEMPLATE},
        ],
    )
    def test_unusable_entry_warns_and_is_dropped(self, entry: dict[str, Any]) -> None:
        gen = self._gen([entry])

        assert gen._valid_fabric_templates() == []
        gen.logger.warning.assert_called_once()
        gen.logger.error.assert_not_called()

    def test_one_bad_entry_does_not_drop_the_good_ones(self) -> None:
        gen = self._gen([{"role": "leaf", "quantity": 2, "template": _ACCESS_TEMPLATE}, _DISTRIBUTION_ENTRY])

        assert gen._valid_fabric_templates() == [_DISTRIBUTION_ENTRY]

    def test_second_entry_for_a_role_is_dropped(self) -> None:
        """Device names derive from the role alone, so a second entry would rename the first one's devices."""
        second = {"role": "access-switch", "quantity": 4, "template": _ACCESS_TEMPLATE}
        gen = self._gen([_ACCESS_ENTRY, second])

        assert gen._valid_fabric_templates() == [_ACCESS_ENTRY]
        gen.logger.warning.assert_called_once()

    def test_no_fabric_templates_is_no_fabric(self) -> None:
        gen = self._gen([])

        assert gen._valid_fabric_templates() == []
        gen.logger.warning.assert_not_called()


class TestPools:
    @pytest.mark.asyncio
    async def test_slices_each_bootstrap_pool_under_the_office_name(self) -> None:
        gen = _make_generator()
        gen.fabric_name = "c005-office"

        pools = await gen._ensure_office_pools(office_id="office-1")

        calls = {call.kwargs["pool_name"]: call.kwargs for call in gen.ensure_sliced_pool.await_args_list}
        assert calls == {
            "c005-office-loopback-pool": {
                "pool_name": "c005-office-loopback-pool",
                "parent_pool_name": "Loopback-IPv4",
                "prefix_length": _OFFICE_LOOPBACK_PREFIX_LENGTH,
                "role": "loopback",
                "kind": "address",
            },
            "c005-office-management-pool": {
                "pool_name": "c005-office-management-pool",
                "parent_pool_name": "Management-IPv4",
                "prefix_length": _OFFICE_MANAGEMENT_PREFIX_LENGTH,
                "role": "management",
                "kind": "address",
                "namespace": "MANAGEMENT",
            },
            "c005-office-technical-pool": {
                "pool_name": "c005-office-technical-pool",
                "parent_pool_name": "Technical-IPv4",
                "prefix_length": _OFFICE_TECHNICAL_PREFIX_LENGTH,
                "role": "technical",
                "kind": "prefix",
            },
        }
        assert set(pools) == {"loopback", "management", "technical"}

    @pytest.mark.asyncio
    async def test_vni_pool_is_the_offices_own_and_attached_as_vni_pool(self) -> None:
        """The pool add_vxlan_segment reads off the office (TopologySegmentHosting.vni_pool)."""
        gen = _make_generator()
        gen.fabric_name = "c005-office"

        await gen._ensure_office_pools(office_id="office-1")

        vni = gen.upsert_number_pool.await_args.kwargs
        assert vni["pool_name"] == "c005-office-vni-pool"
        assert (vni["node"], vni["node_attribute"]) == ("ManagedSegmentDeployment", "vni")
        assert (vni["start_range"], vni["end_range"]) == (10001, 39999)
        gen.attach_refs.assert_awaited_once_with(
            kind=TopologyCustomerOffice, node_id="office-1", refs={"vni_pool": "vni-pool-id"}
        )

    @pytest.mark.asyncio
    async def test_pools_are_created_under_a_per_office_lock(self) -> None:
        gen = _make_generator()
        gen.fabric_name = "c005-office"
        keys: list[str] = []

        @asynccontextmanager
        async def _recording_lock(key: str) -> AsyncGenerator[None]:
            keys.append(key)
            yield

        gen.resource_lock = _recording_lock

        await gen._ensure_office_pools(office_id="office-1")

        assert keys == ["office-pools-office-1"]


class TestDevices:
    @pytest.mark.asyncio
    async def test_every_entry_becomes_devices_of_its_role_with_loopback_and_pools(self) -> None:
        gen = _make_generator()
        gen.fabric_name = "c005-office"
        gen.data = _office()
        pools = {"loopback": MagicMock(), "management": MagicMock(), "technical": MagicMock()}

        names = await gen._create_office_devices(
            templates=[_DISTRIBUTION_ENTRY, _ACCESS_ENTRY], office_id="office-1", pools=pools
        )

        assert names == {
            "distribution-switch": ["ds-c005-office01", "ds-c005-office02"],
            "access-switch": ["as-c005-office01", "as-c005-office02"],
        }
        by_role = {call.kwargs["device_role"]: call.kwargs for call in gen.create_devices.await_args_list}
        for role, entry in (("distribution-switch", _DISTRIBUTION_ENTRY), ("access-switch", _ACCESS_ENTRY)):
            kwargs = by_role[role]
            assert kwargs["deployment_id"] == "office-1"
            assert kwargs["quantity"] == 2
            assert kwargs["template"] is entry["template"]
            assert kwargs["naming_convention"] == "standard"
            options = kwargs["options"]
            assert options["indexes"] == []
            assert options["allocate_loopback"] is True
            assert options["loopback_prefix_length"] == 32
            assert not options.get("virtual")
            # explicit pool objects, not the name lookup a not-yet-visible pool would race
            assert options["loopback_pool"] is pools["loopback"]
            assert options["management_pool"] is pools["management"]

    @pytest.mark.asyncio
    async def test_naming_convention_comes_from_the_office(self) -> None:
        gen = _make_generator()
        gen.fabric_name = "c005-office"
        gen.data = _office(naming_convention="Flat")
        pools = {"loopback": MagicMock(), "management": MagicMock(), "technical": MagicMock()}

        await gen._create_office_devices(templates=[_ACCESS_ENTRY], office_id="office-1", pools=pools)

        assert gen.create_devices.await_args.kwargs["naming_convention"] == "flat"


class TestCabling:
    def _gen(self) -> Any:
        gen = _make_generator()
        gen.fabric_name = "c005-office"
        return gen

    @pytest.mark.asyncio
    async def test_access_uplinks_cable_to_distribution_downlinks_over_a_p2p_pool(self) -> None:
        gen = self._gen()
        pool = MagicMock()

        await gen._cable_office_fabric(
            templates=[_DISTRIBUTION_ENTRY, _ACCESS_ENTRY],
            access_names=["as-c005-office01", "as-c005-office02"],
            distribution_names=["ds-c005-office01", "ds-c005-office02"],
            technical_pool=pool,
        )

        kwargs = gen.create_cabling.await_args.kwargs
        assert kwargs["bottom_devices"] == ["as-c005-office01", "as-c005-office02"]
        assert kwargs["bottom_interfaces"] == ["HundredGigE1/1/1", "HundredGigE1/1/2"]
        assert kwargs["top_devices"] == ["ds-c005-office01", "ds-c005-office02"]
        assert kwargs["top_interfaces"] == ["HundredGigE1/0/1", "HundredGigE1/0/2"]
        # every access-switch dual-homed to every distribution-switch
        assert kwargs["strategy"] == "rack"
        assert kwargs["options"]["pool"] is pool
        assert kwargs["options"]["p2p_prefix_length"] == 31

    @pytest.mark.asyncio
    async def test_the_distribution_pair_is_linked_on_its_interlink_ports(self) -> None:
        gen = self._gen()
        pool = MagicMock()

        pairs = await gen._cable_office_fabric(
            templates=[_DISTRIBUTION_ENTRY, _ACCESS_ENTRY],
            access_names=["as-c005-office01"],
            distribution_names=["ds-c005-office02", "ds-c005-office01"],
            technical_pool=pool,
        )

        hops = gen.create_chain_cabling.await_args.kwargs["hops"]
        assert hops == [
            {"devices": ["ds-c005-office01"], "down_role": "interlink"},
            {"devices": ["ds-c005-office02"], "up_role": "interlink"},
        ]
        assert gen.create_chain_cabling.await_args.kwargs["options"]["pool"] is pool
        # the routing planner gets both the uplinks and the interlink
        assert pairs == [("a", "b"), ("x", "y")]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("count", [1, 3])
    async def test_no_interlink_unless_exactly_two_distribution_switches(self, count: int) -> None:
        gen = self._gen()

        await gen._cable_office_fabric(
            templates=[_DISTRIBUTION_ENTRY, _ACCESS_ENTRY],
            access_names=["as-c005-office01"],
            distribution_names=[f"ds-c005-office0{n}" for n in range(1, count + 1)],
            technical_pool=MagicMock(),
        )

        gen.create_chain_cabling.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_interlink_ports_warn_but_keep_the_uplinks(self) -> None:
        gen = self._gen()
        gen.create_chain_cabling = AsyncMock(return_value=[[]])

        pairs = await gen._cable_office_fabric(
            templates=[_DISTRIBUTION_ENTRY, _ACCESS_ENTRY],
            access_names=["as-c005-office01"],
            distribution_names=["ds-c005-office01", "ds-c005-office02"],
            technical_pool=MagicMock(),
        )

        assert pairs == [("a", "b")]
        gen.logger.warning.assert_called_once()
        assert "interlink" in gen.logger.warning.call_args.args[0]
        gen.logger.error.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("missing", ["uplink", "downlink"])
    async def test_a_template_without_the_needed_ports_is_an_error_and_cables_nothing(self, missing: str) -> None:
        """Empty name lists must never reach create_cabling: an empty name filter matches every port."""
        gen = self._gen()
        access = dict(_ACCESS_TEMPLATE, interfaces=[i for i in _ACCESS_TEMPLATE["interfaces"] if i["role"] != "uplink"])
        distribution = dict(
            _DISTRIBUTION_TEMPLATE,
            interfaces=[i for i in _DISTRIBUTION_TEMPLATE["interfaces"] if i["role"] != "downlink"],
        )
        templates = [
            {**_DISTRIBUTION_ENTRY, "template": distribution if missing == "downlink" else _DISTRIBUTION_TEMPLATE},
            {**_ACCESS_ENTRY, "template": access if missing == "uplink" else _ACCESS_TEMPLATE},
        ]

        pairs = await gen._cable_office_fabric(
            templates=templates,
            access_names=["as-c005-office01"],
            distribution_names=["ds-c005-office01"],
            technical_pool=MagicMock(),
        )

        assert pairs == []
        gen.create_cabling.assert_not_awaited()
        gen.logger.error.assert_called_once()
        assert f"{missing}_ports=0" in gen.logger.error.call_args.args[0]

    @pytest.mark.asyncio
    async def test_distribution_only_office_is_not_cabled_to_access(self) -> None:
        gen = self._gen()

        pairs = await gen._cable_office_fabric(
            templates=[_DISTRIBUTION_ENTRY],
            access_names=[],
            distribution_names=["ds-c005-office01", "ds-c005-office02"],
            technical_pool=MagicMock(),
        )

        gen.create_cabling.assert_not_awaited()
        assert pairs == [("x", "y")]


class TestGenerate:
    @pytest.mark.asyncio
    async def test_runs_every_step_in_order_and_routes_the_underlay_only(self) -> None:
        gen = _make_generator()
        manager = MagicMock()
        for name in (
            "ensure_sliced_pool",
            "upsert_number_pool",
            "create_devices",
            "create_cabling",
            "create_chain_cabling",
            "ensure_ospf_area",
            "create_routing",
            "ensure_lisp_site_key",
            "_ensure_wan_exit",
        ):
            manager.attach_mock(getattr(gen, name), name)

        await gen.generate(_payload(_office()))
        calls = [call[0] for call in manager.mock_calls]

        # the three IP pools and the VNI pool, then devices, links, area, routing, key, and last the WAN exit
        assert calls[:4] == ["ensure_sliced_pool"] * 3 + ["upsert_number_pool"]
        assert calls[4:] == [
            "create_devices",
            "create_devices",
            "create_cabling",
            "create_chain_cabling",
            "ensure_ospf_area",
            "create_routing",
            "ensure_lisp_site_key",
            "_ensure_wan_exit",
        ]
        assert (gen.fabric_name, gen.pod_name, gen.deployment_id) == ("c005-office", None, "office-1")

        routing = gen.create_routing.await_args.kwargs
        assert routing["bottom_devices"] == ["as-c005-office01", "as-c005-office02"]
        assert routing["top_devices"] == ["ds-c005-office01", "ds-c005-office02"]
        assert routing["bottom_role"] == "access-switch"
        assert routing["top_role"] == "distribution-switch"
        assert routing["p2p_interfaces"] == [("a", "b"), ("x", "y")]
        options = routing["options"]
        assert options["no_overlay"] is True
        assert options["ospf_area_id"] == "area-id"
        assert options["design"] == {"routing_strategy": "ospf-ibgp"}
        gen.ensure_lisp_site_key.assert_awaited_once_with(deployment_id="office-1")
        # the exit is built toward the distribution pair, from its own template's ports
        gen._ensure_wan_exit.assert_awaited_once_with(
            distribution_names=["ds-c005-office01", "ds-c005-office02"], distribution_template=_DISTRIBUTION_TEMPLATE
        )
        gen.logger.error.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_distribution_switch_means_no_wan_exit(self) -> None:
        """An access-only office has nothing to exit from."""
        gen = _make_generator()

        await gen.generate(_payload(_office(fabric_templates=[_ACCESS_ENTRY])))

        gen._ensure_wan_exit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_office_without_fabric_templates_creates_nothing(self) -> None:
        """The opt-in: the created-trigger fires for every office, so most runs end here."""
        gen = _make_generator()

        await gen.generate(_payload(_office(fabric_templates=[])))

        gen.ensure_sliced_pool.assert_not_awaited()
        gen.upsert_number_pool.assert_not_awaited()
        gen.create_devices.assert_not_awaited()
        gen.create_routing.assert_not_awaited()
        gen.ensure_lisp_site_key.assert_not_awaited()
        gen._ensure_wan_exit.assert_not_awaited()
        gen.logger.error.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_office_in_the_response_is_an_error(self) -> None:
        gen = _make_generator()

        await gen.generate({"TopologyCustomerOffice": {"edges": []}})

        gen.logger.error.assert_called_once()
        gen.create_devices.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_missing_ospf_area_stops_before_routing_and_the_key(self) -> None:
        gen = _make_generator()
        gen.ensure_ospf_area = AsyncMock(return_value=None)

        await gen.generate(_payload(_office()))

        gen.create_routing.assert_not_awaited()
        gen.ensure_lisp_site_key.assert_not_awaited()
        gen._ensure_wan_exit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_each_office_of_the_response_is_generated_with_its_own_names(self) -> None:
        gen = _make_generator()
        other = _office(id="office-2", name="C002-OFFICE")

        await gen.generate(_payload(_office(), other))

        assert gen.create_routing.await_count == 2
        assert [call.kwargs["node_id"] for call in gen.attach_refs.await_args_list] == ["office-1", "office-2"]
        assert gen.fabric_name == "c002-office"
