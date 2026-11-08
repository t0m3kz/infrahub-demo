"""sync_pbr: what the VRF pass writes for the pbr policies of a device (generators/helpers/pbr_sync.py)."""

from __future__ import annotations

import contextlib
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from generators.helpers.pbr_sync import PbrMixin
from tests.unit.vrf_instances_helpers import facts_of, raw_device, raw_footprint, raw_row, raw_svi


def _raw_policy(policy_id: str = "p1", footprint: str = "fp-a", *, origin: str = "generated") -> dict[str, Any]:
    return {
        "__typename": "RoutingPolicy",
        "id": policy_id,
        "kind": {"value": "pbr"},
        "origin": {"value": origin},
        "customer_deployment": {"node": {"id": footprint}},
    }


class _Node(SimpleNamespace):
    """A RoutingPolicy node of the fake client that records the edges written on it."""


class _Writer(PbrMixin):
    def __init__(self, svis: list[tuple[str, str]], policies: list[_Node] | None = None) -> None:
        self.logger = MagicMock()
        self.client = MagicMock()
        self.created: list[dict[str, Any]] = []
        self.added: list[tuple[str, list[str]]] = []
        self.removed: list[tuple[str, list[str]]] = []
        self.policies = policies or []
        self.locks: list[str] = []
        self.footprint_names = {"fp-a": "C005-P-DC10", "fp-b": "C006-P-DC11"}
        owner = self

        @contextlib.asynccontextmanager
        async def lock(key: str) -> Any:
            owner.locks.append(key)
            yield

        self.resource_lock = lock

        def recorded(node: Any) -> Any:
            node.add_relationships = AsyncMock(
                side_effect=lambda **a: owner.added.append((node.id, a["related_nodes"]))
            )
            node.remove_relationships = AsyncMock(
                side_effect=lambda **a: owner.removed.append((node.id, a["related_nodes"]))
            )
            return node

        async def filters(kind: Any, **kw: Any) -> list[Any]:
            if kind.__name__ == "TopologyCustomer":
                return [SimpleNamespace(id=i, name=SimpleNamespace(value=owner.footprint_names[i])) for i in kw["ids"]]
            if kind.__name__ == "DcimVirtualInterface":
                return [SimpleNamespace(id=i, name=SimpleNamespace(value=n)) for i, n in svis]
            return [
                recorded(p)
                for p in owner.policies
                if p.customer_deployment.id in kw.get("customer_deployment__ids", [])
            ]

        async def get(kind: Any, **kw: Any) -> _Node:
            found = next(
                (p for p in owner.policies if p.id == kw.get("id") or p.name.value == kw.get("name__value")), None
            )
            node = found or _Node(id="new", name=SimpleNamespace(value=kw.get("name__value")))
            node.add_relationships = AsyncMock(
                side_effect=lambda **a: owner.added.append((node.id, a["related_nodes"]))
            )
            node.remove_relationships = AsyncMock(
                side_effect=lambda **a: owner.removed.append((node.id, a["related_nodes"]))
            )
            return node

        async def create(kind: Any, data: dict[str, Any]) -> Any:
            owner.created.append(data)
            return recorded(SimpleNamespace(id=f"new-{data['name']}", save=AsyncMock()))

        self.client.filters, self.client.get, self.client.create = filters, get, create


def _policy_node(policy_id: str, footprint: str, attached: tuple[str, ...] = (), origin: str = "generated") -> _Node:
    return _Node(
        id=policy_id,
        name=SimpleNamespace(value=f"pbr-{footprint}"),
        origin=SimpleNamespace(value=origin),
        customer_deployment=SimpleNamespace(id=footprint),
        capabilities=SimpleNamespace(peers=[SimpleNamespace(id=a) for a in attached]),
    )


def _facts(svi_caps: list[dict[str, Any]] | None = None) -> Any:
    rows = [raw_row("r1", 100, "PROD", footprints=[raw_footprint("fp-a")])]
    return facts_of(
        raw_device("d1", "leaf-1", rows=rows, interfaces=[raw_svi("svi-1", 100, "r1", capabilities=svi_caps)])
    )


async def test_the_first_svi_creates_the_policy_and_is_attached_under_the_footprint_lock() -> None:
    """No policy yet: created as generated pbr of the footprint, then the SVI is added."""
    writer = _Writer([("svi-1", "Vlan100")])
    await writer.sync_pbr(_facts())
    assert writer.created == [
        {"name": "pbr-fp-a", "kind": "pbr", "origin": "generated", "customer_deployment": {"id": "fp-a"}}
    ]
    assert writer.added == [("new-pbr-fp-a", ["svi-1"])] and writer.locks == ["pbr-fp-a", "pbr-fp-a"]


