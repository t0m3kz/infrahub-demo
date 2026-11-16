"""The rack's leaf and ToR flow as one plan: built from the cleaned data and what exists before the run, written by the
reconciler in dependency order (generators/dc/rack.py, generators/dcim/device_plan.py, cabling_plan.py).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from generators.capabilities.mlag import MlagReads
from generators.dc.rack_plan import RackReads
from generators.dcim.device_plan import DeviceReads, ExistingDevice
from generators.dcim.template_interfaces import TemplatePort
from generators.helpers.cable_attributes import INTRA_RACK_CM, RackPosition
from generators.model.errors import GeneratorError
from generators.plan import (
    AddressAllocationSpec,
    EdgesSpec,
    Hfid,
    LinkPrefixSpec,
    NodeRef,
    NodeSpec,
    TopologyPlan,
)
from generators.protocols import ManagedController
from tests.unit.plan_graph_helpers import HfidClient, wire_hfid_generator
from tests.unit.rack_generator_helpers import build_rack_generator, rack_reads
from utils.limits import MLAG_CONTROL_IPV4_POOL

_SPINES = ["dc1-pod1-spine-01", "dc1-pod1-spine-02"]
_UNTRACKED = {"allow_upsert": True, "update_group_context": False}
_TRACKED = {"allow_upsert": True}


class _EveryDeviceInThisRack(dict[str, RackPosition | None]):
    """The one position every device of the test sits at (the same rack: a cable of INTRA_RACK_CM)."""

    def get(self, key: str, default: Any = None) -> RackPosition:  # ty: ignore[invalid-method-override]
        return RackPosition("suite-1", 1, 1, 2, 8, 60, 120)


def _leaf_plan(gen: Any, reads: RackReads | None = None) -> tuple[TopologyPlan, list[str]]:
    gen.calculate_cabling_offsets = MagicMock(return_value=0)
    plan = TopologyPlan(root_id="rack-1")
    created: list[str] = []
    gen._plan_leafs(plan, reads or rack_reads(), created)
    return plan, created


def _by_kind(plan: TopologyPlan, kind: str) -> list[NodeSpec]:
    return [spec for spec in plan.of(NodeSpec) if spec.kind == kind]


class TestDevices:
    def test_a_device_carries_its_templates_owner_when_the_payload_names_none(self) -> None:
        """An upsert by HFID is validated as a create, so the owner a create from the template would copy is sent."""
        gen = build_rack_generator()

        plan, created = _leaf_plan(gen, rack_reads(template_owners={"tmpl-leaf": "org-partner"}))

        device = next(spec for spec in _by_kind(plan, "DcimPhysicalDevice") if spec.data["name"] == created[0])
        assert device.data["owner"] == {"id": "org-partner"}

    def test_a_device_of_a_template_without_owner_sends_none(self) -> None:
        """No owner on the payload nor on the template: nothing is sent (the schema decides whether that is valid)."""
        gen = build_rack_generator()

        plan, created = _leaf_plan(gen)

        device = next(spec for spec in _by_kind(plan, "DcimPhysicalDevice") if spec.data["name"] == created[0])
        assert "owner" not in device.data

    def test_a_leaf_is_a_tracked_device_that_provides_its_templates_ports_with_its_loopback_and_addresses(self) -> None:
        gen = build_rack_generator()

        plan, created = _leaf_plan(gen)

        device = next(spec for spec in _by_kind(plan, "DcimPhysicalDevice") if spec.data["name"] == created[0])
        assert device.ownership == "owned" and device.hfid == Hfid("DcimPhysicalDevice", (created[0],))
        assert device.provides == (
            Hfid("DcimPhysicalInterface", (created[0], "Eth1/1")),
            Hfid("DcimPhysicalInterface", (created[0], "Eth1/2")),
        )
        assert device.data["object_template"] == {"id": "tmpl-leaf"}
        assert device.data["role"] == "leaf" and device.data["rack"] == {"id": "rack-1"}
        loopback = next(spec for spec in _by_kind(plan, "DcimVirtualInterface") if spec.key == f"loopback:{created[0]}")
        assert (
            loopback.ownership == "owned"
            and loopback.data["device"] == device.hfid
            and loopback.data["role"] == "loopback"
        )
        allocations = {spec.key: spec for spec in plan.of(AddressAllocationSpec)}
        assert allocations[f"management:{created[0]}"].identifier == created[0]
        assert allocations[f"management:{created[0]}"].pool_id == "mgmt-pool"
        assert allocations[f"loopback-address:{created[0]}"].pool_id == "lo-pool"
        assert allocations[f"loopback-address:{created[0]}"].prefix_length == 32
        assert device.data["primary_address"] == NodeRef(f"management:{created[0]}")
        assert loopback.data["ip_address"] == NodeRef(f"loopback-address:{created[0]}")

    def test_an_existing_device_keeps_its_address_and_groups_and_is_not_given_its_template_again(self) -> None:
        gen = build_rack_generator()
        _, names = _leaf_plan(gen)
        reads = rack_reads(
            devices=DeviceReads(
                devices={names[0]: ExistingDevice(group_ids=("g-old",), primary_address_id="ip-1")},
                loopback_address_ids={names[0]: "ip-lo"},
                groups={"leafs": "g-leafs"},
            )
        )
        gen2 = build_rack_generator()

        plan, _ = _leaf_plan(gen2, reads)

        device = next(spec for spec in _by_kind(plan, "DcimPhysicalDevice") if spec.data["name"] == names[0])
        assert "object_template" not in device.data
        assert device.data["primary_address"] == "ip-1"
        assert device.data["member_of_groups"] == [{"id": "g-old"}, {"id": "g-leafs"}]
        assert f"management:{names[0]}" not in {spec.key for spec in plan.of(AddressAllocationSpec)}
        assert f"loopback-address:{names[0]}" not in {spec.key for spec in plan.of(AddressAllocationSpec)}
        loopback = next(spec for spec in _by_kind(plan, "DcimVirtualInterface") if spec.key == f"loopback:{names[0]}")
        assert loopback.data["ip_address"] == "ip-lo"

    def test_a_missing_role_group_is_created_once_untracked_and_joined(self) -> None:
        plan, _ = _leaf_plan(build_rack_generator())

        (group,) = _by_kind(plan, "CoreStandardGroup")
        assert group.data["name"] == "leafs" and group.ownership == "shared"
        members = {
            tuple(device.data["member_of_groups"][0].values()) for device in _by_kind(plan, "DcimPhysicalDevice")
        }
        assert members == {(NodeRef("group:leafs"),)}

    def test_a_controller_manages_the_devices_instead_of_the_role_group(self) -> None:
        gen = build_rack_generator()
        gen._controllers = [{"id": "ctl-1", "controller_type": "aci_apic"}]

        plan, created = _leaf_plan(gen)

        assert _by_kind(plan, "CoreStandardGroup") == []
        assert {tuple(device.data["member_of_groups"]) for device in _by_kind(plan, "DcimPhysicalDevice")} == {()}
        (edges,) = plan.of(EdgesSpec)
        assert (edges.node, edges.relation, edges.kind) == ("ctl-1", "managed_devices", ManagedController)
        assert edges.peers == tuple(NodeRef(f"device:{name}") for name in created)

    def test_a_pod_with_mlag_pairs_its_leafs_after_the_writes(self) -> None:
        gen = build_rack_generator()
        gen.data["deployment"]["mlag_create"] = "back-to-back"

        _, created = _leaf_plan(gen)

        (group,) = gen._mlag_groups
        assert (group.names, group.role, group.mlag_create) == (tuple(created), "leaf", "back-to-back")


class TestOwnership:
    def test_every_spec_the_rack_flow_builds_states_who_owns_it(self) -> None:
        gen = build_rack_generator()
        gen._controllers = []

        plan, _ = _leaf_plan(gen)

        written = [spec for spec in plan.specs if isinstance(spec, NodeSpec | AddressAllocationSpec | LinkPrefixSpec)]
        assert written and all(spec.ownership is not None and not getattr(spec, "track", False) for spec in written)
        by_kind: dict[str, set[str | None]] = {}
        for spec in written:
            label = str(spec.kind) if isinstance(spec, NodeSpec) else type(spec).__name__
            by_kind.setdefault(label, set()).add(spec.ownership)
        assert by_kind == {
            "DcimPhysicalDevice": {"owned"},
            "DcimVirtualInterface": {"owned"},
            "DcimCable": {"owned"},
            "DcimPhysicalInterface": {"template"},
            "CoreStandardGroup": {"shared"},
            "AddressAllocationSpec": {"shared"},
            "LinkPrefixSpec": {"owned"},
        }

    def test_a_shared_or_template_node_cannot_ask_to_be_tracked(self) -> None:
        with pytest.raises(ValueError, match="asks to be tracked"):
            NodeSpec("n", "K", {}, track=True, ownership="template")

    def test_an_allocated_address_is_the_pools_and_is_never_owned(self) -> None:
        with pytest.raises(ValueError, match="the pool owns an allocated address"):
            AddressAllocationSpec("a", "id", 32, "d", ownership="owned", pool_id="p")


class TestCabling:
    def test_each_leaf_cables_to_every_spine_and_the_cable_is_named_by_its_sorted_ends(self) -> None:
        gen = build_rack_generator()

        plan, created = _leaf_plan(gen, rack_reads(positions=_EveryDeviceInThisRack()))

        cables = _by_kind(plan, "DcimCable")
        assert len(cables) == 4
        first = cables[0]
        # Leaf ports sort top-down (the pod's setting) and index by spine: the first leaf's Eth1/2 goes to spine 1
        assert first.data["name"] == f"{_SPINES[0]}-Eth1/10__{created[0]}-Eth1/2"
        assert first.ownership == "owned" and first.data["type"] == "mmf"
        assert first.data["endpoints"] == [
            Hfid("DcimPhysicalInterface", (created[0], "Eth1/2")),
            Hfid("DcimPhysicalInterface", (_SPINES[0], "Eth1/10")),
        ]
        assert first.data["deployment"] == {"id": gen.deployment_id} or first.data["deployment"] is None
        assert first.data["length_cm"] == INTRA_RACK_CM and first.data["cable_id"].startswith("CBL-")

    def test_a_link_has_one_p2p_prefix_from_the_pods_pool_and_both_ports_take_its_addresses_untracked(self) -> None:
        gen = build_rack_generator()

        plan, created = _leaf_plan(gen)

        prefix = next(spec for spec in plan.of(LinkPrefixSpec) if created[0] in spec.key and _SPINES[0] in spec.key)
        assert (prefix.pool_id, prefix.prefix_length) == ("p2p-pool", 31)
        ends = {(hfid.parts) for hfid in prefix.ends}
        assert ends == {(created[0], "Eth1/2"), (_SPINES[0], "Eth1/10")}
        ports = {spec.hfid.parts: spec for spec in _by_kind(plan, "DcimPhysicalInterface") if spec.hfid}
        bottom, top = ports[(created[0], "Eth1/2")], ports[(_SPINES[0], "Eth1/10")]
        assert bottom.existing and top.existing
        assert (bottom.ownership, top.ownership) == ("template", "template")
        assert prefix.ownership == "owned"
        assert bottom.data["ip_address"] == NodeRef(prefix.address_keys[0])
        assert top.data["ip_address"] == NodeRef(prefix.address_keys[1])
        assert bottom.data["status"] == "active" and bottom.data["description"] == top.data["description"]

    def test_a_pod_without_a_technical_pool_cables_without_addresses(self) -> None:
        gen = build_rack_generator()
        gen._technical_pool_id = None

        plan, _ = _leaf_plan(gen)

        assert plan.of(LinkPrefixSpec) == []
        assert all("ip_address" not in spec.data for spec in _by_kind(plan, "DcimPhysicalInterface"))

    def test_the_offset_picks_the_spine_port_the_rack_starts_at(self) -> None:
        gen = build_rack_generator()
        gen._spine_device_names = ["spine-01"]
        gen.calculate_cabling_offsets = MagicMock(return_value=1)
        plan = TopologyPlan(root_id="rack-1")

        gen._plan_tors(plan, rack_reads())

        (cable,) = _by_kind(plan, "DcimCable")
        assert cable.data["endpoints"][1] == Hfid("DcimPhysicalInterface", ("spine-01", "Eth1/11"))

    def test_the_spine_layer_is_a_layer_to_route_with_the_cables_it_got(self) -> None:
        gen = build_rack_generator()

        _, created = _leaf_plan(gen)

        (layer,) = gen._layers
        assert (layer.bottom_role, layer.top_role, layer.top_devices) == ("leaf", "spine", tuple(_SPINES))
        assert len(layer.links) == 4 and gen._problems == []

    def test_a_port_the_spine_template_lacks_fails_the_plan_naming_every_problem_before_any_write(self) -> None:
        gen = build_rack_generator()
        reads = rack_reads(template_ports={**rack_reads().template_ports, "tmpl-spine": []})

        with pytest.raises(GeneratorError) as raised:
            gen._plan_rack(reads, [])

        message = str(raised.value)
        assert message.count("spine template has no port ['Eth1/10', 'Eth1/11']") == 1
        assert "nothing is written" in message

    def test_a_port_the_leaf_template_lacks_fails_the_plan_naming_it_instead_of_a_pairing_error(self) -> None:
        gen = build_rack_generator()
        reads = rack_reads(
            template_ports={
                **rack_reads().template_ports,
                "tmpl-leaf": [TemplatePort("Eth1/1", "uplink", "10gbase-x-sfpp")],
            }
        )

        gen.data["tors"] = []

        with pytest.raises(GeneratorError, match=r"leaf template has no port \['Eth1/2'\]"):
            gen._plan_rack(reads, [])

        # the pairing is not attempted for a layer with a missing port: only the ports are named
        assert gen._layers[0].links == ()


def _wired(gen: Any, client: HfidClient) -> Any:
    wire_hfid_generator(gen, client)
    return gen


def _client_for(gen: Any, created: list[str]) -> HfidClient:
    """The spines another generator wrote and the ports each leaf's template brings."""
    client = HfidClient()
    client.seed("CoreStandardGroup", name="mlag_domains")
    for spine in _SPINES:
        device = client.seed("DcimPhysicalDevice", name=spine)
        for port in ("Eth1/10", "Eth1/11"):
            client.seed("DcimPhysicalInterface", name=port, device={"id": device.id}, interface_type="10gbase-x-sfpp")
    for name in created:
        client.template_ports[name] = [
            {"name": port, "interface_type": "10gbase-x-sfpp"} for port in ("Eth1/1", "Eth1/2")
        ]
    return client


