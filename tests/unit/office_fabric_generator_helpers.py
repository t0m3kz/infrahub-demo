"""Shared builders for the ``test_office_fabric*`` modules: the cleaned payload of an office, and an office generator on the
in-memory client with the bootstrap objects it reads.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any
from unittest.mock import AsyncMock

import pytest

from generators.dcim.template_interfaces import TemplatePort, TemplateReads
from generators.helpers.naming import sdwan_edge_name
from generators.office.office_fabric import OfficeFabricGenerator
from tests.unit.colocation_zone_generator_helpers import ColoClient
from tests.unit.plan_graph_helpers import RELATIONSHIPS_OF_KIND, make_hfid_generator
from utils.exchange_transit import transit_pool_name

EDGE_TEMPLATE_NAME = "VCE-610_EDGE"
_QSFP = "100gbase-x-qsfp28"
_COPPER = "1000base-t"

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
EDGE_TEMPLATE: dict[str, Any] = {
    "id": "tpl-edge",
    "template_name": EDGE_TEMPLATE_NAME,
    "template_kind": "TemplateDcimPhysicalDevice",
    "interfaces": [],
}
_EDGE_ENTRY = {"role": "sdwan-edge", "quantity": 1, "template": EDGE_TEMPLATE}

_EDGE = sdwan_edge_name("C005-OFFICE")
_ACCESS_PORTS = [
    TemplatePort("TenGigabitEthernet1/0/1", "customer", _COPPER),
    TemplatePort("HundredGigE1/1/1", "uplink", _QSFP),
    TemplatePort("HundredGigE1/1/2", "uplink", _QSFP),
    TemplatePort("GigabitEthernet0/0", "management", _COPPER),
]
_DISTRIBUTION_PORTS = [
    TemplatePort("HundredGigE1/0/1", "downlink", _QSFP),
    TemplatePort("HundredGigE1/0/2", "downlink", _QSFP),
    TemplatePort("HundredGigE1/0/25", "interlink", _QSFP),
    TemplatePort("GigabitEthernet1/0/26", "uplink", _COPPER),
    TemplatePort("GigabitEthernet0/0", "management", _COPPER),
]
_EDGE_PORTS = [
    TemplatePort("GE1", "uplink", _COPPER),
    TemplatePort("GE3", "downlink", _COPPER),
    TemplatePort("GE4", "downlink", _COPPER),
]
_PORTS_OF_TEMPLATE = {"tpl-access": _ACCESS_PORTS, "tpl-dist": _DISTRIBUTION_PORTS}


class _PortsByDevice(dict[str, list[dict[str, Any]]]):
    """The ports a device saved with its template brings: its role's."""

    def get(self, key: str, default: Any = None) -> list[dict[str, Any]]:  # ty: ignore[invalid-method-override]
        if key.endswith("-EDGE1"):
            ports = _EDGE_PORTS
        else:
            ports = _ACCESS_PORTS if key.startswith("as-") else _DISTRIBUTION_PORTS
        return [{"name": p.name, "role": p.role, "interface_type": p.interface_type} for p in ports]


class OfficeClient(ColoClient):
    """The in-memory client, answering the VRF pass's device query with a world that has no VRF-carrying device and the
    AS descriptions the WAN exit finds its two AS numbers by.
    """

    async def execute_graphql(
        self, query: str, variables: dict[str, Any] | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        if "data" not in (variables or {}):
            return {"DcimDevice": {"edges": []}, "SecurityZone": {"edges": []}}
        return await super().execute_graphql(query, variables, **kwargs)

    async def filters(self, kind: Any, **flt: Any) -> list[Any]:
        found = await super().filters(kind, **flt)
        if "description__values" in flt:
            found = [node for node in found if node.data.get("description") in flt["description__values"]]
        return found


def _office(*, edge: bool = True, **overrides: Any) -> dict[str, Any]:
    """A cleaned office: its switch pair and, with ``edge``, its SD-WAN edge entry (no gateway: no tunnel)."""
    office: dict[str, Any] = {
        "id": "office-1",
        "name": "C005-OFFICE",
        "naming_convention": "standard",
        "owner": {"id": "org-c005", "name": "C005"},
        "sdwan_gateways": [],
        "fabric_templates": [_DISTRIBUTION_ENTRY, _ACCESS_ENTRY, *([_EDGE_ENTRY] if edge else [])],
    }
    office.update(overrides)
    return office


def office_env(
    monkeypatch: pytest.MonkeyPatch,
    *,
    client: OfficeClient | None = None,
    edge: bool = False,
    ports: dict[str, list[TemplatePort]] | None = None,
) -> tuple[Any, OfficeClient]:
    """An office generator on the in-memory client: the bootstrap pools, the PROD zone with its transit pool, the edge template (and, with ``edge``, a device of
    an earlier run) and the templates' ports exist, and every device has its PROD VRF instance.
    """
    monkeypatch.setattr("generators.reconciler.asyncio.sleep", AsyncMock())
    monkeypatch.setitem(RELATIONSHIPS_OF_KIND, "DcimVirtualInterface", ("capabilities",))
    client = client or OfficeClient()
    if not any(node.kind == "CoreIPPrefixPool" for node in client.store.values()):
        for pool in ("Management-IPv4", "Technical-IPv4", "Loopback-IPv4", transit_pool_name("PROD")):
            client.seed("CoreIPPrefixPool", name=pool)
        client.seed("SecurityZone", name="PROD")
        client.seed("TemplateDcimPhysicalDevice", id=EDGE_TEMPLATE["id"], template_name=EDGE_TEMPLATE_NAME)
        if edge:
            device = client.seed("DcimPhysicalDevice", name=_EDGE)
            for port in _EDGE_PORTS:
                client.seed(
                    "DcimPhysicalInterface",
                    device={"id": device.id},
                    name=port.name,
                    role=port.role,
                    interface_type=port.interface_type,
                )
    client.template_ports = _PortsByDevice()
    table = {**_PORTS_OF_TEMPLATE, **(ports or {})}
    table[EDGE_TEMPLATE["id"]] = table.get(EDGE_TEMPLATE["id"], _EDGE_PORTS)

    async def read_templates(_client: Any, ids: Any) -> TemplateReads:
        return TemplateReads(ports={template_id: list(table.get(template_id, [])) for template_id in ids}, owners={})

    async def vrf_ids(_gen: Any, device_ids: Iterable[str], names: Iterable[str]) -> dict[tuple[str, str], str]:
        return {(device_id, name): f"vrf-{device_id}-{name}" for device_id in device_ids for name in names}

    monkeypatch.setattr("generators.office.office_fabric.read_template_ports", read_templates)
    monkeypatch.setattr("generators.office.office_fabric.local_vrf_ids", vrf_ids)
    return make_hfid_generator(client, cls=OfficeFabricGenerator), client
