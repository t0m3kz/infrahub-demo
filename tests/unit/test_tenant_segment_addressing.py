"""Unit tests for the derived tenant segment addressing.

VxlanSegmentGenerator.ensure_segment_addressing (generators/topology/segment.py)
gives a gateway-less segment a prefix from the tenant pool of its
environment's VRF namespace (data/bootstrap/23b_tenant_pools.yml), keyed by
the segment name, and its anycast gateway on .1; an authored gateway is never
touched. The app generator waits for an in-flight add_vxlan_segment run of a
gateway-less segment it uses (AppApplicationGenerator._await_segment_addressing).
"""

from __future__ import annotations

import asyncio
import ipaddress
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from infrahub_sdk.protocols import CoreIPPrefixPool, IpamNamespace

from generators.logger import GeneratorError
from generators.protocols import IpamPrefix
from generators.topology.application_security import AppApplicationGenerator
from generators.topology.segment import VxlanSegmentGenerator, tenant_pool_name
from tests.integration.demo_data import ROOT, declared, name
from utils.exchange_transit import OFFSET_ANYCAST, namespace_type_for_environment

_NAMESPACES = {"prod": ("ns-prod", "PROD"), "non_prod": ("ns-nonprod", "NON-PROD")}
_PREFIXES = {"PROD": "10.176.0.0/24", "NON-PROD": "10.184.0.0/24"}


def _namespace(namespace_type: str) -> MagicMock:
    """An IpamNamespace node of ``namespace_type``."""
    namespace_id, namespace_name = _NAMESPACES[namespace_type]
    node = MagicMock(id=namespace_id)
    node.name.value = namespace_name
    return node


def _prefix(namespace_name: str) -> MagicMock:
    """The /24 the tenant pool of ``namespace_name`` hands out."""
    node = MagicMock(id=f"prefix-{namespace_name}")
    node.prefix.value = _PREFIXES[namespace_name]
    return node


class _Client:
    """The SDK calls ensure_segment_addressing makes, recorded."""

    def __init__(self, *, pool_exists: bool = True, prefix_returned: bool = True, namespaces: int = 1) -> None:
        self.pool_exists = pool_exists
        self.prefix_returned = prefix_returned
        self.namespaces = namespaces
        self.filter_calls: list[tuple[Any, dict[str, Any]]] = []
        self.allocations: list[dict[str, Any]] = []
        self.created: list[tuple[str, dict[str, Any]]] = []
        self.saves: list[dict[str, Any]] = []

    async def filters(self, *, kind: Any, **kwargs: Any) -> list[Any]:
        """IpamNamespace by namespace_type, CoreIPPrefixPool by name."""
        self.filter_calls.append((kind, kwargs))
        if kind is IpamNamespace:
            return [_namespace(kwargs["namespace_type__value"]) for _ in range(self.namespaces)]
        if kind is CoreIPPrefixPool:
            if not self.pool_exists:
                return []
            pool = MagicMock(id=f"pool-{kwargs['name__value']}")
            pool.name.value = kwargs["name__value"]
            return [pool]
        raise AssertionError(f"unexpected filters({kind})")

    async def allocate_next_ip_prefix(self, **kwargs: Any) -> Any:
        """The pool's next prefix for the identifier (None: exhausted)."""
        self.allocations.append(kwargs)
        if not self.prefix_returned:
            return None
        return _prefix(kwargs["resource_pool"].name.value.removeprefix("Tenant-").removesuffix("-IPv4"))

    async def create(self, *, kind: str, data: dict[str, Any]) -> Any:
        """A node whose save records its kwargs."""
        self.created.append((kind, data))
        node = MagicMock()
        node.save = AsyncMock(side_effect=lambda **kwargs: self.saves.append(kwargs))
        return node


def _make_gen(client: _Client) -> Any:
    """A VxlanSegmentGenerator with the recording client, a raising logger (as FailOnErrorLogger) and a lock."""
    gen: Any = VxlanSegmentGenerator.__new__(VxlanSegmentGenerator)
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

    async def upsert(prefix: Any, *, offsets: tuple[int, ...], description: str, track: bool) -> dict[int, Any]:
        gen.upserts.append({"offsets": offsets, "description": description, "track": track})
        network = ipaddress.ip_network(prefix.prefix.value)
        address = MagicMock(id="gw-1")
        address.address.value = f"{network[offsets[0]]}/{network.prefixlen}"
        return {offsets[0]: address}

    gen.upserts = []
    gen.upsert_prefix_addresses = upsert
    return gen