class TestApplied:
    @pytest.mark.asyncio
    async def test_the_plan_writes_devices_then_ports_then_cables_and_a_rerun_changes_nothing(self) -> None:
        gen = build_rack_generator()
        plan, created = _leaf_plan(gen)
        client = _client_for(gen, created)
        _wired(gen, client)

        result = await gen.apply_plan(plan)

        kinds = [kind for kind, *_ in client.saves]
        assert kinds.index("DcimCable") > max(i for i, kind in enumerate(kinds) if kind == "DcimPhysicalInterface")
        assert kinds.index("DcimPhysicalDevice") < kinds.index("DcimPhysicalInterface")
        assert len([kind for kind in kinds if kind == "DcimCable"]) == 4
        tracked = {kind for kind, _, _, kwargs in client.saves if kwargs == _TRACKED}
        untracked = {kind for kind, _, _, kwargs in client.saves if kwargs == _UNTRACKED}
        assert tracked == {"DcimPhysicalDevice", "DcimVirtualInterface", "DcimCable"}
        assert untracked == {"DcimPhysicalInterface", "CoreStandardGroup"}
        assert all(result.ids[Hfid("DcimPhysicalDevice", (name,))] for name in created)

        snapshot = {node_id: dict(node.data) for node_id, node in client.store.items()}
        created_before = list(client.created_ids)
        again, _ = _leaf_plan(build_rack_generator())
        await _wired(build_rack_generator(), client).apply_plan(again)

        assert {node_id: dict(node.data) for node_id, node in client.store.items()} == snapshot
        assert client.created_ids == created_before

    @pytest.mark.asyncio
    async def test_the_mlag_pair_is_planned_with_its_devices_and_a_rerun_changes_nothing(self) -> None:
        gen = build_rack_generator()
        gen.data["deployment"]["mlag_create"] = "virtual"
        gen.data["leafs"][0]["template"]["platform"] = {"name": "sonic"}
        reads = rack_reads(mlag=MlagReads(control_pools=frozenset({MLAG_CONTROL_IPV4_POOL})))
        plan, created = _leaf_plan(gen, reads)
        gen._plan_mlag(plan, reads)
        client = _client_for(gen, created)
        _wired(gen, client)

        result = await gen.apply_plan(plan)

        (domain,) = [node for node in client.store.values() if node.kind == "ManagedMLAG"]
        assert domain.data["name"] == f"{created[0]}-{created[1]}-mlag" and domain.data["virtual_peer_link"] is True
        assert [member["id"] for member in domain.data["capabilities"]] == [
            result.ids[Hfid("DcimPhysicalDevice", (name,))] for name in created
        ]
        assert [
            node.data["name"]
            for node in client.store.values()
            if node.kind == "DcimVirtualInterface" and node.data.get("role") == "mlag-peer"
        ] == ["Loopback100"] * 2
        # the control prefix is keyed by the id the domain got in this very run
        assert [identifier for _, identifier in client.allocations if identifier.startswith("mlag-control")] == [
            f"mlag-control__{domain.id}"
        ]
        saved = list(client.created_ids)
        snapshot = {node_id: dict(node.data) for node_id, node in client.store.items() if node.kind != "IpamIPAddress"}

        await gen.apply_plan(plan)

        assert client.created_ids == saved
        assert {
            node_id: dict(node.data) for node_id, node in client.store.items() if node.kind != "IpamIPAddress"
        } == snapshot


