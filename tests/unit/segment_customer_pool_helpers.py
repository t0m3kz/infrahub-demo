"""Builders the customer pool tests share: a recording SDK client, the generator and the cleaned segment data."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager, contextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from infrahub_sdk.exceptions import GraphQLError, NodeNotFoundError
from infrahub_sdk.protocols import CoreIPPrefixPool

from generators.base import BaseGenerator
from generators.model.errors import GeneratorError
from generators.protocols import IpamIPAddress
from generators.segments.segment import VxlanSegmentGenerator


class CustomerPoolsHost(BaseGenerator):
    """What a footprint generator is to the customer pools: the generator base."""


def footprint(kind: str, name: str) -> dict[str, Any]:
    return {
        "typename": kind,
        "name": name,
        "design": {
            "name": "S",
            "address_prefix_length": 23,
            "segment_prefix_length": 24,
            "ipv6_prefix_length": 60,
            "ipv6_segment_prefix_length": 64,
        },
    }


def exhausted() -> GraphQLError:
    return GraphQLError(errors=[{"message": "no more prefix available in the pool"}])


class Client:
    """The SDK calls the addressing makes, recorded; a pool answers for the names in ``pools``."""

    def __init__(self, *, pools: dict[str, MagicMock] | None = None, tenant_blocks: int = 8) -> None:
        self.pools = pools or {}
        self.tenant = MagicMock(id="pool-tenant")
        self.tenant.name.value = "Tenant-PROD-IPv4"
        self.tenant.default_prefix_length.value = 24  # 23b_tenant_pools.yml; an office segment takes it
        self.tenant6 = MagicMock(id="pool-tenant6")
        self.tenant6.name.value = "Tenant-PROD-IPv6"
        self.tenant6.default_prefix_length.value = 64
        self.tenants = {"Tenant-PROD-IPv4": self.tenant, "Tenant-PROD-IPv6": self.tenant6}
        for name in ("Tenant-NONPROD-IPv4", "Tenant-NONPROD-IPv6"):
            pool = MagicMock(id=f"pool-{name.lower()}")
            pool.name.value = name
            pool.default_prefix_length.value = 64 if name.endswith("IPv6") else 24
            self.tenants[name] = pool
        self.allocations: list[dict[str, Any]] = []
        self.tenant_blocks = tenant_blocks  # blocks the tenant pool can still carve
        self.customer_room: dict[str, int] = {}  # pool name -> allocations it can still serve (absent: unlimited)
        self.edges: list[dict[str, Any]] = []
        self.created: list[tuple[str, dict[str, Any]]] = []
        self.known: dict[tuple[str, str], MagicMock] = {}
        self.refill_on_edge: dict[str, int] = {}
        self.group_context = SimpleNamespace(related_node_ids=[])
        self.sliced: list[dict[str, Any]] = []  # the pools carved from a tenant pool (``recording_pool_functions``)

    async def filters(self, *, kind: Any, **kwargs: Any) -> list[Any]:
        if kind is IpamIPAddress:  # the find-before-create read of a prefix's addresses: none exist yet
            return []
        assert kind is CoreIPPrefixPool
        name = kwargs["name__value"]
        if name in self.tenants:
            return [self.tenants[name]]
        return [self.pools[name]] if name in self.pools else []

    async def get(self, *, kind: Any, **kwargs: Any) -> Any:
        """``resolve_pool``'s read of a tenant pool by name (a missing one is not found)."""
        assert kind is CoreIPPrefixPool
        if kwargs["name__value"] not in self.tenants:
            raise NodeNotFoundError(branch_name="main", node_type="CoreIPPrefixPool", identifier=kwargs)
        return self.tenants[kwargs["name__value"]]

    async def allocate_next_ip_prefix(self, **kwargs: Any) -> Any:
        """A prefix per (pool, identifier): the same identifier returns the same prefix; a length change is refused."""
        pool = kwargs["resource_pool"]
        self.allocations.append(kwargs)
        key = (pool.id, kwargs["identifier"])
        if key in self.known:
            known = self.known[key]
            if int(known.prefix.value.split("/")[1]) != kwargs["prefix_length"]:
                raise GraphQLError(errors=[{"message": "the prefix length cannot be changed for this identifier"}])
            return known
        if pool in self.tenants.values():
            if self.tenant_blocks <= 0:
                raise exhausted()
            self.tenant_blocks -= 1
        else:
            room = self.customer_room.get(pool.name.value)
            if room is not None:
                if room <= 0:
                    raise exhausted()
                self.customer_room[pool.name.value] = room - 1
        node = MagicMock(id=f"prefix-{len(self.known)}")
        if pool.name.value.lower().endswith("ipv6"):
            node.prefix.value = f"fd5a:1760:{len(self.known)}::/{kwargs['prefix_length']}"
        else:
            node.prefix.value = f"10.176.{len(self.known)}.0/{kwargs['prefix_length']}"
        self.known[key] = node
        return node

    async def create(self, *, kind: Any, data: dict[str, Any]) -> Any:
        if kind is IpamIPAddress:  # an address of an allocated prefix: the gateway, not recorded in ``created``
            address = MagicMock(id="gw-1")
            address.address.value = data["address"]
            address.save = AsyncMock()
            return address
        self.created.append((kind, data))
        node = MagicMock()
        node.save = AsyncMock()
        return node

    async def execute_graphql(self, *, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        self.edges.append(variables["data"])
        self.customer_room.update(self.refill_on_edge)  # the new block gives the pool room
        return {"RelationshipAdd": {"ok": True}}


def customer_pool(name: str, blocks: int = 1) -> MagicMock:
    pool = MagicMock(id=f"pool-{name}")
    pool.name.value = name
    pool.resources.peers = [MagicMock() for _ in range(blocks)]
    return pool


def make_gen(client: Client, cls: Any = VxlanSegmentGenerator) -> Any:
    """A generator (default: the segment one) with the recording client, a raising logger and a recording lock."""
    gen: Any = cls.__new__(cls)
    gen.client = client
    gen.logger = MagicMock()

    def _fail(message: str, *args: object, **kwargs: object) -> None:
        raise GeneratorError(message)

    gen.logger.error.side_effect = _fail
    gen.locks = []

    @asynccontextmanager
    async def lock(key: str) -> AsyncGenerator[None]:
        gen.locks.append(key)
        yield

    gen.resource_lock = lock

    gen.sliced = client.sliced
    return gen


@contextmanager
def recording_pool_functions() -> Generator[None]:
    """The two functions ``generators/ipam/customer_pool.py`` boards a pool with, answered by the recording ``Client``: the
    parent is the tenant pool by name, the carve is recorded (``Client.sliced``, with the parent's name) and the pool
    it makes is found from then on.
    """

    async def carve(client: Any, _logger: Any, parent: Any, **kwargs: Any) -> MagicMock:
        client.sliced.append({**kwargs, "parent_pool_name": parent.name.value})
        pool = customer_pool(kwargs["pool_name"])
        client.pools[kwargs["pool_name"]] = pool
        return pool

    async def parent_pool(gen: Any, name: str) -> Any:
        return gen.client.tenants[name]

    with (
        patch("generators.ipam.customer_pool.ensure_sliced_pool", carve),
        patch("generators.ipam.customer_pool.get_parent_pool", parent_pool),
    ):
        yield


def segment_data(
    host_count: int | None = None,
    footprints: list[dict[str, Any]] | None = None,
    gateway: Any = None,
    ip_version: str = "ipv4",
) -> dict[str, Any]:
    """A cleaned vxlan_segment query result of customer C005 (PROD)."""
    return {
        "id": "seg-1",
        "typename": "ManagedVxlanSegment",
        "name": "c005-web-p",
        "environment": "p",
        "owner": {"id": "cust-1", "org_id": "C005"},
        "security_zone": {"name": "PROD"},
        "host_count": host_count,
        "ip_version": ip_version,
        "customer_deployments": footprints
        if footprints is not None
        else [footprint("TopologyCustomerDC", "C005-P-DC10")],
        "gateway": gateway,
    }