async def test_an_existing_svi_is_attached_to_an_existing_policy() -> None:
    """The policy exists but holds nothing of the device: the SVI is added, nothing is created."""
    writer = _Writer([("svi-1", "Vlan100")], [_policy_node("p1", "fp-a")])
    await writer.sync_pbr(_facts())
    assert writer.created == [] and writer.added == [("p1", ["svi-1"])]


async def test_everything_in_place_writes_nothing() -> None:
    """The SVI is already on the policy of its footprint."""
    writer = _Writer([("svi-1", "Vlan100")], [_policy_node("p1", "fp-a", ("svi-1",))])
    await writer.sync_pbr(_facts([_raw_policy("p1", "fp-a")]))
    assert writer.created == [] and writer.added == [] and writer.removed == []


async def test_an_svi_of_another_footprint_is_detached() -> None:
    """The SVI is on the policy of footprint fp-old, but its segment now belongs to fp-a."""
    writer = _Writer([("svi-1", "Vlan100")], [_policy_node("p-old", "fp-old", ("svi-1",)), _policy_node("p1", "fp-a")])
    await writer.sync_pbr(_facts([_raw_policy("p-old", "fp-old")]))
    assert writer.removed == [("p-old", ["svi-1"])] and writer.added == [("p1", ["svi-1"])]


async def test_a_manual_policy_keeps_its_attachment() -> None:
    """A person's policy on the SVI is never detached; the generator attaches its own beside it."""
    writer = _Writer([("svi-1", "Vlan100")], [_policy_node("p-man", "fp-x", ("svi-1",), origin="manual")])
    await writer.sync_pbr(_facts([_raw_policy("p-man", "fp-x", origin="manual")]))
    assert writer.removed == []


async def test_a_device_with_no_steered_svi_and_no_policy_reads_nothing() -> None:
    """No footprint with a context and no policy held: the pass makes no read at all."""
    writer = _Writer([("svi-1", "Vlan100")])
    writer.client.filters = AsyncMock(side_effect=AssertionError("must not read"))
    rows = [raw_row("r1", 100, "PROD")]
    await writer.sync_pbr(facts_of(raw_device("d1", "leaf-1", rows=rows, interfaces=[raw_svi("svi-1", 100, "r1")])))


async def test_a_border_leaf_attaches_every_served_footprint_to_its_l3_vni_svi() -> None:
    """The context on a service port serves two footprints: both policies are created and hold the L3 VNI SVI."""
    from tests.unit.test_pbr_facts import _firewall_context
    from tests.unit.vrf_instances_helpers import raw_interface

    port = raw_interface(
        "p1", "Ethernet1/25", "firewall", None, [_firewall_context(None, ["fp-a", "fp-b"])], "DcimPhysicalInterface"
    )
    l3vni = raw_svi("l3-1", 3900, None, name="Vlan3900")
    l3vni["role"] = {"value": "l3vni"}
    facts = facts_of(raw_device("d1", "bl-1", role="border-leaf", interfaces=[port, l3vni]))
    writer = _Writer([("l3-1", "Vlan3900")])
    await writer.sync_pbr(facts)
    assert sorted(d["customer_deployment"]["id"] for d in writer.created) == ["fp-a", "fp-b"]
    assert sorted(ids for _, ids in writer.added) == [["l3-1"], ["l3-1"]]


async def test_a_policy_created_in_this_pass_is_attached_without_reading_it_back_by_name() -> None:
    """A read by name right after the create can miss the node on the other replica: the created node is used."""
    writer = _Writer([("svi-1", "Vlan100")])
    reads: list[str] = []
    original = writer.client.get

    async def get(kind: Any, **kw: Any) -> Any:
        reads.append(str(kw))
        return await original(kind=kind, **kw)

    writer.client.get = get
    await writer.sync_pbr(_facts())
    assert writer.added == [("new-pbr-fp-a", ["svi-1"])] and reads == []


async def test_a_policy_created_by_a_border_leaf_is_named_after_its_footprint() -> None:
    """The border leaf has no row of the footprint: the name comes from the footprint, not from its id."""
    from tests.unit.test_pbr_facts import _firewall_context
    from tests.unit.vrf_instances_helpers import raw_interface

    port = raw_interface(
        "p1", "Ethernet1/25", "firewall", None, [_firewall_context(None, ["fp-a"])], "DcimPhysicalInterface"
    )
    l3vni = raw_svi("l3-1", 3900, None, name="Vlan3900")
    l3vni["role"] = {"value": "l3vni"}
    writer = _Writer([("l3-1", "Vlan3900")])
    await writer.sync_pbr(facts_of(raw_device("d1", "bl-1", role="border-leaf", interfaces=[port, l3vni])))
    assert [d["name"] for d in writer.created] == ["pbr-c005-p-dc10"]
