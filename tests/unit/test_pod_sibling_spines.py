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

from generators.pod_config import spine_slot_device_names
from generators.protocols import DcimPhysicalDevice, DcimPhysicalInterface
from generators.rack import RackMixin
from generators.topology import pod as pod_module
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


def _sibling(index: int) -> MagicMock:
    sibling = MagicMock()
    sibling.id = f"pod-{index}"
    sibling.index.value = index
    return sibling


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


async def _mesh(gen: Any, *, siblings: list[MagicMock], spines: list[str], spine_role: str) -> None:
    await gen._cable_to_existing_sibling_pods(
        siblings=siblings,
        spines=spines,
        spine_interfaces=_UPLINKS,
        spine_role=spine_role,
        dc=_DC,
        dc_asn_pool_id=None,
        pod_pools={},
        underlay_protocol="ipv6",
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


class TestSiblingSpineLookup:
    @pytest.mark.asyncio
    async def test_border_spine_sibling_deployed_to_the_dc_is_found(self) -> None:
        """DC7: pod 2 mesh-cables to pod 1's border-spines, which sit in the DC's deployment."""
        pod_1 = _pod_spines(1, role="border-spine", deployment_id=_DC_ID)
        pod_2 = _pod_spines(2, role="border-spine", deployment_id=_DC_ID)
        gen = _make_pod_gen(pod_index=2, devices=pod_1 + pod_2)

        with patch.object(pod_module, "_SIBLING_SPINE_MAX_RETRIES", 1):
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

        with patch.object(pod_module, "_SIBLING_SPINE_MAX_RETRIES", 1):
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

        with patch.object(pod_module, "_SIBLING_SPINE_MAX_RETRIES", 1):
            await _mesh(
                gen,
                siblings=[_sibling(1), _sibling(2)],
                spines=[d.name.value for d in pod_2],
                spine_role="spine",
            )

        assert gen.create_cabling.call_args.kwargs["top_devices"] == ["dc7.7.1.sp01", "dc7.7.1.sp02"]
