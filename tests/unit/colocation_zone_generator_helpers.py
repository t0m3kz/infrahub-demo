"""Shared builders of the colocation zone generator tests (test_colocation_zone_generator*.py).

The cleaned zone and virtual colocation rows, the edge templates and the generator factories.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from generators.topology.colocation_fabric import (
    ColocationFabricGenerator,
    ColocationZoneGenerator,
    VirtualColocationGenerator,
)

_EDGE_TEMPLATE = {
    "id": "tpl-edge",
    # The cleaned form of the `template_kind: __typename` alias the query
    # selects (aliased because clean_data() would rewrite __typename to
    # "typename"). This is what tells the generator physical from virtual.
    "template_kind": "TemplateDcimPhysicalDevice",
    "platform": {"id": "plat-nxos", "name": "cisco_nxos"},
    "device_type": "dt-9316d",
}

# Same edge template with the real N9K-C9316D-GX_EDGE port layout attached
# (data/bootstrap/10_physical_devices_templates_cisco_nxos.yaml): a
# `firewall`-role port block, no `load-balancer`-role ports at all.
_EDGE_TEMPLATE_WITH_FIREWALL_PORTS = {
    **_EDGE_TEMPLATE,
    "id": "tpl-edge-fw-ports",
    "interfaces": [
        {"name": "Ethernet1/15", "role": "firewall"},
        {"name": "Ethernet1/16", "role": "firewall"},
    ],
}

# A hypothetical edge template with BOTH firewall- and load-balancer-role ports,
# for tests that want every colocation role to be cablable.
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


def _make_generator(cls: type[ColocationFabricGenerator] = ColocationZoneGenerator, *, fabric: str = "fr2") -> Any:
    gen = cls.__new__(cls)
    gen.logger = MagicMock()
    gen.client = MagicMock()
    gen.client.get = AsyncMock()
    gen.client.filters = AsyncMock(return_value=[])
    gen.client.create = AsyncMock()
    gen.client.allocate_next_ip_prefix = AsyncMock(return_value=MagicMock(id="prefix-1"))
    gen.branch = "main"
    gen.fabric_name = fabric
    gen.pod_name = None
    gen.deployment_id = "zone-1"
    setattr(gen, "acquire_resource_lock", AsyncMock(return_value="lock-id"))  # noqa: B010
    setattr(gen, "release_resource_lock", AsyncMock())  # noqa: B010
    return gen


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


def _created(gen: Any, kind: str) -> list[dict[str, Any]]:
    """Data of every client.create() call for `kind` (a protocol class or its name)."""
    return [
        c.kwargs["data"]
        for c in gen.client.create.await_args_list
        if getattr(c.kwargs["kind"], "__name__", c.kwargs["kind"]) == kind
    ]
