"""A pod finds its lower-index siblings' spine-slot devices by name, not by deployment.

Regression for DC7 (two S_BORDER_SPINE_POD pods, back-to-back, no super-spine):
pod 2 failed with "sibling pod idx=1 still has no spine devices/uplinks" because
it looked the sibling's spines up with ``deployment__ids=[sibling.id]``, while a
border-spine is deployed to the DC, not to its pod. Spine-slot names are
deterministic ([dc.index, pod.index] + role + role_index), so the name is what
attributes a device to its pod, whichever deployment it sits in.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from generators.pod_config import pod_spine_slot_device_names, spine_slot_device_names
from generators.protocols import DcimPhysicalDevice, DcimPhysicalInterface
from generators.rack import RackMixin
from generators.topology import pod_mesh as pod_mesh_module
from generators.topology.pod import PodTopologyGenerator

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
    return interface


class _FakeClient:
    """client.filters over an in-memory inventory, honouring the filters the mesh uses."""

    def __init__(self, devices: list[MagicMock]) -> None:
        self.devices = devices
        self.interfaces = [_interface(port, device=d.name.value) for d in devices for port in _UPLINKS]

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
    gen.data = {"name": f"DC7-7-POD-{pod_index}", "index": pod_index}
    gen.fabric_name = "dc7"
    gen.client = _FakeClient(devices)
    gen.create_cabling = AsyncMock(return_value=[])
    gen.create_routing = AsyncMock()
    return gen


async def _mesh(
    gen: Any, *, siblings: list[dict[str, Any]], spines: list[str], spine_role: str, slot_width: int = 2
) -> None:
    await gen._cable_to_existing_sibling_pods(
        siblings=siblings,
        spines=spines,
        spine_interfaces=_UPLINKS,
        spine_role=spine_role,
        dc=_DC,
        dc_asn_pool_id=None,
        pod_pools={},
        underlay_protocol="ipv6",
        slot_width=slot_width,
    )


class TestSpineSlotDeviceNames:
    def test_names_match_what_create_devices_builds(self) -> None:
        """The same [dc.index, pod.index] naming pod.py creates the devices under."""
        names = spine_slot_device_names(dc=_DC, fabric_name="dc7", pod_index=2, role="border-spine", quantity=2)

        assert names == ["dc7.7.2.bs01", "dc7.7.2.bs02"]

    def test_pods_of_one_dc_never_share_a_name(self) -> None:
        pod_1 = spine_slot_device_names(dc=_DC, fabric_name="dc7", pod_index=1, role="border-spine", quantity=4)
        pod_2 = spine_slot_device_names(dc=_DC, fabric_name="dc7", pod_index=2, role="border-spine", quantity=4)

        assert not set(pod_1) & set(pod_2)

    def test_rack_derives_the_same_names(self) -> None:
        """rack.py's spine-slot derivation is the same helper, so the two cannot drift."""
        gen: Any = RackMixin.__new__(RackMixin)
        gen.logger = MagicMock()
        gen.fabric_name = "dc7"
        gen.data = {
            "name": "ams-1-s-2-r-1-1",
            "pod": {
                "index": 2,
                "parent": _DC,
                "fabric_templates": [
                    {
                        "role": "border-spine",
                        "quantity": 2,
                        "template": {"interfaces": [{"name": "Ethernet1/1", "role": "downlink"}]},
                    }
                ],
            },
        }

        device_names, _ = gen._derive_spine_info()

        assert device_names == spine_slot_device_names(
            dc=_DC, fabric_name="dc7", pod_index=2, role="border-spine", quantity=2
        )


