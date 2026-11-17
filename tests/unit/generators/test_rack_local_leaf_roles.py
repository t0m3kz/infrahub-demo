"""The l2-leaf and access-leaf stage of the rack plan (generators/dc/rack.py): planned from the leafs of the same plan
(or the row's leafs a compute rack reads), in the one plan the reconciler writes.
"""

from __future__ import annotations

from typing import Any, Literal, cast
from unittest.mock import MagicMock

import pytest

from generators.dc.rack import RackGenerator
from generators.dc.rack_data import TopologyRackData
from generators.dc.rack_plan import RackReads, RowLeafs
from generators.dcim.cabling_plan import PortFacts
from generators.dcim.template_interfaces import TemplatePort
from generators.model.errors import GeneratorError
from generators.plan import Hfid, LinkPrefixSpec, NodeSpec, TopologyPlan
from tests.unit.plan_graph_helpers import HfidClient, wire_hfid_generator
from tests.unit.simulators.fabric_designs import pod_design

_SFP = "10gbase-x-sfpp"
_L2_TEMPLATE = {"id": "tmpl-l2", "interfaces": [{"name": "Eth1/1", "role": "uplink"}]}
_ACCESS_TEMPLATE = {"id": "tmpl-access", "interfaces": [{"name": "Eth1/1", "role": "uplink"}]}
_LEAFS = ["leaf-01", "leaf-02"]
_ROW_PORTS = ("Eth1/20", "Eth1/21", "Eth1/22", "Eth1/23")
_SPINES = ["dc1-pod1-spine-01", "dc1-pod1-spine-02"]


def _build_gen(*, mlag_create: Literal["no", "back-to-back", "virtual"] = "no") -> Any:
    parent = {
        "id": "dc-1",
        "name": "DC1",
        "index": 1,
        "naming_convention": "standard",
        "management_pool": {"id": "mgmt-pool", "name": "mgmt"},
    }
    pod = {
        "id": "pod-1",
        "name": "pod-1",
        "index": 2,
        "parent": parent,
        "leaf_interface_sorting_method": "top_down",
        "spine_interface_sorting_method": "bottom_up",
        "mlag_create": mlag_create,
        "loopback_pool": {"id": "lo-pool", "name": "lo"},
        "prefix_pool": {"id": "p2p-pool", "name": "p2p"},
        "design": pod_design("S_MIXED"),
        "fabric_templates": [
            {
                "role": "spine",
                "quantity": 2,
                "template": {"id": "tmpl-spine", "interfaces": [{"name": "Eth1/10"}, {"name": "Eth1/11"}]},
            }
        ],
    }
    rack = {
        "id": "rack-1",
        "name": "RACK-1",
        "index": 1,
        "rack_type": "compute",
        "row_index": 1,
        "parent": {"index": 1},
        "deployment": pod,
        "l2_leafs": [{"role": "l2-leaf", "quantity": 2, "template": _L2_TEMPLATE}],
        "access_leafs": [{"role": "access-leaf", "quantity": 2, "template": _ACCESS_TEMPLATE}],
    }
    gen = RackGenerator.__new__(RackGenerator)
    gen._controllers = []
    gen.data = cast(TopologyRackData, rack)
    gen.logger = MagicMock()
    gen.client = MagicMock()
    gen.client.group_context = MagicMock(related_node_ids=[])
    gen.fabric_name = "dc1"
    gen.deployment_id = "dc-1"
    gen._naming_conv = "standard"
    gen._device_indexes = [1, 2, 1, 1, 1]
    gen._loopback_pool_id = "lo-pool"
    gen._management_pool_id = "mgmt-pool"
    gen._loopback_prefix_length = 32
    gen._spine_device_names = list(_SPINES)
    gen._spine_interfaces = ["Eth1/10", "Eth1/11"]
    gen._spine_role = "spine"
    gen._technical_pool_id = "p2p-pool"
    gen._p2p_prefix_length = 31
    gen._routing_options = {"design": {"routing_strategy": "ebgp-ebgp"}}
    gen._created_device_names = set()
    gen._layers = []
    gen._mlag_groups = []
    gen._problems = []
    gen._role_of = {}
    gen._leaf_ports = {}
    return gen


