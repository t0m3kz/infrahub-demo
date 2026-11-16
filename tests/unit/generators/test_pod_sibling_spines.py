"""A pod finds its lower-index siblings' spine-slot devices by name, not by deployment."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from generators.base import BaseGenerator
from generators.dc import pod as pod_module
from generators.dc import pod_mesh as pod_mesh_module
from generators.dc.pod import PodTopologyGenerator
from generators.dc.pod_mesh import read_sibling_spines
from generators.dcim.template_interfaces import TemplatePort
from generators.plan import TopologyPlan
from generators.protocols import DcimPhysicalDevice, DcimPhysicalInterface
from utils.limits import WAIT_FOR_PEER_GENERATOR

_DC_ID = "dc-7"
_UPLINKS = [f"Ethernet{n}/1" for n in range(17, 25)]

# DC7's S design hosts several pod designs; the widest holds 4 spines.
_DC = {
    "id": _DC_ID,
    "name": "DC7",
    "index": 7,
    "naming_convention": "hierarchical",
    "design": {
        "name": "S",
        "pod_templates": [
            {"name": "S_BORDER_SPINE_POD", "max_spines_per_pod": 2},
            {"name": "S_POD", "max_spines_per_pod": 4},
        ],
    },
}


def _device(name: str, *, role: str, deployment_id: str) -> MagicMock:
    device = MagicMock()
    device.name.value = name
    device.role.value = role
    device.deployment_id = deployment_id
    return device


def _interface(name: str, *, device: str) -> MagicMock:
    interface = MagicMock()
    interface.name.value = name
    interface.device_name = device
    interface.device.peer.name.value = device
    interface.interface_type.value = "100gbase-x-qsfp28"
    return interface


class _FakeClient:
    """client.filters over an in-memory inventory, honouring the filters the mesh uses."""

    def __init__(self, devices: list[MagicMock]) -> None:
        self.devices = devices
        self.interfaces = [_interface(port, device=d.name.value) for d in devices for port in _UPLINKS]
        self.group_context = SimpleNamespace(related_node_ids=[])

    async def filters(self, *, kind: Any, **kwargs: Any) -> list[MagicMock]:
        if kind is DcimPhysicalDevice:
            found = [d for d in self.devices if d.role.value == kwargs["role__value"]]
            if "deployment__ids" in kwargs:
                found = [d for d in found if d.deployment_id in kwargs["deployment__ids"]]
            if "name__values" in kwargs:
                found = [d for d in found if d.name.value in kwargs["name__values"]]
            return found
        if kind is DcimPhysicalInterface:
            return [i for i in self.interfaces if i.device_name in kwargs["device__name__values"]]
        raise AssertionError(f"unexpected filters kind {kind}")


def _sibling(index: int) -> dict[str, Any]:
    """A pod of the DC as the ``children`` of the DC in pod.gql carry it."""
    return {"id": f"pod-{index}", "index": index, "design": {"id": "design-pod"}}


def _pod_spines(pod_index: int, *, role: str, deployment_id: str) -> list[MagicMock]:
    return [
        _device(
            f"dc7.7.{pod_index}.{'bs' if role == 'border-spine' else 'sp'}0{n}", role=role, deployment_id=deployment_id
        )
        for n in (1, 2)
    ]


def _make_pod_gen(*, pod_index: int, devices: list[MagicMock]) -> Any:
    gen: Any = PodTopologyGenerator.__new__(PodTopologyGenerator)
    gen.logger = MagicMock()
    gen.data = {"id": f"pod-{pod_index}", "name": f"DC7-7-POD-{pod_index}", "index": pod_index}
    gen.fabric_name = "dc7"
    gen.pod_name = f"dc7-7-pod-{pod_index}"
    gen.client = _FakeClient(devices)
    gen.fail = BaseGenerator.fail.__get__(gen)
    return gen


class _Mesh:
    """What the mesh planned: the cables (local ports, the sibling's spines, the offset) and the routing calls."""

    def __init__(self) -> None:
        self.cabling: list[tuple[list[str], list[str], int]] = []
        self.routing: list[list[str]] = []


async def _mesh(
    gen: Any, *, siblings: list[dict[str, Any]], spines: list[str], spine_role: str, slot_width: int = 2
) -> _Mesh:
    """Read the lower siblings' spines and plan the mesh to them, as ``generate()`` does."""
    pod_index = gen.data["index"]
    found = await read_sibling_spines(
        gen,
        siblings=[s for s in siblings if s["index"] < pod_index],
        dc=_DC,
        fabric_name=gen.fabric_name,
        spine_role=spine_role,
        slot_width=slot_width,
        pod_name=gen.data["name"],
    )
    planned = _Mesh()
    real = pod_module.plan_cabling

    def spy(plan: Any, **kwargs: Any) -> Any:
        bottom = [port.port_name for port in kwargs["bottom_ports"][: len(kwargs["bottom_ports"]) // len(spines)]]
        planned.cabling.append((bottom, sorted({port.device_name for port in kwargs["top_ports"]}), kwargs["offset"]))
        return real(plan, **kwargs)

    model = SimpleNamespace(
        pod_index=pod_index,
        spine_role=spine_role,
        spine_uplink_interfaces=tuple(_UPLINKS),
        dc_id=_DC_ID,
        underlay_protocol="ipv6",
    )
    reads = SimpleNamespace(siblings=found, positions={})
    ports = {name: [TemplatePort(port, "uplink", "100gbase-x-qsfp28") for port in _UPLINKS] for name in spines}

    def route(bottom: list[str], top: list[str], *_: Any, **__: Any) -> None:
        planned.routing.append(top)

    with patch.object(pod_module, "plan_cabling", spy):
        gen._plan_mesh(TopologyPlan(root_id="pod"), model, reads, spines, ports, None, slot_width, route, [])
    return planned


class TestSiblingSpineLookup:
    @pytest.mark.asyncio
    async def test_border_spine_sibling_deployed_to_the_dc_is_found(self) -> None:
        """DC7: pod 2 mesh-cables to pod 1's border-spines, which sit in the DC's deployment."""
        pod_1 = _pod_spines(1, role="border-spine", deployment_id=_DC_ID)
        pod_2 = _pod_spines(2, role="border-spine", deployment_id=_DC_ID)
        gen = _make_pod_gen(pod_index=2, devices=pod_1 + pod_2)

        with patch.object(pod_mesh_module, "WAIT_FOR_PEER_GENERATOR", replace(WAIT_FOR_PEER_GENERATOR, attempts=1)):
            mesh = await _mesh(
                gen,
                siblings=[_sibling(1), _sibling(2)],
                spines=[d.name.value for d in pod_2],
                spine_role="border-spine",
            )

        assert [top for _, top, _ in mesh.cabling] == [["dc7.7.1.bs01", "dc7.7.1.bs02"]]
        assert mesh.routing == [["dc7.7.1.bs01", "dc7.7.1.bs02"]]

    @pytest.mark.asyncio
    async def test_border_spine_pods_do_not_pick_up_each_others_devices(self) -> None:
        """Every border-spine of the DC shares one deployment; pod 3 still cables each sibling to its own."""
        pod_1 = _pod_spines(1, role="border-spine", deployment_id=_DC_ID)
        pod_2 = _pod_spines(2, role="border-spine", deployment_id=_DC_ID)
        pod_3 = _pod_spines(3, role="border-spine", deployment_id=_DC_ID)
        gen = _make_pod_gen(pod_index=3, devices=pod_1 + pod_2 + pod_3)

        with patch.object(pod_mesh_module, "WAIT_FOR_PEER_GENERATOR", replace(WAIT_FOR_PEER_GENERATOR, attempts=1)):
            mesh = await _mesh(
                gen,
                siblings=[_sibling(1), _sibling(2), _sibling(3)],
                spines=[d.name.value for d in pod_3],
                spine_role="border-spine",
            )

        assert [top for _, top, _ in mesh.cabling] == [
            ["dc7.7.1.bs01", "dc7.7.1.bs02"],
            ["dc7.7.2.bs01", "dc7.7.2.bs02"],
        ]
        # One uplink pair per sibling on this pod's side: Ethernet17-18 to pod 1, 19-20 to pod 2.
        assert [bottom for bottom, _, _ in mesh.cabling] == [_UPLINKS[0:2], _UPLINKS[2:4]]

    @pytest.mark.asyncio
    async def test_plain_spine_sibling_deployed_to_its_pod_is_still_found(self) -> None:
        pod_1 = _pod_spines(1, role="spine", deployment_id="pod-1")
        pod_2 = _pod_spines(2, role="spine", deployment_id="pod-2")
        gen = _make_pod_gen(pod_index=2, devices=pod_1 + pod_2)

        with patch.object(pod_mesh_module, "WAIT_FOR_PEER_GENERATOR", replace(WAIT_FOR_PEER_GENERATOR, attempts=1)):
            mesh = await _mesh(
                gen,
                siblings=[_sibling(1), _sibling(2)],
                spines=[d.name.value for d in pod_2],
                spine_role="spine",
            )

        assert [top for _, top, _ in mesh.cabling] == [["dc7.7.1.sp01", "dc7.7.1.sp02"]]


class TestMeshPortSlots:
    """The pair slots come from the pod indexes and the shared slot width, never from what a sibling has built."""

    @staticmethod
    def _slots(mesh: _Mesh) -> list[tuple[list[str], int]]:
        """(local uplinks, offset on the sibling's uplinks) of every pair cabled, in order."""
        return [(bottom, offset) for bottom, _, offset in mesh.cabling]

    @pytest.mark.asyncio
    async def test_a_sibling_seen_with_only_some_spines_keeps_the_full_slot(self) -> None:
        """Pod 1 is mid-creation (1 of its 2 spines visible)"""
        pod_1 = _pod_spines(1, role="spine", deployment_id="pod-1")[:1]
        pod_2 = _pod_spines(2, role="spine", deployment_id="pod-2")
        pod_3 = _pod_spines(3, role="spine", deployment_id="pod-3")
        gen = _make_pod_gen(pod_index=3, devices=pod_1 + pod_2 + pod_3)

        with patch.object(pod_mesh_module, "WAIT_FOR_PEER_GENERATOR", replace(WAIT_FOR_PEER_GENERATOR, attempts=1)):
            mesh = await _mesh(
                gen,
                siblings=[_sibling(1), _sibling(2), _sibling(3)],
                spines=[d.name.value for d in pod_3],
                spine_role="spine",
            )

        assert self._slots(mesh) == [(_UPLINKS[0:1], 2), (_UPLINKS[2:4], 2)]

    @pytest.mark.asyncio
    async def test_slots_stride_by_the_slot_width(self) -> None:
        """A 4-spine mesh: pod 3's local slots are uplinks 0-3 and 4-7, and it lands past each
        sibling's own outbound slots (pod 1: none, slot 1 at port 4; pod 2: slot 0, slot 1 at port 4)."""
        pod_1 = _pod_spines(1, role="spine", deployment_id="pod-1")
        pod_2 = _pod_spines(2, role="spine", deployment_id="pod-2")
        gen = _make_pod_gen(pod_index=3, devices=pod_1 + pod_2)

        with patch.object(pod_mesh_module, "WAIT_FOR_PEER_GENERATOR", replace(WAIT_FOR_PEER_GENERATOR, attempts=1)):
            mesh = await _mesh(
                gen,
                siblings=[_sibling(1), _sibling(2)],
                spines=["dc7.7.3.sp01", "dc7.7.3.sp02", "dc7.7.3.sp03", "dc7.7.3.sp04"],
                spine_role="spine",
                slot_width=4,
            )

        assert self._slots(mesh) == [(_UPLINKS[0:2], 4), (_UPLINKS[4:6], 4)]

    @pytest.mark.asyncio
    async def test_slots_follow_the_pod_index_not_the_list_position(self) -> None:
        """Pods 2 and 3 only (no pod 1 yet): pod 3's slot for pod 2 is slot 1, the one pod 2 itself
        leaves free for its pod-3 link, not slot 0."""
        pod_2 = _pod_spines(2, role="spine", deployment_id="pod-2")
        gen = _make_pod_gen(pod_index=3, devices=pod_2)

        with patch.object(pod_mesh_module, "WAIT_FOR_PEER_GENERATOR", replace(WAIT_FOR_PEER_GENERATOR, attempts=1)):
            mesh = await _mesh(gen, siblings=[_sibling(2), _sibling(3)], spines=["dc7.7.3.sp01"], spine_role="spine")

        assert self._slots(mesh) == [(_UPLINKS[2:4], 2)]


class TestMeshSlotWidth:
    """mesh_slot_width: one spine width for every pod of a back-to-back mesh."""

    @staticmethod
    def _sibling_with_design(index: int, design_id: str) -> dict[str, Any]:
        return {**_sibling(index), "design": {"id": design_id}}

    def test_the_width_is_the_pods_own_design_width(self) -> None:
        design = {"name": "S_POD", "max_spines_per_pod": 4}
        dc_design = {"pod_templates": [{"id": "d-4", "max_spines_per_pod": 4}, {"id": "d-2", "max_spines_per_pod": 2}]}

        assert pod_mesh_module.mesh_slot_width(design, dc_design, [self._sibling_with_design(1, "d-4")]) == 4

    def test_mixed_spine_counts_are_refused(self) -> None:
        """An S DC hosts 2- and 4-spine designs; pods of both kinds cannot share one mesh."""
        design = {"name": "S_POD", "max_spines_per_pod": 4}
        dc_design = {"pod_templates": [{"id": "d-4", "max_spines_per_pod": 4}, {"id": "d-2", "max_spines_per_pod": 2}]}
        siblings = [self._sibling_with_design(1, "d-2"), self._sibling_with_design(2, "d-4")]

        with pytest.raises(ValueError, match=r"lower-index sibling pod\(s\) \[1\]"):
            pod_mesh_module.mesh_slot_width(design, dc_design, siblings)

    def test_a_sibling_whose_design_the_dc_does_not_list_is_refused(self) -> None:
        design = {"name": "S_POD", "max_spines_per_pod": 2}
        dc_design = {"pod_templates": [{"id": "d-2", "max_spines_per_pod": 2}]}

        with pytest.raises(ValueError, match="another max_spines_per_pod"):
            pod_mesh_module.mesh_slot_width(design, dc_design, [self._sibling_with_design(1, "unknown")])
