"""add_pod against its DC's fabric design: the envelope is enforced before any write, and the pod pools are sized from it.

- the pod's design must be one of the DC design's pod_templates;
- the DC holds at most max_pods pods (this pod counted once, so its own re-run is not refused);
- the pod technical/loopback pools come from generators/fabric_sizing.py for (pod design, DC design),
  converted to the address family of the DC pool each is sliced from;
- the super-spine port stride is the widest pod design the DC may host;
- a back-to-back pod refuses a lower-index sibling whose design has another spine width.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from generators.fabric_sizing import host_bits_to_prefix_length, pod_loopback_bits, pod_technical_bits
from generators.topology.pod import PodTopologyGenerator
from tests.unit.simulators.fabric_designs import dc_design, pod_design


class _StopAfterPools(Exception):
    """Raised by the allocate_resource_pools mock: the test only needs its arguments."""


def _pod(pod_id: str, *, index: int = 9, design: str = "S_MIDDLE") -> MagicMock:
    """A pod of the DC as client.filters returns it: id, index and its design's id."""
    pod = MagicMock()
    pod.id = pod_id
    pod.index.value = index
    pod.design.id = pod_design(design)["id"]
    return pod


def _payload(*, pod: str = "S_MIDDLE", dc: str = "S", underlay_protocol: str = "ipv6") -> dict[str, Any]:
    """A back-to-back DC (no super-spines) with one pod, ready for pool allocation."""
    return {
        "TopologyPod": [
            {
                "id": "pod-1",
                "name": "DC1-1-POD-1",
                "index": 1,
                "design": pod_design(pod),
                "fabric_templates": [
                    {
                        "role": "spine",
                        "quantity": 2,
                        "template": {"id": "tmpl-spine", "interfaces": [{"name": "Ethernet1/31", "role": "uplink"}]},
                    }
                ],
                "parent": {
                    "id": "dc-1",
                    "name": "DC1",
                    "index": 1,
                    "design": dc_design(dc),
                    "routing_strategy": "ospf-ibgp",
                    "underlay_protocol": underlay_protocol,
                    "fabric_templates": [],
                    "devices": [],
                },
            }
        ]
    }


def _make_generator(*, pods_of_the_dc: list[MagicMock]) -> Any:
    gen: Any = PodTopologyGenerator.__new__(PodTopologyGenerator)
    gen.logger = MagicMock()
    gen.client = MagicMock()
    gen.client.group_context = MagicMock()
    gen.client.group_context.related_node_ids = []
    gen.client.filters = AsyncMock(return_value=pods_of_the_dc)
    gen.wait_for_parent_generator_and_refetch = AsyncMock(return_value=None)
    gen.allocate_resource_pools = AsyncMock(side_effect=_StopAfterPools)
    gen.create_devices = AsyncMock()
    gen.create_routing = AsyncMock()
    return gen


class TestEnvelopeEnforcement:
    @pytest.mark.asyncio
    async def test_a_design_the_dc_design_does_not_host_fails_before_any_write(self) -> None:
        gen = _make_generator(pods_of_the_dc=[])
        payload = _payload()
        payload["TopologyPod"][0]["parent"]["design"]["pod_templates"] = [pod_design("S_TOR")]

        await gen.generate(payload)

        gen.logger.error.assert_called_once()
        message = gen.logger.error.call_args.args[0]
        assert "design 'S_MIDDLE' is not one of DC DC1's design 'S' pod_templates ['S_TOR']" in message
        gen.client.filters.assert_not_awaited()
        gen.allocate_resource_pools.assert_not_awaited()
        gen.create_devices.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_more_pods_than_max_pods_fails_before_any_write(self) -> None:
        """The S design holds at most 2 pods: a third one is refused."""
        gen = _make_generator(pods_of_the_dc=[_pod("pod-a"), _pod("pod-b")])

        await gen.generate(_payload(dc="S"))

        gen.logger.error.assert_called_once()
        assert "DC DC1 holds 3 pods but its design 'S' allows at most 2" in gen.logger.error.call_args.args[0]
        gen.allocate_resource_pools.assert_not_awaited()
        gen.create_devices.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_pod_itself_is_counted_once(self) -> None:
        """A re-run of a pod already in the DC's pod list is within max_pods."""
        gen = _make_generator(pods_of_the_dc=[_pod("pod-1"), _pod("pod-b")])

        with pytest.raises(_StopAfterPools):
            await gen.generate(_payload(dc="S"))

        gen.logger.error.assert_not_called()
        gen.allocate_resource_pools.assert_awaited_once()


