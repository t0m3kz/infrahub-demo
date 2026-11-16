"""The pod generator (generators/dc/pod.py) against its DC's fabric design, and the plan it builds: pools, spines, the
shared spine AS, the uplinks to the super-spines or the mesh, the routing. The plan is the one handed to the reconciler;
the reads it is built from are answered here.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from generators.capabilities.bgp import RoutingReads, SharedFacts
from generators.capabilities.ha import DC_GENERATORS
from generators.dc.pod import PodReads, PodTopologyGenerator
from generators.dc.routing_inputs import RoutingInputs
from generators.dcim.cabling_plan import PortFacts
from generators.dcim.device_plan import DeviceReads
from generators.dcim.template_interfaces import TemplatePort, TemplateReads
from generators.model.errors import GeneratorError
from generators.model.sizing import host_bits_to_prefix_length, pod_loopback_bits, pod_technical_bits
from generators.plan import (
    AddressAllocationSpec,
    ExternalProducer,
    Hfid,
    LinkPrefixSpec,
    NodeRef,
    NodeSpec,
    PrefixAllocationSpec,
    TopologyPlan,
    TrackSpec,
)
from tests.unit.simulators.fabric_designs import dc_design, pod_design

_SFP = "10gbase-x-sfpp"
_SS_PORTS = [TemplatePort(f"Eth1/{n}", "uplink", _SFP) for n in range(1, 9)]
_SPINE_PORTS = [TemplatePort("Ethernet1/31", "uplink", _SFP), TemplatePort("Ethernet1/1", "downlink", _SFP)]


class _StopAfterPlan(Exception):
    """Raised by the apply_plan mock: the test only needs the plan."""


def _pod(pod_id: str, *, index: int = 9, design: str = "S_MIDDLE") -> dict[str, Any]:
    """A pod of the DC as the DC's ``children`` carry it in the query payload: id, index and its design's id."""
    return {"id": pod_id, "index": index, "design": {"node": {"id": pod_design(design)["id"]}}}


def _payload(
    *,
    pod: str = "S_MIDDLE",
    dc: str = "S",
    underlay_protocol: str = "ipv6",
    pods_of_the_dc: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
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
                    "children": [{}, *(pods_of_the_dc or [])],
                },
            }
        ]
    }


def _routed_payload(*, strategy: str = "ebgp-ebgp", super_spines: bool = True, asn_pool: bool = True) -> dict[str, Any]:
    """An L DC pod 2 (S_MIDDLE) whose DC has (or has not) a super-spine tier and an ASN pool."""
    payload = _payload(pod="S_MIDDLE", dc="L")
    pod = payload["TopologyPod"][0]
    pod["index"] = 2
    pod["parent"]["routing_strategy"] = strategy
    pod["parent"]["management_pool"] = {"id": "mgmt-id", "name": "mgmt"}
    if asn_pool:
        pod["parent"]["fabric_asn_pool"] = {"id": "asn-id", "name": "dc1-asn-pool"}
    if super_spines:
        pod["parent"]["devices"] = [{"name": "ss-01"}]
        pod["parent"]["fabric_templates"] = [
            {
                "role": "super-spine",
                "quantity": 1,
                "template": {"id": "tmpl-ss", "interfaces": [{"name": f"Eth1/{n}"} for n in range(1, 9)]},
            }
        ]
    return payload


def _generator(monkeypatch: pytest.MonkeyPatch, *, ports: dict[str, list[TemplatePort]] | None = None) -> Any:
    """A pod generator whose reads of other generators' data are answered and whose apply_plan stops the run."""
    gen: Any = PodTopologyGenerator.__new__(PodTopologyGenerator)
    gen.logger = MagicMock()
    gen.client = MagicMock()
    gen.client.group_context = MagicMock(related_node_ids=[])
    gen.client.filters = AsyncMock(return_value=[])
    gen.wait_for_parent_generator_and_refetch = AsyncMock(return_value=None)
    gen.apply_plan = AsyncMock(side_effect=_StopAfterPlan)
    gen.locked_keys = []

    class _Lock:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *_: Any) -> None:
            return None

    def resource_lock(key: str) -> _Lock:
        gen.locked_keys.append(key)
        return _Lock()

    gen.resource_lock = resource_lock
    table = {"tmpl-spine": _SPINE_PORTS, "tmpl-ss": _SS_PORTS, **(ports or {})}

    async def read_templates(_client: Any, ids: Any) -> TemplateReads:
        return TemplateReads(ports={template_id: table.get(template_id, []) for template_id in ids}, owners={})

    async def read_routing(generator: Any, **_: Any) -> RoutingInputs:
        facts = SharedFacts(overlay_as_id="as-overlay", ospf_area_id="area-0", evpn_address_family_id="af-evpn")
        reads = RoutingReads(
            loopbacks={"ss-01": (Hfid("DcimVirtualInterface", ("ss-01", "Loopback0")), "ip-ss-01")},
            overlay=frozenset({"ss-01"}),
        )
        return RoutingInputs(reads=reads, facts=facts)

    monkeypatch.setattr("generators.dc.pod.read_template_ports", read_templates)
    monkeypatch.setattr("generators.dc.pod.read_routing_inputs", read_routing)
    monkeypatch.setattr("generators.dc.pod.bgp_processes_ready", AsyncMock(return_value=True))
    monkeypatch.setattr("generators.dc.pod.asyncio.sleep", AsyncMock())
    return gen