def client_device_id(client: HfidClient, name: str) -> str:
    return next(
        node.id for node in client.store.values() if node.kind == "DcimPhysicalDevice" and node.data["name"] == name
    )


class TestWholePlan:
    def test_the_plan_is_rooted_at_the_rack_and_waits_for_the_pod_generators_not_for_ports(self) -> None:
        gen = build_rack_generator()
        # The leaf pair starts at the spines' first port, the ToR at the next one
        gen.calculate_cabling_offsets = MagicMock(side_effect=[0, 1])
        gen._spine_device_names = _SPINES[:1]
        gen.data["leafs"][0]["quantity"] = 1

        plan = gen._plan_rack(rack_reads(), [])

        assert plan.root_id == "rack-1"
        assert [(producer.generators, producer.parent_id) for producer in plan.producers] == [
            (("add_pod", "pod_rack_cascade"), "pod-1")
        ]
        plan.validate()
        # A leaf and a ToR, each cabled to the spine: every cable, port and address is in the one plan
        assert len(_by_kind(plan, "DcimPhysicalDevice")) == 2 and len(_by_kind(plan, "DcimCable")) == 2

    @pytest.mark.asyncio
    async def test_the_reads_cover_the_devices_the_templates_and_where_everything_sits(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("generators.dc.routing_inputs.asyncio.sleep", AsyncMock())
        gen = build_rack_generator()
        # The pod's generators are not running: the routing read asks them between its attempts
        gen.wait_for_parent_generators = AsyncMock(return_value=False)
        seen: list[tuple[str, dict[str, Any]]] = []
        leaf_port = MagicMock()
        leaf_port.name.value, leaf_port.role.value, leaf_port.interface_type.value = (
            "Eth1/1",
            "uplink",
            "10gbase-x-sfpp",
        )
        template = MagicMock(id="tmpl-leaf")
        template.interfaces.peers = [MagicMock(peer=leaf_port)]

        async def filters(kind: Any, **flt: Any) -> list[Any]:
            seen.append((getattr(kind, "__name__", kind), flt))
            return [template] if kind == "TemplateDcimPhysicalDevice" else []

        gen.client.filters = filters

        reads = await gen._read_plan_inputs()

        kinds = [kind for kind, _ in seen]
        assert {"DcimVirtualInterface", "ManagedBGP", "ManagedMLAG"} <= set(kinds)  # the routing state
        assert kinds.count("DcimPhysicalDevice") == 2  # the devices of earlier runs, the spines
        assert reads.template_ports["tmpl-leaf"][0].name == "Eth1/1"
        template_read = next(flt for kind, flt in seen if kind == "TemplateDcimPhysicalDevice")
        assert template_read["ids"] == ["tmpl-leaf", "tmpl-spine", "tmpl-tor"]
        assert set(reads.positions) >= {"lf-dc11111201"}

    @pytest.mark.asyncio
    async def test_a_rack_without_leafs_and_tors_reads_nothing(self) -> None:
        gen = build_rack_generator()
        gen.data["leafs"], gen.data["tors"] = [], []
        gen.client.filters = AsyncMock(side_effect=AssertionError("nothing to plan, nothing to read"))

        assert await gen._read_plan_inputs() == RackReads()
