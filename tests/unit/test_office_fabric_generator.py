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
- the names the devices get;
- the WAN exit to the office's SD-WAN edge: when it is built, what it cables and
  addresses, and that it skips (never raises) while the edge is not there.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from generators.helpers.naming import DeviceNameContext, DeviceNamingConfig, sdwan_edge_name
from generators.logger import GeneratorError
from generators.protocols import (
    DcimPhysicalDevice,
    DcimPhysicalInterface,
    DcimVirtualInterface,
    ManagedBGP,
    TopologyCustomerOffice,
)
from generators.topology.office_fabric import (
    _EDGE_WAIT_ATTEMPTS,
    _OFFICE_LOOPBACK_PREFIX_LENGTH,
    _OFFICE_MANAGEMENT_PREFIX_LENGTH,
    _OFFICE_TECHNICAL_PREFIX_LENGTH,
    _OFFICE_VALID_FABRIC_ROLES,
    OfficeFabricGenerator,
)
from utils.exchange_transit import wan_exit_vlan
from utils.fabric_roles import CAMPUS_FABRIC_ROLES, LISP_BORDER_ROLES

_ACCESS_TEMPLATE: dict[str, Any] = {
    "id": "tpl-access",
    "template_kind": "TemplateDcimPhysicalDevice",
    "template_name": "C9300-48UXM_ACCESS_SWITCH",
    "platform": {"id": "plat-xe", "name": "cisco_xe"},
    "device_type": "dt-9300",
    "interfaces": [
        {"name": "TenGigabitEthernet1/0/1", "role": "customer"},
        {"name": "HundredGigE1/1/1", "role": "uplink"},
        {"name": "HundredGigE1/1/2", "role": "uplink"},
        {"name": "GigabitEthernet0/0", "role": "management"},
    ],
}
_DISTRIBUTION_TEMPLATE: dict[str, Any] = {
    "id": "tpl-dist",
    "template_kind": "TemplateDcimPhysicalDevice",
    "template_name": "C9500-32C_DISTRIBUTION",
    "platform": {"id": "plat-xe", "name": "cisco_xe"},
    "device_type": "dt-9500",
    "interfaces": [
        {"name": "HundredGigE1/0/1", "role": "downlink"},
        {"name": "HundredGigE1/0/2", "role": "downlink"},
        {"name": "HundredGigE1/0/25", "role": "interlink"},
        {"name": "GigabitEthernet1/0/26", "role": "uplink"},
        {"name": "GigabitEthernet0/0", "role": "management"},
    ],
}
_VIRTUAL_TEMPLATE = {"id": "tpl-virtual", "template_kind": "TemplateDcimVirtualDevice", "template_name": "C8000V"}

_DISTRIBUTION_ENTRY = {"role": "distribution-switch", "quantity": 2, "template": _DISTRIBUTION_TEMPLATE}
_ACCESS_ENTRY = {"role": "access-switch", "quantity": 2, "template": _ACCESS_TEMPLATE}


def _make_generator(*, wan_exit: bool = False) -> Any:
    """A generator with its Infrahub-facing mixins mocked; ``wan_exit`` keeps the real WAN exit step."""
    gen: Any = OfficeFabricGenerator.__new__(OfficeFabricGenerator)
    gen.logger = MagicMock()
    gen.client = MagicMock()
    gen.fabric_name = ""
    gen.pod_name = None
    gen.deployment_id = ""

    @asynccontextmanager
    async def _no_lock(key: str) -> AsyncGenerator[None]:
        yield

    gen.resource_lock = _no_lock
    gen.ensure_sliced_pool = AsyncMock(side_effect=lambda **kwargs: MagicMock(name=kwargs["pool_name"], **kwargs))
    gen.upsert_number_pool = AsyncMock(return_value=MagicMock(id="vni-pool-id"))
    gen.attach_refs = AsyncMock()
    gen.create_devices = AsyncMock(side_effect=_fake_names)
    gen.create_cabling = AsyncMock(return_value=[("a", "b")])
    gen.create_chain_cabling = AsyncMock(return_value=[[("x", "y")]])
    gen.ensure_ospf_area = AsyncMock(return_value="area-id")
    gen.create_routing = AsyncMock()
    gen.ensure_lisp_site_key = AsyncMock(return_value="site-key-id")
    if not wan_exit:
        gen._ensure_wan_exit = AsyncMock()
    return gen


