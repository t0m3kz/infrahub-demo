"""An in-memory Infrahub client that upserts by HFID the way the server does, for the plans that reference objects by
HFID (generators/plan.py ``Hfid``): a reference resolves to the stored node, a write without an id finds its node by its
HFID, a device saved with an ``object_template`` brings its template's ports.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from infrahub_sdk.exceptions import GraphQLError

from generators.plan import HFID_FIELDS
from tests.unit.plan_reconciler_helpers import FakeClient, FakeNode, PlanGenerator, kind_name, make_generator_of


class HfidNode(FakeNode):
    """A stored node whose relationships answer ``.id`` and ``.peer`` (``include=`` puts the peer in the store)."""

    def __getattr__(self, attribute: str) -> Any:
        if attribute == "member_of_groups":
            # The tracking groups of the node (``generated`` in the seed): their kinds, as ``include=`` reads them
            groups = self.data.get("groups") or []
            return SimpleNamespace(peers=[SimpleNamespace(typename=kind) for kind in groups])
        if attribute.startswith("_") or attribute not in self.data:
            raise AttributeError(attribute)
        value = self.data[attribute]
        store = self._client.store
        if value is None:
            # An unset attribute or relationship (a device without a rack)
            return SimpleNamespace(value=None, id=None, peer=None, initialized=False)
        if isinstance(value, dict):
            return SimpleNamespace(id=value.get("id"), peer=store.get(value.get("id")), initialized=True)
        if isinstance(value, str) and value in store:
            return SimpleNamespace(id=value, peer=store[value], initialized=True)
        if isinstance(value, list) and value and all(isinstance(item, dict) and "id" in item for item in value):
            peers = [
                SimpleNamespace(
                    id=item["id"],
                    peer=store.get(item["id"]),
                    typename=getattr(store.get(item["id"]), "kind", ""),
                    display_label=(store[item["id"]].data.get("name") if item["id"] in store else None),
                )
                for item in value
            ]
            return SimpleNamespace(value=value, peers=peers)
        if isinstance(value, list) and not value:
            return SimpleNamespace(value=value, peers=[])
        return SimpleNamespace(value=value)


RELATIONSHIPS_OF_KIND = {"ManagedMLAG": ("interfaces",)}
"""The relationships a stored node of the kind answers an empty read for (a read with ``include=`` of a relationship it
was saved without)."""

MANDATORY_FIELDS = {"RoutingPassword": ("password",)}
"""The attributes a create of the kind must carry (the schema's non-optional ones the tests rely on)."""


class HfidClient(FakeClient):
    """``FakeClient`` plus the server's HFID behaviour. ``hfid_saves`` counts saves that found an existing node. A save
    without an id is validated as a create, even when the HFID names an existing object: it must carry the mandatory
    fields (``MANDATORY_FIELDS``), as the server demands (``ATTRIBUTE_REQUIRED``).
    """

    def __init__(self) -> None:
        super().__init__()
        self.found_by_hfid: list[tuple[str, tuple[str, ...]]] = []
        self.address_pool_allocations: dict[str, HfidNode] = {}
        self.addresses: dict[str, HfidNode] = {}
        self.edge_adds: list[tuple[str, str, list[str]]] = []
        self.schema: Any = (
            None  # what ``client.schema`` serves the reconciler's schema check (tests/unit/schema_fakes.py)
        )

    def seed(self, kind: str, **data: Any) -> HfidNode:
        node = HfidNode(self, kind, {"id": self._next_id(kind), **data})
        self.store[node.id] = node
        return node

    def _peer_name(self, ref: Any) -> str:
        """The name a relationship value points at: a ``{"hfid": [...]}`` reference or an ``{"id": ...}`` one."""
        if isinstance(ref, dict) and "hfid" in ref:
            return ref["hfid"][0]
        node_id = ref["id"] if isinstance(ref, dict) else ref
        return self.store[node_id].data["name"]

    def hfid_of(self, kind: str, data: dict[str, Any]) -> tuple[str, ...] | None:
        fields = HFID_FIELDS.get(kind)
        if fields is None:
            return None
        return tuple(
            self._peer_name(data[field.partition("__")[0]]) if "__" in field else str(data[field]) for field in fields
        )

    def resolve(self, value: Any) -> Any:
        """A ``{"hfid", "kind"}`` reference as the id the server resolves it to; a missing target is a server error."""
        if isinstance(value, dict) and "hfid" in value and "kind" in value:
            for node in self.store.values():
                if node.kind == value["kind"] and self.hfid_of(node.kind, node.data) == tuple(value["hfid"]):
                    return {"id": node.id}
            raise LookupError(f"no {value['kind']} {value['hfid']} to reference")
        if isinstance(value, list):
            return [self.resolve(item) for item in value]
        return value

    async def create(self, kind: Any, data: dict[str, Any]) -> HfidNode:
        if kind_name(kind) == "IpamIPAddress" and "id" not in data:
            # An address of an allocated prefix (``generators/ipam/addresses.py``): stored by its text, found next run
            address = self.seed("IpamIPAddress", address=data["address"])
            address.save = AsyncMock()  # the addresses are not what these plans' saves are counted on
            self.created_ids.append(address.id)
            self.addresses[data["address"]] = address
            return address
        return HfidNode(self, kind_name(kind), data)

    def record_save(self, node: FakeNode, kwargs: dict[str, Any]) -> None:
        if not node.id:
            for attribute in MANDATORY_FIELDS.get(node.kind, ()):
                if attribute not in node.data:
                    raise GraphQLError(
                        errors=[
                            {"message": f"ATTRIBUTE_REQUIRED {attribute} is mandatory for {node.kind} at {attribute}"}
                        ]
                    )
        node.data = {key: self.resolve(value) for key, value in node.data.items()}
        for relationship in RELATIONSHIPS_OF_KIND.get(node.kind, ()):
            node.data.setdefault(relationship, [])
        if not node.id and (parts := self.hfid_of(node.kind, node.data)):
            for stored in self.store.values():
                if stored.kind == node.kind and self.hfid_of(stored.kind, stored.data) == parts:
                    node.id = stored.id
                    self.found_by_hfid.append((node.kind, parts))
                    break
        super().record_save(node, kwargs)

    async def filters(self, kind: Any, **flt: Any) -> list[FakeNode]:
        wanted = kind_name(kind)
        if wanted == "IpamIPAddress":  # the find-before-create read of a prefix's addresses
            return [self.addresses[address] for address in flt["address__values"] if address in self.addresses]
        fields = HFID_FIELDS.get(wanted, ())
        if fields and all(f"{field}__values" in flt for field in fields):
            # A read by the parts of an HFID (``plan_graph.lookup_hfids``): each part among the values asked
            return [
                node
                for node in self.store.values()
                if node.kind == wanted
                and all(
                    str(part) in {str(value) for value in flt[f"{field}__values"]}
                    for field, part in zip(fields, self.hfid_of(node.kind, node.data) or (), strict=True)
                )
            ]
        return await super().filters(kind, **flt)

    async def execute_graphql(self, query: str, variables: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
        """The RelationshipAdd of ``generators/helpers/edges.py``: the peers join the relation of the stored node (once)."""
        data = (variables or {})["data"]
        node = self.store[data["id"]]
        peers = node.data.setdefault(data["name"], [])
        peers.extend({"id": peer["id"]} for peer in data["nodes"] if {"id": peer["id"]} not in peers)
        self.edge_adds.append((data["id"], data["name"], [peer["id"] for peer in data["nodes"]]))
        return {"RelationshipAdd": {"ok": True}}

    async def allocate_next_ip_prefix(self, resource_pool: Any, identifier: str, **kwargs: Any) -> FakeNode:
        node = await super().allocate_next_ip_prefix(resource_pool, identifier, **kwargs)
        setattr(node, "display_label", f"prefix-of-{identifier}")
        # What the addresses of the prefix are cut from (``generators/ipam/addresses.py``)
        if "prefix" not in vars(node):
            setattr(node, "prefix", SimpleNamespace(value=f"10.{len(self.allocations)}.0.0/{kwargs['prefix_length']}"))
            setattr(node, "ip_namespace", SimpleNamespace(id="ns-default"))
        return node

    async def allocate_next_ip_address(self, resource_pool: Any, identifier: str, **kwargs: Any) -> HfidNode:
        key = f"{getattr(resource_pool, 'id', None)}/{identifier}"
        if key not in self.address_pool_allocations:
            node = self.seed("IpamIPAddress", identifier=identifier, **kwargs.get("data", {}))
            self.created_ids.append(node.id)
            self.address_pool_allocations[key] = node
        return self.address_pool_allocations[key]


def wire_hfid_generator(gen: Any, client: HfidClient) -> Any:
    """``gen`` on ``client`` and no producer in flight."""
    gen.client = client

    gen.wait_for_parent_generators = AsyncMock(return_value=False)
    return gen


def make_hfid_generator(client: HfidClient | None = None, *, cls: type[Any] = PlanGenerator) -> Any:
    """A generator of ``cls`` on an ``HfidClient`` (see ``wire_hfid_generator``)."""
    client = client or HfidClient()
    return wire_hfid_generator(make_generator_of(cls, client), client)