def _segment(environment: str | None = "p", gateway: dict[str, Any] | None = None) -> dict[str, Any]:
    """A cleaned vxlan_segment query result."""
    return {
        "id": "seg-1",
        "typename": "ManagedVxlanSegment",
        "name": "c008-krafven-dev-d" if environment == "d" else "c008-trade-portal-local-dc10-p",
        "environment": environment,
        "gateway": gateway,
    }


class TestEnsureSegmentAddressing:
    """A gateway-less segment gets one prefix and its .1 gateway from its environment's tenant pool."""

    def test_prod_segment_is_addressed_from_the_prod_pool(self) -> None:
        """environment p -> the namespace of type prod -> Tenant-PROD-IPv4, identifier = the segment name."""
        client = _Client()
        gen = _make_gen(client)
        segment = _segment("p")

        asyncio.run(gen.ensure_segment_addressing(segment))

        assert client.filter_calls == [
            (IpamNamespace, {"namespace_type__value": "prod"}),
            (CoreIPPrefixPool, {"name__value": "Tenant-PROD-IPv4"}),
        ]
        (allocation,) = client.allocations
        assert allocation["identifier"] == "c008-trade-portal-local-dc10-p"
        assert allocation["kind"] is IpamPrefix
        assert allocation["member_type"] == "address"
        assert allocation["data"] == {"role": "customer", "status": "active", "description": segment["name"]}
        # The prefix length is the pool's default (data), not a Python constant.
        assert "prefix_length" not in allocation
        assert gen.upserts == [
            {"offsets": (OFFSET_ANYCAST,), "description": f"{segment['name']} anycast gateway", "track": False}
        ]

    def test_non_prod_segment_is_addressed_from_the_non_prod_pool(self) -> None:
        """Every environment but p is non-prod: Tenant-NON-PROD-IPv4."""
        client = _Client()
        gen = _make_gen(client)

        asyncio.run(gen.ensure_segment_addressing(_segment("d")))

        assert client.filter_calls[0] == (IpamNamespace, {"namespace_type__value": "non_prod"})
        assert client.filter_calls[1] == (CoreIPPrefixPool, {"name__value": "Tenant-NON-PROD-IPv4"})

    def test_gateway_is_written_untracked_on_the_segment_and_into_the_run_dict(self) -> None:
        """The segment (the run's own target) is saved with update_group_context=False; the zone and tag
        helpers of the same run read the gateway's namespace type from the dict."""
        client = _Client()
        gen = _make_gen(client)
        segment = _segment("p")

        asyncio.run(gen.ensure_segment_addressing(segment))

        assert client.created == [("ManagedVxlanSegment", {"id": "seg-1", "gateway": {"id": "gw-1"}})]
        assert client.saves == [{"allow_upsert": True, "update_group_context": False}]
        assert segment["gateway"] == {
            "id": "gw-1",
            "address": "10.176.0.1/24",
            "ip_prefix": {
                "id": "prefix-PROD",
                "prefix": "10.176.0.0/24",
                "ip_namespace": {"id": "ns-prod", "name": "PROD", "namespace_type": "prod"},
            },
        }
        assert gen.locks == ["segment-addressing-seg-1"]

    def test_authored_gateway_is_never_overwritten(self) -> None:
        """A segment with a gateway (hand-set or allocated earlier) is left alone: nothing is read or written."""
        client = _Client()
        gen = _make_gen(client)
        authored = {"id": "gw-hand", "ip_prefix": {"prefix": "10.250.0.0/24"}}
        segment = _segment("p", gateway=authored)

        asyncio.run(gen.ensure_segment_addressing(segment))

        assert client.filter_calls == client.allocations == client.created == []
        assert segment["gateway"] is authored

    def test_second_pass_allocates_nothing_new(self) -> None:
        """Idempotency: the second run reads the gateway the first one wrote and skips."""
        client = _Client()
        gen = _make_gen(client)
        segment = _segment("p")

        asyncio.run(gen.ensure_segment_addressing(segment))
        asyncio.run(gen.ensure_segment_addressing(segment))

        assert len(client.allocations) == 1
        assert len(client.created) == 1

    def test_retried_allocation_uses_the_same_identifier(self) -> None:
        """A run that failed after the allocation retries with the same identifier, so the pool returns the
        same prefix: the identifier is the segment name (its HFID), never a run-specific value."""
        first, second = _Client(), _Client()
        asyncio.run(_make_gen(first).ensure_segment_addressing(_segment("p")))
        asyncio.run(_make_gen(second).ensure_segment_addressing(_segment("p")))

        assert first.allocations[0]["identifier"] == second.allocations[0]["identifier"]

    @pytest.mark.parametrize(
        ("client", "message"),
        [
            (_Client(pool_exists=False), "tenant pool Tenant-PROD-IPv4 not found"),
            (_Client(prefix_returned=False), "returned no prefix"),
            (_Client(namespaces=0), "expected one IpamNamespace of type prod, found 0"),
            (_Client(namespaces=2), "expected one IpamNamespace of type prod, found 2"),
        ],
    )
    def test_missing_or_exhausted_pool_fails_the_run(self, client: _Client, message: str) -> None:
        """No pool, no namespace or an empty pool is an error, and no gateway is written."""
        gen = _make_gen(client)
        segment = _segment("p")

        with pytest.raises(GeneratorError, match=message):
            asyncio.run(gen.ensure_segment_addressing(segment))

        assert client.created == []
        assert segment["gateway"] is None

    def test_missing_environment_is_not_defaulted_to_production(self) -> None:
        """No environment, no pool: the PROD namespace is never guessed."""
        client = _Client()
        gen = _make_gen(client)

        with pytest.raises(GeneratorError, match="no environment"):
            asyncio.run(gen.ensure_segment_addressing(_segment(None)))

        assert client.filter_calls == []