def _reads(**overrides: Any) -> RackReads:
    reads = RackReads(
        template_ports={
            "tmpl-l2": [TemplatePort("Eth1/1", "uplink", _SFP), TemplatePort("Eth1/2", "uplink", _SFP)],
            "tmpl-access": [TemplatePort("Eth1/1", "uplink", _SFP), TemplatePort("Eth1/2", "uplink", _SFP)],
        }
    )
    return RackReads(**{**reads.__dict__, **overrides})


def _row() -> RowLeafs:
    return RowLeafs(_LEAFS, [PortFacts(leaf, port, _SFP) for leaf in _LEAFS for port in _ROW_PORTS])


def _plan_compute(gen: Any, reads: RackReads | None = None) -> TopologyPlan:
    plan = TopologyPlan(root_id="rack-1")
    gen._plan_local_leaf_roles(plan, reads or _reads(row_leafs=_row()), [])
    return plan


def _by_kind(plan: TopologyPlan, kind: str) -> list[NodeSpec]:
    return [spec for spec in plan.of(NodeSpec) if spec.kind == kind]


class TestDevices:
    def test_the_l2_leaf_is_l2_only_and_cannot_pair_virtually(self) -> None:
        """An l2-leaf has no loopback, no routing and no EVPN: the pod's virtual MLAG falls back to back-to-back."""
        gen = _build_gen(mlag_create="virtual")

        plan = _plan_compute(gen)

        l2 = [spec for spec in _by_kind(plan, "DcimPhysicalDevice") if spec.data["role"] == "l2-leaf"]
        assert len(l2) == 2 and all(spec.data["rack"] == {"id": "rack-1"} for spec in l2)
        assert not [spec for spec in _by_kind(plan, "DcimVirtualInterface") if "l2" in spec.key]
        group = next(group for group in gen._mlag_groups if group.role == "l2-leaf")
        assert (group.mlag_create, group.supports_virtual) == ("virtual", False)

    def test_the_access_leaf_is_a_routed_vtep_that_can_pair_virtually(self) -> None:
        gen = _build_gen(mlag_create="virtual")

        plan = _plan_compute(gen)

        access = [
            spec.data["name"] for spec in _by_kind(plan, "DcimPhysicalDevice") if spec.data["role"] == "access-leaf"
        ]
        assert {spec.key for spec in _by_kind(plan, "DcimVirtualInterface")} == {f"loopback:{name}" for name in access}
        group = next(group for group in gen._mlag_groups if group.role == "access-leaf")
        assert (group.mlag_create, group.supports_virtual) == ("virtual", True)

    def test_no_mlag_create_pairs_nothing(self) -> None:
        gen = _build_gen(mlag_create="no")

        _plan_compute(gen)

        assert gen._mlag_groups == []

    def test_a_template_planned_twice_is_skipped(self) -> None:
        gen = _build_gen()
        plan = _plan_compute(gen)
        before = len(plan.specs)

        gen._plan_local_leaf_roles(plan, _reads(row_leafs=_row()), [])

        assert len(plan.specs) == before