async def _plan_of(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any], **kwargs: Any) -> TopologyPlan:
    """The plan the pod generator hands to apply_plan for ``payload``."""
    gen = _generator(monkeypatch, **kwargs)
    gen.apply_plan = AsyncMock(return_value=MagicMock())
    await gen.generate(payload)
    return _handed(gen)


def _handed(gen: Any) -> TopologyPlan:
    """The plan the generator handed to its (mocked) apply_plan."""
    call = gen.apply_plan.await_args
    assert call is not None
    return call.args[0]


def _nodes(plan: TopologyPlan, kind: str) -> list[NodeSpec]:
    return [spec for spec in plan.of(NodeSpec) if spec.kind == kind]


@pytest.mark.asyncio
class TestEnvelopeEnforcement:
    async def test_a_design_the_dc_design_does_not_host_fails_before_any_write(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gen = _generator(monkeypatch)
        payload = _payload()
        payload["TopologyPod"][0]["parent"]["design"]["pod_templates"] = [pod_design("S_TOR")]

        await gen.generate(payload)

        gen.logger.error.assert_called_once()
        message = gen.logger.error.call_args.args[0]
        assert "design 'S_MIDDLE' is not one of DC DC1's design 'S' pod_templates ['S_TOR']" in message
        gen.client.filters.assert_not_awaited()
        gen.apply_plan.assert_not_awaited()

    async def test_more_pods_than_max_pods_fails_before_any_write(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The S design holds at most 2 pods: a third one is refused."""
        gen = _generator(monkeypatch)

        await gen.generate(_payload(dc="S", pods_of_the_dc=[_pod("pod-a"), _pod("pod-b")]))

        gen.logger.error.assert_called_once()
        gen.apply_plan.assert_not_awaited()

    async def test_the_pod_itself_is_counted_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A re-run of a pod already in the DC's pod list is within max_pods."""
        gen = _generator(monkeypatch)

        with pytest.raises(_StopAfterPlan):
            await gen.generate(_payload(dc="S", pods_of_the_dc=[_pod("pod-1"), _pod("pod-b")]))

        gen.logger.error.assert_not_called()


@pytest.mark.asyncio
class TestMeshSlotWidth:
    async def test_a_lower_sibling_of_another_spine_width_fails_before_any_write(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An S DC lists 2- and 4-spine pod designs: meshing a 2-spine pod 2 to a 4-spine pod 1 would slice pod 1's
        uplinks in 2-port slots for one pair and 4-port slots for the other."""
        gen = _generator(monkeypatch)
        payload = _payload(dc="S", pods_of_the_dc=[_pod("pod-a", index=1, design="S_TOR")])
        payload["TopologyPod"][0]["index"] = 2

        await gen.generate(payload)

        message = gen.logger.error.call_args.args[0]
        assert "back-to-back mesh needs one spine width" in message
        assert "design 'S_MIDDLE' allows 2 spines, lower-index sibling pod(s) [1]" in message
        gen.apply_plan.assert_not_awaited()

    async def test_a_dc_with_super_spines_does_not_check_mesh_widths(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Mixed spine widths are fine under super-spines: every pod owns its own super-spine port stride."""
        gen = _generator(monkeypatch)
        payload = _routed_payload(super_spines=True)
        payload["TopologyPod"][0]["parent"]["children"] = [{}, _pod("pod-a", index=1, design="S_TOR")]

        with pytest.raises(_StopAfterPlan):
            await gen.generate(payload)

        gen.logger.error.assert_not_called()


@pytest.mark.asyncio
class TestPodPoolSizes:
    @pytest.mark.parametrize(
        ("protocol", "technical_ipv6", "loopback_ipv6"),
        [("ipv6", True, True), ("ipv4", False, False), ("dual_stack", True, False)],
    )
    @pytest.mark.parametrize(("dc", "pod"), [("S", "S_MIDDLE"), ("M", "M_MIDDLE"), ("M", "S_MIXED")])
    async def test_pools_are_sized_from_the_pod_and_dc_designs(
        self,
        monkeypatch: pytest.MonkeyPatch,
        dc: str,
        pod: str,
        protocol: str,
        technical_ipv6: bool,
        loopback_ipv6: bool,
    ) -> None:
        plan = await _plan_of(monkeypatch, _payload(pod=pod, dc=dc, underlay_protocol=protocol))

        technical = host_bits_to_prefix_length(pod_technical_bits(pod_design(pod), dc_design(dc)), ipv6=technical_ipv6)
        loopback = host_bits_to_prefix_length(pod_loopback_bits(pod_design(pod)), ipv6=loopback_ipv6)
        assert {spec.role: spec.prefix_length for spec in plan.of(PrefixAllocationSpec)} == {
            "technical": technical,
            "loopback": loopback,
        }

    async def test_m_middle_in_an_m_dc_gets_a_256_address_technical_pool(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Pinned example: 87 links (174 addresses) need 8 host bits at 75% fill -> /120 in IPv6."""
        plan = await _plan_of(monkeypatch, _payload(pod="M_MIDDLE", dc="M"))

        assert {spec.role: spec.prefix_length for spec in plan.of(PrefixAllocationSpec)} == {
            "technical": 120,
            "loopback": 123,
        }


@pytest.mark.asyncio
class TestPodPlan:
    async def test_the_plan_root_is_the_pod_and_waits_for_the_dcs_generators(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plan = await _plan_of(monkeypatch, _routed_payload())

        assert plan.root_id == "pod-1"
        assert plan.producers == [ExternalProducer(DC_GENERATORS, "dc-1")]
        plan.validate()

    async def test_the_pools_are_slices_of_the_dcs_pools_attached_to_the_pod_under_the_pods_locks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gen = _generator(monkeypatch)
        gen.apply_plan = AsyncMock(return_value=MagicMock())

        await gen.generate(_routed_payload())

        plan = _handed(gen)
        slices = {spec.identifier: spec for spec in plan.of(PrefixAllocationSpec)}
        assert list(slices) == ["dc1-1-pod-1-technical-pool", "dc1-1-pod-1-loopback-pool"]
        assert slices["dc1-1-pod-1-technical-pool"].parent == Hfid("CoreIPPrefixPool", ("dc1-technical-pool",))
        (refs,) = [spec for spec in plan.of(NodeSpec) if spec.existing and spec.data.get("id") == "pod-1"]
        assert set(refs.data) == {"id", "prefix_pool", "loopback_pool"} and refs.ownership == "shared"
        assert gen.locked_keys == ["pool-alloc-pod-pod-1", "shared-as-dc1-1-pod-1 spine underlay ASN"]

    async def test_the_spines_take_the_dc_management_pool_and_the_pods_loopback_pool(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plan = await _plan_of(monkeypatch, _routed_payload())

        spines = [spec for spec in _nodes(plan, "DcimPhysicalDevice") if spec.data["role"] == "spine"]
        assert len(spines) == 2 and all(spec.data["deployment"] == {"id": "pod-1"} for spec in spines)
        allocations = {spec.key: spec for spec in plan.of(AddressAllocationSpec)}
        name = spines[0].data["name"]
        assert allocations[f"management:{name}"].pool_id == "mgmt-id"
        assert allocations[f"loopback-address:{name}"].pool_id == NodeRef("pool:dc1-1-pod-1-loopback-pool")

    async def test_ebgp_with_an_asn_pool_pre_seeds_the_spines_bgp_on_one_shared_underlay_as(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plan = await _plan_of(monkeypatch, _routed_payload())

        (shared,) = [spec for spec in _nodes(plan, "RoutingAutonomousSystem")]
        assert shared.key == "pod-spine-as" and shared.data["description"] == "dc1-1-pod-1 spine underlay ASN"
        assert shared.create_only == {"asn": {"from_pool": {"id": "asn-id"}}} and shared.sequential
        processes = {spec.data["name"]: spec for spec in _nodes(plan, "ManagedBGP")}
        spine_underlay = [spec for name, spec in processes.items() if name.endswith("-bgp-underlay")]
        assert {spec.data["local_as"]["id"] for spec in spine_underlay} == {NodeRef("pod-spine-as")}
        assert {"ss-01-bgp-underlay"}.isdisjoint(processes)  # the super-spine's processes are the DC's

    async def test_an_existing_shared_as_is_tracked_and_not_drawn_again(self, monkeypatch: pytest.MonkeyPatch) -> None:
        gen = _generator(monkeypatch)
        gen.apply_plan = AsyncMock(return_value=MagicMock())
        gen.client.filters = AsyncMock(
            side_effect=lambda kind, **flt: [MagicMock(id="as-found")] if "description__value" in flt else []
        )

        await gen.generate(_routed_payload())

        plan = _handed(gen)
        assert not _nodes(plan, "RoutingAutonomousSystem") and TrackSpec(("as-found",)) in plan.of(TrackSpec)

    async def test_without_an_ebgp_underlay_there_is_no_shared_as(self, monkeypatch: pytest.MonkeyPatch) -> None:
        plan = await _plan_of(monkeypatch, _routed_payload(strategy="ospf-ibgp"))

        assert _nodes(plan, "RoutingAutonomousSystem") == []
        assert {spec.data["name"] for spec in _nodes(plan, "ManagedOSPF")} >= {"ss-01-ospf-underlay"}

    async def test_without_an_asn_pool_there_is_no_shared_as(self, monkeypatch: pytest.MonkeyPatch) -> None:
        gen = _generator(monkeypatch)
        gen.apply_plan = AsyncMock(return_value=MagicMock())

        await gen.generate(_routed_payload(asn_pool=False, strategy="ospf-ibgp"))

        assert not _nodes(_handed(gen), "RoutingAutonomousSystem")

    async def test_the_super_spine_cables_start_at_the_pods_port_stride_with_the_pods_pool_and_p2p_length(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pod 2 of an L DC starts at super-spine port 4: the widest pod design the L design hosts has 4 spines."""
        plan = await _plan_of(monkeypatch, _routed_payload())

        prefixes = plan.of(LinkPrefixSpec)
        assert len(prefixes) == 2 and {(spec.pool_id, spec.prefix_length) for spec in prefixes} == {
            (NodeRef("pool:dc1-1-pod-1-technical-pool"), 127)
        }
        top_ports = {hfid.parts for spec in prefixes for hfid in spec.ends if hfid.parts[0] == "ss-01"}
        assert top_ports == {("ss-01", "Eth1/5"), ("ss-01", "Eth1/6")}
        # the super-spine's ports are the DC's: nothing in the plan writes them but their update
        assert all(
            spec.existing
            for spec in _nodes(plan, "DcimPhysicalInterface")
            if spec.hfid and spec.hfid.parts[0] == "ss-01"
        )

    async def test_a_dc_without_super_spines_pre_seeds_the_overlay_only_and_cables_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plan = await _plan_of(monkeypatch, _routed_payload(super_spines=False))

        assert _nodes(plan, "DcimCable") == []
        overlay = [spec for spec in _nodes(plan, "ManagedBGP") if spec.data["process_role"] == "overlay"]
        assert len(overlay) == 2 and all(spec.data["name"].startswith("sp-") for spec in overlay)
        assert [spec for spec in _nodes(plan, "ManagedBGPPeering") if spec.data["peering_role"] == "underlay"] == []

    async def test_role_index_continues_across_spine_entries(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Two spine entries (2 + 2): the second starts at role_index 3, so it never re-creates (upserts over) the first
        entry's spines."""
        payload = _payload(pod="S_TOR", dc="S")
        entry = payload["TopologyPod"][0]["fabric_templates"][0]
        payload["TopologyPod"][0]["fabric_templates"] = [entry, {**entry, "template": {**entry["template"]}}]

        plan = await _plan_of(monkeypatch, payload)

        names = [spec.data["name"] for spec in _nodes(plan, "DcimPhysicalDevice")]
        assert len(names) == len(set(names)) == 4

    async def test_the_same_data_gives_the_same_plan(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert (await _plan_of(monkeypatch, _routed_payload())).specs == (
            await _plan_of(monkeypatch, _routed_payload())
        ).specs

    async def test_a_spine_template_without_the_uplink_port_fails_before_anything_is_written(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gen = _generator(monkeypatch, ports={"tmpl-spine": []})

        with pytest.raises(GeneratorError, match="spine template has no port"):
            await gen.generate(_routed_payload())

        gen.apply_plan.assert_not_awaited()

    async def test_a_super_spine_port_that_does_not_exist_is_named_and_nothing_is_written(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gen = _generator(monkeypatch, ports={"tmpl-ss": [TemplatePort("Eth1/1", "uplink", _SFP)]})

        with pytest.raises(GeneratorError, match="super-spine template has no port"):
            await gen.generate(_routed_payload())

        gen.apply_plan.assert_not_awaited()


@pytest.mark.asyncio
class TestBorderLeafUplinks:
    """The pod's P2P links take the prefix length of the technical pool's address family."""

    @pytest.mark.parametrize(("protocol", "p2p"), [("ipv4", 31), ("ipv6", 127), ("dual_stack", 127)])
    async def test_border_leaf_uplinks_use_the_technical_pool_family(
        self, monkeypatch: pytest.MonkeyPatch, protocol: str, p2p: int
    ) -> None:
        gen = _generator(
            monkeypatch,
            ports={
                "tmpl-spine": [
                    *_SPINE_PORTS,
                    *(TemplatePort(f"Ethernet1/{n}", "downlink", _SFP) for n in range(10, 18)),
                ]
            },
        )
        gen.apply_plan = AsyncMock(return_value=MagicMock())
        border_leaf = MagicMock()
        border_leaf.name.value = "dc1-bl-01"
        uplinks = [MagicMock(), MagicMock()]
        for uplink, name in zip(uplinks, ("Ethernet1/1", "Ethernet1/2"), strict=True):
            uplink.name.value, uplink.device.peer.name.value, uplink.interface_type.value = name, "dc1-bl-01", _SFP
        calls: list[str] = []

        async def filters(kind: Any, **flt: Any) -> list[Any]:
            calls.append(getattr(kind, "__name__", kind))
            if getattr(kind, "__name__", kind) == "DcimPhysicalDevice" and flt.get("role__value") == "border-leaf":
                return [border_leaf]
            if getattr(kind, "__name__", kind) == "DcimPhysicalInterface":
                return uplinks
            return []

        gen.client.filters = filters
        payload = _payload(pod="S_MIXED", dc="M", underlay_protocol=protocol)
        payload["TopologyPod"][0]["fabric_templates"][0]["template"]["interfaces"].extend(
            {"name": f"Ethernet1/{n}", "role": "downlink"} for n in range(10, 18)
        )

        await gen.generate(payload)

        plan = _handed(gen)
        links = [spec for spec in plan.of(LinkPrefixSpec) if any(hfid.parts[0] == "dc1-bl-01" for hfid in spec.ends)]
        assert links and {spec.prefix_length for spec in links} == {p2p}
        assert isinstance(links[0].pool_id, NodeRef)


class TestReadsArePerPod:
    def test_pod_reads_hold_what_the_plan_needs(self) -> None:
        reads = PodReads(
            devices=DeviceReads(),
            templates=TemplateReads(ports={}, owners={}),
            pool_ids={},
            shared_as_id=None,
            siblings=[],
            border_leafs=SimpleNamespace(names=[], uplinks=[]),  # ty: ignore[invalid-argument-type]
            routing=RoutingInputs(reads=RoutingReads(), facts=SharedFacts()),
            positions={},
            services=None,
        )

        assert reads.shared_as_id is None and PortFacts("a", "b", None).hfid == Hfid(
            "DcimPhysicalInterface", ("a", "b")
        )


_FW_PORTS = [
    TemplatePort("eth1", "uplink", _SFP),
    TemplatePort("eth2", "uplink", _SFP),
    TemplatePort("ha1", "ha", _SFP),
]
_BORDER_SPINE_PORTS = [
    TemplatePort("Ethernet1/31", "uplink", _SFP),
    TemplatePort("Ethernet1/1", "downlink", _SFP),
    *[TemplatePort(f"Ethernet1/{n}", "firewall", _SFP) for n in range(10, 14)],
    TemplatePort("Ethernet1/14", "load-balancer", _SFP),
]


def _border_spine_payload(*, entries: list[dict[str, Any]]) -> dict[str, Any]:
    """A pod whose spine tier is border-spine and that declares its own service devices."""
    payload = _payload()
    pod = payload["TopologyPod"][0]
    pod["fabric_templates"] = [
        {
            "role": "border-spine",
            "quantity": 1,
            "template": {
                "id": "tmpl-bs",
                "interfaces": [{"name": "Ethernet1/31", "role": "uplink"}, {"name": "Ethernet1/1", "role": "downlink"}],
            },
        },
        *entries,
    ]
    return payload


@pytest.mark.asyncio
class TestPodServices:
    """A pod with border-spines creates its own firewalls and load balancers in the one plan and cables them to its
    border-spines."""

    async def test_the_firewall_pair_its_ha_domain_and_its_cables_are_in_the_pods_plan(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        payload = _border_spine_payload(
            entries=[
                {
                    "role": "internal-firewall",
                    "quantity": 2,
                    "template": {"id": "tmpl-fw", "interfaces": []},
                }
            ]
        )

        plan = await _plan_of(
            monkeypatch,
            payload,
            ports={"tmpl-bs": _BORDER_SPINE_PORTS, "tmpl-fw": _FW_PORTS},
        )

        firewalls = [spec for spec in _nodes(plan, "DcimPhysicalDevice") if spec.data["role"] == "internal-firewall"]
        # The devices belong to the pod, the cables to the DC
        assert len(firewalls) == 2 and {spec.data["deployment"]["id"] for spec in firewalls} == {"pod-1"}
        (domain,) = _nodes(plan, "ManagedFirewallHA")
        assert domain.hfid is not None and domain.hfid.parts[0].startswith("fw-")
        cables = {spec.data["name"]: spec.data for spec in _nodes(plan, "DcimCable")}
        sync = [name for name in cables if name.endswith("-SYNC")]
        border = [name for name in cables if name not in sync and "fw-" in name]
        # the pair's sync cable and the two firewalls' two uplinks each to the border-spine, all under the DC
        assert len(sync) == 1 and len(border) == 4
        assert all(data["deployment"] == {"id": "dc-1"} for data in cables.values() if "fw-" in data["name"])
        assert plan.root_id == "pod-1"

    async def test_a_pod_without_service_entries_plans_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        plan = await _plan_of(monkeypatch, _border_spine_payload(entries=[]), ports={"tmpl-bs": _BORDER_SPINE_PORTS})

        assert not [spec for spec in _nodes(plan, "DcimPhysicalDevice") if spec.data["role"] != "border-spine"]
        assert _nodes(plan, "ManagedFirewallHA") == []

    async def test_a_pod_whose_spines_are_not_border_spines_plans_no_services(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        payload = _payload()
        payload["TopologyPod"][0]["fabric_templates"].append(
            {"role": "internal-firewall", "quantity": 2, "template": {"id": "tmpl-fw", "interfaces": []}}
        )

        plan = await _plan_of(monkeypatch, payload, ports={"tmpl-fw": _FW_PORTS})

        assert _nodes(plan, "ManagedFirewallHA") == []

    async def test_a_border_spine_without_firewall_ports_fails_before_any_write(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        payload = _border_spine_payload(
            entries=[{"role": "internal-firewall", "quantity": 2, "template": {"id": "tmpl-fw", "interfaces": []}}]
        )
        gen = _generator(monkeypatch, ports={"tmpl-bs": _SPINE_PORTS, "tmpl-fw": _FW_PORTS})

        with pytest.raises(GeneratorError, match="firewall_ports=0"):
            await gen.generate(payload)

        gen.apply_plan.assert_not_awaited()

    async def test_the_pairs_hold_their_locks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        payload = _border_spine_payload(
            entries=[{"role": "internal-firewall", "quantity": 2, "template": {"id": "tmpl-fw", "interfaces": []}}]
        )
        gen = _generator(monkeypatch, ports={"tmpl-bs": _BORDER_SPINE_PORTS, "tmpl-fw": _FW_PORTS})
        gen.apply_plan = AsyncMock(return_value=MagicMock())

        await gen.generate(payload)

        assert "firewall-ha-group-pool" in gen.locked_keys and any(
            key.startswith("ha-domain-fw-") for key in gen.locked_keys
        )
