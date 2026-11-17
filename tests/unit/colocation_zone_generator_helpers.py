"""Shared builders of the colocation zone generator tests (test_colocation_zone*.py): the cleaned payloads of a zone and a
virtual colocation, a generator that only validates its templates, and a generator on the in-memory client that writes
its plan (``zone_env``).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from generators.colocation.colocation_fabric import (
    ColocationFabricGenerator,
    ColocationZoneGenerator,
    VirtualColocationGenerator,
)
from generators.dcim.template_interfaces import TemplatePort, TemplateReads
from tests.unit.plan_graph_helpers import HfidClient, HfidNode, make_hfid_generator
from utils.limits import GROUP_HA_DOMAINS

_EDGE_TEMPLATE = {
    "id": "tpl-edge",
    # The cleaned form of the `template_kind: __typename` alias the query selects (aliased because clean_data() would
    # rewrite __typename to "typename").
    "template_kind": "TemplateDcimPhysicalDevice",
    "platform": {"id": "plat-nxos", "name": "cisco_nxos"},
    "device_type": "dt-9316d",
}

# Same edge template with the real N9K-C9316D-GX_EDGE port layout attached
# (data/bootstrap/10_physical_devices_templates_cisco_nxos.yaml): a `firewall`-role port block.
_EDGE_TEMPLATE_WITH_FIREWALL_PORTS = {
    **_EDGE_TEMPLATE,
    "id": "tpl-edge-fw-ports",
    "interfaces": [
        {"name": "Ethernet1/15", "role": "firewall"},
        {"name": "Ethernet1/16", "role": "firewall"},
    ],
}

# A hypothetical edge template with BOTH firewall- and load-balancer-role ports, for tests that want every colocation
# role to be cablable.
_EDGE_TEMPLATE_WITH_ALL_SERVICE_PORTS = {
    **_EDGE_TEMPLATE,
    "id": "tpl-edge-all-ports",
    "interfaces": [
        {"name": "Ethernet1/15", "role": "firewall"},
        {"name": "Ethernet1/16", "role": "firewall"},
        {"name": "Ethernet1/17", "role": "load-balancer"},
        {"name": "Ethernet1/18", "role": "load-balancer"},
    ],
}

_VIRTUAL_EDGE_TEMPLATE = {
    "id": "tpl-vedge",
    "template_kind": "TemplateCloudNetworkAppliance",
}

_METRO_ASN_POOL = {"id": "asn-pool", "name": "fr-asn-pool"}
_PROCESSES = {"eg-fr201": "bgp-1", "eg-fr202": "bgp-2"}


def _zone(**overrides: Any) -> dict[str, Any]:
    """A cleaned zone row as colocation_zone.gql returns it: zone FR2 in metro FR."""
    zone: dict[str, Any] = {
        "id": "zone-1",
        "name": "FR2",
        "parent": {
            "id": "metro-1",
            "name": "FR",
            "naming_convention": "standard",
            "fabric_asn_pool": _METRO_ASN_POOL,
        },
        "fabric_templates": [{"role": "edge", "quantity": 2, "template": _EDGE_TEMPLATE}],
    }
    zone.update(overrides)
    return zone


def _virtual(**overrides: Any) -> dict[str, Any]:
    """A cleaned virtual colocation row as colocation_virtual.gql returns it: AM-NE in metro AM."""
    node: dict[str, Any] = {
        "id": "vc-1",
        "name": "AM-NE",
        "parent": {
            "id": "metro-am",
            "name": "AM",
            "naming_convention": "standard",
            "fabric_asn_pool": {"id": "am-asn-pool", "name": "am-asn-pool"},
        },
        "fabric_templates": [{"role": "edge", "quantity": 2, "template": _VIRTUAL_EDGE_TEMPLATE}],
    }
    node.update(overrides)
    return node


def _made_zone(**overrides: Any) -> Any:
    gen = _make_generator()
    gen.data = _zone(**overrides)
    return gen


def _made_virtual(**overrides: Any) -> Any:
    gen = _make_generator(VirtualColocationGenerator, fabric="am-ne")
    gen.data = _virtual(**overrides)
    return gen


def _make_generator(cls: type[ColocationFabricGenerator] = ColocationZoneGenerator, *, fabric: str = "fr2") -> Any:
    """A generator with a mock client, enough to validate templates and read the guards of generate()."""
    gen = cls.__new__(cls)
    gen.logger = MagicMock()
    gen.client = MagicMock()
    gen.fabric_name = fabric
    gen.pod_name = None
    gen.deployment_id = "zone-1"
    return gen


_SFP = "10gbase-x-sfpp"
_PORTS = [
    TemplatePort("Ethernet1/1", "interlink", _SFP),
    TemplatePort("Ethernet1/2", "interlink", _SFP),
    TemplatePort("Ethernet1/15", "firewall", _SFP),
    TemplatePort("Ethernet1/16", "firewall", _SFP),
    TemplatePort("Ethernet1/17", "load-balancer", _SFP),
    TemplatePort("Ethernet1/18", "load-balancer", _SFP),
    TemplatePort("Ethernet1/31", "uplink", _SFP),
    TemplatePort("Ethernet1/32", "uplink", _SFP),
    TemplatePort("ha1", "ha", _SFP),
]
BOOTSTRAP_POOLS = ("Management-IPv4", "Technical-IPv4", "Technical-IPv6", "Loopback-IPv4", "Loopback-IPv6")


class _EveryDeviceBringsItsPorts(dict[str, list[dict[str, Any]]]):
    """The ports a device saved with its template brings: the same on every device."""

    def get(self, key: str, default: Any = None) -> list[dict[str, Any]]:  # ty: ignore[invalid-method-override]
        return [{"name": port.name, "role": port.role, "interface_type": port.interface_type} for port in _PORTS]


class ColoClient(HfidClient):
    """The in-memory client, answering the description and address family filters the colocation reads send and cutting
    an IPv6 prefix where an IPv6 length is asked for.
    """

    async def allocate_next_ip_prefix(self, resource_pool: Any, identifier: str, **kwargs: Any) -> Any:
        node = await super().allocate_next_ip_prefix(resource_pool, identifier, **kwargs)
        if kwargs["prefix_length"] > 32 and ":" not in str(node.prefix.value):
            setattr(
                node, "prefix", SimpleNamespace(value=f"2001:db8:{len(self.allocations)}::/{kwargs['prefix_length']}")
            )
        return node

    async def filters(self, kind: Any, **flt: Any) -> list[Any]:
        found = await super().filters(kind, **flt)
        if "description__value" in flt:
            found = [node for node in found if node.data.get("description") == flt["description__value"]]
        if "afi__values" in flt:
            found = [node for node in found if node.data.get("afi") in flt["afi__values"]]
        return found


def zone_env(
    monkeypatch: pytest.MonkeyPatch,
    *,
    cls: type[ColocationFabricGenerator] = ColocationZoneGenerator,
    client: ColoClient | None = None,
    ports: list[TemplatePort] | None = None,
    metro_pools: tuple[str, ...] = (),
) -> tuple[Any, ColoClient]:
    """A zone (or virtual colocation) generator on the in-memory client: the bootstrap pools (and the named metro supernets)
    exist, the metro and the node are there to point at, and every template has the ports of ``_PORTS``.
    """
    client = client or ColoClient()
    if not any(node.kind == "CoreIPPrefixPool" for node in client.store.values()):
        for parent in (*BOOTSTRAP_POOLS, *metro_pools):
            client.seed("CoreIPPrefixPool", name=parent)
        client.seed("CoreStandardGroup", name=GROUP_HA_DOMAINS)
    client.template_ports = _EveryDeviceBringsItsPorts()
    table = ports if ports is not None else _PORTS

    async def read_templates(_client: Any, ids: Any) -> TemplateReads:
        return TemplateReads(ports={template_id: list(table) for template_id in ids}, owners={})

    monkeypatch.setattr("generators.colocation.colocation_fabric.read_template_ports", read_templates)
    return make_hfid_generator(client, cls=cls), client


def seed_nodes(client: ColoClient, *payloads: dict[str, Any]) -> None:
    """The zone, its metro and the ASN pool of the metro, as the query payload names them (they exist before the run)."""
    for payload in payloads:
        for node in (payload, payload.get("parent") or {}):
            if node.get("id") and node["id"] not in client.store:
                client.store[node["id"]] = HfidNode(client, "Topology", {"id": node["id"], "name": node["name"]})