class TestGenerateOrder:
    """The addressing runs right after the id/name guard, before the zone and the tag."""

    def test_tag_helper_sees_the_new_gateway(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """is_routed_tenant_segment reads gateway.ip_prefix.ip_namespace: written later, the first run
        would leave the segment untagged."""
        order: list[str] = []
        seen: list[Any] = []

        async def zone(client: Any, **kwargs: Any) -> str:
            order.append("zone")
            return "PROD-ZONE"

        async def tag(client: Any, lock: Any, *, segment: dict[str, Any]) -> None:
            order.append("tag")
            seen.append(segment.get("gateway"))

        monkeypatch.setattr("generators.topology.segment.ensure_security_zone", zone)
        monkeypatch.setattr("generators.topology.segment.ensure_segment_tag", tag)
        gen: Any = VxlanSegmentGenerator.__new__(VxlanSegmentGenerator)
        gen.client = AsyncMock()
        gen.logger = MagicMock()

        async def address(segment: dict[str, Any]) -> None:
            order.append("address")
            segment["gateway"] = {"id": "gw-1"}

        gen.ensure_segment_addressing = address
        gen._resolve_target_deployments = MagicMock(return_value=[])
        data = {
            "ManagedVxlanSegment": {
                "edges": [
                    {
                        "node": {
                            "id": "seg-1",
                            "__typename": "ManagedVxlanSegment",
                            "name": {"value": "c008-krafven-dev-d"},
                            "environment": {"value": "d"},
                            "gateway": {"node": None},
                        }
                    }
                ]
            }
        }

        asyncio.run(gen.generate(data))

        assert order == ["address", "zone", "tag"]
        assert seen == [{"id": "gw-1"}]


class TestTenantPoolData:
    """The bootstrap pools the helper looks up by name exist, in the namespace they serve."""

    @staticmethod
    def _pools() -> dict[str, dict[str, Any]]:
        """Bootstrap CoreIPPrefixPool name -> declaration."""
        return {pool.item["name"]: pool.item for pool in declared("CoreIPPrefixPool", ROOT / "data" / "bootstrap")}

    @pytest.mark.parametrize("environment", ["p", "d", "t"])
    def test_every_environment_has_a_pool_in_its_own_namespace(self, environment: str) -> None:
        """The pool of each environment's namespace allocates inside that namespace, a /24 by default."""
        namespaces = {
            namespace.item["namespace_type"]: namespace.item["name"]
            for namespace in declared("IpamNamespace", ROOT / "data" / "bootstrap")
        }
        namespace = namespaces[namespace_type_for_environment(environment)]
        pool = self._pools()[tenant_pool_name(namespace)]

        assert pool["ip_namespace"] == namespace
        assert pool["default_prefix_length"] == 24
        (resource,) = pool["resources"]["data"]
        assert resource["ip_namespace"] == namespace
        assert resource["is_pool"] is True

    def test_tenant_supernets_are_disjoint(self) -> None:
        """PROD and NON-PROD tenant space never overlap, so a tenant prefix is unique across both VRFs."""
        supernets = [
            ipaddress.ip_network(self._pools()[tenant_pool_name(namespace)]["resources"]["data"][0]["prefix"])
            for namespace in ("PROD", "NON-PROD")
        ]
        assert [str(net) for net in supernets] == ["10.176.0.0/13", "10.184.0.0/13"]
        assert not supernets[0].overlaps(supernets[1])

    def test_no_demo_prefix_sits_in_the_tenant_space(self) -> None:
        """A hand-typed prefix inside the tenant supernets would collide with an allocation."""
        tenant = [ipaddress.ip_network("10.176.0.0/13"), ipaddress.ip_network("10.184.0.0/13")]
        offenders = [
            f"{obj.path.relative_to(ROOT)}: {obj.item['prefix']}"
            for obj in declared("IpamPrefix", ROOT / "data" / "demos" / "30_all")
            if any(ipaddress.ip_network(obj.item["prefix"], strict=False).overlaps(net) for net in tenant)
        ]
        assert not offenders


class TestAppWaitsForSegmentAddressing:
    """add_app_application waits for an in-flight add_vxlan_segment run of a gateway-less segment it uses."""

    @staticmethod
    def _gen(refetched: dict[str, Any] | None) -> Any:
        """An AppApplicationGenerator whose wait helper returns ``refetched`` (raw query data)."""
        gen: Any = AppApplicationGenerator.__new__(AppApplicationGenerator)
        gen.client = AsyncMock()
        gen.logger = MagicMock()
        gen.wait_for_parent_generator_and_refetch = AsyncMock(return_value=refetched)
        return gen

    @staticmethod
    def _app(*segments: dict[str, Any], label: str = "first-read") -> dict[str, Any]:
        """A cleaned application with one component per segment."""
        return {
            "id": "app-1",
            "name": label,
            "children": [{"id": f"comp-{i}", "network_segment": segment} for i, segment in enumerate(segments)],
        }

    def test_waits_only_for_gateway_less_vxlan_segments(self) -> None:
        """A VLAN segment (no generator) and an addressed VXLAN segment are not waited for."""
        gen = self._gen(None)
        app = self._app(
            {"id": "seg-new", "typename": "ManagedVxlanSegment", "gateway": None},
            {"id": "seg-done", "typename": "ManagedVxlanSegment", "gateway": {"id": "gw"}},
            {"id": "seg-vlan", "typename": "ManagedVlanSegment", "gateway": None},
            {"id": "seg-new", "typename": "ManagedVxlanSegment", "gateway": None},
        )

        result = asyncio.run(gen._await_segment_addressing(app))

        gen.wait_for_parent_generator_and_refetch.assert_awaited_once_with(("add_vxlan_segment",), "seg-new")
        assert result is app

    def test_edge_target_segments_are_waited_for(self) -> None:
        """The target of a dependency is tagged in this run too, so its segment counts."""
        gen = self._gen(None)
        target = {"id": "comp-t", "network_segment": {"id": "seg-t", "typename": "ManagedVxlanSegment"}}
        app = self._app({"id": "seg-own", "typename": "ManagedVxlanSegment", "gateway": {"id": "gw"}})
        app["children"][0]["depends_on"] = [{"id": "dep-1", "target": target}]

        asyncio.run(gen._await_segment_addressing(app))

        gen.wait_for_parent_generator_and_refetch.assert_awaited_once_with(("add_vxlan_segment",), "seg-t")

    def test_the_application_is_re_read_after_an_in_flight_run(self) -> None:
        """The refetched query result replaces the stale application dict."""
        refetched = {"AppApplication": {"edges": [{"node": {"id": "app-1", "name": {"value": "re-read"}}}]}}
        gen = self._gen(refetched)

        result = asyncio.run(
            gen._await_segment_addressing(self._app({"id": "seg-new", "typename": "ManagedVxlanSegment"}))
        )

        assert result["name"] == "re-read"

    def test_generate_reconciles_the_re_read_application(self) -> None:
        """generate() hands _reconcile_application_rules the application after the wait."""
        gen = self._gen(None)
        gen._await_segment_addressing = AsyncMock(return_value={"id": "app-1", "name": "re-read"})
        gen._reconcile_application_rules = AsyncMock()

        asyncio.run(gen.generate({"AppApplication": {"edges": [{"node": {"id": "app-1"}}]}}))

        gen._reconcile_application_rules.assert_awaited_once_with({"id": "app-1", "name": "re-read"})


def test_all_demo_vxlan_tenant_segments_declare_no_addressing() -> None:
    """Every 30_all VXLAN segment's gateway and prefix are derived: none is typed."""
    segments = declared("ManagedVxlanSegment", ROOT / "data" / "demos" / "30_all")
    assert segments, "the scan found no VXLAN segment"
    typed = [f"{obj.path.relative_to(ROOT)}: {name(obj)}" for obj in segments if "gateway" in obj.item]
    assert not typed, "VXLAN segments with a hand-typed gateway:\n" + "\n".join(typed)
