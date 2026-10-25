"""Unit tests for OfficeFabricGenerator's WAN exit to the office's SD-WAN edge (generators/topology/office_fabric.py).

The mixins that talk to Infrahub are replaced by mocks. The tests pin when the WAN exit is built, what it cables and
addresses, and that it skips (never raises) while the edge is not there.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from generators.helpers.naming import sdwan_edge_name
from generators.logger import GeneratorError
from generators.protocols import DcimPhysicalDevice, DcimPhysicalInterface, DcimVirtualInterface, ManagedBGP
from tests.unit.office_fabric_generator_helpers import _DISTRIBUTION_TEMPLATE, _make_generator, _office
from utils.exchange_transit import wan_exit_vlan
from utils.limits import OFFICE_EDGE_WAIT


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

        assert exit_.gen.client.filters.await_count == OFFICE_EDGE_WAIT.attempts
        assert sleep.await_count == OFFICE_EDGE_WAIT.attempts - 1
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
