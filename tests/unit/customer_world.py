"""An in-memory Infrahub for the customer boarding flows (generators/customers): the HFID-upserting client of
``plan_graph_helpers`` with the objects a DC (or a colocation zone) has before a customer arrives (the VRF namespaces, the
transit pools, an HA pair of firewalls and one of load balancers, each member cabled to a border service port) and the
reads of the generators' filters. What the flows write lands in ``store``: the tests read the resulting objects, and run a
flow twice on the same world to see what a re-run leaves alone.
"""

from __future__ import annotations

import ipaddress
from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager, contextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from infrahub_sdk.exceptions import NodeNotFoundError

from tests.unit.plan_graph_helpers import HfidClient, HfidNode, make_hfid_generator
from tests.unit.plan_reconciler_helpers import FakeNode, kind_name

NAMESPACES = {
    "prod": ("PROD", "100.66.0.0"),
    "non_prod": ("NONPROD", "100.66.16.0"),
    "internet": ("INTERNET", "100.66.32.0"),
    "management": ("MANAGEMENT", "100.66.48.0"),
}
# pool name -> prefix length handed out (data/bootstrap/23_transit_pools.yml)
PREFIX_POOLS = {
    **{f"FW-Transit-{name}-IPv4": 29 for name, _ in NAMESPACES.values()},
    "LB-Leg-PROD-IPv4": 29,
    "LB-Leg-NONPROD-IPv4": 29,
    "LB-VIP-PROD-IPv4": 26,
    "LB-VIP-NONPROD-IPv4": 26,
}
# the base NICs of a virtual customer template (data/bootstrap 09_virtual_device_templates_*): name -> role
VIRTUAL_NICS = {"eth0": "management", "eth1": "uplink", "eth2": "uplink", "eth7": "ha"}
VLAN_POOLS = {"dc10-fw-context-vlan-pool": 3000, "dc10-lb-context-vlan-pool": 3100}
CONTEXT_KINDS = ("ManagedFirewallContext", "ManagedLoadbalancerContext")


class WorldNode(HfidNode):
    """A stored node that also answers ``typename`` like an SDK node."""

    @property
    def typename(self) -> str:
        return self.kind


def _ids(value: Any) -> set[str]:
    """The ids a relationship value points at (an ``{"id"}`` reference, a list of them, an id)."""
    if isinstance(value, dict):
        return {value["id"]} if value.get("id") else set()
    if isinstance(value, list):
        return {peer for item in value for peer in _ids(item)}
    return {value} if isinstance(value, str) else set()


