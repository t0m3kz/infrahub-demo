"""Unit tests for DCTopologyGenerator.generate() and _create_shared_routing_objects().

Covers the branches _ensure_routing_password tests (test_dc_shared_routing_password.py)
don't reach: missing/invalid GraphQL data, super-spine count validation,
back-to-back vs super-spine design_mode, per-strategy pool/routing dispatch, and the
overlay-AS/OSPF-area creation paths in _create_shared_routing_objects().
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from generators.dc_config import DC_SIZE_LAYOUTS
from generators.helpers.routing import RoutingStrategy
from generators.protocols import RoutingAutonomousSystem
from generators.topology.dc import DCTopologyGenerator


def _design(
    *,
    max_super_spines_per_fabric: int = 2,
    max_hyper_spines_per_fabric: int = 0,
    max_border_leafs_per_fabric: int = 4,
    max_pods: int = 2,
    max_spines_per_pod: int = 4,
) -> str:
    """Register a throwaway DC_SIZE_LAYOUTS entry for this test scenario and
    return its key (a DCModel.size value). TopologyDataCenterDesign no longer
    exists — DCModel.design is now a property resolving DCModel.size through
    DC_SIZE_LAYOUTS/DCSizeLayout.from_name(); routing_strategy/underlay_protocol
    live directly on the DC instance (see _deployment()), not here.

    Each call gets a unique key so distinct tests' custom capacity numbers
    never collide with each other or with the real S/M/L/XL entries."""
    key = f"TEST_{uuid.uuid4().hex}"
    DC_SIZE_LAYOUTS[key] = {
        "max_pods": max_pods,
        "max_super_spines_per_fabric": max_super_spines_per_fabric,
        "max_hyper_spines_per_fabric": max_hyper_spines_per_fabric,
        "max_border_leafs_per_fabric": max_border_leafs_per_fabric,
        "max_spines_per_pod": max_spines_per_pod,
        "loopback_prefix_length": 23,
        "technical_prefix_length": 19,
        "management_prefix_length": 25,
        "dc_fabric_loopback_host_bits": 4,
    }
    return key


def _fabric_templates(
    *,
    amount_of_super_spines: int = 0,
    super_spine_template: dict[str, Any] | None = None,
    amount_of_hyper_spines: int = 0,
    hyper_spine_template: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Build a fabric_templates list with super-spine/hyper-spine entries, mirroring
    the pre-refactor amount_of_super_spines/super_spine_template scalar kwargs."""
    entries: list[dict[str, Any]] = []
    if amount_of_super_spines > 0 and super_spine_template is not None:
        entries.append(
            {
                "role": "super-spine",
                "quantity": amount_of_super_spines,
                "template": super_spine_template,
            }
        )
    if amount_of_hyper_spines > 0 and hyper_spine_template is not None:
        entries.append(
            {
                "role": "hyper-spine",
                "quantity": amount_of_hyper_spines,
                "template": hyper_spine_template,
            }
        )
    return entries


