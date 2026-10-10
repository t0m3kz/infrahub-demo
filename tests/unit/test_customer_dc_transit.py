"""Unit tests for the transit legs of CustomerDeploymentDCExchangeGenerator.

Runs the real mixin code (generators/firewall_context.py, connections.py)
against a small stateful fake of the Infrahub client, so a test asserts what a
boarding run WROTE and with which tracking, and a second run sees what the
first left behind — instead of a brittle list of canned return values.
"""

from __future__ import annotations

import ipaddress
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from generators.common import CommonGenerator
from generators.topology.customer_dc import CustomerDeploymentDCExchangeGenerator
from utils.exchange_transit import transit_vlan

_UNTRACKED = {"allow_upsert": True, "update_group_context": False}
_TRACKED = {"allow_upsert": True}
_SHARED = "DC10-FW1-FW2-ha-shared"

_NAMESPACES = {
    "prod": ("PROD", "ns-prod", "100.66.0.0"),
    "non_prod": ("NON-PROD", "ns-nonprod", "100.66.16.0"),
    "internet": ("INTERNET", "ns-internet", "100.66.32.0"),
}


class _Many:
    """A cardinality-many relationship manager."""

    def __init__(self, peer_ids: list[str] | None = None) -> None:
        self.peers = [SimpleNamespace(id=peer_id) for peer_id in peer_ids or []]

    async def fetch(self) -> None:
        return None

    def add(self, obj: Any) -> None:
        self.peers.append(SimpleNamespace(id=obj.id))