async def _fake_names(*, device_role: str, quantity: int, **_: Any) -> list[str]:
    code = {"access-switch": "as", "distribution-switch": "ds"}[device_role]
    return [f"{code}-c005-office{n:02d}" for n in range(1, quantity + 1)]


def _office(**overrides: Any) -> dict[str, Any]:
    office: dict[str, Any] = {
        "id": "office-1",
        "name": "C005-OFFICE",
        "naming_convention": "standard",
        "sdwan_gateway": {"id": "gw-1"},
        "fabric_templates": [_DISTRIBUTION_ENTRY, _ACCESS_ENTRY],
    }
    office.update(overrides)
    return office


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


def _named(obj_id: str, name: str) -> MagicMock:
    """A mock node with an ``id`` and a ``name.value`` (MagicMock(name=...) would name the mock itself)."""
    node = MagicMock(id=obj_id)
    node.name.value = name
    return node


def _port(port_id: str, name: str, device_id: str, device_name: str, role: str) -> MagicMock:
    port = _named(port_id, name)
    port.role.value = role
    port.device.id = device_id
    port.device.display_label = device_name
    return port


_EDGE = "C005-OFFICE-EDGE1"


class _WanExit:
    """A generator wired for the real WAN exit step, and the mocks it talks to."""

    def __init__(self, *, sdwan_gateway: bool = True, edge_present: bool = True) -> None:
        self.gen = _make_generator(wan_exit=True)
        self.gen.fabric_name = "c005-office"
        self.gen.data = _office(sdwan_gateway={"id": "gw-1"} if sdwan_gateway else None)
        self.created: list[dict[str, Any]] = []
        self.edge = MagicMock(id="edge-id")
        self.namespace = _named("ns-id", "PROD")
        self.edge_ports = [_named("p1", "GE3"), _named("p2", "GE4")]
        self.loopbacks = [
            self._loopback("ds-id-1", "lo-ip-1"),
            self._loopback("ds-id-2", "lo-ip-2"),
        ]
        self.kinds: dict[Any, Any] = {
            DcimPhysicalDevice: [self.edge] if edge_present else [],
            DcimPhysicalInterface: self.edge_ports,
            "IpamNamespace": [self.namespace],
            "CoreIPPrefixPool": [MagicMock(id="transit-pool")],
            DcimVirtualInterface: self.loopbacks,
        }
        client = MagicMock()
        client.filters = AsyncMock(side_effect=self._filters)
        client.get = AsyncMock(return_value=MagicMock(id="asn-pool-id"))
        client.create = AsyncMock(side_effect=self._create)
        client.allocate_next_ip_prefix = AsyncMock(side_effect=lambda **kw: MagicMock(id=f"prefix-{kw['identifier']}"))
        client.group_context.related_node_ids = []
        self.gen.client = client
        self.gen.create_cabling = AsyncMock(
            return_value=[
                (_port("e-1", "GE3", "edge-id", _EDGE, "downlink"), self._switch_port(1)),
                (_port("e-2", "GE4", "edge-id", _EDGE, "downlink"), self._switch_port(2)),
            ]
        )
        self.gen.upsert_p2p_addresses = AsyncMock(
            side_effect=lambda prefix, **kw: [MagicMock(id=f"sw-{prefix.id}"), MagicMock(id=f"eg-{prefix.id}")]
        )
        self.gen.ensure_vlan_subinterface = AsyncMock(side_effect=lambda **kw: MagicMock(id=f"sub-{kw['device_id']}"))
        self.gen._ensure_routing_password = AsyncMock(return_value="key-id")
        self.gen.ensure_shared_as = AsyncMock(side_effect=lambda description, asn_pool_id: f"as[{description}]")
        self.gen.ensure_address_family = AsyncMock(return_value="af-ipv4")
        self.gen.upsert_bgp_peering = AsyncMock()
        self.sleep = patch("generators.topology.office_fabric.asyncio.sleep", new=AsyncMock())

    @staticmethod
    def _switch_port(index: int) -> MagicMock:
        return _port(f"s-{index}", "GigabitEthernet1/0/26", f"ds-id-{index}", f"ds-c005-office0{index}", "uplink")

    @staticmethod
    def _loopback(device_id: str, ip_id: str) -> MagicMock:
        loopback = MagicMock()
        loopback.device.id = device_id
        loopback.ip_address.id = ip_id
        loopback.ip_address.initialized = True
        return loopback

    async def _filters(self, *, kind: Any, **kwargs: Any) -> list[Any]:
        return self.kinds[kind if kind in self.kinds else getattr(kind, "__name__", kind)]

    async def _create(self, *, kind: Any, data: dict[str, Any]) -> MagicMock:
        self.created.append({"kind": kind, **data})
        node = MagicMock(id=f"{kind.__name__}-{data['name']}")
        node.save = AsyncMock()
        return node

    async def run(self, template: dict[str, Any] = _DISTRIBUTION_TEMPLATE) -> None:
        with self.sleep:
            await self.gen._ensure_wan_exit(
                distribution_names=["ds-c005-office01", "ds-c005-office02"], distribution_template=template
            )

    def processes(self) -> dict[str, dict[str, Any]]:
        return {c["name"]: c for c in self.created if c["kind"] is ManagedBGP}

    def filter_calls(self, kind_name: str) -> list[dict[str, Any]]:
        """The client.filters() arguments (``kind`` left out) of the calls for one kind."""
        return [
            {k: v for k, v in call.kwargs.items() if k != "kind"}
            for call in self.gen.client.filters.await_args_list
            if getattr(call.kwargs["kind"], "__name__", call.kwargs["kind"]) == kind_name
        ]


