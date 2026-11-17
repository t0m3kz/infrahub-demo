"""The routing of a rack inside its plan, written through the reconciler: the AS before the processes, the processes before
the peerings that name them, the spines' own processes and ports found where another generator left them, a re-run that
changes nothing.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from generators.capabilities.bgp import RoutingReads, SharedFacts
from generators.dc.rack_plan import RackReads
from generators.dc.routing_inputs import RoutingInputs
from generators.plan import NodeSpec
from tests.unit.plan_graph_helpers import HfidClient, wire_hfid_generator
from tests.unit.rack_generator_helpers import build_rack_generator, rack_reads

_SPINES = ["dc1-pod1-spine-01", "dc1-pod1-spine-02"]
_SFP = "10gbase-x-sfpp"


def _routing(**overrides: Any) -> RoutingInputs:
    reads = RoutingReads(
        loopbacks={spine: (_hfid("DcimVirtualInterface", spine, "Loopback0"), f"ip-{spine}") for spine in _SPINES},
        overlay=frozenset(_SPINES),
        **overrides,
    )
    facts = SharedFacts(
        overlay_as_id="as-overlay",
        underlay_password_id="pw-u",
        overlay_password_id="pw-o",
        evpn_address_family_id="af-evpn",
    )
    return RoutingInputs(reads=reads, facts=facts)


def _hfid(kind: str, *parts: str) -> Any:
    from generators.plan import Hfid

    return Hfid(kind, parts)


def _generator(strategy: str, routing: RoutingInputs) -> tuple[Any, RackReads]:
    gen = build_rack_generator()
    gen._routing_options = {"design": {"routing_strategy": strategy}, "asn_pool": "asn-pool"}
    gen.calculate_cabling_offsets = lambda **_: 0
    gen.data["tors"] = []
    base = rack_reads()
    reads = replace(base, routing=routing)
    return gen, reads


def _client(gen: Any, plan: Any) -> HfidClient:
    """The spines another generator wrote: their ports, loopbacks and BGP processes."""
    client = HfidClient()
    for spine in _SPINES:
        device = client.seed("DcimPhysicalDevice", name=spine)
        for port in ("Eth1/10", "Eth1/11"):
            client.seed("DcimPhysicalInterface", name=port, device={"id": device.id}, interface_type=_SFP)
        client.seed("DcimVirtualInterface", name="Loopback0", device={"id": device.id})
        for role in ("underlay", "overlay"):
            client.seed("ManagedBGP", name=f"{spine}-bgp-{role}")
    for spec in plan.of(NodeSpec):
        if spec.kind == "DcimPhysicalDevice":
            client.template_ports[spec.data["name"]] = [
                {"name": port, "interface_type": _SFP} for port in ("Eth1/1", "Eth1/2")
            ]
    return client


def _kinds(client: HfidClient) -> list[str]:
    return [kind for kind, *_ in client.saves]


@pytest.mark.asyncio
class TestEbgpRack:
    async def test_the_as_comes_before_the_processes_and_the_processes_before_the_peerings(self) -> None:
        gen, reads = _generator("ebgp-ibgp", _routing())
        plan = gen._plan_rack(reads, [])
        client = _client(gen, plan)
        wire_hfid_generator(gen, client)

        await gen.apply_plan(plan)

        kinds = _kinds(client)
        last_system = max(i for i, kind in enumerate(kinds) if kind == "RoutingAutonomousSystem")
        first_process = kinds.index("ManagedBGP")
        last_process = max(i for i, kind in enumerate(kinds) if kind == "ManagedBGP")
        assert last_system < first_process <= last_process < kinds.index("ManagedBGPPeering")
        # one AS per leaf (no MLAG pair here), drawn from the DC's pool
        systems = [data for kind, _, data, _ in client.saves if kind == "RoutingAutonomousSystem"]
        assert len(systems) == 2 and all(data["asn"] == {"from_pool": {"id": "asn-pool"}} for data in systems)

    async def test_the_peerings_point_at_the_stored_processes_and_ports_of_both_ends(self) -> None:
        gen, reads = _generator("ebgp-ibgp", _routing())
        plan = gen._plan_rack(reads, [])
        client = _client(gen, plan)
        wire_hfid_generator(gen, client)

        await gen.apply_plan(plan)

        names = {node.id: node.data["name"] for node in client.store.values() if "name" in node.data}
        underlay = [data for kind, _, data, _ in client.saves if kind == "ManagedBGPPeering"]
        assert len(underlay) == 4 + 4  # four cables, four EVPN sessions (every leaf with every spine)
        first = next(data for data in underlay if data["name"].startswith("underlay--"))
        assert {names[ref["id"]] for ref in first["bgp_processes"]} == {
            f"{first['name'].split('--')[1]}-bgp-underlay",
            f"{first['name'].split('--')[3]}-bgp-underlay",
        }
        assert len(first["capabilities"]) == 2 and all(ref["id"] in client.store for ref in first["capabilities"])

    async def test_every_object_of_the_rack_flow_is_owned_by_the_run_or_shared(self) -> None:
        gen, reads = _generator("ebgp-ibgp", _routing())
        plan = gen._plan_rack(reads, [])

        written = [spec for spec in plan.of(NodeSpec) if spec.kind.startswith(("ManagedBGP", "RoutingAuto"))]
        assert written and {spec.ownership for spec in written} == {"owned"}

    async def test_a_rerun_with_the_as_it_found_writes_the_same_objects(self) -> None:
        gen, reads = _generator("ebgp-ibgp", _routing())
        plan = gen._plan_rack(reads, [])
        client = _client(gen, plan)
        wire_hfid_generator(gen, client)
        await gen.apply_plan(plan)
        systems = {
            node.data["description"].split()[0]: node.id
            for node in client.store.values()
            if node.kind == "RoutingAutonomousSystem"
        }
        process_snapshot = {
            node.id: dict(node.data) for node in client.store.values() if node.kind.startswith("ManagedBGP")
        }

        rerun, reads_again = _generator(
            "ebgp-ibgp",
            _routing(
                underlay_as={
                    f"{name}-bgp-underlay".removesuffix("-bgp-underlay"): as_id for name, as_id in systems.items()
                }
            ),
        )
        again = rerun._plan_rack(reads_again, [])
        await wire_hfid_generator(rerun, client).apply_plan(again)

        assert not [spec for spec in again.of(NodeSpec) if spec.kind == "RoutingAutonomousSystem"]
        assert {
            node.id: dict(node.data) for node in client.store.values() if node.kind.startswith("ManagedBGP")
        } == process_snapshot

    async def test_an_mlag_pair_shares_one_as_and_the_domain_is_paired_after_the_writes(self) -> None:
        gen, reads = _generator("ebgp-ibgp", _routing())
        gen.data["deployment"]["mlag_create"] = "virtual"
        plan = gen._plan_rack(reads, [])

        systems = [spec for spec in plan.of(NodeSpec) if spec.kind == "RoutingAutonomousSystem"]
        assert len(systems) == 1 and len(gen._mlag_groups) == 1


@pytest.mark.asyncio
class TestOspfRack:
    async def test_the_ospf_underlay_needs_the_shared_area_and_writes_interfaces_before_the_peerings(self) -> None:
        routing = _routing()
        routing = RoutingInputs(
            reads=routing.reads,
            facts=SharedFacts(
                overlay_as_id="as-overlay",
                ospf_area_id="area-0",
                underlay_password_id="pw-u",
                overlay_password_id="pw-o",
                evpn_address_family_id="af-evpn",
            ),
        )
        gen, reads = _generator("ospf-ibgp", routing)
        plan = gen._plan_rack(reads, [])
        client = _client(gen, plan)
        for spine in _SPINES:
            client.seed("ManagedOSPF", name=f"{spine}-ospf-underlay")
        wire_hfid_generator(gen, client)

        await gen.apply_plan(plan)

        kinds = _kinds(client)
        assert kinds.index("RoutingOSPFInterface") < kinds.index("ManagedOSPFPeering")
        assert "RoutingAutonomousSystem" not in kinds
        assert kinds.count("ManagedOSPFPeering") == 4


class TestNoRoutingDesign:
    def test_a_rack_without_a_routing_design_is_cabled_and_not_routed(self) -> None:
        gen, reads = _generator("ebgp-ibgp", _routing())
        gen._routing_options = {}

        plan = gen._plan_rack(reads, [])

        assert not [spec for spec in plan.of(NodeSpec) if spec.kind.startswith("ManagedBGP")]
        assert [spec for spec in plan.of(NodeSpec) if spec.kind == "DcimCable"]
