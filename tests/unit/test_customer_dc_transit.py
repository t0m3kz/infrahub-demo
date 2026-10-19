"""Unit tests for the context legs of CustomerDeploymentDCExchangeGenerator.

FirewallContext transit legs and LoadbalancerContext one-arm legs: the same
mixin flow (generators/firewall_context.py), one ContextSpec each. Runs the
real mixin code (generators/firewall_context.py, connections.py)
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
from generators.firewall_context import FIREWALL_CONTEXT, LOADBALANCER_CONTEXT
from generators.topology.customer_dc import CustomerDeploymentDCExchangeGenerator
from utils.exchange_transit import transit_vlan

_UNTRACKED = {"allow_upsert": True, "update_group_context": False}
_TRACKED = {"allow_upsert": True}
_SHARED = "DC10-FW1-FW2-ha-shared"
_LB_PROD = "DC10-LB1-LB2-ha-PROD"
_LB_NON_PROD = "DC10-LB1-LB2-ha-NON-PROD"

_NAMESPACES = {
    "prod": ("PROD", "ns-prod", "100.66.0.0"),
    "non_prod": ("NON-PROD", "ns-nonprod", "100.66.16.0"),
    "internet": ("INTERNET", "ns-internet", "100.66.32.0"),
    "management": ("MANAGEMENT", "ns-management", "100.66.48.0"),
}

# pool name -> (namespace id, base address, prefix length handed out); data/bootstrap/23_transit_pools.yml.
_PREFIX_POOLS = {
    **{f"FW-Transit-{ns[0]}-IPv4": (ns[1], ns[2], 29) for ns in _NAMESPACES.values()},
    "LB-Leg-PROD-IPv4": ("ns-prod", "100.66.64.0", 29),
    "LB-Leg-NON-PROD-IPv4": ("ns-nonprod", "100.66.80.0", 29),
    "LB-VIP-PROD-IPv4": ("ns-prod", "100.66.96.0", 26),
    "LB-VIP-NON-PROD-IPv4": ("ns-nonprod", "100.66.112.0", 26),
}
_CONTEXT_KINDS = ("ManagedFirewallContext", "ManagedLoadbalancerContext")
# VLAN pool name -> first VLAN it hands out (the two halves of the context band).
_VLAN_POOLS = {"dc10-fw-context-vlan-pool": 3000, "dc10-lb-context-vlan-pool": 3100}


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

    def __init__(
        self,
        *,
        namespaces: tuple[str, ...] = ("prod", "non_prod", "internet", "management"),
        pools: bool = True,
        snat_pool: bool = True,
        missing_pools: tuple[str, ...] = (),
        vlan_pools: tuple[str, ...] = tuple(_VLAN_POOLS),
    ) -> None:
        self.snat_pool = snat_pool
        self.vlan_pools = {name: _VLAN_POOLS[name] for name in vlan_pools}
        self.snat_allocations: dict[str, Any] = {}
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
                name: SimpleNamespace(id=f"pool-{name}", name=SimpleNamespace(value=name))
                for name in _PREFIX_POOLS
                if name not in missing_pools
            }
            if pools
            else {}
        )
        self.cluster = SimpleNamespace(
            id="cluster-1",
            name=SimpleNamespace(value="DC10-FW1-FW2-ha"),
            capabilities=SimpleNamespace(peers=[SimpleNamespace(id="fw-1"), SimpleNamespace(id="fw-2")]),
        )
        self.lb_cluster = SimpleNamespace(
            id="lb-cluster-1",
            name=SimpleNamespace(value="DC10-LB1-LB2-ha"),
            capabilities=SimpleNamespace(peers=[SimpleNamespace(id="lb-1"), SimpleNamespace(id="lb-2")]),
        )
        self.bl_ports = {
            device_id: SimpleNamespace(
                id=f"bl-port-{device_id}",
                role=SimpleNamespace(value=role),
                device=SimpleNamespace(id=f"bl-{device_id}", display_label=f"BL-{device_id}"),
                capabilities=_Many(),
            )
            for device_id, role in (
                ("fw-1", "firewall"),
                ("fw-2", "firewall"),
                ("lb-1", "load-balancer"),
                ("lb-2", "load-balancer"),
            )
        }
        self.contexts: dict[str, Any] = {}
        self.exchanges: dict[str, Any] = {}
        self.sub_interfaces: dict[tuple[str, str], Any] = {}
        self.addresses: dict[tuple[str, str], Any] = {}
        self.saves: list[tuple[str, str, dict[str, Any]]] = []
        self.tags: list[tuple[str, list[str]]] = []
        self.allocated: dict[str, Any] = {}
        self.allocate_calls = 0
        self.vlan_pool_reads: list[str] = []
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
        if name == "ManagedLoadbalancerHA":
            return [self.lb_cluster]
        if name in _CONTEXT_KINDS:
            context = self.contexts.get(kwargs["name__value"])
            return [context] if context and context.kind == name else []
        if name == "DcimPhysicalInterface":
            return [port for port in self.bl_ports.values() if port.id in kwargs["ids"]]
        if name == "TopologyRoutedExchange":
            return [ex for ex_name, ex in self.exchanges.items() if ex_name in kwargs["name__values"]]
        if name == "DcimVirtualInterface":
            ids = set(kwargs["capabilities__ids"])
            return [sub for sub in self.sub_interfaces.values() if ids & {peer.id for peer in sub.capabilities.peers}]
        if name == "IpamIPAddress":
            return [ip for ip in self.addresses.values() if ip.id in kwargs["ids"]]
        raise AssertionError(f"unexpected filters({name})")

    async def get(self, *, kind: Any, **kwargs: Any) -> Any:
        name = kind.__name__
        if name == "CoreNumberPool":
            self.vlan_pool_reads.append(kwargs["name__value"])
            if kwargs["name__value"] not in self.vlan_pools:
                raise RuntimeError("pool not found")
            return SimpleNamespace(id=kwargs["name__value"])
        if name in _CONTEXT_KINDS:
            return next(ctx for ctx in self.contexts.values() if ctx.id == kwargs["id"])
        if name == "CoreIPAddressPool":
            if not self.snat_pool:
                raise RuntimeError("pool not found")
            assert kwargs["name__value"] == "FW-SNAT-INTERNET-IPv4"
            return SimpleNamespace(id="pool-snat")
        if name == "IpamIPAddress":
            return self.addresses.get((kwargs["address__value"], kwargs["ip_namespace__ids"][0]))
        raise AssertionError(f"unexpected get({name})")

    async def allocate_next_ip_prefix(
        self, *, resource_pool: Any, identifier: str, prefix_length: int, **_: Any
    ) -> Any:
        self.allocate_calls += 1
        namespace_id, base, length = _PREFIX_POOLS[resource_pool.name.value]
        assert prefix_length == length
        if identifier not in self.allocated:
            count = sum(1 for p in self.allocated.values() if p.pool == resource_pool.id)
            self.allocated[identifier] = SimpleNamespace(
                id=f"prefix-{len(self.allocated)}",
                pool=resource_pool.id,
                prefix=SimpleNamespace(value=f"{ipaddress.ip_address(base) + 2 ** (32 - length) * count}/{length}"),
                ip_namespace=SimpleNamespace(id=namespace_id),
            )
        return self.allocated[identifier]

    async def allocate_next_ip_address(
        self, *, resource_pool: Any, identifier: str, prefix_length: int, **_: Any
    ) -> Any:
        self.allocate_calls += 1
        assert resource_pool.id == "pool-snat"
        assert prefix_length == 32
        if identifier not in self.snat_allocations:
            host = ipaddress.ip_address("192.0.2.0") + 1 + len(self.snat_allocations)
            self.snat_allocations[identifier] = SimpleNamespace(
                id=f"snat-{len(self.snat_allocations)}", address=SimpleNamespace(value=f"{host}/32")
            )
        return self.snat_allocations[identifier]

    # --- writes ----------------------------------------------------------
    async def create(self, *, kind: Any, data: dict[str, Any]) -> Any:
        name = kind.__name__
        if name in _CONTEXT_KINDS:
            for ref in ("snat_address", "vip_prefix"):  # a relationship write on an existing context
                if ref in data:
                    context = next(ctx for ctx in self.contexts.values() if ctx.id == data["id"])
                    return self._node(
                        name,
                        ref,
                        on_save=lambda context=context, ref=ref: setattr(
                            context, ref, SimpleNamespace(id=data[ref]["id"])
                        ),
                    )
            if "vlan_id" in data:  # the VLAN allocation re-save of an existing context
                context = next(ctx for ctx in self.contexts.values() if ctx.id == data["id"])
                pool = data["vlan_id"]["from_pool"]["id"]
                return self._node(
                    name, "vlan_id", on_save=lambda: setattr(context.vlan_id, "value", self._take_vlan(pool))
                )
            context = self.contexts.setdefault(data["name"], self._context(name, data["name"]))
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
                    capabilities=_Many(),
                ),
            )
            sub.ip_address = SimpleNamespace(id=(data.get("ip_address") or {}).get("id") or sub.ip_address.id)
            return self._node(name, data["name"], backing=sub)
        raise AssertionError(f"unexpected create({name})")

    def _context(self, kind: str, name: str) -> Any:
        return SimpleNamespace(
            id=f"ctx-{len(self.contexts)}",
            kind=kind,
            name=SimpleNamespace(value=name),
            vlan_id=SimpleNamespace(value=None),
            tenant_id=None,
            snat_address=SimpleNamespace(id=None),
            vip_prefix=SimpleNamespace(id=None),
            add_relationships=None,
        )

    def _take_vlan(self, pool: str) -> int:
        vlan = self.vlan_pools[pool]
        self.vlan_pools[pool] = vlan + 1
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
        if kind in _CONTEXT_KINDS and backing is not None:

            async def add_relationships(*, relation_to_update: str, related_nodes: list[str]) -> None:
                assert relation_to_update == "capabilities"
                world.tags.append((backing.id, list(related_nodes)))
                for port in world.bl_ports.values():
                    if port.id in related_nodes:
                        port.capabilities.peers.append(SimpleNamespace(id=backing.id))

            node.add_relationships = add_relationships
        return node

    # --- assertions helpers ---------------------------------------------
    @property
    def writes(self) -> int:
        return len(self.saves) + len(self.tags) + self.allocate_calls

    def saved(self, kind: str) -> list[str]:
        return [label for saved_kind, label, _ in self.saves if saved_kind == kind]


_LB_DEVICES = [
    {"id": "lb-1", "name": "DC10-LB1", "kind": "DcimPhysicalDevice", "platform": {"name": "f5_tmos"}},
    {"id": "lb-2", "name": "DC10-LB2", "kind": "DcimPhysicalDevice", "platform": {"name": "f5_tmos"}},
]


def _payload(
    *,
    customer_id: str = "cust-1",
    environment: str = "p",
    dedicated: bool = False,
    firewalls: bool = True,
    lb_devices: list[dict[str, Any]] | None = None,
    dedicated_loadbalancer: bool = False,
    connectivity_mode: str = "pbr",
) -> dict[str, Any]:
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
                    "connectivity_mode": {"value": connectivity_mode},
                    "size": {"value": "M"},
                    "firewall_devices": devices if firewalls else [],
                    "loadbalancer_devices": lb_devices or [],
                    "security_manager_controllers": [],
                    "lb_manager_controllers": [],
                },
                "design": {
                    "dedicated_firewall": {"value": dedicated},
                    "dedicated_loadbalancer": {"value": dedicated_loadbalancer},
                },
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
    gen.link_serving_context = AsyncMock()
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
    return {key: [peer.id for peer in sub.capabilities.peers] for key, sub in world.sub_interfaces.items()}


def test_generator_is_a_common_generator() -> None:
    assert issubclass(CustomerDeploymentDCExchangeGenerator, CommonGenerator)


class TestFreshSharedBoarding:
    @pytest.mark.asyncio
    async def test_prod_customer_gets_prod_internet_and_management_legs_on_both_members(self) -> None:
        world = _World()
        gen, events = _make_gen(world)

        await _run(gen, world, _payload())

        vlan = world.contexts[_SHARED].vlan_id.value
        assert vlan == 3000
        names = {(device, name) for device, name in world.sub_interfaces}
        assert names == {
            (device, f"Ethernet1/25.{transit_vlan(vlan, ns_type)}")
            for device in ("fw-1", "fw-2")
            for ns_type in ("prod", "internet", "management")
        }
        assert f"Ethernet1/25.{transit_vlan(vlan, 'internet')}" == "Ethernet1/25.3400"
        assert f"Ethernet1/25.{transit_vlan(vlan, 'management')}" == "Ethernet1/25.3600"
        gen.logger.error.assert_not_called()

    @pytest.mark.asyncio
    async def test_addresses_follow_the_fixed_offsets_per_namespace(self) -> None:
        world = _World()
        gen, _ = _make_gen(world)

        await _run(gen, world, _payload())

        by_namespace: dict[str, list[str]] = {}
        for address, namespace_id in world.addresses:
            by_namespace.setdefault(namespace_id, []).append(address)
        assert sorted(by_namespace) == ["ns-internet", "ns-management", "ns-prod"]
        assert sorted(by_namespace["ns-prod"]) == ["100.66.0.1/29", "100.66.0.4/29", "100.66.0.5/29", "100.66.0.6/29"]
        assert sorted(by_namespace["ns-internet"]) == [
            "100.66.32.1/29",
            "100.66.32.4/29",
            "100.66.32.5/29",
            "100.66.32.6/29",
        ]
        assert sorted(by_namespace["ns-management"]) == [
            "100.66.48.1/29",
            "100.66.48.4/29",
            "100.66.48.5/29",
            "100.66.48.6/29",
        ]
        member_ips = {(device, name): sub.ip_address.id for (device, name), sub in world.sub_interfaces.items()}
        fw1 = world.addresses[("100.66.0.5/29", "ns-prod")].id
        fw2 = world.addresses[("100.66.0.6/29", "ns-prod")].id
        assert member_ips[("fw-1", "Ethernet1/25.3000")] == fw1
        assert member_ips[("fw-2", "Ethernet1/25.3000")] == fw2

    @pytest.mark.asyncio
    async def test_one_exchange_per_peer_joins_the_legs_and_names_its_gateway(self) -> None:
        world = _World()
        gen, _ = _make_gen(world)

        await _run(gen, world, _payload())

        assert sorted(world.exchanges) == [f"{_SHARED}-PROD-INTERNET", f"{_SHARED}-PROD-MANAGEMENT"]
        context = world.contexts[_SHARED]
        for peer, namespace_id in (("INTERNET", "ns-internet"), ("MANAGEMENT", "ns-management")):
            exchange = world.exchanges[f"{_SHARED}-PROD-{peer}"]
            assert exchange.data["gateway"] == {"id": context.id}
            assert exchange.data["namespace_a"] == {"id": "ns-prod"}
            assert exchange.data["namespace_z"] == {"id": namespace_id}
        vlan = context.vlan_id.value
        for ns_type, expected in (
            ("prod", {f"{_SHARED}-PROD-INTERNET", f"{_SHARED}-PROD-MANAGEMENT"}),
            ("internet", {f"{_SHARED}-PROD-INTERNET"}),
            ("management", {f"{_SHARED}-PROD-MANAGEMENT"}),
        ):
            sub = world.sub_interfaces[("fw-1", f"Ethernet1/25.{transit_vlan(vlan, ns_type)}")]
            assert {peer.id for peer in sub.capabilities.peers} == {
                context.id,
                *(world.exchanges[name].id for name in expected),
            }, ns_type

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
        gen.link_serving_context.assert_awaited_once_with(
            FIREWALL_CONTEXT, customer_id="cust-1", context_id=world.contexts[_SHARED].id
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
        assert gen.link_serving_context.await_count == 2

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
        gen.link_serving_context.assert_awaited_with(
            FIREWALL_CONTEXT, customer_id="cust-2", context_id=world.contexts[_SHARED].id
        )

    @pytest.mark.asyncio
    async def test_non_prod_customer_grows_its_own_leg_and_exchange_never_pairing_with_prod(self) -> None:
        world = _World()
        gen, _ = _make_gen(world)
        await _run(gen, world, _payload(customer_id="cust-1", environment="p"))

        await _run(gen, world, _payload(customer_id="cust-2", environment="d"))

        assert sorted(world.exchanges) == [
            f"{_SHARED}-NON-PROD-INTERNET",
            f"{_SHARED}-NON-PROD-MANAGEMENT",
            f"{_SHARED}-PROD-INTERNET",
            f"{_SHARED}-PROD-MANAGEMENT",
        ]
        for name, exchange in world.exchanges.items():
            assert {exchange.data["namespace_a"]["id"], exchange.data["namespace_z"]["id"]} in (
                {"ns-prod", "ns-internet"},
                {"ns-prod", "ns-management"},
                {"ns-nonprod", "ns-internet"},
                {"ns-nonprod", "ns-management"},
            ), name
        vlan = world.contexts[_SHARED].vlan_id.value
        non_prod_sub = world.sub_interfaces[("fw-1", f"Ethernet1/25.{transit_vlan(vlan, 'non_prod')}")]
        internet_sub = world.sub_interfaces[("fw-1", f"Ethernet1/25.{transit_vlan(vlan, 'internet')}")]
        prod_sub = world.sub_interfaces[("fw-1", f"Ethernet1/25.{transit_vlan(vlan, 'prod')}")]
        non_prod_exchange = world.exchanges[f"{_SHARED}-NON-PROD-INTERNET"].id
        prod_exchange = world.exchanges[f"{_SHARED}-PROD-INTERNET"].id
        assert non_prod_exchange in {peer.id for peer in non_prod_sub.capabilities.peers}
        assert prod_exchange not in {peer.id for peer in non_prod_sub.capabilities.peers}
        assert {non_prod_exchange, prod_exchange} <= {peer.id for peer in internet_sub.capabilities.peers}
        assert non_prod_exchange not in {peer.id for peer in prod_sub.capabilities.peers}
        # The INTERNET and MANAGEMENT /29s are the ones allocated for the first customer, not a second.
        assert len([k for k in world.allocated if k.endswith("-INTERNET-transit")]) == 1
        assert len([k for k in world.allocated if k.endswith("-MANAGEMENT-transit")]) == 1

    @pytest.mark.asyncio
    async def test_non_prod_customer_alone_uses_non_prod_and_the_peers_only(self) -> None:
        world = _World()
        gen, _ = _make_gen(world)

        await _run(gen, world, _payload(environment="t"))

        assert sorted(world.exchanges) == [f"{_SHARED}-NON-PROD-INTERNET", f"{_SHARED}-NON-PROD-MANAGEMENT"]
        assert {ns for _, ns in world.addresses} == {"ns-nonprod", "ns-internet", "ns-management"}
        assert {name for _, name in world.sub_interfaces} == {
            "Ethernet1/25.3200",
            "Ethernet1/25.3400",
            "Ethernet1/25.3600",
        }


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
        assert len(world.saved("IpamIPAddress")) == 12
        assert len(world.saved("DcimVirtualInterface")) == 6
        assert sorted(world.saved("TopologyRoutedExchange")) == [
            "DC10-ded-ha-context-PROD-INTERNET",
            "DC10-ded-ha-context-PROD-MANAGEMENT",
        ]
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

        assert len(world.sub_interfaces) == 6
        assert all(len(caps) == (3 if name.endswith(".3000") else 2) for (_, name), caps in _sub_names(world).items())
        gen.logger.error.assert_not_called()
        before = world.writes

        await _run(gen, world, _payload())  # healed: back to the zero-write fast path

        assert world.writes == before

    @pytest.mark.asyncio
    async def test_untagged_port_alone_is_healed_without_touching_the_rest(self) -> None:
        world = _World()
        gen, _ = _make_gen(world)
        await _run(gen, world, _payload())
        world.bl_ports["fw-2"].capabilities.peers.clear()
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


class TestSnatAddress:
    """One public SNAT /32 per context with an INTERNET leg (FW-SNAT-INTERNET-IPv4)."""

    @pytest.mark.asyncio
    async def test_shared_context_gets_one_untracked_snat_shared_by_all_tenant_vrfs(self) -> None:
        world = _World()
        gen, _ = _make_gen(world)

        await _run(gen, world, _payload(customer_id="cust-1", environment="p"))
        await _run(gen, world, _payload(customer_id="cust-2", environment="d"))

        context = world.contexts[_SHARED]
        assert list(world.snat_allocations) == [f"{context.id}-snat"]
        assert context.snat_address.id == "snat-0"
        snat_saves = [kwargs for kind, label, kwargs in world.saves if label == "snat_address"]
        assert snat_saves == [_UNTRACKED]

    @pytest.mark.asyncio
    async def test_dedicated_context_snat_write_is_tracked(self) -> None:
        world = _World()
        gen, _ = _make_gen(world)
        TestDedicatedContext()._dedicated(gen)

        await _run(gen, world, _payload(dedicated=True))

        assert [kwargs for _, label, kwargs in world.saves if label == "snat_address"] == [_TRACKED]
        assert world.contexts["DC10-ded-ha-context"].snat_address.id == "snat-0"

    @pytest.mark.asyncio
    async def test_rerun_with_snat_set_writes_nothing(self) -> None:
        world = _World()
        gen, events = _make_gen(world)
        await _run(gen, world, _payload())
        events.clear()
        before = world.writes

        await _run(gen, world, _payload())

        assert world.writes == before
        assert events == []

    @pytest.mark.asyncio
    async def test_missing_snat_is_healed_with_the_same_address(self) -> None:
        world = _World()
        gen, _ = _make_gen(world)
        await _run(gen, world, _payload())
        context = world.contexts[_SHARED]
        first = context.snat_address.id
        context.snat_address = SimpleNamespace(id=None)

        await _run(gen, world, _payload())

        assert context.snat_address.id == first
        assert len(world.snat_allocations) == 1
        before = world.writes
        await _run(gen, world, _payload())
        assert world.writes == before

    @pytest.mark.asyncio
    async def test_missing_snat_pool_is_an_error_before_the_context_is_created(self) -> None:
        world = _World(snat_pool=False)
        gen, _ = _make_gen(world)

        await _run(gen, world, _payload())

        gen.logger.error.assert_called_once()
        assert "FW-SNAT-INTERNET-IPv4" in gen.logger.error.call_args.args[0]
        assert world.contexts == {}

    @pytest.mark.asyncio
    async def test_context_without_an_internet_leg_gets_no_snat(self) -> None:
        world = _World(snat_pool=False)  # the pool is never looked up
        gen, _ = _make_gen(world)

        with patch("generators.firewall_context.EXCHANGE_PEERS", {"prod": ()}):
            await _run(gen, world, _payload())

        assert world.snat_allocations == {}
        assert world.contexts[_SHARED].snat_address.id is None
        gen.logger.error.assert_not_called()


def _lb_payload(**kwargs: Any) -> dict[str, Any]:
    """A DC customer on a DC whose only service pair is its load balancers."""
    return _payload(firewalls=False, lb_devices=_LB_DEVICES, **kwargs)


class TestVlanPoolPerContextKind:
    @pytest.mark.asyncio
    async def test_each_kind_reads_its_own_half_of_the_context_band(self) -> None:
        """FW contexts take their VLAN from <dc>-fw-context-vlan-pool (3000-3099), LB ones from -lb- (3100-3199)."""
        world = _World()
        gen, _ = _make_gen(world)

        await _run(gen, world, _payload(lb_devices=_LB_DEVICES))

        assert world.vlan_pool_reads == ["dc10-fw-context-vlan-pool", "dc10-lb-context-vlan-pool"]
        assert world.contexts[_SHARED].vlan_id.value == 3000
        assert world.contexts[_LB_PROD].vlan_id.value == 3100
        assert [call.args[0] for call in gen.link_serving_context.await_args_list] == [
            FIREWALL_CONTEXT,
            LOADBALANCER_CONTEXT,
        ]
        gen.logger.error.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_lb_vlan_pool_is_an_error(self) -> None:
        """A DC whose generator never created the LB VLAN pool cannot give a context its VLAN."""
        world = _World(vlan_pools=("dc10-fw-context-vlan-pool",))
        gen, _ = _make_gen(world)

        await _run(gen, world, _lb_payload())

        gen.logger.error.assert_called_once()
        assert "dc10-lb-context-vlan-pool" in gen.logger.error.call_args.args[0]
        assert world.sub_interfaces == {}


class TestLoadbalancerSharedContext:
    @pytest.mark.asyncio
    async def test_prod_customer_gets_one_tenant_leg_and_no_exchange_or_snat(self) -> None:
        world = _World()
        gen, events = _make_gen(world)

        await _run(gen, world, _lb_payload())

        context = world.contexts[_LB_PROD]
        assert context.kind == "ManagedLoadbalancerContext"
        assert context.tenant_id is None
        assert set(world.sub_interfaces) == {("lb-1", "Ethernet1/25.3100"), ("lb-2", "Ethernet1/25.3100")}
        for sub in world.sub_interfaces.values():
            assert [peer.id for peer in sub.capabilities.peers] == [context.id]
        assert world.exchanges == {}
        assert world.snat_allocations == {}
        assert sorted(address for address, _ in world.addresses) == [
            "100.66.64.1/29",
            "100.66.64.4/29",
            "100.66.64.5/29",
            "100.66.64.6/29",
        ]
        assert {namespace for _, namespace in world.addresses} == {"ns-prod"}
        assert world.sub_interfaces[("lb-1", "Ethernet1/25.3100")].ip_address.id == (
            world.addresses[("100.66.64.5/29", "ns-prod")].id
        )
        assert world.sub_interfaces[("lb-2", "Ethernet1/25.3100")].ip_address.id == (
            world.addresses[("100.66.64.6/29", "ns-prod")].id
        )
        assert world.tags == [(context.id, ["bl-port-lb-1", "bl-port-lb-2"])]
        assert events == [f"acquire:lb-context-{_LB_PROD}", "release"]
        gen.link_serving_context.assert_awaited_once_with(
            LOADBALANCER_CONTEXT, customer_id="cust-1", context_id=context.id
        )
        gen.logger.error.assert_not_called()

    @pytest.mark.asyncio
    async def test_leg_and_vip_range_use_the_contract_identifiers(self) -> None:
        """/29 leg `{ctx}-{NS}-lb-leg`, /26 VIP range `{ctx}-{NS}-lb-vip` stored in context.vip_prefix."""
        world = _World()
        gen, _ = _make_gen(world)

        await _run(gen, world, _lb_payload())

        context = world.contexts[_LB_PROD]
        leg = world.allocated[f"{context.id}-PROD-lb-leg"]
        vip = world.allocated[f"{context.id}-PROD-lb-vip"]
        assert (leg.pool, leg.prefix.value) == ("pool-LB-Leg-PROD-IPv4", "100.66.64.0/29")
        assert (vip.pool, vip.prefix.value) == ("pool-LB-VIP-PROD-IPv4", "100.66.96.0/26")
        assert context.vip_prefix.id == vip.id
        assert sorted(world.allocated) == [f"{context.id}-PROD-lb-leg", f"{context.id}-PROD-lb-vip"]

    @pytest.mark.asyncio
    async def test_shared_context_writes_are_all_untracked(self) -> None:
        world = _World()
        gen, _ = _make_gen(world)

        await _run(gen, world, _lb_payload())

        assert {kind for kind, _, _ in world.saves} == {
            "ManagedLoadbalancerContext",
            "IpamIPAddress",
            "DcimVirtualInterface",
        }
        for kind, label, kwargs in world.saves:
            assert kwargs == _UNTRACKED, (kind, label)

    @pytest.mark.asyncio
    async def test_one_shared_context_per_tenant_namespace_created_lazily(self) -> None:
        """The first NON-PROD customer creates the cluster's NON-PROD context; the PROD one is untouched."""
        world = _World()
        gen, _ = _make_gen(world)
        await _run(gen, world, _lb_payload(customer_id="cust-1", environment="p"))
        assert sorted(world.contexts) == [_LB_PROD]
        prod_context = world.contexts[_LB_PROD]
        prod_saves = len(world.saves)

        await _run(gen, world, _lb_payload(customer_id="cust-2", environment="d"))

        assert sorted(world.contexts) == [_LB_NON_PROD, _LB_PROD]
        non_prod = world.contexts[_LB_NON_PROD]
        assert non_prod.vlan_id.value == 3101
        assert ("lb-1", f"Ethernet1/25.{transit_vlan(3101, 'non_prod')}") in world.sub_interfaces
        assert world.allocated[f"{non_prod.id}-NON-PROD-lb-leg"].prefix.value == "100.66.80.0/29"
        assert world.allocated[f"{non_prod.id}-NON-PROD-lb-vip"].prefix.value == "100.66.112.0/26"
        assert all(label != _LB_PROD for _, label, _ in world.saves[prod_saves:])
        assert prod_context.vlan_id.value == 3100
        gen.link_serving_context.assert_awaited_with(LOADBALANCER_CONTEXT, customer_id="cust-2", context_id=non_prod.id)

    @pytest.mark.asyncio
    async def test_rerun_and_second_customer_write_nothing_and_never_take_the_lock(self) -> None:
        world = _World()
        gen, events = _make_gen(world)
        await _run(gen, world, _lb_payload(customer_id="cust-1"))
        events.clear()
        before = world.writes

        await _run(gen, world, _lb_payload(customer_id="cust-1"))
        await _run(gen, world, _lb_payload(customer_id="cust-2"))

        assert world.writes == before
        assert events == []
        # No exchanges to look up: an empty name__values filter is never sent.
        assert "TopologyRoutedExchange" not in world.executed_filters
        gen.link_serving_context.assert_awaited_with(
            LOADBALANCER_CONTEXT, customer_id="cust-2", context_id=world.contexts[_LB_PROD].id
        )

    @pytest.mark.asyncio
    async def test_missing_vip_prefix_is_healed_with_the_same_range(self) -> None:
        world = _World()
        gen, _ = _make_gen(world)
        await _run(gen, world, _lb_payload())
        context = world.contexts[_LB_PROD]
        first = context.vip_prefix.id
        context.vip_prefix = SimpleNamespace(id=None)

        await _run(gen, world, _lb_payload())

        assert context.vip_prefix.id == first
        assert len([key for key in world.allocated if key.endswith("-lb-vip")]) == 1
        before = world.writes
        await _run(gen, world, _lb_payload())
        assert world.writes == before


