"""The reconciler's properties for what a generator owns or shares: ``NodeSpec.create_only`` (a field sent when the object is
new), ``NodeSpec.replaces`` and ``RetireSpec`` (a generated object removed before another is written, never a hand-written
one), and ``plan_locks`` (the locks around a plan's reads and its apply).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from infrahub_sdk.exceptions import GraphQLError

from generators.model.errors import GeneratorError
from generators.plan import Hfid, NodeSpec, RetireSpec, TopologyPlan
from tests.unit.plan_graph_helpers import HfidClient, make_hfid_generator

LEAF = Hfid("DcimPhysicalDevice", ("leaf-1",))
_GENERATED = ["CoreGeneratorGroup"]
_TRACKED = {"allow_upsert": True}
_UNTRACKED = {"allow_upsert": True, "update_group_context": False}


def row(vlan: int, *, replaces: tuple[Hfid, ...] = ()) -> NodeSpec:
    """The row of VLAN ``vlan`` on leaf-1, owned by the run."""
    return NodeSpec(
        f"row-{vlan}",
        "ManagedSegmentVlan",
        {"segment_deployment": {"id": "sd-1"}, "device": LEAF, "vlan_id": vlan},
        hfid=Hfid("ManagedSegmentVlan", ("leaf-1", str(vlan))),
        ownership="owned",
        replaces=replaces,
    )


def old_row(vlan: int) -> Hfid:
    return Hfid("ManagedSegmentVlan", ("leaf-1", str(vlan)))


class _OrderedClient(HfidClient):
    """Remembers what had been deleted when each node was saved."""

    def __init__(self) -> None:
        super().__init__()
        self.deleted_at_save: list[list[str]] = []

    def record_save(self, node: Any, kwargs: dict[str, Any]) -> None:
        self.deleted_at_save.append(list(self.deleted))
        super().record_save(node, kwargs)


def world(
    *, groups: list[str] | None = None, vlan: int = 100, client: HfidClient | None = None
) -> tuple[HfidClient, str]:
    """leaf-1 and a row of ``vlan`` on it (tracked by ``groups``); returns the client and the row's id."""
    client = client or HfidClient()
    leaf = client.seed("DcimPhysicalDevice", name="leaf-1")
    old = client.seed("ManagedSegmentVlan", device={"id": leaf.id}, vlan_id=vlan, groups=groups or [])
    return client, old.id


class TestReplaces:
    @pytest.mark.asyncio
    async def test_the_generated_row_in_the_way_is_deleted_before_the_new_one_is_written(self) -> None:
        client, old_id = world(groups=_GENERATED, client=_OrderedClient())
        assert isinstance(client, _OrderedClient)
        gen = make_hfid_generator(client)

        await gen.apply_plan(TopologyPlan(None, [row(101, replaces=(old_row(100),))]))

        assert client.deleted == [old_id]
        assert client.deleted_at_save == [[old_id]]
        assert [data["vlan_id"] for kind, _, data, _ in client.saves if kind == "ManagedSegmentVlan"] == [101]

    @pytest.mark.asyncio
    async def test_a_row_no_generator_tracks_is_never_deleted_and_the_plan_writes_nothing(self) -> None:
        client, old_id = world(groups=[])
        gen = make_hfid_generator(client)

        with pytest.raises(
            GeneratorError, match=r"ManagedSegmentVlan leaf-1/100 is in the way and no generator tracks"
        ):
            await gen.apply_plan(TopologyPlan(None, [row(101, replaces=(old_row(100),))]))

        assert client.deleted == [] and old_id in client.store
        assert client.saves == []

    @pytest.mark.asyncio
    async def test_what_is_not_there_is_not_an_error(self) -> None:
        client, _ = world(groups=_GENERATED)
        gen = make_hfid_generator(client)

        await gen.apply_plan(TopologyPlan(None, [row(101, replaces=(old_row(555),))]))

        assert client.deleted == []
        assert len(client.saves) == 1

    def test_a_node_cannot_replace_itself(self) -> None:
        with pytest.raises(ValueError, match="replaces itself"):
            row(100, replaces=(old_row(100),))

    @pytest.mark.asyncio
    async def test_an_owned_node_is_tracked_and_a_second_apply_changes_nothing(self) -> None:
        client, old_id = world(groups=_GENERATED)
        gen = make_hfid_generator(client)
        plan = TopologyPlan(None, [row(101, replaces=(old_row(100),))])

        await gen.apply_plan(plan)
        before = {node_id: dict(node.data) for node_id, node in client.store.items()}
        await gen.apply_plan(plan)

        assert {node_id: dict(node.data) for node_id, node in client.store.items()} == before
        assert client.deleted == [old_id]
        assert [kwargs for kind, _, _, kwargs in client.saves if kind == "ManagedSegmentVlan"] == [_TRACKED, _TRACKED]