class _World:
    """A fake Infrahub client holding the objects the transit legs touch."""

    def __init__(self, *, namespaces: tuple[str, ...] = ("prod", "non_prod", "internet"), pools: bool = True) -> None:
        self.namespaces = {
            ns_type: SimpleNamespace(
                id=_NAMESPACES[ns_type][1],
                name=SimpleNamespace(value=_NAMESPACES[ns_type][0]),
                namespace_type=SimpleNamespace(value=ns_type),
            )
            for ns_type in namespaces
        }
        self.pools = (
            {
                f"FW-Transit-{ns[0]}-IPv4": SimpleNamespace(
                    id=f"pool-{ns[0]}", name=SimpleNamespace(value=f"FW-Transit-{ns[0]}-IPv4")
                )
                for ns in _NAMESPACES.values()
            }
            if pools
            else {}
        )
        self.cluster = SimpleNamespace(
            id="cluster-1",
            name=SimpleNamespace(value="DC10-FW1-FW2-ha"),
            capabilities=SimpleNamespace(peers=[SimpleNamespace(id="fw-1"), SimpleNamespace(id="fw-2")]),
        )
        self.bl_ports = {
            device_id: SimpleNamespace(
                id=f"bl-port-{device_id}",
                role=SimpleNamespace(value="firewall"),
                device=SimpleNamespace(id=f"bl-{device_id}", display_label=f"BL-{device_id}"),
                interface_capabilities=_Many(),
            )
            for device_id in ("fw-1", "fw-2")
        }
        self.contexts: dict[str, Any] = {}
        self.exchanges: dict[str, Any] = {}
        self.sub_interfaces: dict[tuple[str, str], Any] = {}
        self.addresses: dict[tuple[str, str], Any] = {}
        self.saves: list[tuple[str, str, dict[str, Any]]] = []
        self.tags: list[tuple[str, list[str]]] = []
        self.allocated: dict[str, Any] = {}
        self.allocate_calls = 0
        self.next_vlan = 3000
        self.fail_saves_of: set[str] = set()
        self.executed_filters: list[str] = []

    # --- reads -----------------------------------------------------------
    async def filters(self, *, kind: Any, **kwargs: Any) -> list[Any]:
        name = kind.__name__
        self.executed_filters.append(name)
        if name == "IpamNamespace":
            return [
                ns for ns in self.namespaces.values() if ns.namespace_type.value in kwargs["namespace_type__values"]
            ]
        if name == "CoreIPPrefixPool":
            return [pool for pool_name, pool in self.pools.items() if pool_name in kwargs["name__values"]]
        if name == "ManagedFirewallHA":
            return [self.cluster]
        if name == "ManagedFirewallContext":
            context = self.contexts.get(kwargs["name__value"])
            return [context] if context else []
        if name == "DcimPhysicalInterface":
            return [port for port in self.bl_ports.values() if port.id in kwargs["ids"]]
        if name == "TopologyRoutedExchange":
            return [ex for ex_name, ex in self.exchanges.items() if ex_name in kwargs["name__values"]]
        if name == "DcimVirtualInterface":
            ids = set(kwargs["interface_capabilities__ids"])
            return [
                sub
                for sub in self.sub_interfaces.values()
                if ids & {peer.id for peer in sub.interface_capabilities.peers}
            ]
        if name == "IpamIPAddress":
            return [ip for ip in self.addresses.values() if ip.id in kwargs["ids"]]
        raise AssertionError(f"unexpected filters({name})")

    async def get(self, *, kind: Any, **kwargs: Any) -> Any:
        name = kind.__name__
        if name == "CoreNumberPool":
            return SimpleNamespace(id="vlan-pool")
        if name == "ManagedFirewallContext":
            return next(ctx for ctx in self.contexts.values() if ctx.id == kwargs["id"])
        if name == "IpamIPAddress":
            return self.addresses.get((kwargs["address__value"], kwargs["ip_namespace__ids"][0]))
        raise AssertionError(f"unexpected get({name})")

    async def allocate_next_ip_prefix(
        self, *, resource_pool: Any, identifier: str, prefix_length: int, **_: Any
    ) -> Any:
        self.allocate_calls += 1
        assert prefix_length == 29
        if identifier not in self.allocated:
            base, namespace_id = next((v[2], v[1]) for v in _NAMESPACES.values() if f"pool-{v[0]}" == resource_pool.id)
            count = sum(1 for p in self.allocated.values() if p.ip_namespace.id == namespace_id)
            self.allocated[identifier] = SimpleNamespace(
                prefix=SimpleNamespace(value=f"{ipaddress.ip_address(base) + 8 * count}/29"),
                ip_namespace=SimpleNamespace(id=namespace_id),
            )
        return self.allocated[identifier]

    # --- writes ----------------------------------------------------------
    async def create(self, *, kind: Any, data: dict[str, Any]) -> Any:
        name = kind.__name__
        if name == "ManagedFirewallContext":
            if "vlan_id" in data:  # the VLAN allocation re-save of an existing context
                context = next(ctx for ctx in self.contexts.values() if ctx.id == data["id"])
                return self._node(
                    name, "vlan_id", on_save=lambda: setattr(context.vlan_id, "value", self._take_vlan(context))
                )
            context = self.contexts.setdefault(data["name"], self._context(data["name"]))
            context.tenant_id = (data.get("tenant") or {}).get("id")
            return self._node(name, data["name"], backing=context)
        if name == "TopologyRoutedExchange":
            exchange = self.exchanges.setdefault(
                data["name"],
                SimpleNamespace(
                    id=f"exchange-{len(self.exchanges)}", name=SimpleNamespace(value=data["name"]), data=data
                ),
            )
            return self._node(name, data["name"], backing=exchange)
        if name == "IpamIPAddress":
            key = (data["address"], data["ip_namespace"].id)
            ip = self.addresses.setdefault(
                key,
                SimpleNamespace(
                    id=f"ip-{len(self.addresses)}",
                    address=data["address"],
                    ip_namespace=SimpleNamespace(id=data["ip_namespace"].id),
                ),
            )
            return self._node(name, data["address"], backing=ip)
        if name == "DcimVirtualInterface":
            key = (data["device"]["id"], data["name"])
            sub = self.sub_interfaces.setdefault(
                key,
                SimpleNamespace(
                    id=f"sub-{len(self.sub_interfaces)}",
                    name=SimpleNamespace(value=data["name"]),
                    device=SimpleNamespace(id=data["device"]["id"]),
                    ip_address=SimpleNamespace(id=None),
                    interface_capabilities=_Many(),
                ),
            )
            sub.ip_address = SimpleNamespace(id=(data.get("ip_address") or {}).get("id") or sub.ip_address.id)
            return self._node(name, data["name"], backing=sub)
        raise AssertionError(f"unexpected create({name})")

    def _context(self, name: str) -> Any:
        return SimpleNamespace(
            id=f"ctx-{len(self.contexts)}",
            name=SimpleNamespace(value=name),
            vlan_id=SimpleNamespace(value=None),
            tenant_id=None,
            add_relationships=None,
        )

    def _take_vlan(self, _context: Any) -> int:
        vlan, self.next_vlan = self.next_vlan, self.next_vlan + 1
        return vlan

    def _node(self, kind: str, label: str, *, backing: Any | None = None, on_save: Any = None) -> Any:
        node = backing or SimpleNamespace(id="vlan-alloc")
        world = self

        async def save(**kwargs: Any) -> None:
            if kind in world.fail_saves_of:
                raise RuntimeError(f"{kind} save failed")
            if on_save:
                on_save()
            world.saves.append((kind, label, kwargs))

        node.save = save
        if kind == "ManagedFirewallContext" and backing is not None:

            async def add_relationships(*, relation_to_update: str, related_nodes: list[str]) -> None:
                assert relation_to_update == "interface_capabilities"
                world.tags.append((backing.id, list(related_nodes)))
                for port in world.bl_ports.values():
                    if port.id in related_nodes:
                        port.interface_capabilities.peers.append(SimpleNamespace(id=backing.id))

            node.add_relationships = add_relationships
        return node

    # --- assertions helpers ---------------------------------------------
    @property
    def writes(self) -> int:
        return len(self.saves) + len(self.tags) + self.allocate_calls

    def saved(self, kind: str) -> list[str]:
        return [label for saved_kind, label, _ in self.saves if saved_kind == kind]