class TestMeshSlotWidth:
    @pytest.mark.asyncio
    async def test_a_lower_sibling_of_another_spine_width_fails_before_any_write(self) -> None:
        """An S DC lists 2- and 4-spine pod designs: meshing a 2-spine pod 2 to a 4-spine pod 1 would
        slice pod 1's uplinks in 2-port slots for one pair and 4-port slots for the other."""
        gen = _make_generator(pods_of_the_dc=[_pod("pod-a", index=1, design="S_TOR")])
        payload = _payload(dc="S")
        payload["TopologyPod"][0]["index"] = 2

        await gen.generate(payload)

        gen.logger.error.assert_called_once()
        message = gen.logger.error.call_args.args[0]
        assert "back-to-back mesh needs one spine width" in message
        assert "design 'S_MIDDLE' allows 2 spines, lower-index sibling pod(s) [1]" in message
        gen.allocate_resource_pools.assert_not_awaited()
        gen.create_devices.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_siblings_of_the_same_width_pass(self) -> None:
        """S_MIDDLE and S_BORDER_SPINE_POD are different designs with the same 2-spine width."""
        gen = _make_generator(
            pods_of_the_dc=[_pod("pod-a", index=1, design="S_BORDER_SPINE_POD"), _pod("pod-c", index=3, design="S_TOR")]
        )
        payload = _payload(dc="M")
        payload["TopologyPod"][0]["index"] = 2

        with pytest.raises(_StopAfterPools):
            await gen.generate(payload)

        gen.logger.error.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_dc_with_super_spines_does_not_check_mesh_widths(self) -> None:
        """Mixed spine widths are fine under super-spines: every pod owns its own super-spine port stride."""
        gen = _make_generator(pods_of_the_dc=[_pod("pod-a", index=1, design="S_TOR")])
        gen.bgp_processes_ready = AsyncMock(return_value=True)
        payload = _payload(dc="L")
        payload["TopologyPod"][0]["index"] = 2
        payload["TopologyPod"][0]["parent"]["devices"] = [{"name": "ss-01"}]
        payload["TopologyPod"][0]["parent"]["fabric_templates"] = [
            {"role": "super-spine", "quantity": 1, "template": {"id": "tmpl-ss", "interfaces": [{"name": "Eth1/1"}]}}
        ]

        with pytest.raises(_StopAfterPools):
            await gen.generate(payload)

        gen.logger.error.assert_not_called()


class TestPodPoolSizes:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("protocol", "technical_ipv6", "loopback_ipv6"),
        [("ipv6", True, True), ("ipv4", False, False), ("dual_stack", True, False)],
    )
    @pytest.mark.parametrize(("dc", "pod"), [("S", "S_MIDDLE"), ("M", "M_MIDDLE"), ("M", "S_MIXED")])
    async def test_pools_are_sized_from_the_pod_and_dc_designs(
        self, dc: str, pod: str, protocol: str, technical_ipv6: bool, loopback_ipv6: bool
    ) -> None:
        gen = _make_generator(pods_of_the_dc=[])

        with pytest.raises(_StopAfterPools):
            await gen.generate(_payload(pod=pod, dc=dc, underlay_protocol=protocol))

        technical = host_bits_to_prefix_length(pod_technical_bits(pod_design(pod), dc_design(dc)), ipv6=technical_ipv6)
        loopback = host_bits_to_prefix_length(pod_loopback_bits(pod_design(pod)), ipv6=loopback_ipv6)
        assert gen.allocate_resource_pools.call_args.kwargs["pools"] == {"technical": technical, "loopback": loopback}

    @pytest.mark.asyncio
    async def test_m_middle_in_an_m_dc_gets_a_256_address_technical_pool(self) -> None:
        """Pinned example: 87 links (174 addresses) need 8 host bits at 75% fill -> /120 in IPv6."""
        gen = _make_generator(pods_of_the_dc=[])

        with pytest.raises(_StopAfterPools):
            await gen.generate(_payload(pod="M_MIDDLE", dc="M"))

        assert gen.allocate_resource_pools.call_args.kwargs["pools"] == {"technical": 120, "loopback": 123}


class TestSuperSpineStride:
    @pytest.mark.asyncio
    async def test_cabling_offset_strides_by_the_widest_pod_design(self) -> None:
        """Pod 2 of an L DC starts at super-spine port 4: the widest pod design the L design hosts has 4 spines."""
        gen = _make_generator(pods_of_the_dc=[])
        gen.allocate_resource_pools = AsyncMock(return_value={"technical": MagicMock(), "loopback": MagicMock()})
        gen.create_devices = AsyncMock(return_value=["spine-01", "spine-02"])
        gen.create_cabling = AsyncMock(return_value=[])
        gen.bgp_processes_ready = AsyncMock(return_value=True)
        gen._cable_border_leafs_to_spines = AsyncMock()
        payload = _payload(pod="S_MIDDLE", dc="L")
        payload["TopologyPod"][0]["index"] = 2
        payload["TopologyPod"][0]["parent"]["devices"] = [{"name": "ss-01"}]
        payload["TopologyPod"][0]["parent"]["fabric_templates"] = [
            {"role": "super-spine", "quantity": 1, "template": {"id": "tmpl-ss", "interfaces": [{"name": "Eth1/1"}]}}
        ]

        await gen.generate(payload)

        assert gen.create_cabling.call_args.kwargs["options"]["cabling_offset"] == 4


class TestSpineSlotEntries:
    @pytest.mark.asyncio
    async def test_role_index_continues_across_spine_entries(self) -> None:
        """Two spine entries (2 + 2): the second starts at role_index 3, so it never re-creates
        (upserts over) the first entry's spines."""
        gen = _make_generator(pods_of_the_dc=[])
        gen.allocate_resource_pools = AsyncMock(return_value={"technical": MagicMock(), "loopback": MagicMock()})
        gen.create_devices = AsyncMock(side_effect=[["sp-01", "sp-02"], ["sp-03", "sp-04"]])
        gen._cable_to_existing_sibling_pods = AsyncMock()
        gen._cable_border_leafs_to_spines = AsyncMock()
        payload = _payload(pod="S_TOR", dc="S")
        entry = payload["TopologyPod"][0]["fabric_templates"][0]
        payload["TopologyPod"][0]["fabric_templates"] = [entry, {**entry, "template": {**entry["template"]}}]

        await gen.generate(payload)

        starts = [call.kwargs["options"]["role_index_start"] for call in gen.create_devices.call_args_list]
        assert starts == [1, 3]
