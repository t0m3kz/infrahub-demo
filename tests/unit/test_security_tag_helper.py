"""Unit tests for generators/helpers/security_tag.py (segment and component group tags from the profile's tag pool)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from infrahub_sdk.exceptions import GraphQLError

from generators.helpers.security_tag import clear_component_tag, ensure_security_tag, ensure_segment_tag
from generators.protocols import AppComponent, DcimGroupPolicyProfile, SecurityTag

FQDN = "cache.fraud-detection.c013.demo.local"
SEGMENT = "c005-web-frontend-local-dc10-p"


class _Lock:
    """Records the lock keys taken and whether a lookup ran while one was held."""

    def __init__(self) -> None:
        self.keys: list[str] = []
        self.held = False

    @asynccontextmanager
    async def __call__(self, key: str) -> AsyncGenerator[None]:
        self.keys.append(key)
        self.held = True
        try:
            yield
        finally:
            self.held = False


def _profile(pool_id: str | None, pool_name: str = "SGT-POOL") -> Any:
    """A DcimGroupPolicyProfile node whose tag_pool is ``pool_id`` (None: no pool)."""
    profile = MagicMock()
    profile.tag_pool.id = pool_id
    profile.tag_pool.display_label = pool_name
    return profile


def _client(
    *, existing_tags: list[Any] | None = None, profiles: list[Any] | None = None, lock: _Lock | None = None
) -> Any:
    """A client whose tag lookup returns ``existing_tags`` and whose profile lookup returns ``profiles``.

    By default the NX-OS row names SGT-POOL and the EOS row names no pool. With
    ``lock``, every tag lookup records whether it ran under the lock.
    """
    client = MagicMock()
    profile_list = [_profile("pool-sgt"), _profile(None)] if profiles is None else profiles
    client.lookups_under_lock = []

    async def _filters(*, kind: Any, **_kwargs: Any) -> list[Any]:
        if kind is DcimGroupPolicyProfile:
            return profile_list
        client.lookups_under_lock.append(lock.held if lock else None)
        return list(existing_tags or [])

    client.filters = AsyncMock(side_effect=_filters)
    created: list[tuple[Any, dict[str, Any], Any]] = []

    async def _create(*, kind: Any, data: dict[str, Any]) -> Any:
        node = MagicMock(id="tag-new" if kind is SecurityTag else data.get("id"))
        node.save = AsyncMock()
        created.append((kind, data, node))
        return node

    client.create = AsyncMock(side_effect=_create)
    client.created = created
    return client


def _component(tag_id: str | None = None) -> dict[str, Any]:
    return {"id": "comp-1", "fqdn": FQDN, "security_tag": {"id": tag_id} if tag_id else None}


def _segment(
    *,
    kind: str = "ManagedVxlanSegment",
    namespace_type: str | None = "prod",
    zone: str | None = "PROD-ZONE",
    tag_id: str | None = None,
) -> dict[str, Any]:
    """A cleaned segment as vxlan_segment.gql / application.gql select it (routed PROD tenant by default)."""
    segment: dict[str, Any] = {
        "id": "seg-1",
        "typename": kind,
        "name": SEGMENT,
        "security_zone": {"name": zone} if zone else None,
        "security_tag": {"id": tag_id} if tag_id else None,
    }
    if namespace_type:
        segment["gateway"] = {"ip_prefix": {"ip_namespace": {"name": "PROD", "namespace_type": namespace_type}}}
    return segment


def _tag_component(client: Any, lock: _Lock, component: dict[str, Any]) -> str:
    return asyncio.run(ensure_security_tag(client, lock, kind=AppComponent, node=component, name=component.get("fqdn")))


def _tag_segment(client: Any, lock: _Lock, segment: dict[str, Any]) -> str | None:
    return asyncio.run(ensure_segment_tag(client, lock, segment=segment))


class TestEnsureSecurityTag:
    """Query first, lock, create from the pool once, attach untracked by id."""

    def test_node_that_has_its_tag_writes_nothing(self) -> None:
        """Fast path: no lock, no lookup, no write."""
        client, lock = _client(), _Lock()

        assert _tag_component(client, lock, _component("tag-7")) == "tag-7"

        assert lock.keys == []
        client.filters.assert_not_awaited()
        client.create.assert_not_awaited()

    def test_new_tag_is_drawn_from_the_pool_and_attached_untracked(self) -> None:
        """The lookup runs under the tag's lock; tag and component are upserted untracked by id."""
        lock = _Lock()
        client = _client(lock=lock)
        component = _component()

        assert _tag_component(client, lock, component) == "tag-new"

        assert lock.keys == [f"sgt-{FQDN}"]
        assert client.lookups_under_lock == [True]
        (tag_kind, tag_data, tag), (comp_kind, comp_data, comp) = client.created
        assert tag_kind is SecurityTag
        assert tag_data == {"name": FQDN, "group_id": {"from_pool": {"id": "pool-sgt"}}}
        tag.save.assert_awaited_once_with(allow_upsert=True, update_group_context=False)
        assert comp_kind is AppComponent
        assert comp_data == {"id": "comp-1", "security_tag": {"id": "tag-new"}}
        comp.save.assert_awaited_once_with(allow_upsert=True, update_group_context=False)
        assert component["security_tag"] == {"id": "tag-new"}

    def test_tag_found_under_the_lock_is_reused_not_reallocated(self) -> None:
        """A run that lost the race (or a node read before its tag was attached) reuses the tag."""
        client, lock = _client(existing_tags=[MagicMock(id="tag-old")]), _Lock()

        assert _tag_component(client, lock, _component()) == "tag-old"

        assert [kind for kind, _data, _node in client.created] == [AppComponent]
        assert client.created[0][1] == {"id": "comp-1", "security_tag": {"id": "tag-old"}}
        assert all(call.kwargs["kind"] is SecurityTag for call in client.filters.await_args_list)

    def test_second_run_after_attach_writes_nothing(self) -> None:
        """The run after the first sees the tag on the node (idempotent)."""
        tag_id = _tag_component(_client(), _Lock(), _component())
        second_run, lock = _client(), _Lock()

        assert _tag_component(second_run, lock, _component(tag_id)) == tag_id
        second_run.create.assert_not_awaited()
        second_run.filters.assert_not_awaited()
        assert lock.keys == []

    def test_one_node_read_twice_in_a_run_is_written_once(self) -> None:
        """Two dicts of one node (a component's segment and an edge target's) cost one attach."""
        client, lock = _client(), _Lock()

        first, second = _component(), _component()
        assert _tag_component(client, lock, first) == _tag_component(client, lock, second) == "tag-new"

        assert [kind for kind, _data, _node in client.created] == [SecurityTag, AppComponent]
        assert second["security_tag"] == {"id": "tag-new"}

    def test_pool_is_looked_up_once_per_client(self) -> None:
        client, lock = _client(), _Lock()

        _tag_component(client, lock, _component())
        _tag_component(client, lock, {"id": "comp-2", "fqdn": "other.example", "security_tag": None})

        pool_lookups = [c for c in client.filters.await_args_list if c.kwargs["kind"] is DcimGroupPolicyProfile]
        assert len(pool_lookups) == 1

    @pytest.mark.parametrize("profiles", [[], [MagicMock(tag_pool=MagicMock(id=None))]])
    def test_no_profile_naming_a_pool_raises_lookup_error(self, profiles: list[Any]) -> None:
        """No row, or rows without a pool: nothing to draw from, fail closed."""
        with pytest.raises(LookupError, match="name 0 tag pools"):
            _tag_component(_client(profiles=profiles), _Lock(), _component())

    def test_profiles_naming_two_pools_raise_lookup_error(self) -> None:
        """Group IDs are fabric-wide: two pools would let one number mean two groups."""
        profiles = [_profile("pool-sgt"), _profile("pool-other", "OTHER-POOL")]
        with pytest.raises(LookupError, match=r"name 2 tag pools \['OTHER-POOL', 'SGT-POOL'\]"):
            _tag_component(_client(profiles=profiles), _Lock(), _component())

    @staticmethod
    def _failing_tag_saves(client: Any) -> None:
        """Every SecurityTag save raises a GraphQLError (an exhausted pool, or a uniqueness error)."""
        original_create = client.create.side_effect

        async def _failing_create(*, kind: Any, data: dict[str, Any]) -> Any:
            node = await original_create(kind=kind, data=data)
            if kind is SecurityTag:
                node.save = AsyncMock(side_effect=GraphQLError(errors=[{"message": "No more resources"}]))
            return node

        client.create.side_effect = _failing_create

    def test_allocation_failure_names_the_pool(self) -> None:
        """An exhausted pool (GraphQLError on save, still no tag) is re-raised with the pool name."""
        client, lock = _client(), _Lock()
        self._failing_tag_saves(client)

        with pytest.raises(RuntimeError, match=f"SGT-POOL: no group_id allocated for {FQDN}"):
            _tag_component(client, lock, _component())

    def test_failed_create_of_a_tag_that_now_exists_reuses_it(self) -> None:
        """Replica lag: the uniqueness error names a tag the second lookup finds; no second allocation."""
        lookups = iter([[], [MagicMock(id="tag-lagging")]])
        client, lock = _client(), _Lock()
        self._failing_tag_saves(client)
        profiles = [_profile("pool-sgt")]

        async def _filters(*, kind: Any, **_kwargs: Any) -> list[Any]:
            return profiles if kind is DcimGroupPolicyProfile else next(lookups)

        client.filters.side_effect = _filters

        assert _tag_component(client, lock, _component()) == "tag-lagging"
        assert client.created[-1][1] == {"id": "comp-1", "security_tag": {"id": "tag-lagging"}}

    @pytest.mark.parametrize("component", [{"id": "comp-1"}, {"fqdn": FQDN}])
    def test_node_without_id_or_name_is_refused(self, component: dict[str, Any]) -> None:
        with pytest.raises(ValueError, match="needs an id and a name"):
            _tag_component(_client(), _Lock(), component)