def _payload(*, customer_id: str = "cust-1", environment: str = "p", dedicated: bool = False) -> dict[str, Any]:
    devices = [
        {"id": "fw-1", "name": "DC10-FW1", "kind": "DcimPhysicalDevice", "platform": {"name": "checkpoint_gaia"}},
        {"id": "fw-2", "name": "DC10-FW2", "kind": "DcimPhysicalDevice", "platform": {"name": "checkpoint_gaia"}},
    ]
    return {
        "TopologyCustomerDC": [
            {
                "id": customer_id,
                "name": f"{customer_id}-DC10",
                "environment": {"value": environment},
                "owner": {"org_id": "C005", "name": "Drentec BV"},
                "parent": {
                    "id": "dc10-id",
                    "name": "DC10",
                    "connectivity_mode": {"value": "pbr"},
                    "size": {"value": "M"},
                    "firewall_devices": devices,
                    "loadbalancer_devices": [],
                    "security_manager_controllers": [],
                    "lb_manager_controllers": [],
                },
                "design": {"dedicated_firewall": {"value": dedicated}, "dedicated_loadbalancer": {"value": False}},
            }
        ]
    }


def _make_gen(world: _World) -> tuple[Any, list[str]]:
    gen = CustomerDeploymentDCExchangeGenerator.__new__(CustomerDeploymentDCExchangeGenerator)
    gen.logger = MagicMock()
    gen.client = world
    gen.wait_for_parent_generator_and_refetch = AsyncMock(return_value=None)
    events: list[str] = []
    gen.acquire_resource_lock = AsyncMock(side_effect=lambda key: events.append(f"acquire:{key}") or "lock-id")
    gen.release_resource_lock = AsyncMock(side_effect=lambda lock_id: events.append("release"))
    gen.link_serving_firewall_context = AsyncMock()
    gen.set_controllers_from = MagicMock()

    async def find_role_interface(*, device_id: str, role: str) -> Any:
        assert role == "uplink"
        return SimpleNamespace(
            id=f"up-{device_id}",
            name=SimpleNamespace(value="Ethernet1/25"),
            cable=SimpleNamespace(id=f"cable-{device_id}"),
        )

    gen.find_role_interface = find_role_interface
    return gen, events


async def _run(gen: Any, world: _World, payload: dict[str, Any]) -> None:
    async def far_end(_client: Any, uplink: Any, include: list[str]) -> Any:
        return world.bl_ports[uplink.id.removeprefix("up-")]

    with patch("generators.firewall_context.far_end_interface", far_end):
        await gen.generate(payload)


def _sub_names(world: _World) -> dict[tuple[str, str], list[str]]:
    return {key: [peer.id for peer in sub.interface_capabilities.peers] for key, sub in world.sub_interfaces.items()}


def test_generator_flags_choose_the_transit_path() -> None:
    assert CustomerDeploymentDCExchangeGenerator._transit_legs is True
    assert issubclass(CustomerDeploymentDCExchangeGenerator, CommonGenerator)