class TestWanExit:
    @pytest.mark.asyncio
    async def test_cables_the_edge_to_the_switches_uplink_ports_one_each(self) -> None:
        exit_ = _WanExit()

        await exit_.run()

        kwargs = exit_.gen.create_cabling.await_args.kwargs
        assert kwargs["bottom_devices"] == [_EDGE]
        assert kwargs["bottom_interfaces"] == ["GE3", "GE4"]
        assert kwargs["top_devices"] == ["ds-c005-office01", "ds-c005-office02"]
        assert kwargs["top_interfaces"] == ["GigabitEthernet1/0/26"]
        assert kwargs["strategy"] == "rack"
        # no technical pool: the link's addresses are the tenant-VRF ones on the sub-interfaces
        assert "options" not in kwargs

    @pytest.mark.asyncio
    async def test_the_edge_is_looked_up_by_the_one_spelling_of_its_name(self) -> None:
        exit_ = _WanExit()

        await exit_.run()

        assert sdwan_edge_name("C005-OFFICE") == _EDGE
        assert exit_.filter_calls("DcimPhysicalDevice") == [{"name__values": [_EDGE]}]

    @pytest.mark.asyncio
    async def test_each_link_gets_its_own_31_from_the_vrf_transit_pool_tracked_by_the_run(self) -> None:
        exit_ = _WanExit()

        await exit_.run()

        calls = exit_.gen.client.allocate_next_ip_prefix.await_args_list
        assert [c.kwargs["identifier"] for c in calls] == ["wan-exit__e-1__s-1", "wan-exit__e-2__s-2"]
        assert {c.kwargs["prefix_length"] for c in calls} == {31}
        assert {c.kwargs["member_type"] for c in calls} == {"address"}
        assert all(c.kwargs["resource_pool"].id == "transit-pool" for c in calls)
        # a pool mutation, not a save: the run claims the prefix itself
        assert exit_.gen.client.group_context.related_node_ids == [
            "prefix-wan-exit__e-1__s-1",
            "prefix-wan-exit__e-2__s-2",
        ]
        assert exit_.filter_calls("CoreIPPrefixPool") == [{"name__value": "FW-Transit-PROD-IPv4"}]

    @pytest.mark.asyncio
    async def test_both_ends_get_a_dot1q_sub_interface_with_the_vrf_tag_and_their_address(self) -> None:
        exit_ = _WanExit()

        await exit_.run()

        calls = [c.kwargs for c in exit_.gen.ensure_vlan_subinterface.await_args_list]
        assert [(c["device_name"], c["device_id"], c["vlan_id_value"], c["role"]) for c in calls[:2]] == [
            ("ds-c005-office01", "ds-id-1", 3800, "uplink"),
            (_EDGE, "edge-id", 3800, "downlink"),
        ]
        # the switch takes the lower address, the edge the upper
        assert calls[0]["ip_address_id"] == "sw-prefix-wan-exit__e-1__s-1"
        assert calls[1]["ip_address_id"] == "eg-prefix-wan-exit__e-1__s-1"
        assert calls[0]["trunk_iface"].id == "s-1" and calls[1]["trunk_iface"].id == "e-1"
        # nothing to link: the peering names the sub-interfaces as its interfaces
        assert all("capability_obj" not in c for c in calls)

    @pytest.mark.asyncio
    async def test_an_office_always_exits_in_the_prod_vrf_with_the_prod_tag(self) -> None:
        """An office has no environment: its exit is the PROD namespace's, never a NON-PROD one."""
        exit_ = _WanExit()

        await exit_.run()

        assert wan_exit_vlan("prod") == 3800
        assert {c.kwargs["vlan_id_value"] for c in exit_.gen.ensure_vlan_subinterface.await_args_list} == {3800}
        assert exit_.filter_calls("CoreIPPrefixPool") == [{"name__value": "FW-Transit-PROD-IPv4"}]
        assert exit_.filter_calls("IpamNamespace") == [{"namespace_type__value": "prod"}]

    @pytest.mark.asyncio
    async def test_one_process_per_switch_and_one_on_the_edge_all_regular_and_tracked(self) -> None:
        exit_ = _WanExit()

        await exit_.run()

        processes = exit_.processes()
        assert set(processes) == {
            "ds-c005-office01-bgp-wan-exit",
            "ds-c005-office02-bgp-wan-exit",
            f"{_EDGE}-bgp-wan-exit",
        }
        assert {p["process_role"] for p in processes.values()} == {"regular"}
        switch = processes["ds-c005-office01-bgp-wan-exit"]
        assert switch["capabilities"] == [{"id": "ds-id-1"}]
        assert switch["router_id"] == {"id": "lo-ip-1"}  # the switch's Loopback0
        assert switch["local_as"] == {"id": "as[c005-office WAN exit ASN]"}
        edge = processes[f"{_EDGE}-bgp-wan-exit"]
        assert edge["capabilities"] == [{"id": "edge-id"}]
        assert edge["router_id"] == {"id": "eg-prefix-wan-exit__e-1__s-1"}  # no loopback: its first handoff address
        assert edge["local_as"] == {"id": "as[c005-office SD-WAN edge ASN]"}

    @pytest.mark.asyncio
    async def test_the_two_as_numbers_are_found_by_description_before_one_is_drawn(self) -> None:
        """A Number pool allocation is not idempotent per run: ensure_shared_as is the query-first helper."""
        exit_ = _WanExit()

        await exit_.run()

        calls = exit_.gen.ensure_shared_as.await_args_list
        assert [c.kwargs for c in calls] == [
            {"description": "c005-office WAN exit ASN", "asn_pool_id": "asn-pool-id"},
            {"description": "c005-office SD-WAN edge ASN", "asn_pool_id": "asn-pool-id"},
        ]
        exit_.gen.client.get.assert_awaited_once()
        assert exit_.gen.client.get.await_args.kwargs["name__value"] == "PRIVATE-ASN32"
        assert not [c for c in exit_.created if "asn" in c]  # no AS node is created here, never a from_pool

    @pytest.mark.asyncio
    async def test_one_ebgp_regular_peering_per_link_on_both_processes_and_both_sub_interfaces(self) -> None:
        exit_ = _WanExit()

        await exit_.run()

        calls = [c.kwargs for c in exit_.gen.upsert_bgp_peering.await_args_list]
        assert [c["name"] for c in calls] == [
            f"WAN-EXIT-ds-c005-office01-{_EDGE}",
            f"WAN-EXIT-ds-c005-office02-{_EDGE}",
        ]
        for call, index in zip(calls, (1, 2)):
            assert call["session_type"] == "EBGP"
            assert call["peering_role"] == "regular"
            assert call["status"] == "active"
            assert call["address_family_ids"] == ["af-ipv4"]
            assert call["password_id"] == "key-id"
            assert call["process_ids"] == [
                f"ManagedBGP-ds-c005-office0{index}-bgp-wan-exit",
                f"ManagedBGP-{_EDGE}-bgp-wan-exit",
            ]
            assert call["interface_ids"] == [f"sub-ds-id-{index}", "sub-edge-id"]
        exit_.gen.ensure_address_family.assert_awaited_once_with("ipv4", "unicast")

    @pytest.mark.asyncio
    async def test_the_writes_run_under_a_per_office_lock_and_the_wait_does_not(self) -> None:
        """Two overlapping runs of one office would both create the processes and sub-interfaces."""
        exit_ = _WanExit()
        held: list[str] = []
        written_under_lock: list[bool] = []

        @asynccontextmanager
        async def _recording_lock(key: str) -> AsyncGenerator[None]:
            held.append(key)
            yield
            held.remove(key)

        async def _cabling(**_: Any) -> list[Any]:
            written_under_lock.append(bool(held))
            return []

        exit_.gen.resource_lock = _recording_lock
        exit_.gen.create_cabling = _cabling

        await exit_.run()

        assert written_under_lock == [True]
        assert held == []

    @pytest.mark.asyncio
    async def test_the_key_is_the_shared_find_or_create_password_helper(self) -> None:
        exit_ = _WanExit()

        await exit_.run()

        exit_.gen._ensure_routing_password.assert_awaited_once()
        assert exit_.gen._ensure_routing_password.await_args.kwargs["name"] == "c005-office-wan-exit-key"

    @pytest.mark.asyncio
    async def test_a_rerun_asks_for_the_same_identifiers_and_names(self) -> None:
        """Idempotency by construction: nothing in the exit depends on a counter or a random value."""
        exit_ = _WanExit()

        await exit_.run()
        first = (
            [c.kwargs["identifier"] for c in exit_.gen.client.allocate_next_ip_prefix.await_args_list],
            [c["name"] for c in exit_.created],
            [c.kwargs["name"] for c in exit_.gen.upsert_bgp_peering.await_args_list],
        )
        exit_.gen.client.allocate_next_ip_prefix.reset_mock()
        exit_.gen.upsert_bgp_peering.reset_mock()
        exit_.created.clear()
        await exit_.run()

        assert first == (
            [c.kwargs["identifier"] for c in exit_.gen.client.allocate_next_ip_prefix.await_args_list],
            [c["name"] for c in exit_.created],
            [c.kwargs["name"] for c in exit_.gen.upsert_bgp_peering.await_args_list],
        )


