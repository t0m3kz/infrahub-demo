"""Shared builders for the ``test_office_fabric_generator*`` modules."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from generators.topology.office_fabric import OfficeFabricGenerator

_ACCESS_TEMPLATE: dict[str, Any] = {
    "id": "tpl-access",
    "template_kind": "TemplateDcimPhysicalDevice",
    "platform": {"id": "plat-xe", "name": "cisco_xe"},
    "device_type": "dt-9300",
    "interfaces": [
        {"name": "TenGigabitEthernet1/0/1", "role": "customer"},
        {"name": "HundredGigE1/1/1", "role": "uplink"},
        {"name": "HundredGigE1/1/2", "role": "uplink"},
        {"name": "GigabitEthernet0/0", "role": "management"},
    ],
}
_DISTRIBUTION_TEMPLATE: dict[str, Any] = {
    "id": "tpl-dist",
    "template_kind": "TemplateDcimPhysicalDevice",
    "platform": {"id": "plat-xe", "name": "cisco_xe"},
    "device_type": "dt-9500",
    "interfaces": [
        {"name": "HundredGigE1/0/1", "role": "downlink"},
        {"name": "HundredGigE1/0/2", "role": "downlink"},
        {"name": "HundredGigE1/0/25", "role": "interlink"},
        {"name": "GigabitEthernet1/0/26", "role": "uplink"},
        {"name": "GigabitEthernet0/0", "role": "management"},
    ],
}
_VIRTUAL_TEMPLATE = {"id": "tpl-virtual", "template_kind": "TemplateDcimVirtualDevice"}

_DISTRIBUTION_ENTRY = {"role": "distribution-switch", "quantity": 2, "template": _DISTRIBUTION_TEMPLATE}
_ACCESS_ENTRY = {"role": "access-switch", "quantity": 2, "template": _ACCESS_TEMPLATE}


def _make_generator(*, wan_exit: bool = False) -> Any:
    """A generator with its Infrahub-facing mixins mocked; ``wan_exit`` keeps the real WAN exit step."""
    gen: Any = OfficeFabricGenerator.__new__(OfficeFabricGenerator)
    gen.logger = MagicMock()
    gen.client = MagicMock()
    gen.fabric_name = ""
    gen.pod_name = None
    gen.deployment_id = ""

    @asynccontextmanager
    async def _no_lock(key: str) -> AsyncGenerator[None]:
        yield

    gen.resource_lock = _no_lock
    gen.ensure_sliced_pool = AsyncMock(side_effect=lambda **kwargs: MagicMock(name=kwargs["pool_name"], **kwargs))
    gen.upsert_number_pool = AsyncMock(return_value=MagicMock(id="vni-pool-id"))
    gen.attach_refs = AsyncMock()
    gen.create_devices = AsyncMock(side_effect=_fake_names)
    gen.create_cabling = AsyncMock(return_value=[("a", "b")])
    gen.create_chain_cabling = AsyncMock(return_value=[[("x", "y")]])
    gen.ensure_ospf_area = AsyncMock(return_value="area-id")
    gen.create_routing = AsyncMock()
    gen.ensure_lisp_site_key = AsyncMock(return_value="site-key-id")
    # The VRF instances of the exit's two ends have their own tests (test_vrf_callers.py).
    gen.vrfs_held_calls = []

    @asynccontextmanager
    async def _held(needs: dict[str, set[str]]):  # noqa: ANN202
        gen.vrfs_held_calls.append(needs)
        gen.vrfs_events.append("held")
        yield
        gen.vrfs_events.append("released")

    gen.vrfs_events = []
    gen.vrfs_held = _held
    if not wan_exit:
        gen._ensure_wan_exit = AsyncMock()
    return gen


async def _fake_names(*, device_role: str, quantity: int, **_: Any) -> list[str]:
    code = {"access-switch": "as", "distribution-switch": "ds"}[device_role]
    return [f"{code}-c005-office{n:02d}" for n in range(1, quantity + 1)]


def _office(**overrides: Any) -> dict[str, Any]:
    office: dict[str, Any] = {
        "id": "office-1",
        "name": "C005-OFFICE",
        "naming_convention": "standard",
        "sdwan_gateway": {"id": "gw-1"},
        "fabric_templates": [_DISTRIBUTION_ENTRY, _ACCESS_ENTRY],
    }
    office.update(overrides)
    return office