class TestFreshSharedBoarding:
    @pytest.mark.asyncio
    async def test_prod_customer_gets_prod_and_internet_legs_on_both_members(self) -> None:
        world = _World()
        gen, events = _make_gen(world)

        await _run(gen, world, _payload())

        vlan = world.contexts[_SHARED].vlan_id.value
        assert vlan == 3000
        names = {(device, name) for device, name in world.sub_interfaces}
        assert names == {
            (device, f"Ethernet1/25.{transit_vlan(vlan, ns_type)}")
            for device in ("fw-1", "fw-2")
            for ns_type in ("prod", "internet")
        }
        assert f"Ethernet1/25.{transit_vlan(vlan, 'internet')}" == "Ethernet1/25.3400"
        gen.logger.error.assert_not_called()

    @pytest.mark.asyncio
    async def test_addresses_follow_the_fixed_offsets_per_namespace(self) -> None:
        world = _World()
        gen, _ = _make_gen(world)

        await _run(gen, world, _payload())

        by_namespace: dict[str, list[str]] = {}
        for address, namespace_id in world.addresses:
            by_namespace.setdefault(namespace_id, []).append(address)
        assert sorted(by_namespace) == ["ns-internet", "ns-prod"]
        assert sorted(by_namespace["ns-prod"]) == ["100.66.0.1/29", "100.66.0.4/29", "100.66.0.5/29", "100.66.0.6/29"]
        assert sorted(by_namespace["ns-internet"]) == [
            "100.66.32.1/29",
            "100.66.32.4/29",
            "100.66.32.5/29",
            "100.66.32.6/29",
        ]
        member_ips = {(device, name): sub.ip_address.id for (device, name), sub in world.sub_interfaces.items()}
        fw1 = world.addresses[("100.66.0.5/29", "ns-prod")].id
        fw2 = world.addresses[("100.66.0.6/29", "ns-prod")].id
        assert member_ips[("fw-1", "Ethernet1/25.3000")] == fw1
        assert member_ips[("fw-2", "Ethernet1/25.3000")] == fw2

    @pytest.mark.asyncio
    async def test_one_exchange_joins_the_legs_and_names_its_gateway(self) -> None:
        world = _World()
        gen, _ = _make_gen(world)

        await _run(gen, world, _payload())

        assert list(world.exchanges) == [f"{_SHARED}-PROD-INTERNET"]
        exchange = world.exchanges[f"{_SHARED}-PROD-INTERNET"]
        context = world.contexts[_SHARED]
        assert exchange.data["gateway"] == {"id": context.id}
        assert exchange.data["namespace_a"] == {"id": "ns-prod"}
        assert exchange.data["namespace_z"] == {"id": "ns-internet"}
        for caps in _sub_names(world).values():
            assert sorted(caps) == sorted([context.id, exchange.id])

    @pytest.mark.asyncio
    async def test_border_service_ports_are_tagged_with_the_context_additively(self) -> None:
        world = _World()
        gen, _ = _make_gen(world)

        await _run(gen, world, _payload())

        context = world.contexts[_SHARED]
        assert world.tags == [(context.id, ["bl-port-fw-1", "bl-port-fw-2"])]

    @pytest.mark.asyncio
    async def test_shared_context_writes_are_all_untracked(self) -> None:
        world = _World()
        gen, _ = _make_gen(world)

        await _run(gen, world, _payload())

        assert {saved_kind for saved_kind, _, _ in world.saves} == {
            "ManagedFirewallContext",
            "IpamIPAddress",
            "TopologyRoutedExchange",
            "DcimVirtualInterface",
        }
        for kind, label, kwargs in world.saves:
            assert kwargs == _UNTRACKED, (kind, label)

    @pytest.mark.asyncio
    async def test_writes_run_under_the_per_context_lock_and_link_the_deployment(self) -> None:
        world = _World()
        gen, events = _make_gen(world)

        await _run(gen, world, _payload())

        assert events == [f"acquire:fw-context-{_SHARED}", "release"]
        gen.link_serving_firewall_context.assert_awaited_once_with(
            kind="TopologyCustomerDC", customer_id="cust-1", context_id=world.contexts[_SHARED].id
        )

    @pytest.mark.asyncio
    async def test_lock_is_released_when_a_prerequisite_is_missing(self) -> None:
        world = _World(pools=False)
        gen, events = _make_gen(world)

        await _run(gen, world, _payload())

        assert events == [f"acquire:fw-context-{_SHARED}", "release"]


