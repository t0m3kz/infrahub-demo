"""Shared fake client, world and payload builders for the ``test_customer_dc_transit_*`` modules."""

from __future__ import annotations

import ipaddress
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from infrahub_sdk.query_groups import InfrahubGroupContextBase

from generators.topology.customer_dc import CustomerDeploymentDCExchangeGenerator
from tests.unit.vrf_instances_helpers import edges, raw_device, raw_instance, raw_interface, raw_service_port
from tests.unit.vrf_reconcile_helpers import VrfHost
from utils.exchange_transit import transit_vlan

_UNTRACKED = {"allow_upsert": True, "update_group_context": False}
_TRACKED = {"allow_upsert": True}
_SHARED = "shared-DC10-FW1-FW2-prod"
_SHARED_NON_PROD = "shared-DC10-FW1-FW2-nonprod"
_LB_PROD = "shared-DC10-LB1-LB2-prod"
_LB_NON_PROD = "shared-DC10-LB1-LB2-nonprod"

_NAMESPACES = {
    "prod": ("PROD", "ns-prod", "100.66.0.0"),
    "non_prod": ("NONPROD", "ns-nonprod", "100.66.16.0"),
    "internet": ("INTERNET", "ns-internet", "100.66.32.0"),
    "management": ("MANAGEMENT", "ns-management", "100.66.48.0"),
}

# pool name -> (namespace id, base address, prefix length handed out); data/bootstrap/23_transit_pools.yml.
_PREFIX_POOLS = {
    **{f"FW-Transit-{ns[0]}-IPv4": (ns[1], ns[2], 29) for ns in _NAMESPACES.values()},
    "LB-Leg-PROD-IPv4": ("ns-prod", "100.66.64.0", 29),
    "LB-Leg-NONPROD-IPv4": ("ns-nonprod", "100.66.80.0", 29),
    "LB-VIP-PROD-IPv4": ("ns-prod", "100.66.96.0", 26),
    "LB-VIP-NONPROD-IPv4": ("ns-nonprod", "100.66.112.0", 26),
}
_CONTEXT_KINDS = ("ManagedFirewallContext", "ManagedLoadbalancerContext")
# VLAN pool name -> first VLAN it hands out (the two halves of the context band).
_VLAN_POOLS = {"dc10-fw-context-vlan-pool": 3000, "dc10-lb-context-vlan-pool": 3100}