class TestWanExitSkips:
    """The exit never raises for an edge that is not there: it logs, writes nothing, and the next run builds it."""

    def _assert_nothing_written(self, exit_: _WanExit) -> None:
        exit_.gen.create_cabling.assert_not_awaited()
        exit_.gen.client.allocate_next_ip_prefix.assert_not_awaited()
        exit_.gen.ensure_vlan_subinterface.assert_not_awaited()
        exit_.gen.upsert_bgp_peering.assert_not_awaited()
        exit_.gen.logger.error.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_office_without_a_gateway_looks_once_and_skips(self) -> None:
        exit_ = _WanExit(sdwan_gateway=False, edge_present=False)
        sleep = AsyncMock()

        with patch("generators.topology.office_fabric.asyncio.sleep", new=sleep):
            await exit_.gen._ensure_wan_exit(
                distribution_names=["ds-c005-office01"], distribution_template=_DISTRIBUTION_TEMPLATE
            )

        sleep.assert_not_awaited()
        assert exit_.gen.client.filters.await_count == 1
        self._assert_nothing_written(exit_)
        assert "the next run builds it" in exit_.gen.logger.info.call_args.args[0]

    @pytest.mark.asyncio
    async def test_an_office_with_a_gateway_waits_for_its_edge_then_skips(self) -> None:
        exit_ = _WanExit(edge_present=False)
        sleep = AsyncMock()

        with patch("generators.topology.office_fabric.asyncio.sleep", new=sleep):
            await exit_.gen._ensure_wan_exit(
                distribution_names=["ds-c005-office01"], distribution_template=_DISTRIBUTION_TEMPLATE
            )

        assert exit_.gen.client.filters.await_count == _EDGE_WAIT_ATTEMPTS
        assert sleep.await_count == _EDGE_WAIT_ATTEMPTS - 1
        self._assert_nothing_written(exit_)

    @pytest.mark.asyncio
    async def test_an_edge_without_its_ports_yet_is_waited_for_like_a_missing_edge(self) -> None:
        exit_ = _WanExit()
        exit_.kinds[DcimPhysicalInterface] = []

        await exit_.run()

        self._assert_nothing_written(exit_)

    @pytest.mark.asyncio
    async def test_the_edge_arriving_while_waiting_is_picked_up(self) -> None:
        exit_ = _WanExit()
        answers = iter([[], [], [exit_.edge]])

        async def _filters(*, kind: Any, **kwargs: Any) -> list[Any]:
            if kind is DcimPhysicalDevice:
                return next(answers)
            return exit_.kinds[kind if kind in exit_.kinds else getattr(kind, "__name__", kind)]

        exit_.gen.client.filters = AsyncMock(side_effect=_filters)

        await exit_.run()

        exit_.gen.create_cabling.assert_awaited_once()
        assert exit_.gen.upsert_bgp_peering.await_count == 2

    @pytest.mark.asyncio
    async def test_a_template_without_an_uplink_port_warns_and_cables_nothing(self) -> None:
        exit_ = _WanExit()
        template = dict(
            _DISTRIBUTION_TEMPLATE,
            interfaces=[i for i in _DISTRIBUTION_TEMPLATE["interfaces"] if i["role"] != "uplink"],
        )

        await exit_.run(template)

        self._assert_nothing_written(exit_)
        exit_.gen.logger.warning.assert_called_once()
        assert "uplink" in exit_.gen.logger.warning.call_args.args[0]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("kind", "expected"), [("IpamNamespace", "IpamNamespace of type prod"), ("CoreIPPrefixPool", "transit pool")]
    )
    async def test_a_missing_namespace_or_transit_pool_fails_the_run_before_any_write(
        self, kind: str, expected: str
    ) -> None:
        exit_ = _WanExit()
        exit_.kinds[kind] = []

        with pytest.raises(GeneratorError, match=expected):
            await exit_.run()

        exit_.gen.create_cabling.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_cabling_plan_that_produced_no_link_stops_the_exit(self) -> None:
        exit_ = _WanExit()
        exit_.gen.create_cabling = AsyncMock(return_value=[])

        await exit_.run()

        exit_.gen.client.allocate_next_ip_prefix.assert_not_awaited()
        exit_.gen.upsert_bgp_peering.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_link_whose_sub_interface_failed_is_left_out_and_the_others_are_peered(self) -> None:
        exit_ = _WanExit()
        exit_.gen.ensure_vlan_subinterface = AsyncMock(
            side_effect=lambda **kw: None if kw["device_id"] == "ds-id-1" else MagicMock(id=f"sub-{kw['device_id']}")
        )

        await exit_.run()

        exit_.gen.logger.warning.assert_called()
        peerings = [c.kwargs["name"] for c in exit_.gen.upsert_bgp_peering.await_args_list]
        assert peerings == [f"WAN-EXIT-ds-c005-office02-{_EDGE}"]

    @pytest.mark.asyncio
    async def test_a_switch_without_an_addressed_loopback_gets_no_session(self) -> None:
        exit_ = _WanExit()
        exit_.loopbacks[0].ip_address.initialized = False

        await exit_.run()

        exit_.gen.logger.warning.assert_called()
        assert "ds-c005-office01" in exit_.gen.logger.warning.call_args.args[0]
        peerings = [c.kwargs["name"] for c in exit_.gen.upsert_bgp_peering.await_args_list]
        assert peerings == [f"WAN-EXIT-ds-c005-office02-{_EDGE}"]
        assert "ds-c005-office01-bgp-wan-exit" not in exit_.processes()

    @pytest.mark.asyncio
    async def test_no_key_means_no_session(self) -> None:
        exit_ = _WanExit()
        exit_.gen._ensure_routing_password = AsyncMock(return_value=None)

        await exit_.run()

        exit_.gen.upsert_bgp_peering.assert_not_awaited()
        assert exit_.created == []
