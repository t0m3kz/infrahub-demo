"""Unit tests for the LISP overlay control plane on the generator side.

A fabric with ``overlay_control_plane: lisp`` keeps the OSPF underlay but has no
overlay BGP: no overlay AS (``evpn_rt_as``), no overlay key, no EVPN address
family, nothing for a pod or rack to wait for. Its DC creates one shared LISP
site key instead and attaches it as ``lisp_site_key``.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from generators.protocols import RoutingAutonomousSystem
from generators.routing import RoutingMixin
from generators.topology.dc import DCTopologyGenerator
from generators.topology.pod import PodTopologyGenerator
from generators.types import RoutingOptions

# ---------------------------------------------------------------------------
# create_routing (RoutingMixin)
# ---------------------------------------------------------------------------


def _routing_mixin() -> Any:
    """A RoutingMixin with a mock client and a recording planner input."""
    m: Any = RoutingMixin.__new__(RoutingMixin)
    m.fabric_name = "dc1"
    m.deployment_id = "dc-id-1"
    m.logger = MagicMock()
    m.client = MagicMock()
    m.client.group_context.related_node_ids = []
    m.client.filters = AsyncMock(return_value=[])

    @asynccontextmanager
    async def _no_lock(key: str) -> AsyncGenerator[None]:
        yield

    m.resource_lock = _no_lock
    m.ensure_address_family = AsyncMock(return_value="evpn-af-1")
    m._resolve_shared_objects = AsyncMock(return_value=(None, "area-1"))
    m._resolve_shared_passwords = AsyncMock(return_value=(None, None))
    return m


def _capture_planner(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Replace the planner with one that records its input and plans nothing."""
    inputs: list[Any] = []

    class _Planner:
        def __init__(self, deployment_id: str, logger: Any, strict: bool = False) -> None:
            pass

        def build_routing_plan(self, plan_input: Any) -> SimpleNamespace:
            inputs.append(plan_input)
            return SimpleNamespace(
                autonomous_systems=[],
                bgp_processes=[],
                ospf_processes=[],
                ospf_interfaces=[],
                bgp_peerings=[],
                ospf_peerings=[],
            )

    monkeypatch.setattr("generators.routing.RoutingPlanner", _Planner)
    return inputs