class TestRetire:
    @pytest.mark.asyncio
    async def test_generated_rows_are_deleted_before_the_nodes_are_written(self) -> None:
        client, old_id = world(groups=["CoreGeneratorAwareGroup"])
        gen = make_hfid_generator(client)
        plan = TopologyPlan(None, [row(101), RetireSpec((old_row(100),))])

        await gen.apply_plan(plan)

        assert client.deleted == [old_id]
        assert [data["vlan_id"] for kind, _, data, _ in client.saves if kind == "ManagedSegmentVlan"] == [101]

    @pytest.mark.asyncio
    async def test_a_row_no_generator_tracks_fails_the_run_and_stays(self) -> None:
        client, old_id = world(groups=["CoreStandardGroup"])
        gen = make_hfid_generator(client)

        with pytest.raises(GeneratorError, match="no generator tracks it"):
            await gen.apply_plan(TopologyPlan(None, [RetireSpec((old_row(100),))]))

        assert client.deleted == [] and old_id in client.store

    @pytest.mark.asyncio
    async def test_a_second_apply_finds_nothing_to_remove(self) -> None:
        client, _ = world(groups=_GENERATED)
        gen = make_hfid_generator(client)
        plan = TopologyPlan(None, [RetireSpec((old_row(100),))])

        await gen.apply_plan(plan)
        await gen.apply_plan(plan)

        assert len(client.deleted) == 1


def policy(**overrides: Any) -> NodeSpec:
    """A shared policy: found by its name, created with its contract only when it is not there."""
    return NodeSpec(
        "policy",
        "SecurityPolicy",
        {"name": "seg-a-egress", **overrides},
        hfid=Hfid("SecurityPolicy", ("seg-a-egress",)),
        ownership="shared",
        create_only={"description": "Egress", "default_action": "deny", "segment": {"id": "seg-1"}},
    )


class TestCreateOnly:
    @pytest.mark.asyncio
    async def test_a_missing_object_is_created_with_the_create_only_fields_untracked(self) -> None:
        client = HfidClient()
        gen = make_hfid_generator(client)

        result = await gen.apply_plan(TopologyPlan(None, [policy()]))

        ((kind, node_id, data, kwargs),) = client.saves
        assert kind == "SecurityPolicy" and result.nodes["policy"].id == node_id
        assert data == {
            "name": "seg-a-egress",
            "description": "Egress",
            "default_action": "deny",
            "segment": {"id": "seg-1"},
        }
        assert kwargs == _UNTRACKED

    @pytest.mark.asyncio
    async def test_an_existing_object_keeps_what_it_has_and_is_not_written(self) -> None:
        client = HfidClient()
        existing = client.seed("SecurityPolicy", name="seg-a-egress", default_action="allow")
        gen = make_hfid_generator(client)

        result = await gen.apply_plan(TopologyPlan(None, [policy()]))

        assert client.saves == []
        assert result.nodes["policy"].id == existing.id
        assert existing.data["default_action"] == "allow"

    @pytest.mark.asyncio
    async def test_an_existing_object_is_updated_by_id_with_the_data_alone(self) -> None:
        client = HfidClient()
        existing = client.seed("SecurityPolicy", name="seg-a-egress", default_action="allow")
        gen = make_hfid_generator(client)

        await gen.apply_plan(TopologyPlan(None, [policy(enabled=True)]))

        ((_, node_id, data, kwargs),) = client.saves
        assert node_id == existing.id and kwargs == _UNTRACKED
        assert data["enabled"] is True and "description" not in data and "default_action" not in data
        assert existing.data["default_action"] == "allow"

    @pytest.mark.asyncio
    async def test_a_second_apply_creates_it_once(self) -> None:
        client = HfidClient()
        gen = make_hfid_generator(client)

        await gen.apply_plan(TopologyPlan(None, [policy()]))
        await gen.apply_plan(TopologyPlan(None, [policy()]))

        assert len(client.saves) == 1 and len(client.created_ids) == 1


def password() -> NodeSpec:
    """A shared auth key whose secret is mandatory on create: a ``create_only`` field."""
    return NodeSpec(
        "key",
        "RoutingPassword",
        {"name": "dc1-underlay-key", "description": "Underlay key"},
        hfid=Hfid("RoutingPassword", ("dc1-underlay-key",)),
        ownership="owned",
        create_only={"password": "s3cret"},
    )