class TestLoadbalancerDedicatedContext:
    def _dedicated(self, gen: Any) -> None:
        cluster = SimpleNamespace(
            id="ded-lb-cluster", name=SimpleNamespace(value="DC10-LB-ded-ha"), capabilities=SimpleNamespace(peers=[])
        )
        members = [
            SimpleNamespace(id="lb-1", name=SimpleNamespace(value="DC10-LB1-C005-p-dedicated"), hosting_device=None),
            SimpleNamespace(id="lb-2", name=SimpleNamespace(value="DC10-LB2-C005-p-dedicated"), hosting_device=None),
        ]
        gen._ensure_dedicated_device_pair = AsyncMock(return_value=(cluster, members))

    @pytest.mark.asyncio
    async def test_dedicated_context_carries_the_tenant_and_rerun_resaves_tracked(self) -> None:
        world = _World()
        gen, events = _make_gen(world)
        self._dedicated(gen)

        await _run(gen, world, _lb_payload(dedicated_loadbalancer=True))

        assert gen._ensure_dedicated_device_pair.call_args.kwargs["role"] == "load-balancer"
        assert gen._ensure_dedicated_device_pair.call_args.kwargs["ha_kind"] == "ManagedLoadbalancerHA"
        assert "tenant_id" not in gen._ensure_dedicated_device_pair.call_args.kwargs
        context = world.contexts["DC10-LB-ded-ha-context"]
        assert context.tenant_id == "cust-1"
        assert context.vip_prefix.id is not None
        for kind, label, kwargs in world.saves:
            assert kwargs == _TRACKED, (kind, label)
        events.clear()
        world.saves.clear()

        await _run(gen, world, _lb_payload(dedicated_loadbalancer=True))

        assert world.saved("ManagedLoadbalancerContext") == ["DC10-LB-ded-ha-context"]
        assert len(world.saved("IpamIPAddress")) == 4
        assert len(world.saved("DcimVirtualInterface")) == 2
        for kind, label, kwargs in world.saves:
            assert kwargs == _TRACKED, (kind, label)
        assert events == ["acquire:lb-context-DC10-LB-ded-ha-context", "release"]

    @pytest.mark.asyncio
    async def test_dedicated_request_without_a_pair_provisions_no_context(self) -> None:
        world = _World()
        gen, _ = _make_gen(world)
        gen._ensure_dedicated_device_pair = AsyncMock(return_value=None)

        await _run(gen, world, _lb_payload(dedicated_loadbalancer=True))

        assert world.contexts == {}
        assert world.writes == 0