class TestCabling:
    def test_the_l2_leaf_is_cabled_to_the_leafs_of_its_row_without_addresses(self) -> None:
        """l2-leaf <-> leaf links are 802.1Q trunks: no prefix, no address on either end."""
        gen = _build_gen()
        gen.data["access_leafs"] = []

        plan = _plan_compute(gen)

        cables = _by_kind(plan, "DcimCable")
        assert len(cables) == 2 and all(cable.ownership == "owned" for cable in cables)
        assert plan.of(LinkPrefixSpec) == []
        assert all("ip_address" not in port.data for port in _by_kind(plan, "DcimPhysicalInterface"))
        assert gen._layers == []

    def test_the_access_leaf_links_get_the_pods_technical_pool(self) -> None:
        gen = _build_gen()
        gen.data["l2_leafs"] = []

        plan = _plan_compute(gen)

        prefixes = plan.of(LinkPrefixSpec)
        assert len(prefixes) == 2 and {(spec.pool_id, spec.prefix_length) for spec in prefixes} == {("p2p-pool", 31)}

    def test_the_access_leaf_is_routed_to_the_leafs_and_peers_evpn_with_the_spines(self) -> None:
        gen = _build_gen()
        gen.data["l2_leafs"] = []

        _plan_compute(gen)

        local, overlay = gen._layers
        assert (local.bottom_role, local.top_role, local.top_devices) == ("access-leaf", "leaf", tuple(_LEAFS))
        assert len(local.links) == 2 and not local.overlay_only
        # the overlay peering to the spines: no underlay, no links
        assert overlay.overlay_only and overlay.links == () and overlay.top_devices == tuple(_SPINES)
        assert (overlay.top_role, overlay.bottom_role) == ("spine", "access-leaf")

    def test_a_compute_rack_starts_at_its_own_slot_of_the_rows_leaf_ports(self) -> None:
        gen = _build_gen()
        gen.data["index"] = 2
        gen.data["access_leafs"] = []

        plan = _plan_compute(gen)

        # rack 2 of the row: offset (2 - 1) * 2 devices; the first l2-leaf's first uplink goes to the third leaf port
        first = _by_kind(plan, "DcimCable")[0]
        assert Hfid("DcimPhysicalInterface", ("leaf-01", "Eth1/22")) in first.data["endpoints"]

    def test_a_network_rack_cables_to_the_leafs_of_the_same_plan(self) -> None:
        gen = _build_gen()
        gen.data["access_leafs"] = []
        gen._leaf_ports = {
            leaf: [TemplatePort("Eth1/30", "downlink", _SFP), TemplatePort("Eth1/31", "downlink", _SFP)]
            for leaf in _LEAFS
        }
        plan = TopologyPlan(root_id="rack-1")

        gen._plan_local_leaf_roles(plan, _reads(), list(_LEAFS))

        ends = {hfid for cable in _by_kind(plan, "DcimCable") for hfid in cable.data["endpoints"]}
        assert {hfid.parts for hfid in ends if hfid.parts[0] in _LEAFS} == {
            ("leaf-01", "Eth1/30"),
            ("leaf-01", "Eth1/31"),
        }

    def test_a_rack_without_a_row_leaf_to_cable_to_fails_before_any_write(self) -> None:
        gen = _build_gen()

        with pytest.raises(GeneratorError, match="No middle-rack leafs in row 1"):
            gen._plan_rack(_reads(), [])

    def test_the_plan_is_deterministic(self) -> None:
        first, second = (_plan_compute(_build_gen(mlag_create="back-to-back")) for _ in range(2))

        assert first.specs == second.specs


@pytest.mark.asyncio
class TestApplied:
    async def test_the_devices_are_written_before_the_cables_that_need_their_ports_and_a_rerun_changes_nothing(
        self,
    ) -> None:
        gen = _build_gen()
        gen.data["access_leafs"] = []
        plan = _plan_compute(gen)
        client = HfidClient()
        for leaf in _LEAFS:
            device = client.seed("DcimPhysicalDevice", name=leaf)
            for port in _ROW_PORTS:
                client.seed("DcimPhysicalInterface", name=port, device={"id": device.id}, interface_type=_SFP)
        for spec in _by_kind(plan, "DcimPhysicalDevice"):
            client.template_ports[spec.data["name"]] = [
                {"name": port, "interface_type": _SFP} for port in ("Eth1/1", "Eth1/2")
            ]
        wire_hfid_generator(gen, client)

        await gen.apply_plan(plan)

        kinds = [kind for kind, *_ in client.saves]
        assert kinds.index("DcimPhysicalDevice") < kinds.index("DcimCable")
        assert len([kind for kind in kinds if kind == "DcimCable"]) == 2
        snapshot = {node_id: dict(node.data) for node_id, node in client.store.items()}
        rerun = _build_gen()
        rerun.data["access_leafs"] = []
        again = _plan_compute(rerun)
        gen2 = wire_hfid_generator(_build_gen(), client)
        await gen2.apply_plan(again)

        assert {node_id: dict(node.data) for node_id, node in client.store.items()} == snapshot
        assert client.group_context.related_node_ids == ["rack-1"]