class TestMandatoryCreateOnly:
    """An upsert by HFID without an id is validated as a create (ATTRIBUTE_REQUIRED for a missing mandatory field), so an
    object that exists is written by its id and without the create-only fields.
    """

    @pytest.mark.asyncio
    async def test_a_missing_key_is_created_with_its_secret(self) -> None:
        client = HfidClient()
        gen = make_hfid_generator(client)

        await gen.apply_plan(TopologyPlan(None, [password()]))

        ((_, _, data, kwargs),) = client.saves
        assert data["password"] == "s3cret" and kwargs == _TRACKED

    @pytest.mark.asyncio
    async def test_an_existing_key_is_updated_by_id_and_its_secret_is_not_sent(self) -> None:
        client = HfidClient()
        existing = client.seed("RoutingPassword", name="dc1-underlay-key", password="old-secret")
        gen = make_hfid_generator(client)

        result = await gen.apply_plan(TopologyPlan(None, [password()]))

        ((_, node_id, data, kwargs),) = client.saves
        assert node_id == existing.id == result.nodes["key"].id and kwargs == _TRACKED
        assert "password" not in data and data["description"] == "Underlay key"
        assert existing.data["password"] == "old-secret"

    @pytest.mark.asyncio
    async def test_the_fake_refuses_an_hfid_upsert_that_lacks_a_mandatory_field(self) -> None:
        client = HfidClient()
        client.seed("RoutingPassword", name="dc1-underlay-key", password="old-secret")
        node = await client.create("RoutingPassword", {"name": "dc1-underlay-key"})

        with pytest.raises(GraphQLError, match="ATTRIBUTE_REQUIRED"):
            await node.save(allow_upsert=True)

    @pytest.mark.asyncio
    async def test_an_object_without_an_hfid_gets_its_create_only_fields_as_the_builder_decided(self) -> None:
        client = HfidClient()
        gen = make_hfid_generator(client)
        spec = NodeSpec(
            "as", "RoutingAutonomousSystem", {"description": "d"}, create_only={"asn": {"from_pool": {"id": "p"}}}
        )

        await gen.apply_plan(TopologyPlan(None, [spec]))

        ((_, _, data, _),) = client.saves
        assert data["asn"] == {"from_pool": {"id": "p"}}


class _Locked:
    """What ``plan_locks`` does around a body, recorded."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.gen = make_hfid_generator()

        @asynccontextmanager
        async def lock(key: str) -> AsyncGenerator[None]:
            self.events.append(f"lock:{key}")
            try:
                yield
            finally:
                self.events.append(f"unlock:{key}")

        self.gen.resource_lock = lock


class TestPlanLocks:
    @pytest.mark.asyncio
    async def test_the_keys_are_taken_sorted_and_once_and_held_around_the_body(self) -> None:
        locked = _Locked()

        async with locked.gen.plan_locks("b", "a", "b"):
            locked.events.append("body")

        assert locked.events == ["lock:a", "lock:b", "body", "unlock:b", "unlock:a"]

    @pytest.mark.asyncio
    async def test_the_reads_and_the_apply_run_under_the_locks(self) -> None:
        locked = _Locked()
        client = locked.gen.client
        original = client.filters

        async def filters(kind: Any, **flt: Any) -> Any:
            locked.events.append("read")
            return await original(kind, **flt)

        client.filters = filters
        client.seed("DcimPhysicalDevice", name="leaf-1")

        async with locked.gen.plan_locks("k"):
            await client.filters("DcimPhysicalDevice", name__values=["leaf-1"])  # the builder's read
            await locked.gen.apply_plan(TopologyPlan(None, [row(100)]))  # then the apply

        assert locked.events[0] == "lock:k" and locked.events[-1] == "unlock:k"
        assert "read" in locked.events and locked.events.count("lock:k") == 1

    @pytest.mark.asyncio
    async def test_the_locks_are_released_when_the_body_fails(self) -> None:
        locked = _Locked()

        with pytest.raises(RuntimeError):
            async with locked.gen.plan_locks("a", "b"):
                raise RuntimeError("boom")

        assert locked.events == ["lock:a", "lock:b", "unlock:b", "unlock:a"]

    @pytest.mark.asyncio
    async def test_no_key_takes_no_lock(self) -> None:
        locked = _Locked()

        async with locked.gen.plan_locks():
            pass

        assert locked.events == []