class TestPodSpineSlotDeviceNames:
    def test_role_index_continues_across_entries(self) -> None:
        """Two border-spine entries of 2: role_index 1..4, the way add_pod numbers them."""
        pod = {
            "index": 2,
            "parent": _DC,
            "fabric_templates": [
                {"role": "border-spine", "quantity": 2, "template": {}},
                {"role": "border-spine", "quantity": 2, "template": {}},
                {"role": "firewall", "quantity": 2, "template": {}},
            ],
        }

        assert pod_spine_slot_device_names(pod) == [
            "dc7.7.2.bs01",
            "dc7.7.2.bs02",
            "dc7.7.2.bs03",
            "dc7.7.2.bs04",
        ]

    def test_a_pod_without_spine_slot_entries_has_none(self) -> None:
        assert pod_spine_slot_device_names({"index": 1, "parent": _DC, "fabric_templates": []}) == []

    def test_rack_derives_distinct_names_for_two_entries(self) -> None:
        gen: Any = RackMixin.__new__(RackMixin)
        gen.logger = MagicMock()
        gen.fabric_name = "dc7"
        entry = {
            "role": "spine",
            "quantity": 2,
            "template": {"interfaces": [{"name": "Ethernet1/1", "role": "downlink"}]},
        }
        gen.data = {"name": "r", "pod": {"index": 1, "parent": _DC, "fabric_templates": [entry, entry]}}

        device_names, _ = gen._derive_spine_info()

        assert device_names == ["dc7.7.1.sp01", "dc7.7.1.sp02", "dc7.7.1.sp03", "dc7.7.1.sp04"]


class TestSiblingSpineLookup:
    @pytest.mark.asyncio
    async def test_border_spine_sibling_deployed_to_the_dc_is_found(self) -> None:
        """DC7: pod 2 mesh-cables to pod 1's border-spines, which sit in the DC's deployment."""
        pod_1 = _pod_spines(1, role="border-spine", deployment_id=_DC_ID)
        pod_2 = _pod_spines(2, role="border-spine", deployment_id=_DC_ID)
        gen = _make_pod_gen(pod_index=2, devices=pod_1 + pod_2)

        with patch.object(pod_mesh_module, "_SIBLING_SPINE_MAX_RETRIES", 1):
            await _mesh(
                gen,
                siblings=[_sibling(1), _sibling(2)],
                spines=[d.name.value for d in pod_2],
                spine_role="border-spine",
            )

        gen.create_cabling.assert_awaited_once()
        assert gen.create_cabling.call_args.kwargs["top_devices"] == ["dc7.7.1.bs01", "dc7.7.1.bs02"]
        assert gen.create_routing.call_args.kwargs["top_devices"] == ["dc7.7.1.bs01", "dc7.7.1.bs02"]

    @pytest.mark.asyncio
    async def test_border_spine_pods_do_not_pick_up_each_others_devices(self) -> None:
        """Every border-spine of the DC shares one deployment; pod 3 still cables each sibling to its own."""
        pod_1 = _pod_spines(1, role="border-spine", deployment_id=_DC_ID)
        pod_2 = _pod_spines(2, role="border-spine", deployment_id=_DC_ID)
        pod_3 = _pod_spines(3, role="border-spine", deployment_id=_DC_ID)
        gen = _make_pod_gen(pod_index=3, devices=pod_1 + pod_2 + pod_3)

        with patch.object(pod_mesh_module, "_SIBLING_SPINE_MAX_RETRIES", 1):
            await _mesh(
                gen,
                siblings=[_sibling(1), _sibling(2), _sibling(3)],
                spines=[d.name.value for d in pod_3],
                spine_role="border-spine",
            )

        top_devices = [call.kwargs["top_devices"] for call in gen.create_cabling.call_args_list]
        assert top_devices == [["dc7.7.1.bs01", "dc7.7.1.bs02"], ["dc7.7.2.bs01", "dc7.7.2.bs02"]]
        # One uplink pair per sibling on this pod's side: Ethernet17-18 to pod 1, 19-20 to pod 2.
        local_uplinks = [call.kwargs["bottom_interfaces"] for call in gen.create_cabling.call_args_list]
        assert local_uplinks == [_UPLINKS[0:2], _UPLINKS[2:4]]

    @pytest.mark.asyncio
    async def test_plain_spine_sibling_deployed_to_its_pod_is_still_found(self) -> None:
        pod_1 = _pod_spines(1, role="spine", deployment_id="pod-1")
        pod_2 = _pod_spines(2, role="spine", deployment_id="pod-2")
        gen = _make_pod_gen(pod_index=2, devices=pod_1 + pod_2)

        with patch.object(pod_mesh_module, "_SIBLING_SPINE_MAX_RETRIES", 1):
            await _mesh(
                gen,
                siblings=[_sibling(1), _sibling(2)],
                spines=[d.name.value for d in pod_2],
                spine_role="spine",
            )

        assert gen.create_cabling.call_args.kwargs["top_devices"] == ["dc7.7.1.sp01", "dc7.7.1.sp02"]