class _Many:
    """A cardinality-many relationship manager."""

    def __init__(self, peer_ids: list[str] | None = None, *, on_add: Any = None) -> None:
        self.peers = [SimpleNamespace(id=peer_id, typename=None) for peer_id in peer_ids or []]
        # peer id -> its typename, called on every add (the world knows which ids are VRF instances).
        self.on_add = on_add

    async def fetch(self) -> None:
        return None

    def add(self, obj: Any) -> None:
        peer_id = obj["id"] if isinstance(obj, dict) else obj.id
        self.peers.append(SimpleNamespace(id=peer_id, typename=self.on_add(peer_id) if self.on_add else None))


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
        self.group_context = InfrahubGroupContextBase()
        self.snat_pool = snat_pool
        self.vlan_pools = {name: _VLAN_POOLS[name] for name in vlan_pools}
        self.snat_allocations: dict[str, Any] = {}
        self.namespaces = {
            ns_type: SimpleNamespace(
                id=_NAMESPACES[ns_type][1],
                name=SimpleNamespace(value=_NAMESPACES[ns_type][0]),
                vrf_type=SimpleNamespace(value=ns_type),
                l3_vni=SimpleNamespace(value=3900),
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
        self.pool_resources = {
            name: SimpleNamespace(
                id=f"resource-{name}",
                prefix=SimpleNamespace(value=f"{_PREFIX_POOLS[name][1]}/20"),
            )
            for name in self.pools
        }
        for name, pool in self.pools.items():
            pool.resources = _Many([self.pool_resources[name].id])
        self.cluster = SimpleNamespace(
            id="cluster-1",
            name=SimpleNamespace(value="DC10-FW1-FW2"),
            capabilities=SimpleNamespace(peers=[SimpleNamespace(id="fw-1"), SimpleNamespace(id="fw-2")]),
        )
        self.lb_cluster = SimpleNamespace(
            id="lb-cluster-1",
            name=SimpleNamespace(value="DC10-LB1-LB2"),
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
        self.group_adds: list[tuple[str, list[str]]] = []
        self.sub_interfaces: dict[tuple[str, str], Any] = {}
        self.addresses: dict[tuple[str, str], Any] = {}
        self.saves: list[tuple[str, str, dict[str, Any]]] = []
        self.allocated: dict[str, Any] = {}
        self.allocate_calls = 0
        self.vlan_pool_reads: list[str] = []
        self.fail_saves_of: set[str] = set()
        self.fail_uplink_lookup = False
        self.executed_filters: list[str] = []
        self.uplink_lookups: list[list[str]] = []
        self.gets: list[str] = []
        self.routing = VrfHost()
        self.border_svis: dict[tuple[str, str], dict[str, Any]] = {}
        self.lock_events: list[tuple[str, str]] = []
        self.vrf_edges: list[tuple[str, str, list[str]]] = []

    async def execute_graphql(self, *, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        """Return the current border-device graph, including scoped memberships and context legs; a RelationshipAdd
        or RelationshipRemove on a sub-interface's ``capabilities`` (generators/helpers/interface_vrf.py: the edges
        are written one by one, never as a saved list) changes that sub-interface's peers."""
        if "RelationshipAdd" in query or "RelationshipRemove" in query:
            data = variables["data"]
            assert data["name"] == "capabilities"
            sub = next(sub for sub in self.sub_interfaces.values() if sub.id == data["id"])
            for peer in data["nodes"]:
                if "RelationshipAdd" in query:
                    sub.capabilities.add({"id": peer["id"]})
                else:
                    self.vrf_edges.append(("remove", peer["id"], [sub.id]))
                    sub.capabilities.peers = [p for p in sub.capabilities.peers if p.id != peer["id"]]
            return {}
        assert "query vrf_instances" in query
        devices = []
        for device_id in variables["device_ids"]:
            memberships = [
                raw_instance(vrf.id, device_id, vrf.security_zone.peer.name.value, deployment_id="dc10-id")
                for vrf in self.routing.store.vrfs.values()
                if device_id in vrf.members
            ]
            service_ports: list[dict[str, Any]] = []
            svis: list[dict[str, Any]] = []
            for member_id, port in self.bl_ports.items():
                if port.device.id != device_id:
                    continue
                # The border port faces the member cabled to it: the contexts it is a member of are behind the port.
                for context in self.contexts.values():
                    if member_id not in context.members or not context.vlan_id.value:
                        continue
                    legs = []
                    for sub in self.sub_interfaces.values():
                        vrf_id = self.vrf_of(sub)
                        if sub.device.id != member_id or vrf_id is None or not sub.ip_address.id:
                            continue
                        vrf = self._raw_vrf(self.routing.store.vrfs[vrf_id])
                        ns_type = vrf["security_zone"]["node"]["vrf_type"]["value"]
                        if sub.vlan_id.value != transit_vlan(context.vlan_id.value, ns_type):
                            continue  # a leg of another context of the member
                        ip = next(ip for ip in self.addresses.values() if ip.id == sub.ip_address.id)
                        legs.append(
                            {
                                "_ns_type": vrf["security_zone"]["node"]["vrf_type"]["value"],
                                "ip_address": {"node": {"address": {"value": ip.address.value}}},
                                "zone_capabilities": {"edges": [{"node": vrf}]},
                            }
                        )
                    # The border port is cabled to the uplink of a member of the context: the legs sit on that member.
                    service_ports.append(
                        raw_service_port(
                            port.id,
                            port.role.value,
                            context.kind,
                            context.vlan_id.value,
                            context_id=context.id,
                            member_role="internal-firewall" if "Firewall" in context.kind else "load-balancer",
                            raw_legs=tuple(legs),
                        )
                    )
            for (owner, _), data in self.border_svis.items():
                if owner == device_id:
                    svis.append(
                        {
                            **raw_interface(data["id"], data["name"], "l3vni", typename="DcimVirtualInterface"),
                            "zone_capabilities": edges(
                                self._raw_vrf(self.routing.store.vrfs[data["capabilities"][0]["id"]])
                            ),
                        }
                    )
            device = raw_device(
                device_id,
                device_id,
                role="border-leaf",
                deployment_kind="TopologyDataCenter",
                hosting_id="dc10-id",
                interfaces=svis,
                service_ports=service_ports,
                vrfs=memberships,
            )
            device["deployment"]["node"]["name"] = {"value": "DC10"}
            devices.append(device)
        return {
            "DcimDevice": edges(*devices),
            "ManagedVRF": edges(),
            "SecurityZone": edges(
                *(
                    {
                        "id": zone.id,
                        "name": {"value": zone.name.value},
                        "vrf_type": {"value": zone.vrf_type.value},
                        "l3_vni": {"value": zone.l3_vni.value},
                    }
                    for zone in self.namespaces.values()
                )
            ),
        }

    def _raw_vrf(self, vrf: Any) -> dict[str, Any]:
        zone = next(zone for zone in self.namespaces.values() if zone.name.value == vrf.security_zone.peer.name.value)
        return {
            "__typename": "ManagedVRF",
            "id": vrf.id,
            "deployment": {"node": {"id": vrf.deployment.id}},
            "security_zone": {
                "node": {
                    "id": zone.id,
                    "name": {"value": zone.name.value},
                    "vrf_type": {"value": zone.vrf_type.value},
                    "l3_vni": {"value": zone.l3_vni.value},
                }
            },
        }

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
        if name == "ManagedVRF" and "capabilities__ids" in kwargs:  # the VRF of interfaces (the interface edge)
            wanted = set(kwargs["capabilities__ids"])
            return [
                self._vrf_node(vrf_id)
                for vrf_id in sorted(self.routing.store.vrfs)
                if wanted & set(self.vrf_interfaces(vrf_id))
            ]
        if name == "TopologyExchangeGateway":  # generators/helpers/vrf.py: a routed exchange keeps an instance alive
            end, ids = next((key, value) for key, value in kwargs.items() if key.endswith("__ids"))
            return [ex for ex in self.exchanges.values() if ex.data[end.removesuffix("__ids")]["id"] in ids]
        if name == "ManagedVRF":
            found = list(self.routing.store.vrfs.values())
            for key, accessor in (
                ("ids", lambda node: {node.id}),
                ("deployment__ids", lambda node: {node.deployment.id}),
                ("security_zone__ids", lambda node: {node.security_zone.id}),
                ("shared_devices__ids", lambda node: node.members),
            ):
                if key in kwargs:
                    found = [node for node in found if accessor(node).intersection(kwargs[key])]
            return found
        self.executed_filters.append(name)
        if name in ("DcimPhysicalInterface", "DcimVirtualInterface") and "role__value" in kwargs:
            assert kwargs["role__value"] == "uplink"
            if name == "DcimPhysicalInterface":  # the first read of every member; a virtual one adds a second
                self.uplink_lookups.append(list(kwargs["device__ids"]))
            if self.fail_uplink_lookup:
                raise RuntimeError("uplink lookup failed")
            virtual = name == "DcimVirtualInterface"
            return [
                self.uplinks[device_id]
                for device_id in kwargs["device__ids"]
                if device_id in self.uplinks
                and (getattr(self.uplinks[device_id], "typename", None) == "DcimVirtualInterface") is virtual
            ]
        if name == "DcimCable":
            assert kwargs["include"] == ["endpoints"]
            cables = []
            for uplink in (up for up in self.uplinks.values() if hasattr(up, "cable")):
                cable_id = uplink.cable.id
                if cable_id in kwargs["ids"] and cable_id not in {cable.id for cable in cables}:
                    port = self.bl_ports.get(cable_id.removeprefix("cable-"))
                    peers = [uplink.id, *([port.id] if port else [])]
                    cables.append(SimpleNamespace(id=cable_id, endpoints=_Many(peers)))
            return cables
        if name == "SecurityZone":
            return [ns for ns in self.namespaces.values() if ns.vrf_type.value in kwargs["vrf_type__values"]]
        if name == "CoreIPPrefixPool":
            return [pool for pool_name, pool in self.pools.items() if pool_name in kwargs["name__values"]]
        if name == "ManagedFirewallHA":
            return [self.cluster]
        if name == "ManagedLoadbalancerHA":
            return [self.lb_cluster]
        if name == "ManagedVxlanSegment":
            return []
        if name in _CONTEXT_KINDS:
            if "ids" in kwargs:
                return [ctx for ctx in self.contexts.values() if ctx.id in kwargs["ids"] and ctx.kind == name]
            context = self.contexts.get(kwargs["name__value"])
            return [context] if context and context.kind == name else []
        if name == "DcimPhysicalInterface":
            ports = [*self.bl_ports.values(), *self.uplinks.values()]
            return [port for port in ports if port.id in kwargs["ids"]]
        if name == "TopologyRoutedExchange":
            return [ex for ex_name, ex in self.exchanges.items() if ex_name in kwargs["name__values"]]
        if name == "DcimVirtualInterface":  # a context's legs: the members' sub-interfaces of their uplinks
            assert "capabilities__ids" not in kwargs
            return [
                sub
                for sub in self.sub_interfaces.values()
                if sub.parent_interface.id in kwargs["parent_interface__ids"]
                and sub.device.id in kwargs["device__ids"]
                and ("vlan_id__value" not in kwargs or sub.vlan_id.value == kwargs["vlan_id__value"])
            ]
        if name == "IpamIPAddress":
            if "address__values" in kwargs:  # CablingMixin._find_addresses: one read for a prefix's addresses
                wanted = {(address, ns) for address in kwargs["address__values"] for ns in kwargs["ip_namespace__ids"]}
                return [ip for key, ip in self.addresses.items() if key in wanted]
            # the SDK hands out parsed values (IPv4Interface), not the strings the data carries
            return [
                SimpleNamespace(
                    **{**vars(ip), "address": SimpleNamespace(value=ipaddress.ip_interface(ip.address.value))}
                )
                for ip in [*self.addresses.values(), *self.snat_allocations.values()]
                if ip.id in kwargs["ids"]
            ]
        if name == "IpamPrefix":
            return [
                prefix
                for prefix in [*self.allocated.values(), *self.pool_resources.values()]
                if prefix.id in kwargs["ids"]
            ]
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
        if name == "CoreStandardGroup":
            return SimpleNamespace(id="group-service-contexts")
        if name == "CoreIPAddressPool":
            if not self.snat_pool:
                raise RuntimeError("pool not found")
            assert kwargs["name__value"] == "FW-SNAT-INTERNET-IPv4"
            return SimpleNamespace(id="pool-snat")
        if name == "IpamIPAddress":
            return self.addresses.get((kwargs["address__value"], kwargs["ip_namespace__ids"][0]))
        if name == "DcimVirtualInterface":  # ensure_vlan_subinterface: the stored sub-interface, with its capabilities
            return next(sub for sub in self.sub_interfaces.values() if sub.id == kwargs["id"])
        if name == "IpamPrefix":
            return next(prefix for prefix in self.allocated.values() if prefix.id == kwargs["id"])
        if name == "ManagedVRF":
            instance_id = kwargs["id"]
            self.routing.store.external_refs[instance_id] = {"capabilities": set(self.vrf_interfaces(instance_id))}
            return await self.routing._get(kind, instance_id, kwargs["include"])
        raise AssertionError(f"unexpected get({name})")

    async def allocate_next_ip_prefix(
        self, *, resource_pool: Any, identifier: str, prefix_length: int, **_: Any
    ) -> Any:
        assert "security_zone" not in (_.get("data") or {})
        self.allocate_calls += 1
        namespace_id, base, length = _PREFIX_POOLS[resource_pool.name.value]
        assert prefix_length == length
        if identifier not in self.allocated:
            count = sum(1 for p in self.allocated.values() if p.pool == resource_pool.id)
            self.allocated[identifier] = SimpleNamespace(
                id=f"prefix-{len(self.allocated)}",
                pool=resource_pool.id,
                prefix=SimpleNamespace(value=f"{ipaddress.ip_address(base) + 2 ** (32 - length) * count}/{length}"),
                ip_namespace=SimpleNamespace(id="default", peer=SimpleNamespace(name=SimpleNamespace(value="default"))),
                member_type=SimpleNamespace(value="address"),
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
        if name == "ManagedVRF":
            node = await self.routing._create(kind, data)
            zone = next(zone for zone in self.namespaces.values() if zone.id == data["security_zone"]["id"])
            node.security_zone = SimpleNamespace(id=zone.id, peer=zone)
            return node
        if name == "DcimVirtualInterface" and data.get("role") == "l3vni":
            node = await self.routing._create(kind, data)
            self.border_svis[(data["device"]["id"], data["name"])] = {**data, "id": node.id}
            return node
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
            context.members = [member["id"] for member in data.get("shared_devices") or []]
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
            assert "security_zone" not in data
            namespace_id = data["ip_namespace"].id if "ip_namespace" in data else "default"
            key = (data["address"], namespace_id)
            ip = self.addresses.setdefault(
                key,
                SimpleNamespace(
                    id=f"ip-{len(self.addresses)}",
                    address=SimpleNamespace(value=data["address"]),
                    ip_namespace=SimpleNamespace(
                        id=namespace_id, peer=SimpleNamespace(name=SimpleNamespace(value="default"))
                    ),
                    ip_prefix=next(
                        p
                        for p in self.allocated.values()
                        if ipaddress.ip_interface(data["address"]).ip in ipaddress.ip_network(p.prefix.value)
                    ),
                ),
            )
            return self._node(name, data["address"], backing=ip)
        if name == "DcimVirtualInterface":
            assert "security_zone" not in data
            key = (data["device"]["id"], data["name"])
            sub = self.sub_interfaces.setdefault(
                key,
                SimpleNamespace(
                    id=f"sub-{len(self.sub_interfaces)}",
                    name=SimpleNamespace(value=data["name"]),
                    device=SimpleNamespace(id=data["device"]["id"]),
                    parent_interface=SimpleNamespace(id=(data.get("parent_interface") or {}).get("id")),
                    vlan_id=SimpleNamespace(value=data.get("vlan_id")),
                    ip_address=SimpleNamespace(id=None),
                    capabilities=_Many(on_add=lambda peer_id, key=key: self._joined(key, peer_id)),
                ),
            )
            sub.ip_address = SimpleNamespace(id=(data.get("ip_address") or {}).get("id") or sub.ip_address.id)
            # The VRF is the instance's interface edge, written after the save (generators/helpers/interface_vrf.py).
            assert "vrf" not in data and "capabilities" not in data
            return self._node(name, data["name"], backing=sub)
        raise AssertionError(f"unexpected create({name})")

    def _context(self, kind: str, name: str) -> Any:
        """A context as the SDK returns it: a firewall context has a SNAT address, a load balancer's a VIP prefix."""
        context = SimpleNamespace(
            id=f"ctx-{len(self.contexts)}",
            kind=kind,
            members=[],
            name=SimpleNamespace(value=name),
            vlan_id=SimpleNamespace(value=None),
            tenant_id=None,
            add_relationships=None,
        )
        if kind == "ManagedLoadbalancerContext":
            context.vip_prefix = SimpleNamespace(id=None)
        else:
            context.snat_address = SimpleNamespace(id=None)
        return context

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
                # A context has no interface edge: its only relationship write is joining service_contexts.
                assert relation_to_update == "member_of_groups"
                world.group_adds.append((backing.id, list(related_nodes)))

            node.add_relationships = add_relationships
        return node

    # --- the VRF interface edge -------------------------------------------
    def vrf_of(self, sub: Any) -> str | None:
        """The instance a sub-interface is in: the ManagedVRF among its capabilities."""
        return next((peer.id for peer in sub.capabilities.peers if peer.typename == "ManagedVRF"), None)

    def vrf_interfaces(self, vrf_id: str) -> list[str]:
        """The interfaces an instance lists in its capabilities (the same edges, seen from the instance)."""
        return [sub.id for sub in self.sub_interfaces.values() if self.vrf_of(sub) == vrf_id]

    def _joined(self, key: tuple[str, str], peer_id: str) -> str | None:
        """A capability added to a sub-interface (its RelationshipAdd): a VRF instance is the leg joining it, while the
        border devices' VRF locks and the context's lock are held."""
        vrf = self.routing.store.vrfs.get(peer_id)
        if vrf is None:
            return None
        active: set[str] = set()
        for operation, lock in self.lock_events:
            if operation == "acquire":
                active.add(lock)
            else:
                active.remove(lock)
        assert active.intersection(f"vrf-device-{member}" for member in vrf.members)
        assert any(lock.startswith(("fw-context-", "lb-context-")) for lock in active)
        self.vrf_edges.append(("add", peer_id, [self.sub_interfaces[key].id]))
        return "ManagedVRF"

    def _vrf_node(self, vrf_id: str) -> Any:
        """An instance as a ``capabilities__ids`` read sees it."""
        vrf = self.routing.store.vrfs[vrf_id]
        return SimpleNamespace(
            id=vrf_id,
            security_zone=vrf.security_zone,
            capabilities=SimpleNamespace(peers=[SimpleNamespace(id=sub) for sub in self.vrf_interfaces(vrf_id)]),
        )

    # --- assertions helpers ---------------------------------------------
    @property
    def writes(self) -> int:
        return len(self.saves) + len(self.vrf_edges) + self.allocate_calls

    def saved(self, kind: str) -> list[str]:
        return [label for saved_kind, label, _ in self.saves if saved_kind == kind]

    def leg_zone(self, prefix: Any) -> str:
        """Infer the transit zone from its pool, never from an IPAM routing field."""
        pool = next(pool for pool in self.pools.values() if pool.id == prefix.pool)
        return _PREFIX_POOLS[pool.name.value][0]

    async def delete(self, *, kind: Any, id: str) -> None:  # noqa: A002
        """Apply scoped routing cleanup to the same graph subsequent reads use."""
        await self.routing._delete(kind, id)
        self.border_svis = {key: data for key, data in self.border_svis.items() if data["id"] != id}


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

    async def acquire(key: str) -> str:
        world.lock_events.append(("acquire", key))
        if not key.startswith("vrf-"):
            events.append(f"acquire:{key}")
        return key

    async def release(key: str) -> None:
        world.lock_events.append(("release", key))
        if not key.startswith("vrf-"):
            events.append("release")

    gen.acquire_resource_lock = AsyncMock(side_effect=acquire)
    gen.release_resource_lock = AsyncMock(side_effect=release)
    gen.link_serving_context = AsyncMock()
    gen.set_controllers_from = MagicMock()

    async def reconcile_border(target: Any, _label: str) -> None:
        """The border devices facing the context's members (the real one resolves them through their cables)."""
        member_ids = [device["id"] if isinstance(device, dict) else device.id for device in target.devices]
        await gen.reconcile_vrfs({world.bl_ports[m].device.id for m in member_ids if m in world.bl_ports})

    gen._reconcile_border_vrfs = AsyncMock(side_effect=reconcile_border)
    return gen, events


def _dedicated(gen: Any) -> None:
    """A dedicated pair already provisioned: the context flow starts at the legs."""
    cluster = SimpleNamespace(
        id="ded-cluster", name=SimpleNamespace(value="DC10-ded-ha-prod"), capabilities=SimpleNamespace(peers=[])
    )
    members = [
        SimpleNamespace(id="fw-1", name=SimpleNamespace(value="c005-DC10-FW1-prod"), hosting_device=None),
        SimpleNamespace(id="fw-2", name=SimpleNamespace(value="DC10-FW2-C005-p-dedicated"), hosting_device=None),
    ]
    gen._ensure_dedicated_device_pair = AsyncMock(return_value=(cluster, members))


async def _run(gen: Any, world: _World, payload: dict[str, Any]) -> None:
    await gen.generate(payload)


def _sub_names(world: _World) -> dict[tuple[str, str], list[str]]:
    """The capabilities of each sub-interface but its VRF (none: a leg's exchanges and context are derived)."""
    return {
        key: [peer.id for peer in sub.capabilities.peers if peer.typename != "ManagedVRF"]
        for key, sub in world.sub_interfaces.items()
    }