class CustomerWorld(HfidClient):
    """The DC10 a customer boards on. ``vlan_pools``: pool name -> next VLAN it hands out. ``missing``: names of seeded
    objects to leave out (a transit pool, the SNAT pool, a VLAN pool, a namespace), to see a flow fail before it writes.
    """

    def __init__(
        self,
        *,
        namespaces: tuple[str, ...] = tuple(NAMESPACES),
        missing: tuple[str, ...] = (),
        vlan_pools: dict[str, int] | None = None,
    ) -> None:
        super().__init__()
        self.vlan_pools = dict(VLAN_POOLS if vlan_pools is None else vlan_pools)
        self.reads: list[tuple[str, dict[str, Any]]] = []
        self.seed("IpamNamespace", id="ns-default", name="default")
        for ns_type in namespaces:
            name, _ = NAMESPACES[ns_type]
            if name in missing:
                continue
            zone = self.seed("SecurityZone", name=name, vrf_type=ns_type, l3_vni=3900)
            # the border devices' instance of the zone (``border_vrfs``): the interfaces in it list it as a capability
            self.seed(
                "ManagedVRF", id=f"vrf-{name}", name=f"DC10-{name}", security_zone={"id": zone.id}, capabilities=[]
            )
        for index, name in enumerate(PREFIX_POOLS):
            if name not in missing:
                # the block of the pool: what ``allocate_next_ip_prefix`` below hands out of lies in it
                self.add_pool(
                    "CoreIPPrefixPool", name, resources=[f"{ipaddress.ip_address('100.66.0.0') + index * 4096}/20"]
                )
        if "FW-SNAT-INTERNET-IPv4" not in missing:
            self.add_pool("CoreIPAddressPool", "FW-SNAT-INTERNET-IPv4")
        for name in self.vlan_pools:
            if name not in missing:
                self.seed("CoreNumberPool", name=name)
        self.seed("CoreStandardGroup", name="service_contexts")
        self.fail_saves_of: set[str] = set()
        self.border_ports: dict[str, FakeNode] = {}
        self.uplinks: dict[str, FakeNode] = {}

    def seed(self, kind: str, **data: Any) -> WorldNode:
        node = WorldNode(self, kind, {"id": data.pop("id", None) or self._next_id(kind), **data})
        self.store[node.id] = node
        return node

    # --- the DC the customer boards on --------------------------------------
    def add_pool(self, kind: str, name: str, *, resources: list[str] | None = None) -> WorldNode:
        """A bootstrap pool; a prefix pool has its resource prefixes (the blocks an authored leg must lie in)."""
        refs = [self.seed("IpamPrefix", prefix=prefix, member_type="address").id for prefix in resources or []]
        return self.seed(kind, name=name, resources=[{"id": ref} for ref in refs])

    def add_member(
        self,
        name: str,
        *,
        role: str = "firewall",
        border: str | None = None,
        uplink_kind: str = "DcimPhysicalInterface",
    ) -> WorldNode:
        """A device of a pair (id = its name), its role=uplink interface and the cable to a border service port of
        ``role`` (``firewall`` or ``load-balancer``) on the border leaf ``border`` (default ``BL-<name>``).
        """
        device = self.seed("DcimPhysicalDevice", id=name, name=name)
        port = self.seed(
            "DcimPhysicalInterface",
            id=f"bl-port-{name}",
            name="Ethernet1/1",
            role=role,
            device={"id": border or f"BL-{name}"},
        )
        cable = self.seed("DcimCable", id=f"cable-{name}", endpoints=[{"id": f"up-{name}"}, {"id": port.id}])
        for host_port, host_role in (("MGMT", "management"), ("sync", "ha")):
            self.seed(
                "DcimPhysicalInterface",
                id=f"{name}-{host_port}",
                name=host_port,
                role=host_role,
                device={"id": name},
                cable=None,
            )
        self.uplinks[name] = self.seed(
            uplink_kind,
            id=f"up-{name}",
            name="Ethernet1/25",
            role="uplink",
            device={"id": name},
            cable={"id": cable.id},
        )
        self.border_ports[name] = port
        return device

    def add_hosted_member(self, host: str, name: str, *, pinned: bool = True) -> WorldNode:
        """A virtual instance ``name`` hosted on the cabled device ``host``: its role=uplink NIC rides the host's uplink
        port through its ``parent``, unless ``pinned`` is False.
        """
        device = self.seed("DcimVirtualDevice", id=name, name=name, hosting_device={"id": host})
        self.seed(
            "DcimVirtualInterface",
            id=f"nic-{name}",
            name="eth1",
            role="uplink",
            device={"id": name},
            cable=None,
            parent={"id": self.uplinks[host].id} if pinned else {"id": None},
        )
        return device

    def add_dedicated_prerequisites(self) -> None:
        """What a dedicated pair is built from on DC10: the customer templates of a firewall and a load balancer, the DC's
        management address pool, the ``ha_domains`` group and the firewall HA group id pool.
        """
        self.add_virtual_template("CloudGuard_EDGE_CUSTOMER_M")
        self.add_virtual_template("BIG-IP-VE_LOAD_BALANCER_CUSTOMER_M")
        self.seed("CoreIPAddressPool", name="dc10-management-pool")
        self.seed("CoreStandardGroup", name="ha_domains")
        self.seed("CoreNumberPool", name="Firewall-HA-Group-ID")

    def add_virtual_template(self, name: str) -> WorldNode:
        """A ``TemplateDcimVirtualDevice`` with its device type and platform."""
        device_type = self.seed("DcimDeviceType", name=f"{name}-type")
        platform = self.seed("DcimPlatform", name=f"{name}-platform")
        return self.seed(
            "TemplateDcimVirtualDevice",
            template_name=name,
            device_type={"id": device_type.id},
            platform={"id": platform.id},
        )

    def add_pair(self, kind: str, first: str, second: str, *, name: str, role: str = "firewall") -> WorldNode:
        """Two cabled members and the HA domain ``name`` that holds them."""
        for member in (first, second):
            self.add_member(member, role=role)
        return self.seed(kind, name=name, capabilities=[{"id": first}, {"id": second}])

    # --- reads ---------------------------------------------------------------
    async def filters(self, kind: Any, **flt: Any) -> list[FakeNode]:
        wanted = kind_name(kind)
        self.reads.append((wanted, flt))
        if wanted == "IpamIPAddress" and "address__values" in flt:
            return await super().filters(kind, **flt)
        if wanted == "DcimVirtualInterface" and "device__name__values" in flt:
            pass  # the generic matcher resolves the device's name
        kinds = {"DcimVirtualInterface", "CloudPeeringInterface"} if wanted == "DcimSubInterface" else {wanted}
        return [node for node in self.store.values() if node.kind in kinds and self._matches(node, flt)]

    def _matches(self, node: FakeNode, flt: dict[str, Any]) -> bool:
        for key, wanted in flt.items():
            if key == "include":
                continue
            if key == "ids":
                if node.id not in wanted:
                    return False
                continue
            path, _, op = key.rpartition("__")
            if op in ("ids", "id"):
                if not _ids(self._field(node, path)) & set(wanted if isinstance(wanted, list | set) else [wanted]):
                    return False
            elif op in ("value", "values"):
                values = [wanted] if op == "value" else wanted
                if self._field(node, path) not in values:
                    return False
        return True

    def _field(self, node: FakeNode, path: str) -> Any:
        """A filter path on a node: an attribute (``role``), or an attribute of a relationship's peer (``device__name``)."""
        head, _, rest = path.partition("__")
        value = node.data.get(head)
        if not rest:
            return value
        peers = [self.store[peer] for peer in _ids(value) if peer in self.store]
        return peers[0].data.get(rest) if peers else None

    async def get(self, kind: Any, **flt: Any) -> Any:
        if "id" in flt:
            flt["ids"] = [flt.pop("id")]
        found = await self.filters(kind, **flt)
        if not found:
            raise NodeNotFoundError(branch_name="main", node_type=kind_name(kind), identifier=flt)
        return found[0]

    # --- writes --------------------------------------------------------------
    async def create(self, kind: Any, data: dict[str, Any]) -> WorldNode:
        name = kind_name(kind)
        if name == "IpamIPAddress" and "id" not in data:
            address: Any = self.addresses.get(data["address"])
            if address is None:
                address = self.seed("IpamIPAddress", address=data["address"], ip_namespace={"id": "ns-default"})
            setattr(address, "save", AsyncMock())  # the addresses of a prefix are not counted among the plan's saves
            holder = ipaddress.ip_interface(data["address"]).ip
            address.data["ip_prefix"] = next(
                (
                    {"id": prefix.id}
                    for prefix in self.nodes("IpamPrefix")
                    if prefix.data.get("identifier") and holder in ipaddress.ip_network(prefix.data["prefix"])
                ),
                None,
            )
            self.addresses[data["address"]] = address
            self.created_ids.append(address.id)
            return address
        return WorldNode(self, name, data)

    def record_save(self, node: FakeNode, kwargs: dict[str, Any]) -> None:
        if node.kind in self.fail_saves_of:
            raise RuntimeError(f"{node.kind} save failed")
        for field, value in list(node.data.items()):
            if hasattr(value, "id") and not isinstance(value, dict | str | list):
                node.data[field] = {"id": value.id}  # an SDK object as a relationship value
            elif isinstance(value, str) and value in self.store:
                node.data[field] = {"id": value}  # the id of a node the plan saved
            elif (
                isinstance(value, list)
                and value
                and all(isinstance(item, str) and item in self.store for item in value)
            ):
                node.data[field] = [{"id": item} for item in value]
        vlan = node.data.get("vlan_id")
        if isinstance(vlan, dict) and "from_pool" in vlan:
            stored = self.store.get(node.id)
            if stored is not None and stored.data.get("vlan_id"):
                node.data["vlan_id"] = stored.data["vlan_id"]
            else:
                pool = self.store[vlan["from_pool"]["id"]].data["name"]
                node.data["vlan_id"] = self.vlan_pools[pool]
                self.vlan_pools[pool] += 1
        if node.kind == "DcimVirtualInterface":
            node.data.setdefault("capabilities", [])
        new_virtual = node.kind == "DcimVirtualDevice" and "object_template" in node.data and node.id not in self.store
        super().record_save(node, kwargs)
        if new_virtual:
            for nic, role in VIRTUAL_NICS.items():  # the server instantiates the template's NICs with the device
                self.seed("DcimVirtualInterface", name=nic, role=role, status="free", device={"id": node.id})

    async def execute_graphql(self, query: str, variables: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
        """The RelationshipAdd of ``HfidClient``; an interface added to a VRF instance is listed by the instance too."""
        result = await super().execute_graphql(query, variables)
        data = (variables or {})["data"]
        for peer in data["nodes"]:
            instance = self.store.get(peer["id"])
            if data["name"] == "capabilities" and instance is not None and instance.kind == "ManagedVRF":
                instance.data["capabilities"].append({"id": data["id"]})
        return result

    async def allocate_next_ip_prefix(self, resource_pool: Any, identifier: str, **kwargs: Any) -> FakeNode:
        node = await super().allocate_next_ip_prefix(resource_pool, identifier, **kwargs)
        pool = resource_pool.data["name"]
        length = kwargs["prefix_length"]
        count = sum(1 for (pool_id, _) in self.allocations if pool_id == resource_pool.id) - 1
        slot = (list(PREFIX_POOLS).index(pool) if pool in PREFIX_POOLS else len(PREFIX_POOLS)) * 4096
        base = ipaddress.ip_address("100.66.0.0") + slot + 2 ** (32 - length) * count
        for stub in ("prefix", "ip_namespace"):  # the stand-ins of HfidClient: the data below answers instead
            vars(node).pop(stub, None)
        node.data["prefix"] = f"{base}/{length}"
        node.data["member_type"] = kwargs.get("member_type")
        node.data["ip_namespace"] = {"id": "ns-default"}
        node.data["pool"] = pool
        return node

    # --- what a test looks at ------------------------------------------------
    def address_saves(self) -> list[dict[str, Any]]:
        """The keyword arguments each address of an allocated prefix was saved with."""
        return [dict(getattr(address, "save").await_args.kwargs) for address in self.addresses.values()]

    def nodes(self, kind: str) -> list[FakeNode]:
        return [node for node in self.store.values() if node.kind == kind]

    def named(self, kind: str, name: str) -> FakeNode:
        (node,) = (node for node in self.nodes(kind) if name in (node.data.get("name"), node.data.get("template_name")))
        return node

    def saves_of(self, kind: str) -> list[tuple[str, str, dict[str, Any], dict[str, Any]]]:
        return [save for save in self.saves if save[0] == kind]

    def untracked(self, kind: str) -> bool:
        """Whether every save of ``kind`` left the run's tracking group."""
        saves = self.saves_of(kind)
        return bool(saves) and all(kwargs.get("update_group_context") is False for _, _, _, kwargs in saves)

    def tracked(self, kind: str) -> bool:
        saves = self.saves_of(kind)
        return bool(saves) and all("update_group_context" not in kwargs for _, _, _, kwargs in saves)

    def named_prefix(self, identifier: str) -> FakeNode:
        """The prefix allocated under ``identifier``."""
        (node,) = (node for node in self.nodes("IpamPrefix") if node.data.get("identifier") == identifier)
        return node

    def sub_interfaces(self) -> list[FakeNode]:
        """The sub-interfaces of the context legs (role service)."""
        return [node for node in self.nodes("DcimVirtualInterface") if node.data.get("role") == "service"]


def dc_world(
    *, loadbalancers: bool = False, customers: tuple[str, ...] = ("cust-1", "cust-2"), **kwargs: Any
) -> CustomerWorld:
    """DC10 with its firewall pair ``DC10-FW1-FW2`` (and, with ``loadbalancers``, its load-balancer pair
    ``DC10-LB1-LB2``) and the footprints of ``customers`` (boarded by ``dc_payload``). ``kwargs``: see ``CustomerWorld``.
    """
    world = CustomerWorld(**kwargs)
    world.add_pair("ManagedFirewallHA", "DC10-FW1", "DC10-FW2", name="DC10-FW1-FW2")
    if loadbalancers:
        world.add_pair("ManagedLoadbalancerHA", "DC10-LB1", "DC10-LB2", name="DC10-LB1-LB2", role="load-balancer")
    for customer in customers:
        world.seed(
            "TopologyCustomerDC",
            id=customer,
            name=f"{customer}-DC10",
            serving_firewall_contexts=[],
            serving_loadbalancer_context=None,
        )
    return world


def generator(world: CustomerWorld, cls: type[Any]) -> Any:
    """A generator of ``cls`` on ``world``: no parent generator in flight, the others' flows (``run_generator``) recorded."""
    gen = make_hfid_generator(world, cls=cls)
    gen.wait_for_parent_generator_and_refetch = AsyncMock(return_value=None)
    gen.run_generator = AsyncMock()
    return gen


@contextmanager
def border_vrfs() -> Generator[SimpleNamespace]:
    """The border devices' VRF convergence (generators/capabilities/vrf.py, tested on its own) answered by a recording
    stand-in: ``vrfs_held`` keeps the device locks of ``needs`` while its body runs, the instance of a zone is ``vrf-<zone>``
    on every border device, ``reconcile_vrfs`` is awaited with the devices it converges.
    """
    recorded = SimpleNamespace(held=[], reconciled=[], events=[])

    @asynccontextmanager
    async def held(_gen: Any, needs: dict[str, set[str]]) -> AsyncGenerator[None]:
        recorded.held.append({device: set(zones) for device, zones in needs.items()})
        recorded.events.append("held")
        yield
        recorded.events.append("released")

    async def local_ids(_gen: Any, device_ids: Any, names: Any) -> dict[tuple[str, str], str]:
        return {(device, zone): f"vrf-{zone}" for device in device_ids for zone in names}

    async def reconcile(_gen: Any, device_ids: Any) -> None:
        recorded.reconciled.append(set(device_ids))
        recorded.events.append("reconciled")

    with (
        patch("generators.customers.firewall_context.vrfs_held", held),
        patch("generators.customers.firewall_context.local_vrf_ids", local_ids),
        patch("generators.customers.customer_facility.reconcile_vrfs", reconcile),
    ):
        yield recorded


def dc_payload(
    *,
    customer_id: str = "cust-1",
    environment: str = "p",
    dedicated_firewall: bool = False,
    dedicated_loadbalancer: bool = False,
    firewalls: tuple[str, ...] = ("DC10-FW1", "DC10-FW2"),
    loadbalancers: tuple[str, ...] = (),
    external_firewalls: tuple[str, ...] = (),
    connectivity_mode: str = "pbr",
    serving: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The TopologyCustomerDC boarding query result of customer C005 on DC10 (cleaned shape)."""

    def device(name: str, platform: str) -> dict[str, Any]:
        return {"id": name, "name": name, "kind": "DcimPhysicalDevice", "platform": {"name": platform}}

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
                    "firewall_devices": [device(name, "checkpoint_gaia") for name in firewalls],
                    "loadbalancer_devices": [device(name, "f5_tmos") for name in loadbalancers],
                    "external_firewall_devices": [device(name, "checkpoint_gaia") for name in external_firewalls],
                    "security_manager_controllers": [],
                    "lb_manager_controllers": [],
                },
                "design": {
                    "dedicated_firewall": {"value": dedicated_firewall},
                    "dedicated_loadbalancer": {"value": dedicated_loadbalancer},
                },
                **(serving or {}),
            }
        ]
    }
