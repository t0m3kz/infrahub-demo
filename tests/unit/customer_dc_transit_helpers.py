"""Shared fake client, world and payload builders for the ``test_customer_dc_transit_*`` modules."""

from __future__ import annotations

import ipaddress
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from generators.topology.customer_dc import CustomerDeploymentDCExchangeGenerator

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
        # device id -> its role=uplink interface, cabled (cable-<device>) to the border port bl-port-<device>.
        self.uplinks: dict[str, Any] = {device_id: self.uplink(device_id) for device_id in self.bl_ports}
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
        self.fail_uplink_lookup = False
        self.executed_filters: list[str] = []
        self.uplink_lookups: list[list[str]] = []
        self.gets: list[str] = []

    @staticmethod
    def uplink(device_id: str, *, cabled_to: str | None = None) -> Any:
        """A role=uplink interface of device_id, cabled to bl-port-<cabled_to> (default: its own device id)."""
        return SimpleNamespace(
            id=f"up-{device_id}",
            name=SimpleNamespace(value="Ethernet1/25"),
            device=SimpleNamespace(id=device_id),
            cable=SimpleNamespace(id=f"cable-{cabled_to or device_id}"),
        )

    # --- reads -----------------------------------------------------------
    async def filters(self, *, kind: Any, **kwargs: Any) -> list[Any]:
        name = kind.__name__
        self.executed_filters.append(name)
        if name == "DcimPhysicalInterface" and "device__ids" in kwargs:
            assert kwargs["role__value"] == "uplink"
            self.uplink_lookups.append(list(kwargs["device__ids"]))
            if self.fail_uplink_lookup:
                raise RuntimeError("uplink lookup failed")
            return [self.uplinks[device_id] for device_id in kwargs["device__ids"] if device_id in self.uplinks]
        if name == "DcimCable":
            assert kwargs["include"] == ["endpoints"]
            cables = []
            for uplink in self.uplinks.values():
                cable_id = uplink.cable.id
                if cable_id in kwargs["ids"] and cable_id not in {cable.id for cable in cables}:
                    port = self.bl_ports.get(cable_id.removeprefix("cable-"))
                    peers = [uplink.id, *([port.id] if port else [])]
                    cables.append(SimpleNamespace(id=cable_id, endpoints=_Many(peers)))
            return cables
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
        self.gets.append(name)
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
                    "design": {"node": {"name": {"value": "M"}}},
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
    return gen, events


async def _run(gen: Any, world: _World, payload: dict[str, Any]) -> None:
    await gen.generate(payload)


def _sub_names(world: _World) -> dict[tuple[str, str], list[str]]:
    return {key: [peer.id for peer in sub.capabilities.peers] for key, sub in world.sub_interfaces.items()}