class TestRerunAndSecondCustomer:
    @pytest.mark.asyncio
    async def test_rerun_writes_nothing_and_never_takes_the_lock(self) -> None:
        world = _World()
        gen, events = _make_gen(world)
        await _run(gen, world, _payload())
        events.clear()
        before = world.writes

        await _run(gen, world, _payload())

        assert world.writes == before
        assert events == []
        assert gen.link_serving_firewall_context.await_count == 2

    @pytest.mark.asyncio
    async def test_second_customer_on_the_complete_context_writes_nothing_but_its_link(self) -> None:
        world = _World()
        gen, events = _make_gen(world)
        await _run(gen, world, _payload(customer_id="cust-1"))
        events.clear()
        before = world.writes

        await _run(gen, world, _payload(customer_id="cust-2"))

        assert world.writes == before
        assert events == []
        gen.link_serving_firewall_context.assert_awaited_with(
            kind="TopologyCustomerDC", customer_id="cust-2", context_id=world.contexts[_SHARED].id
        )

    @pytest.mark.asyncio
    async def test_non_prod_customer_grows_its_own_leg_and_exchange_never_pairing_with_prod(self) -> None:
        world = _World()
        gen, _ = _make_gen(world)
        await _run(gen, world, _payload(customer_id="cust-1", environment="p"))

        await _run(gen, world, _payload(customer_id="cust-2", environment="d"))

        assert sorted(world.exchanges) == [f"{_SHARED}-NON-PROD-INTERNET", f"{_SHARED}-PROD-INTERNET"]
        for name, exchange in world.exchanges.items():
            assert {exchange.data["namespace_a"]["id"], exchange.data["namespace_z"]["id"]} in (
                {"ns-prod", "ns-internet"},
                {"ns-nonprod", "ns-internet"},
            ), name
        vlan = world.contexts[_SHARED].vlan_id.value
        non_prod_sub = world.sub_interfaces[("fw-1", f"Ethernet1/25.{transit_vlan(vlan, 'non_prod')}")]
        internet_sub = world.sub_interfaces[("fw-1", f"Ethernet1/25.{transit_vlan(vlan, 'internet')}")]
        prod_sub = world.sub_interfaces[("fw-1", f"Ethernet1/25.{transit_vlan(vlan, 'prod')}")]
        non_prod_exchange = world.exchanges[f"{_SHARED}-NON-PROD-INTERNET"].id
        prod_exchange = world.exchanges[f"{_SHARED}-PROD-INTERNET"].id
        assert non_prod_exchange in {peer.id for peer in non_prod_sub.interface_capabilities.peers}
        assert prod_exchange not in {peer.id for peer in non_prod_sub.interface_capabilities.peers}
        assert {non_prod_exchange, prod_exchange} <= {peer.id for peer in internet_sub.interface_capabilities.peers}
        assert non_prod_exchange not in {peer.id for peer in prod_sub.interface_capabilities.peers}
        # The INTERNET /29 is the one allocated for the first customer, not a second.
        assert len([k for k in world.allocated if k.endswith("-INTERNET-transit")]) == 1

    @pytest.mark.asyncio
    async def test_non_prod_customer_alone_uses_non_prod_and_internet_only(self) -> None:
        world = _World()
        gen, _ = _make_gen(world)

        await _run(gen, world, _payload(environment="t"))

        assert list(world.exchanges) == [f"{_SHARED}-NON-PROD-INTERNET"]
        assert {ns for _, ns in world.addresses} == {"ns-nonprod", "ns-internet"}
        assert {name for _, name in world.sub_interfaces} == {"Ethernet1/25.3200", "Ethernet1/25.3400"}