def _deployment(
    *,
    design: str | None = None,
    routing_strategy: str = "ebgp-ebgp",
    underlay_protocol: str = "ipv6",
    amount_of_super_spines: int = 0,
    super_spine_template: dict[str, Any] | None = None,
    amount_of_hyper_spines: int = 0,
    hyper_spine_template: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """`design` is now a DC_SIZE_LAYOUTS key (see _design()) — kept as the
    kwarg name so call sites read the same as before the refactor."""
    return {
        "TopologyDeployment": [
            {
                "id": "dc-1",
                "name": "DC1",
                "index": 1,
                "size": design if design is not None else "S",
                "routing_strategy": routing_strategy,
                "underlay_protocol": underlay_protocol,
                "naming_convention": "standard",
                "fabric_interface_sorting_method": "bottom_up",
                "spine_interface_sorting_method": "bottom_up",
                "fabric_templates": _fabric_templates(
                    amount_of_super_spines=amount_of_super_spines,
                    super_spine_template=super_spine_template,
                    amount_of_hyper_spines=amount_of_hyper_spines,
                    hyper_spine_template=hyper_spine_template,
                ),
                "loopback_pool": None,
                "technical_pool": None,
                "management_pool": None,
                "fabric_asn_pool": None,
                "children": [],
            }
        ]
    }


def _make_generator() -> Any:
    gen = DCTopologyGenerator.__new__(DCTopologyGenerator)
    gen.logger = MagicMock()
    gen.client = MagicMock()
    gen.client.group_context = MagicMock()
    gen.client.group_context.related_node_ids = []
    gen.client.filters = AsyncMock(return_value=[])
    gen.client.get = AsyncMock(return_value=None)

    # Stub out every CommonGenerator/RoutingMixin collaborator generate() calls —
    # these are exercised by their own dedicated test modules, not here.
    # acquire_resource_lock/release_resource_lock serialize the super-spine<->
    # hyper-spine cabling call against a concurrent, overlapping generator run
    # for the same DC — not under test here, and the lock's own client.create()
    # call has no CoreStandardGroup-shaped mock on this plain MagicMock client.
    gen.acquire_resource_lock = AsyncMock(return_value="lock-id")
    gen.release_resource_lock = AsyncMock()
    gen.allocate_resource_pools = AsyncMock(return_value={})
    gen.upsert_asn_pool = AsyncMock(return_value=MagicMock(id="asn-pool-1"))
    gen.upsert_number_pool = AsyncMock(return_value=MagicMock(id="num-pool-1"))
    gen.create_devices = AsyncMock(return_value=[])
    gen.create_routing = AsyncMock()
    gen.create_cabling = AsyncMock(return_value=[])
    gen._create_shared_routing_objects = AsyncMock()
    return gen


class TestGenerateGuardClauses:
    @pytest.mark.asyncio
    async def test_no_deployment_data_logs_error_and_returns(self) -> None:
        gen = _make_generator()

        await gen.generate({"TopologyDeployment": []})

        gen.logger.error.assert_called_once()
        gen.allocate_resource_pools.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_data_key_logs_error_and_returns(self) -> None:
        gen = _make_generator()

        await gen.generate({})

        gen.logger.error.assert_called_once()
        gen.allocate_resource_pools.assert_not_called()

    @pytest.mark.asyncio
    async def test_invalid_model_data_logs_error_and_returns(self) -> None:
        gen = _make_generator()
        # Missing required "index" field -> DCModel(**data) raises ValueError/KeyError
        bad_data = {"TopologyDeployment": [{"id": "dc-1", "name": "DC1"}]}

        await gen.generate(bad_data)

        gen.logger.error.assert_called_once()
        gen.allocate_resource_pools.assert_not_called()

    @pytest.mark.asyncio
    async def test_super_spine_count_exceeding_design_max_raises(self) -> None:
        gen = _make_generator()
        data = _deployment(
            design=_design(max_super_spines_per_fabric=2),
            amount_of_super_spines=4,
            super_spine_template={"id": "tmpl-ss", "interfaces": []},
        )

        with pytest.raises(RuntimeError, match="requests 4 super-spines"):
            await gen.generate(data)

        gen.allocate_resource_pools.assert_not_called()


class TestGenerateExistingPodsTracking:
    @pytest.mark.asyncio
    async def test_existing_pods_are_read_but_never_tracked(self) -> None:
        """Pods are data, not add_dc's output: they are kept for border-leaf
        placement and the cascade, but never claimed, so a pod moved to
        another DC is not deleted by this DC's next run."""
        gen = _make_generator()
        pod_a = MagicMock(id="pod-a")
        pod_a.index.value = 1
        pod_b = MagicMock(id="pod-b")
        pod_b.index.value = 2
        gen.client.filters = AsyncMock(return_value=[pod_a, pod_b])

        await gen.generate(_deployment(design=_design()))

        assert gen._existing_pods == [pod_a, pod_b]
        assert gen.client.group_context.related_node_ids == []


class TestGenerateDesignModeDispatch:
    @pytest.mark.asyncio
    async def test_back_to_back_mode_skips_super_spine_creation(self) -> None:
        """max_super_spines_per_fabric == 0 on the design -> back-to-back mode,
        no super-spine devices created regardless of amount_of_super_spines."""
        gen = _make_generator()
        data = _deployment(
            design=_design(max_super_spines_per_fabric=0),
            amount_of_super_spines=0,
        )

        await gen.generate(data)

        gen.create_devices.assert_not_called()
        gen.create_routing.assert_not_called()

    @pytest.mark.asyncio
    async def test_super_spine_mode_creates_devices_and_routing(self) -> None:
        gen = _make_generator()
        gen.create_devices = AsyncMock(return_value=["dc1-ss-01", "dc1-ss-02"])
        data = _deployment(
            design=_design(max_super_spines_per_fabric=2),
            amount_of_super_spines=2,
            super_spine_template={"id": "tmpl-ss", "interfaces": []},
        )

        await gen.generate(data)

        gen.create_devices.assert_awaited_once()
        call_kwargs = gen.create_devices.call_args.kwargs
        assert call_kwargs["device_role"] == "super-spine"
        assert call_kwargs["quantity"] == 2

        gen.create_routing.assert_awaited_once()
        routing_kwargs = gen.create_routing.call_args.kwargs
        assert routing_kwargs["bottom_devices"] == ["dc1-ss-01", "dc1-ss-02"]
        assert routing_kwargs["bottom_role"] == "super-spine"

    @pytest.mark.asyncio
    async def test_two_fabric_template_entries_yield_two_create_devices_calls(self) -> None:
        """fabric_templates with two super-spine entries loops create_devices once
        per entry (dc.py's per-entry loop), not once for the summed quantity."""
        gen = _make_generator()
        gen.create_devices = AsyncMock(side_effect=[["dc1-ss-01"], ["dc1-ss-02", "dc1-ss-03"]])
        data = _deployment(design=_design(max_super_spines_per_fabric=3))
        data["TopologyDeployment"][0]["fabric_templates"] = [
            {"role": "super-spine", "quantity": 1, "template": {"id": "tmpl-ss-a", "interfaces": []}},
            {"role": "super-spine", "quantity": 2, "template": {"id": "tmpl-ss-b", "interfaces": []}},
        ]

        await gen.generate(data)

        assert gen.create_devices.await_count == 2
        amounts = [c.kwargs["quantity"] for c in gen.create_devices.await_args_list]
        assert amounts == [1, 2]

        gen.create_routing.assert_awaited_once()
        routing_kwargs = gen.create_routing.call_args.kwargs
        assert routing_kwargs["bottom_devices"] == ["dc1-ss-01", "dc1-ss-02", "dc1-ss-03"]

    @pytest.mark.asyncio
    async def test_zero_super_spines_with_template_skips_creation(self) -> None:
        """amount_of_super_spines == 0 short-circuits device creation even
        though a template is present and design_mode is not back-to-back."""
        gen = _make_generator()
        data = _deployment(
            design=_design(max_super_spines_per_fabric=2),
            amount_of_super_spines=0,
            super_spine_template={"id": "tmpl-ss", "interfaces": []},
        )

        await gen.generate(data)

        gen.create_devices.assert_not_called()
        gen.create_routing.assert_not_called()

    @pytest.mark.asyncio
    async def test_super_spines_without_template_skips_creation(self) -> None:
        gen = _make_generator()
        data = _deployment(
            design=_design(max_super_spines_per_fabric=2),
            amount_of_super_spines=2,
            super_spine_template=None,
        )

        await gen.generate(data)

        gen.create_devices.assert_not_called()
        gen.create_routing.assert_not_called()

    @pytest.mark.asyncio
    async def test_ospf_ibgp_strategy_skips_underlay_for_super_spines(self) -> None:
        """super-spines sit above the OSPF domain — skip_underlay=True for ospf-ibgp."""
        gen = _make_generator()
        gen.create_devices = AsyncMock(return_value=["dc1-ss-01"])
        data = _deployment(
            design=_design(max_super_spines_per_fabric=1),
            routing_strategy="ospf-ibgp",
            amount_of_super_spines=1,
            super_spine_template={"id": "tmpl-ss", "interfaces": []},
        )

        await gen.generate(data)

        routing_kwargs = gen.create_routing.call_args.kwargs
        assert routing_kwargs["options"]["skip_underlay"] is True


class TestGenerateDCFabricLoopbackPoolAllocation:
    @pytest.mark.asyncio
    async def test_back_to_back_with_border_leaf_capacity_still_allocates_pool(self) -> None:
        """Regression test: a back-to-back design (max_super_spines_per_fabric=0)
        with border-leaf capacity must still get a dc-fabric-loopback pool —
        those border-leaf devices need a loopback IP for overlay BGP just like
        they would under a super-spine design. Previously this pool was only
        allocated when design_mode != "back-to-back", silently breaking
        border-leaf overlay BGP for any back-to-back + border-leaf design."""
        gen = _make_generator()
        data = _deployment(design=_design(max_super_spines_per_fabric=0, max_border_leafs_per_fabric=2))

        await gen.generate(data)

        gen.allocate_resource_pools.assert_awaited_once()
        pools = gen.allocate_resource_pools.call_args.kwargs["pools"]
        assert "dc-fabric-loopback" in pools

    @pytest.mark.asyncio
    async def test_back_to_back_with_no_border_leaf_capacity_skips_pool(self) -> None:
        """Back-to-back with zero border-leaf/hyper-spine capacity (e.g. the
        border-spine micro-fabric pattern) correctly allocates no
        dc-fabric-loopback pool — unchanged existing behavior."""
        gen = _make_generator()
        data = _deployment(design=_design(max_super_spines_per_fabric=0, max_border_leafs_per_fabric=0))

        await gen.generate(data)

        pools = gen.allocate_resource_pools.call_args.kwargs["pools"]
        assert "dc-fabric-loopback" not in pools

    @pytest.mark.asyncio
    async def test_hyper_spine_capacity_alone_allocates_pool(self) -> None:
        gen = _make_generator()
        data = _deployment(
            design=_design(max_super_spines_per_fabric=0, max_border_leafs_per_fabric=0, max_hyper_spines_per_fabric=2)
        )

        await gen.generate(data)

        pools = gen.allocate_resource_pools.call_args.kwargs["pools"]
        assert "dc-fabric-loopback" in pools


class TestGenerateHyperSpineTier:
    @pytest.mark.asyncio
    async def test_no_hyper_spine_templates_skips_creation(self) -> None:
        gen = _make_generator()
        gen.create_devices = AsyncMock(return_value=["dc1-ss-01", "dc1-ss-02"])
        data = _deployment(
            design=_design(max_super_spines_per_fabric=2, max_hyper_spines_per_fabric=0),
            amount_of_super_spines=2,
            super_spine_template={"id": "tmpl-ss", "interfaces": []},
        )

        await gen.generate(data)

        # Only super-spine's own create_devices call — no hyper-spine call.
        assert gen.create_devices.await_count == 1
        gen.create_cabling.assert_not_called()

    @pytest.mark.asyncio
    async def test_hyper_spine_count_exceeding_design_max_raises(self) -> None:
        gen = _make_generator()
        data = _deployment(
            design=_design(max_super_spines_per_fabric=2, max_hyper_spines_per_fabric=1),
            amount_of_hyper_spines=2,
            hyper_spine_template={"id": "tmpl-hs", "interfaces": []},
        )

        with pytest.raises(RuntimeError, match="requests 2 hyper-spines"):
            await gen.generate(data)

    @pytest.mark.asyncio
    async def test_hyper_spine_templates_creates_devices_and_cables_to_super_spine(self) -> None:
        gen = _make_generator()
        gen.create_devices = AsyncMock(
            side_effect=[
                ["dc1-ss-01", "dc1-ss-02"],  # super-spine
                ["dc1-hs-01", "dc1-hs-02"],  # hyper-spine
            ]
        )
        data = _deployment(
            design=_design(max_super_spines_per_fabric=2, max_hyper_spines_per_fabric=2),
            amount_of_super_spines=2,
            super_spine_template={
                "id": "tmpl-ss",
                "interfaces": [{"name": "Ethernet1", "role": "uplink"}],
            },
            amount_of_hyper_spines=2,
            hyper_spine_template={
                "id": "tmpl-hs",
                "interfaces": [{"name": "Ethernet1", "role": "downlink"}],
            },
        )

        await gen.generate(data)

        assert gen.create_devices.await_count == 2
        hyper_spine_call = gen.create_devices.await_args_list[1].kwargs
        assert hyper_spine_call["device_role"] == "hyper-spine"
        assert hyper_spine_call["quantity"] == 2

        gen.create_cabling.assert_awaited_once()
        cabling_kwargs = gen.create_cabling.call_args.kwargs
        assert cabling_kwargs["bottom_devices"] == ["dc1-ss-01", "dc1-ss-02"]
        assert cabling_kwargs["top_devices"] == ["dc1-hs-01", "dc1-hs-02"]
        assert cabling_kwargs["strategy"] == "pod"

        # create_routing called for: super-spine pre-seed, hyper-spine pre-seed,
        # super-spine<->hyper-spine cabling routing = 3 calls.
        assert gen.create_routing.await_count == 3
        cross_tier_call = gen.create_routing.await_args_list[-1].kwargs
        assert cross_tier_call["bottom_devices"] == ["dc1-ss-01", "dc1-ss-02"]
        assert cross_tier_call["top_devices"] == ["dc1-hs-01", "dc1-hs-02"]
        assert cross_tier_call["bottom_role"] == "super-spine"
        assert cross_tier_call["top_role"] == "hyper-spine"

        # Serialized against a concurrent, overlapping generate() for the
        # same DC — see generate()'s own comment for the reproduced DC4
        # collision this guards against.
        gen.acquire_resource_lock.assert_awaited_once_with("hyperspine-cabling-dc-1")
        gen.release_resource_lock.assert_awaited_once_with("lock-id")

    @pytest.mark.asyncio
    async def test_lock_is_held_only_around_the_cabling_call(self) -> None:
        """Acquired right before create_cabling, released right after — not
        held across create_routing, which does not share the race."""
        gen = _make_generator()
        gen.create_devices = AsyncMock(
            side_effect=[
                ["dc1-ss-01"],
                ["dc1-hs-01"],
            ]
        )
        calls: list[str] = []
        gen.acquire_resource_lock = AsyncMock(side_effect=lambda key: calls.append(f"acquire:{key}") or "lock-id")
        gen.release_resource_lock = AsyncMock(side_effect=lambda lock_id: calls.append(f"release:{lock_id}"))
        gen.create_cabling = AsyncMock(side_effect=lambda **_: calls.append("cabling") or [])
        gen.create_routing = AsyncMock(side_effect=lambda **_: calls.append("routing"))
        data = _deployment(
            design=_design(max_super_spines_per_fabric=1, max_hyper_spines_per_fabric=1),
            amount_of_super_spines=1,
            super_spine_template={"id": "tmpl-ss", "interfaces": [{"name": "Ethernet1", "role": "uplink"}]},
            amount_of_hyper_spines=1,
            hyper_spine_template={"id": "tmpl-hs", "interfaces": [{"name": "Ethernet1", "role": "downlink"}]},
        )

        await gen.generate(data)

        acquire_idx = calls.index("acquire:hyperspine-cabling-dc-1")
        cabling_idx = calls.index("cabling")
        release_idx = calls.index("release:lock-id")
        assert acquire_idx < cabling_idx < release_idx

    @pytest.mark.asyncio
    async def test_missing_interfaces_logs_error_and_skips_cabling(self) -> None:
        """No dynamic fallback anymore — templates with no interfaces skip
        super-spine<->hyper-spine cabling and log an error instead."""
        gen = _make_generator()
        gen.create_devices = AsyncMock(
            side_effect=[
                ["dc1-ss-01"],
                ["dc1-hs-01"],
            ]
        )
        data = _deployment(
            design=_design(max_super_spines_per_fabric=1, max_hyper_spines_per_fabric=1),
            amount_of_super_spines=1,
            super_spine_template={"id": "tmpl-ss", "interfaces": []},
            amount_of_hyper_spines=1,
            hyper_spine_template={"id": "tmpl-hs", "interfaces": []},
        )

        await gen.generate(data)

        gen.create_cabling.assert_not_awaited()
        gen.logger.error.assert_called()


class TestGeneratePoolAllocation:
    @pytest.mark.asyncio
    async def test_asn_pool_created_for_ebgp_strategies(self) -> None:
        gen = _make_generator()
        data = _deployment(design=_design(), routing_strategy="ebgp-ibgp")

        await gen.generate(data)

        gen.upsert_asn_pool.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_asn_pool_skipped_for_ospf_ibgp(self) -> None:
        """ospf-ibgp uses OSPF underlay + shared overlay AS — no per-device ASN pool."""
        gen = _make_generator()
        data = _deployment(design=_design(), routing_strategy="ospf-ibgp")

        await gen.generate(data)

        gen.upsert_asn_pool.assert_not_called()

    @pytest.mark.asyncio
    async def test_vni_pool_always_created(self) -> None:
        """VLAN ID pool creation moved off dc.py entirely — it's now per-VLAN-domain
        (MLAG pair or standalone device), created lazily in devices.py/segment.py."""
        gen = _make_generator()
        data = _deployment(design=_design(), routing_strategy="ospf-ibgp")

        await gen.generate(data)

        assert gen.upsert_number_pool.await_count == 1
        pool_names = [c.kwargs["pool_name"] for c in gen.upsert_number_pool.await_args_list]
        assert pool_names == ["dc1-vni-pool"]

    @pytest.mark.asyncio
    async def test_shared_routing_objects_created_with_asn_end_plus_one(self) -> None:
        gen = _make_generator()
        data = _deployment(design=_design(max_pods=2, max_super_spines_per_fabric=2))

        await gen.generate(data)

        gen._create_shared_routing_objects.assert_awaited_once()
        overlay_asn = gen._create_shared_routing_objects.call_args.kwargs["overlay_asn"]
        assert isinstance(overlay_asn, int)


class TestGenerateDCPoolAttachment:
    @pytest.mark.asyncio
    async def test_dc_updated_with_pool_references_when_found(self) -> None:
        gen = _make_generator()
        dc_obj = MagicMock()
        dc_obj.save = AsyncMock()
        gen.client.get = AsyncMock(return_value=dc_obj)
        loopback_pool = MagicMock(id="lo-1")
        gen.allocate_resource_pools = AsyncMock(return_value={"loopback": loopback_pool})

        await gen.generate(_deployment(design=_design()))

        assert dc_obj.loopback_pool == {"id": "lo-1"}
        # Plain save(), not allow_upsert=True: dc_obj is a known-existing node
        # (just fetched via client.get()) — allow_upsert=True would force the
        # full-payload Upsert mutation path and spuriously re-fire `updated`
        # triggers on unrelated fields (fabric_templates/connectivity_mode/status).
        dc_obj.save.assert_awaited_once_with(update_group_context=False)

    @pytest.mark.asyncio
    async def test_asn_and_vni_pools_ride_the_same_single_save(self) -> None:
        """The ASN and VNI pools are attached on the DC's one save, not by the
        pool helpers fetching and saving the DC again."""
        gen = _make_generator()
        dc_obj = MagicMock()
        dc_obj.save = AsyncMock()
        gen.client.get = AsyncMock(return_value=dc_obj)

        await gen.generate(_deployment(design=_design()))

        assert dc_obj.fabric_asn_pool == {"id": "asn-pool-1"}
        assert dc_obj.vni_pool == {"id": "num-pool-1"}
        dc_obj.save.assert_awaited_once_with(update_group_context=False)
        for call in gen.upsert_asn_pool.await_args_list + gen.upsert_number_pool.await_args_list:
            assert "parent_id" not in call.kwargs

    @pytest.mark.asyncio
    async def test_dc_not_found_skips_pool_attachment_without_error(self) -> None:
        gen = _make_generator()
        gen.client.get = AsyncMock(return_value=None)

        await gen.generate(_deployment(design=_design()))

        gen.logger.error.assert_not_called()


class TestCreateSharedRoutingObjects:
    def _make_generator_for_shared_routing(self) -> Any:
        gen = DCTopologyGenerator.__new__(DCTopologyGenerator)
        gen.logger = MagicMock()
        gen.client = MagicMock()
        gen.client.group_context = MagicMock()
        gen.client.group_context.related_node_ids = []
        gen.fabric_name = "dc1"
        gen._ensure_routing_password = AsyncMock()
        setattr(gen, "acquire_resource_lock", AsyncMock(return_value="lock-id"))  # noqa: B010
        setattr(gen, "release_resource_lock", AsyncMock())  # noqa: B010
        return gen

    @pytest.mark.asyncio
    async def test_ebgp_ebgp_creates_fabric_as_for_route_targets(self) -> None:
        """ebgp-ebgp gets the fabric AS too — it is the EVPN route-target identity.

        Under ebgp-ebgp every VTEP runs its own ASN, so there is no other
        fabric-constant ASN to stamp into route-targets. Creating this AS only
        for the iBGP strategies left 9 of 10 demo topologies with no valid RT
        source at all, which is why it is now unconditional.
        """
        gen = self._make_generator_for_shared_routing()
        gen.data = MagicMock(routing_strategy="ebgp-ebgp")
        gen.client.filters = AsyncMock(return_value=[])
        fabric_as = MagicMock(id="fabric-as-1")
        fabric_as.asn.value = 65100
        fabric_as.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=fabric_as)

        await gen._create_shared_routing_objects(overlay_asn=65100)

        assert gen._ensure_routing_password.await_count == 2
        # No asn_pool_id, so the super-spine AS is skipped — the only create()
        # is the fabric AS.
        gen.client.create.assert_awaited_once()
        assert gen.client.create.call_args.kwargs["data"]["asn"] == 65100
        assert "fabric-as-1" in gen.client.group_context.related_node_ids

    @pytest.mark.asyncio
    async def test_ebgp_ibgp_creates_new_overlay_as(self) -> None:
        gen = self._make_generator_for_shared_routing()
        gen.data = MagicMock(routing_strategy="ebgp-ibgp")
        gen.client.filters = AsyncMock(return_value=[])
        new_as = MagicMock(id="as-new-1")
        new_as.asn.value = 65100
        new_as.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=new_as)

        await gen._create_shared_routing_objects(overlay_asn=65100)

        gen.client.create.assert_awaited_once()
        call_kwargs = gen.client.create.call_args.kwargs
        assert call_kwargs["data"]["asn"] == 65100
        new_as.save.assert_awaited_once_with(allow_upsert=True)
        assert "as-new-1" in gen.client.group_context.related_node_ids

    @pytest.mark.asyncio
    async def test_ebgp_ibgp_updates_existing_overlay_as(self) -> None:
        """An existing shared overlay AS is updated in place, not duplicated —
        matched by description, not name, since AS.name is a computed field."""
        gen = self._make_generator_for_shared_routing()
        gen.data = MagicMock(routing_strategy="ebgp-ibgp")
        existing_as = MagicMock(id="as-existing-1")
        existing_as.asn = MagicMock(value=1)
        existing_as.save = AsyncMock()
        gen.client.filters = AsyncMock(return_value=[existing_as])
        gen.client.create = AsyncMock()

        await gen._create_shared_routing_objects(overlay_asn=65200)

        gen.client.create.assert_not_called()
        assert existing_as.asn.value == 65200
        existing_as.save.assert_awaited_once_with(allow_upsert=True)
        assert "as-existing-1" in gen.client.group_context.related_node_ids

    @pytest.mark.asyncio
    async def test_overlay_as_lookup_exception_fails_the_run(self) -> None:
        """A lookup failure is logged as an error (raises in a real run): a run
        that skipped the shared AS would not re-track it, and its cleanup
        would delete the AS every pod's BGP uses."""
        gen = self._make_generator_for_shared_routing()
        gen.data = MagicMock(routing_strategy="ebgp-ibgp")
        gen.client.filters = AsyncMock(side_effect=Exception("db down"))

        await gen._create_shared_routing_objects(overlay_asn=65100)

        # ebgp-ibgp's underlay is also eBGP, so both the shared super-spine AS
        # lookup and the shared overlay AS lookup hit the same failing filters()
        # mock and are each logged independently.
        assert gen.logger.error.call_count == 2

    @pytest.mark.asyncio
    async def test_ospf_ibgp_creates_overlay_as_and_ospf_area(self) -> None:
        gen = self._make_generator_for_shared_routing()
        gen.data = MagicMock(routing_strategy="ospf-ibgp")
        gen.client.filters = AsyncMock(return_value=[])
        gen.client.get = AsyncMock(return_value=None)
        as_obj = MagicMock(id="as-1")
        as_obj.asn.value = 65100
        as_obj.save = AsyncMock()
        area_obj = MagicMock(id="area-1")
        area_obj.save = AsyncMock()
        gen.client.create = AsyncMock(side_effect=[as_obj, area_obj])

        await gen._create_shared_routing_objects(overlay_asn=65100)

        assert gen.client.create.await_count == 2
        area_call_kwargs = gen.client.create.call_args_list[1].kwargs
        assert area_call_kwargs["data"]["area"] == 0
        assert area_call_kwargs["data"]["name"] == "dc1-ospf-area-0"
        area_obj.save.assert_awaited_once_with(allow_upsert=True)
        assert "area-1" in gen.client.group_context.related_node_ids

    @pytest.mark.asyncio
    async def test_ospf_area_creation_exception_fails_the_run(self) -> None:
        """Same as the overlay AS: an error, never a swallowed warning."""
        gen = self._make_generator_for_shared_routing()
        gen.data = MagicMock(routing_strategy="ospf-ibgp")
        gen.client.filters = AsyncMock(return_value=[])
        as_obj = MagicMock(id="as-1")
        as_obj.asn.value = 65100
        as_obj.save = AsyncMock()
        gen.client.create = AsyncMock(side_effect=[as_obj, Exception("create failed")])

        await gen._create_shared_routing_objects(overlay_asn=65100)

        gen.logger.error.assert_called_once()

    @pytest.mark.asyncio
    async def test_ebgp_ebgp_creates_fabric_as_but_no_ospf_area(self) -> None:
        """ebgp-ebgp gets no OSPF area, but does get the fabric AS.

        Both eBGP AS lookups run: the shared super-spine underlay AS and the
        fabric route-target AS. The OSPF area stays strategy-gated.
        """
        gen = self._make_generator_for_shared_routing()
        gen.data = MagicMock(routing_strategy=RoutingStrategy.EBGP_EBGP.value)
        gen.client.filters = AsyncMock(return_value=[])
        fabric_as = MagicMock(id="fabric-as-1")
        fabric_as.asn.value = 65100
        fabric_as.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=fabric_as)

        await gen._create_shared_routing_objects(overlay_asn=65100)

        lookup_descriptions = [c.kwargs["description__value"] for c in gen.client.filters.call_args_list]
        assert lookup_descriptions == [
            "dc1 super-spine underlay ASN",
            "dc1 overlay ASN for iBGP EVPN",
        ]
        # Exactly one create: the fabric AS. No OSPF area (that stays gated on
        # ospf-ibgp) and no super-spine AS (no asn_pool_id was passed).
        gen.client.create.assert_awaited_once()
        assert gen.client.create.call_args.kwargs["data"]["description"] == "dc1 overlay ASN for iBGP EVPN"

    @pytest.mark.asyncio
    async def test_ebgp_ebgp_creates_new_super_spine_as_when_pool_given(self) -> None:
        gen = self._make_generator_for_shared_routing()
        gen.data = MagicMock(routing_strategy=RoutingStrategy.EBGP_EBGP.value)
        gen.client.filters = AsyncMock(return_value=[])
        as_obj = MagicMock(id="ss-as-1")
        as_obj.asn.value = 65001
        as_obj.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=as_obj)

        await gen._create_shared_routing_objects(overlay_asn=65100, asn_pool_id="pool-1")

        # Two creates now: the super-spine underlay AS (from the pool) and the
        # fabric route-target AS (explicit ASN). Assert on the super-spine one.
        assert gen.client.create.await_args_list[0].kwargs == {
            "kind": RoutingAutonomousSystem,
            "data": {"asn": {"from_pool": {"id": "pool-1"}}, "description": "dc1 super-spine underlay ASN"},
        }
        as_obj.save.assert_awaited_with(allow_upsert=True)
        assert "ss-as-1" in gen.client.group_context.related_node_ids

    @pytest.mark.asyncio
    async def test_ebgp_ebgp_reuses_existing_super_spine_as(self) -> None:
        gen = self._make_generator_for_shared_routing()
        gen.data = MagicMock(routing_strategy=RoutingStrategy.EBGP_EBGP.value)
        existing_as = MagicMock(id="ss-as-existing")
        existing_as.asn.value = 65001
        gen.client.filters = AsyncMock(return_value=[existing_as])
        gen.client.create = AsyncMock()

        await gen._create_shared_routing_objects(overlay_asn=65100, asn_pool_id="pool-1")

        gen.client.create.assert_not_called()
        assert "ss-as-existing" in gen.client.group_context.related_node_ids

    @pytest.mark.asyncio
    async def test_ospf_ibgp_skips_super_spine_as_lookup(self) -> None:
        """ospf-ibgp's underlay is OSPF, not eBGP — no super-spine AS lookup
        at all (super-spine's own routing skips underlay entirely for this
        strategy, see dc.py's skip_underlay branch)."""
        gen = self._make_generator_for_shared_routing()
        gen.data = MagicMock(routing_strategy="ospf-ibgp")
        gen.client.filters = AsyncMock(return_value=[])
        gen.client.get = AsyncMock(return_value=None)
        as_obj = MagicMock(id="as-1")
        as_obj.asn.value = 65100
        as_obj.save = AsyncMock()
        area_obj = MagicMock(id="area-1")
        area_obj.save = AsyncMock()
        gen.client.create = AsyncMock(side_effect=[as_obj, area_obj])

        await gen._create_shared_routing_objects(overlay_asn=65100, asn_pool_id="pool-1")

        filters_calls = [c for c in gen.client.filters.await_args_list if "super-spine" in str(c)]
        assert filters_calls == []
