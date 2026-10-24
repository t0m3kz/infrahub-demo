"""Shared builders of the GPO device view tests (test_gpo_device_view*.py).

The leaf fleet roots, the view call and the component placement helpers.
"""

from __future__ import annotations

from functools import cache
from typing import Any

from tests.smoke.fixture_builders.common import _edges
from tests.smoke.fixture_builders.group_policy import (
    _GPO_PAY_DB_TAGS,
    _gp_nic,
    build_gpo_device_data,
    gpo_roots,
    gpo_view,
)
from transforms.helpers.group_policy import (
    component_coverage,
    data_center_contracts,
)
from utils.data_cleaning import clean_data

LEAF_ACTIVATIONS: list[dict[str, Any]] = [
    {"vlan_id": 110, "segment": {"id": "seg-web-np"}},
    {"vlan_id": 120, "segment": {"id": "seg-app-np"}},
    {"vlan_id": 210, "segment": {"id": "seg-pay-p"}},
    {"vlan_id": 220, "segment": {"id": "seg-portal-p"}},
]
VRFS = ("NONPROD", "PROD")


LEAF_SEGMENTS = ("web-np", "app-np", "pay-p", "portal-p")


def _raw(**kwargs: Any) -> dict:
    """The raw query response of the leaf of DC10 (Ethernet10 carries the four segments, the access port
    Ethernet11 only pay-p), with the fleet roots."""
    return build_gpo_device_data(
        device_name="dc1-leaf-01",
        role="leaf",
        segments=LEAF_SEGMENTS,
        host_ports={"Ethernet11": _GPO_PAY_DB_TAGS},
        **kwargs,
    )


@cache
def _leaf_interfaces() -> list[dict[str, Any]]:
    """The cleaned interfaces of that leaf (what ``BaseDeviceTransform._extra_config`` passes the view)."""
    return clean_data(_raw())["DcimDevice"][0]["interfaces"]


def _roots(raw: dict | None = None) -> dict[str, Any]:
    """The cleaned fleet roots (everything but the device root)."""
    return gpo_roots(raw or _raw())


def _view(roots: dict[str, Any], **overrides: Any) -> dict[str, Any] | None:
    arguments: dict[str, Any] = {
        "device_name": "dc1-leaf-01",
        "role": "leaf",
        "platform": "cisco_nxos",
        "activations": LEAF_ACTIVATIONS,
        "vrfs": VRFS,
        "interfaces": _leaf_interfaces(),
    }
    return gpo_view("vxlan_gpo", roots, **{**arguments, **overrides})


PAY_P = ("pay-p",)


def _placed_roots(*, web: list[dict] | None = None, db: list[dict] | None = None) -> dict[str, Any]:
    """The fleet roots with the instances of the web / db component of pay-p replaced (None: the fixture's)."""
    raw = _raw()
    for edge in raw["SecurityTag"]["edges"]:
        tag = edge["node"]
        found = {"tag-pay-web": web, "tag-pay-db": db}[tag["id"]]
        if found is not None:
            tag["components"]["edges"][0]["node"]["instances"] = _edges(found)
    return _roots(raw)


def _coverage(roots: dict[str, Any], dc_id: str = "dc10"):
    contracts = data_center_contracts(roots, dc_id)
    return component_coverage(
        contracts, vtep_names={vtep["name"] for vtep in contracts.vteps}, other_vtep_names={"bl-dc12-01"}
    )


def _nic(host: str, nic: str, switch: str, port: str, tags: tuple[str, ...] = PAY_P, **kwargs: Any) -> dict:
    return _gp_nic(host, nic, switch, port, tags, **kwargs)