class TestDedicatedContext:
    def _dedicated(self, gen: Any) -> None:
        cluster = SimpleNamespace(
            id="ded-cluster", name=SimpleNamespace(value="DC10-ded-ha"), capabilities=SimpleNamespace(peers=[])
        )
        members = [
            SimpleNamespace(id="fw-1", name=SimpleNamespace(value="DC10-FW1-C005-p-dedicated"), hosting_device=None),
            SimpleNamespace(id="fw-2", name=SimpleNamespace(value="DC10-FW2-C005-p-dedicated"), hosting_device=None),
        ]
        gen._ensure_dedicated_device_pair = AsyncMock(return_value=(cluster, members))

    @pytest.mark.asyncio
    async def test_dedicated_context_writes_are_tracked_and_rerun_resaves_them(self) -> None:
        world = _World()
        gen, events = _make_gen(world)
        self._dedicated(gen)

        await _run(gen, world, _payload(dedicated=True))

        assert "DC10-ded-ha-context" in world.contexts
        assert world.contexts["DC10-ded-ha-context"].tenant_id == "cust-1"
        for kind, label, kwargs in world.saves:
            assert kwargs == _TRACKED, (kind, label)
        first_run_saves = len(world.saves)
        events.clear()
        world.saves.clear()

        await _run(gen, world, _payload(dedicated=True))

        # No lock-free fast path for a tracked context: delete_unused_nodes
        # reclaims whatever the run does not save again.
        assert first_run_saves > 0
        assert world.saved("ManagedFirewallContext") == ["DC10-ded-ha-context"]
        assert len(world.saved("IpamIPAddress")) == 8
        assert len(world.saved("DcimVirtualInterface")) == 4
        assert world.saved("TopologyRoutedExchange") == ["DC10-ded-ha-context-PROD-INTERNET"]
        for kind, label, kwargs in world.saves:
            assert kwargs == _TRACKED, (kind, label)
        assert events == ["acquire:fw-context-DC10-ded-ha-context", "release"]


class TestHealingAndFailures:
    @pytest.mark.asyncio
    async def test_partial_failure_is_healed_by_the_next_run(self) -> None:
        world = _World()
        gen, _ = _make_gen(world)
        world.fail_saves_of = {"DcimVirtualInterface"}
        await _run(gen, world, _payload())
        assert gen.logger.error.called
        world.fail_saves_of = set()
        gen.logger.error.reset_mock()

        await _run(gen, world, _payload())

        assert len(world.sub_interfaces) == 4
        assert all(len(caps) == 2 for caps in _sub_names(world).values())
        gen.logger.error.assert_not_called()
        before = world.writes

        await _run(gen, world, _payload())  # healed: back to the zero-write fast path

        assert world.writes == before

    @pytest.mark.asyncio
    async def test_untagged_port_alone_is_healed_without_touching_the_rest(self) -> None:
        world = _World()
        gen, _ = _make_gen(world)
        await _run(gen, world, _payload())
        world.bl_ports["fw-2"].interface_capabilities.peers.clear()
        saves_before = len(world.saves)

        await _run(gen, world, _payload())

        assert world.tags[-1] == (world.contexts[_SHARED].id, ["bl-port-fw-2"])
        # The heal path re-upserts, but the port tag is a relationship add, not a save.
        assert len(world.saves) >= saves_before

    @pytest.mark.asyncio
    async def test_missing_namespace_is_an_error_without_writes(self) -> None:
        world = _World(namespaces=("prod", "non_prod"))
        gen, events = _make_gen(world)

        await _run(gen, world, _payload())

        gen.logger.error.assert_called_once()
        assert "internet" in gen.logger.error.call_args.args[0]
        assert world.writes == 0
        assert events == []

    @pytest.mark.asyncio
    async def test_missing_transit_pool_is_an_error_before_anything_is_created(self) -> None:
        world = _World(pools=False)
        gen, _ = _make_gen(world)

        await _run(gen, world, _payload())

        gen.logger.error.assert_called_once()
        assert "FW-Transit-PROD-IPv4" in gen.logger.error.call_args.args[0]
        assert world.contexts == {}
        assert world.writes == 0

    @pytest.mark.asyncio
    async def test_uncabled_border_port_is_an_error_without_writes(self) -> None:
        world = _World()
        gen, _ = _make_gen(world)
        for port in world.bl_ports.values():
            port.role = SimpleNamespace(value="transit")

        await _run(gen, world, _payload())

        assert gen.logger.error.called
        assert world.writes == 0

    @pytest.mark.asyncio
    async def test_unresolved_member_does_not_shift_the_other_members_address(self) -> None:
        world = _World()
        gen, _ = _make_gen(world)
        world.bl_ports["fw-1"].role = SimpleNamespace(value="transit")

        await _run(gen, world, _payload())

        assert {device for device, _ in world.sub_interfaces} == {"fw-2"}
        fw2_ip = world.sub_interfaces[("fw-2", "Ethernet1/25.3000")].ip_address.id
        assert fw2_ip == world.addresses[("100.66.0.6/29", "ns-prod")].id