class TestEnsureSegmentTag:
    """Every routed tenant segment, and nothing else, gets the group named after it."""

    @pytest.mark.parametrize(
        ("kind", "namespace_type"), [("ManagedVxlanSegment", "prod"), ("ManagedVlanSegment", "non_prod")]
    )
    def test_routed_tenant_segment_is_tagged_with_its_name(self, kind: str, namespace_type: str) -> None:
        """VLAN and VXLAN alike, PROD and NON-PROD alike; attached on the segment's own kind."""
        client, lock = _client(), _Lock()

        assert _tag_segment(client, lock, _segment(kind=kind, namespace_type=namespace_type)) == "tag-new"

        assert lock.keys == [f"sgt-{SEGMENT}"]
        (tag_kind, tag_data, _tag), (seg_kind, seg_data, seg) = client.created
        assert (tag_kind, tag_data["name"]) == (SecurityTag, SEGMENT)
        assert (seg_kind, seg_data) == (kind, {"id": "seg-1", "security_tag": {"id": "tag-new"}})
        seg.save.assert_awaited_once_with(allow_upsert=True, update_group_context=False)

    def test_segment_that_has_its_tag_writes_nothing(self) -> None:
        """The second pass of a generator run writes nothing."""
        client, lock = _client(), _Lock()

        assert _tag_segment(client, lock, _segment(tag_id="tag-3")) == "tag-3"

        client.filters.assert_not_awaited()
        client.create.assert_not_awaited()

    @pytest.mark.parametrize(
        "segment",
        [
            _segment(namespace_type="management", zone="MANAGEMENT-ZONE"),
            _segment(namespace_type="internet", zone="INTERNET-ZONE"),
            _segment(zone="MANAGEMENT-ZONE"),
            _segment(namespace_type=None),
            _segment(kind="ManagedExternalSegment"),
            _segment(kind="CloudNetworkSegment"),
        ],
        ids=["management-vrf", "internet-vrf", "exchange-zone", "no-gateway", "external", "cloud"],
    )
    def test_any_other_segment_is_never_tagged(self, segment: dict[str, Any]) -> None:
        """MANAGEMENT/INTERNET, an exchange zone, no gateway, External or Cloud: nothing looked up or written."""
        client, lock = _client(), _Lock()

        assert _tag_segment(client, lock, segment) is None

        assert lock.keys == []
        client.filters.assert_not_awaited()
        client.create.assert_not_awaited()


class TestClearComponentTag:
    def test_tag_is_detached_on_the_fetched_component_untracked(self) -> None:
        """Clearing needs the fetched node; the tag itself is left (an orphan)."""
        client = MagicMock()
        component = MagicMock()
        component.save = AsyncMock()
        client.get = AsyncMock(return_value=component)

        asyncio.run(clear_component_tag(client, component_id="comp-1"))

        client.get.assert_awaited_once_with(kind=AppComponent, id="comp-1")
        assert component.security_tag is None
        component.save.assert_awaited_once_with(update_group_context=False)