class TestCreateRoutingUnderLisp:
    @pytest.mark.asyncio
    async def test_lisp_plans_without_overlay_prerequisites(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No overlay AS to wait for, no overlay BGP to retry on, no EVPN address family:
        the planner is called once, at once, with the lisp flag and an empty evpn_af_id."""
        sleep = AsyncMock()
        monkeypatch.setattr("generators.routing.asyncio.sleep", sleep)
        inputs = _capture_planner(monkeypatch)
        m = _routing_mixin()
        options = RoutingOptions(design={"routing_strategy": "ospf-ibgp", "overlay_control_plane": "lisp"})

        await m.create_routing(bottom_devices=["leaf-01"], top_devices=["spine-01"], options=options)

        sleep.assert_not_awaited()
        m.logger.error.assert_not_called()
        m.ensure_address_family.assert_not_awaited()
        assert len(inputs) == 1
        assert inputs[0].overlay_control_plane == "lisp"
        assert inputs[0].evpn_af_id == ""
        assert inputs[0].overlay_password_id == ""
        assert "overlay_as_id" not in options
        # the OSPF area is still resolved, and it is the only shared object looked up
        m._resolve_shared_objects.assert_awaited_once_with("ospf-ibgp")

    @pytest.mark.asyncio
    async def test_lisp_overlay_only_call_is_a_noop(self) -> None:
        """An overlay-only call (skip_underlay: super-spine/spine pre-seed, access-leaf to spine)
        has nothing left to create under lisp: it reads nothing and writes nothing."""
        m = _routing_mixin()
        options = RoutingOptions(
            design={"routing_strategy": "ospf-ibgp", "overlay_control_plane": "lisp"}, skip_underlay=True
        )

        await m.create_routing(bottom_devices=["access-01"], top_devices=["spine-01"], options=options)

        m.client.filters.assert_not_awaited()
        m.client.create.assert_not_called()
        m.ensure_address_family.assert_not_awaited()
        m._resolve_shared_objects.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_bgp_evpn_still_resolves_overlay_and_evpn_family(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The default control plane is unchanged: overlay AS resolved, EVPN family ensured."""
        monkeypatch.setattr("generators.routing.asyncio.sleep", AsyncMock())
        inputs = _capture_planner(monkeypatch)
        m = _routing_mixin()
        m._resolve_shared_objects = AsyncMock(return_value=("as-overlay-1", "area-1"))
        options = RoutingOptions(design={"routing_strategy": "ospf-ibgp", "overlay_control_plane": "bgp_evpn"})

        await m.create_routing(bottom_devices=["leaf-01"], top_devices=[], options=options)

        m.ensure_address_family.assert_awaited_once_with("l2vpn", "evpn")
        assert options["overlay_as_id"] == "as-overlay-1"
        assert inputs[0].overlay_control_plane == "bgp_evpn"
        assert inputs[0].evpn_af_id == "evpn-af-1"

    @pytest.mark.asyncio
    async def test_missing_flag_reads_as_bgp_evpn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A design dict without overlay_control_plane (older query, colocation) is the EVPN fabric."""
        monkeypatch.setattr("generators.routing.asyncio.sleep", AsyncMock())
        inputs = _capture_planner(monkeypatch)
        m = _routing_mixin()
        m._resolve_shared_objects = AsyncMock(return_value=("as-overlay-1", "area-1"))

        await m.create_routing(
            bottom_devices=["leaf-01"],
            top_devices=[],
            options=RoutingOptions(design={"routing_strategy": "ospf-ibgp"}),
        )

        assert inputs[0].overlay_control_plane == "bgp_evpn"


# ---------------------------------------------------------------------------
# _create_shared_routing_objects (DC)
# ---------------------------------------------------------------------------


class _Store:
    """A tiny find-or-create backend keyed by (kind, name), standing in for the SDK client."""

    def __init__(self) -> None:
        self.created: list[tuple[str, str]] = []
        self.nodes: dict[tuple[str, str], MagicMock] = {}

    async def get(self, kind: Any, name__value: str, raise_when_missing: bool = True) -> MagicMock | None:
        return self.nodes.get((kind.__name__, name__value))

    async def create(self, kind: Any, data: dict[str, Any]) -> MagicMock:
        name = data["name"]
        node = MagicMock(id=f"{kind.__name__}:{name}")
        node.data = data

        async def _save(**_: Any) -> None:
            self.nodes[(kind.__name__, name)] = node

        node.save = AsyncMock(side_effect=_save)
        self.created.append((kind.__name__, name))
        return node


def _dc_generator(store: _Store, data: dict[str, Any]) -> Any:
    gen: Any = DCTopologyGenerator.__new__(DCTopologyGenerator)
    gen.logger = MagicMock()
    gen.fabric_name = "dc1"
    gen.data = data
    gen.client = MagicMock()
    gen.client.group_context.related_node_ids = []
    gen.client.get = AsyncMock(side_effect=store.get)
    gen.client.create = AsyncMock(side_effect=store.create)
    gen.client.filters = AsyncMock(return_value=[])
    gen.acquire_resource_lock = AsyncMock(return_value="lock-id")
    gen.release_resource_lock = AsyncMock()
    gen.attach_refs = AsyncMock()
    return gen


_LISP_DC = {"routing_strategy": "ospf-ibgp", "overlay_control_plane": "lisp"}


class TestLispSharedRoutingObjects:
    @pytest.mark.asyncio
    async def test_lisp_dc_creates_site_key_and_no_overlay_objects(self) -> None:
        """A lisp DC gets underlay key + LISP site key + OSPF area; no overlay key, no overlay AS, no evpn_rt_as."""
        store = _Store()
        gen = _dc_generator(store, dict(_LISP_DC))

        await gen._create_shared_routing_objects(overlay_asn=65100, deployment_id="dc-1")

        assert store.created == [
            ("RoutingPassword", "dc1-underlay-key"),
            ("RoutingPassword", "dc1-lisp-site-key"),
            ("RoutingOSPFArea", "dc1-ospf-area-0"),
        ]
        assert all(call.kwargs["kind"] is not RoutingAutonomousSystem for call in gen.client.create.await_args_list)
        gen.attach_refs.assert_awaited_once_with(
            kind="TopologySegmentHosting", node_id="dc-1", refs={"lisp_site_key": "RoutingPassword:dc1-lisp-site-key"}
        )
        site_key = store.nodes[("RoutingPassword", "dc1-lisp-site-key")]
        assert site_key.data["password"]
        assert site_key.id in gen.client.group_context.related_node_ids

    @pytest.mark.asyncio
    async def test_site_key_find_or_create_is_idempotent_on_rerun(self) -> None:
        """A second run finds the first run's key by name: no second RoutingPassword, same id attached,
        and it is tracked again so the run's cleanup keeps it."""
        store = _Store()
        first = _dc_generator(store, dict(_LISP_DC))
        await first._create_shared_routing_objects(overlay_asn=65100, deployment_id="dc-1")
        created_after_first = list(store.created)
        secret = store.nodes[("RoutingPassword", "dc1-lisp-site-key")].data["password"]

        second = _dc_generator(store, dict(_LISP_DC))
        await second._create_shared_routing_objects(overlay_asn=65100, deployment_id="dc-1")

        assert store.created == created_after_first
        assert store.nodes[("RoutingPassword", "dc1-lisp-site-key")].data["password"] == secret
        site_key_id = "RoutingPassword:dc1-lisp-site-key"
        second.attach_refs.assert_awaited_once_with(
            kind="TopologySegmentHosting", node_id="dc-1", refs={"lisp_site_key": site_key_id}
        )
        assert site_key_id in second.client.group_context.related_node_ids

    @pytest.mark.asyncio
    async def test_site_key_creation_is_serialized_per_name(self) -> None:
        """The find-or-create runs inside the per-name password lock the other shared keys use."""
        store = _Store()
        gen = _dc_generator(store, dict(_LISP_DC))

        await gen._create_shared_routing_objects(overlay_asn=65100, deployment_id="dc-1")

        locked = [call.args[0] for call in gen.acquire_resource_lock.await_args_list]
        assert "routing-password-dc1-lisp-site-key" in locked

    @pytest.mark.asyncio
    async def test_bgp_evpn_dc_is_unchanged(self) -> None:
        """Default control plane: overlay key and overlay AS (as evpn_rt_as), no LISP site key."""
        store = _Store()
        gen = _dc_generator(store, {"routing_strategy": "ospf-ibgp"})
        fabric_as = MagicMock(id="as-1")
        fabric_as.asn.value = 65100
        fabric_as.save = AsyncMock()

        async def _create(kind: Any, data: dict[str, Any]) -> MagicMock:
            return fabric_as if kind is RoutingAutonomousSystem else await store.create(kind, data)

        gen.client.create = AsyncMock(side_effect=_create)

        await gen._create_shared_routing_objects(overlay_asn=65100, deployment_id="dc-1")

        assert ("RoutingPassword", "dc1-overlay-key") in store.created
        assert ("RoutingPassword", "dc1-lisp-site-key") not in store.created
        gen.attach_refs.assert_awaited_once_with(
            kind="TopologySegmentHosting", node_id="dc-1", refs={"evpn_rt_as": "as-1"}
        )

    @pytest.mark.asyncio
    async def test_lisp_dc_without_ospf_ibgp_still_has_no_overlay_as(self) -> None:
        """The flag, not the strategy, decides: ebgp strategies never create an overlay AS on lisp either."""
        store = _Store()
        gen = _dc_generator(store, {"routing_strategy": "ebgp-ebgp", "overlay_control_plane": "lisp"})

        await gen._create_shared_routing_objects(overlay_asn=65100, deployment_id="dc-1")

        assert not any(kind == "RoutingOSPFArea" for kind, _ in store.created)
        assert all(call.kwargs["kind"] is not RoutingAutonomousSystem for call in gen.client.create.await_args_list)


# ---------------------------------------------------------------------------
# Pod: super-spine overlay wait
# ---------------------------------------------------------------------------


class _StopHere(Exception):
    """Raised by the first write after the super-spine overlay gate, to end generate() there."""


def _pod_payload(overlay_control_plane: str | None) -> dict[str, Any]:
    parent: dict[str, Any] = {
        "id": "dc-1",
        "name": "DC1",
        "index": 1,
        "size": "S",
        "routing_strategy": "ospf-ibgp",
        "fabric_asn_pool": None,
        "fabric_templates": [{"role": "super-spine", "quantity": 2, "template": {"id": "t", "interfaces": []}}],
        "devices": [{"name": "ss-dc1-01"}, {"name": "ss-dc1-02"}],
    }
    if overlay_control_plane:
        parent["overlay_control_plane"] = overlay_control_plane
    return {
        "TopologyPod": [
            {
                "id": "pod-1",
                "name": "POD1",
                "index": 1,
                "deployment_type": "middle_rack",
                "layout": "S_MIDDLE",
                "fabric_templates": [
                    {
                        "role": "spine",
                        "quantity": 2,
                        "template": {"id": "tmpl-spine", "interfaces": [{"name": "Ethernet1/31", "role": "uplink"}]},
                    }
                ],
                "parent": parent,
            }
        ]
    }


def _pod_generator() -> Any:
    gen: Any = PodTopologyGenerator.__new__(PodTopologyGenerator)
    gen.logger = MagicMock()
    gen.client = MagicMock()
    gen.client.group_context.related_node_ids = []
    gen.wait_for_parent_generator_and_refetch = AsyncMock(return_value=None)
    gen.bgp_processes_ready = AsyncMock(return_value=False)
    gen.allocate_resource_pools = AsyncMock(side_effect=_StopHere)
    return gen


class TestPodSuperSpineOverlayWait:
    @pytest.mark.asyncio
    async def test_lisp_pod_does_not_wait_for_super_spine_overlay(self) -> None:
        """Super-spines carry no overlay BGP under lisp, so the pod goes straight on to its pools."""
        gen = _pod_generator()

        with pytest.raises(_StopHere):
            await gen.generate(_pod_payload("lisp"))

        gen.bgp_processes_ready.assert_not_awaited()
        gen.allocate_resource_pools.assert_awaited_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("flag", ["bgp_evpn", None])
    async def test_bgp_evpn_pod_still_waits_for_super_spine_overlay(
        self, flag: str | None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Explicit bgp_evpn or a missing flag: the unchanged wait, and the pod stops before any write."""
        monkeypatch.setattr("generators.topology.pod.asyncio.sleep", AsyncMock())
        gen = _pod_generator()

        await gen.generate(_pod_payload(flag))

        gen.bgp_processes_ready.assert_awaited_with(["ss-dc1-01", "ss-dc1-02"], "overlay")
        gen.allocate_resource_pools.assert_not_awaited()