class TestMeshPortSlots:
    """The pair slots come from the pod indexes and the shared slot width, never from what a sibling has built."""

    @staticmethod
    def _slots(gen: Any) -> list[tuple[list[str], int]]:
        """(local uplinks, offset on the sibling's uplinks) of every pair cabled, in order."""
        return [
            (call.kwargs["bottom_interfaces"], call.kwargs["options"]["cabling_offset"])
            for call in gen.create_cabling.call_args_list
        ]

    @pytest.mark.asyncio
    async def test_a_sibling_seen_with_only_some_spines_keeps_the_full_slot(self) -> None:
        """Pod 1 is mid-creation (1 of its 2 spines visible): pod 3 still lands at port 2 of pod 1
        and of pod 2. The live count used to shrink the stride to 1, putting pod 3 on pod 1's port 1,
        which pod 2's link to pod 1 (ports 0-1) already uses."""
        pod_1 = _pod_spines(1, role="spine", deployment_id="pod-1")[:1]
        pod_2 = _pod_spines(2, role="spine", deployment_id="pod-2")
        pod_3 = _pod_spines(3, role="spine", deployment_id="pod-3")
        gen = _make_pod_gen(pod_index=3, devices=pod_1 + pod_2 + pod_3)

        with patch.object(pod_mesh_module, "_SIBLING_SPINE_MAX_RETRIES", 1):
            await _mesh(
                gen,
                siblings=[_sibling(1), _sibling(2), _sibling(3)],
                spines=[d.name.value for d in pod_3],
                spine_role="spine",
            )

        assert self._slots(gen) == [(_UPLINKS[0:1], 2), (_UPLINKS[2:4], 2)]

    @pytest.mark.asyncio
    async def test_slots_stride_by_the_slot_width(self) -> None:
        """A 4-spine mesh: pod 3's local slots are uplinks 0-3 and 4-7, and it lands past each
        sibling's own outbound slots (pod 1: none, slot 1 at port 4; pod 2: slot 0, slot 1 at port 4)."""
        pod_1 = _pod_spines(1, role="spine", deployment_id="pod-1")
        pod_2 = _pod_spines(2, role="spine", deployment_id="pod-2")
        gen = _make_pod_gen(pod_index=3, devices=pod_1 + pod_2)

        with patch.object(pod_mesh_module, "_SIBLING_SPINE_MAX_RETRIES", 1):
            await _mesh(
                gen,
                siblings=[_sibling(1), _sibling(2)],
                spines=["dc7.7.3.sp01", "dc7.7.3.sp02", "dc7.7.3.sp03", "dc7.7.3.sp04"],
                spine_role="spine",
                slot_width=4,
            )

        assert self._slots(gen) == [(_UPLINKS[0:2], 4), (_UPLINKS[4:6], 4)]

    @pytest.mark.asyncio
    async def test_slots_follow_the_pod_index_not_the_list_position(self) -> None:
        """Pods 2 and 3 only (no pod 1 yet): pod 3's slot for pod 2 is slot 1, the one pod 2 itself
        leaves free for its pod-3 link, not slot 0."""
        pod_2 = _pod_spines(2, role="spine", deployment_id="pod-2")
        gen = _make_pod_gen(pod_index=3, devices=pod_2)

        with patch.object(pod_mesh_module, "_SIBLING_SPINE_MAX_RETRIES", 1):
            await _mesh(gen, siblings=[_sibling(2), _sibling(3)], spines=["dc7.7.3.sp01"], spine_role="spine")

        assert self._slots(gen) == [(_UPLINKS[2:4], 2)]


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