class TestInlineDataCenter:
    """On an inline DC the load balancers sit in the firewall chain, cabled to the firewall, not to a border port."""

    @pytest.mark.asyncio
    async def test_firewall_context_is_provisioned_and_the_lb_context_skipped(self) -> None:
        world = _World()
        gen, events = _make_gen(world)

        await _run(gen, world, _payload(lb_devices=_LB_DEVICES, connectivity_mode="inline"))

        assert sorted(world.contexts) == [_SHARED]
        assert world.vlan_pool_reads == ["dc10-fw-context-vlan-pool"]
        assert not any(device_id.startswith("lb-") for device_id, _ in world.sub_interfaces)
        assert [call.args[0] for call in gen.link_serving_context.await_args_list] == [FIREWALL_CONTEXT]
        assert events == [f"acquire:fw-context-{_SHARED}", "release"]
        gen.logger.error.assert_not_called()
        assert any(
            "connectivity_mode inline" in call.args[0] and "LoadbalancerContext" in call.args[0]
            for call in gen.logger.info.call_args_list
        )

    @pytest.mark.asyncio
    async def test_load_balancer_only_inline_dc_reads_and_writes_nothing(self) -> None:
        world = _World()
        gen, events = _make_gen(world)

        await _run(gen, world, _lb_payload(connectivity_mode="inline"))

        assert world.writes == 0
        assert world.executed_filters == []
        assert events == []
        gen.logger.error.assert_not_called()

    def test_only_the_load_balancer_spec_skips_an_inline_parent(self) -> None:
        inline = {"connectivity_mode": "inline"}

        assert LOADBALANCER_CONTEXT.skip_reason(inline)
        assert LOADBALANCER_CONTEXT.skip_reason({"connectivity_mode": "pbr"}) is None
        assert LOADBALANCER_CONTEXT.skip_reason({}) is None
        assert FIREWALL_CONTEXT.skip_reason(inline) is None


class TestLoadbalancerFailures:
    @pytest.mark.asyncio
    async def test_no_loadbalancer_devices_is_skipped_silently(self) -> None:
        world = _World()
        gen, _ = _make_gen(world)

        await _run(gen, world, _payload(firewalls=False))

        assert world.writes == 0
        assert world.executed_filters == []
        gen.logger.error.assert_not_called()
        assert any("no load-balancer devices" in call.args[0] for call in gen.logger.info.call_args_list)

    @pytest.mark.asyncio
    async def test_member_without_an_uplink_is_an_error_without_writes(self) -> None:
        world = _World()
        gen, _ = _make_gen(world)

        async def no_uplink(*, device_id: str, role: str) -> Any:
            return None

        gen.find_role_interface = no_uplink

        await _run(gen, world, _lb_payload())

        assert gen.logger.error.called
        assert "no load-balancer member" in gen.logger.error.call_args.args[0]
        assert world.writes == 0

    @pytest.mark.asyncio
    async def test_uplink_not_cabled_to_a_load_balancer_port_is_an_error_without_writes(self) -> None:
        """A load balancer cabled to a border `firewall` port is not one-arm toward the border leaf."""
        world = _World()
        gen, _ = _make_gen(world)
        for device_id in ("lb-1", "lb-2"):
            world.bl_ports[device_id].role = SimpleNamespace(value="firewall")

        await _run(gen, world, _lb_payload())

        messages = [call.args[0] for call in gen.logger.error.call_args_list]
        assert any("not cabled to a load-balancer service port" in message for message in messages)
        assert world.writes == 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize("missing", ["LB-Leg-PROD-IPv4", "LB-VIP-PROD-IPv4"])
    async def test_missing_leg_or_vip_pool_is_an_error_before_anything_is_created(self, missing: str) -> None:
        world = _World(missing_pools=(missing,))
        gen, events = _make_gen(world)

        await _run(gen, world, _lb_payload())

        gen.logger.error.assert_called_once()
        assert missing in gen.logger.error.call_args.args[0]
        assert world.contexts == {}
        assert world.writes == 0
        assert events == [f"acquire:lb-context-{_LB_PROD}", "release"]
