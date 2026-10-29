"""The transform's VRF demand and the generator's agree on every smoke golden input.

Two gatherers feed the one derivation (utils/vrf_demand.py ``vrf_demand``): the generators and checks/vrf_instances.py
read queries/validation/vrf_instances.gql through ``device_vrf_facts``, the transforms read their config query through
transforms/helpers/vrf.py ``vrf_requirements``. For each smoke input (a config query response) the device is re-shaped
into the vrf_instances.gql shape (its routed rows are the rows of its own VLAN domain behind its segment activations),
and both must name the same namespaces: a drift in the address, leg, SVI or role handling of either side fails here.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from transforms.common import BaseDeviceTransform
from transforms.helpers.vrf import vrf_requirements
from utils.limits import DEFAULT_NAMESPACE
from utils.vlan_domain import vlan_domain_id
from utils.vrf_demand import device_vrf_facts

CONFIGS = Path(__file__).resolve().parents[1] / "smoke" / "configs"
INPUTS = sorted(CONFIGS.glob("*/input.json"))


def _prepared(path: Path) -> dict[str, Any] | None:
    """The cleaned device of a smoke input with its segment activations, or None for a non-device fixture."""
    raw = json.loads(path.read_text())
    if "DcimDevice" not in raw and "DcimPhysicalDevice" not in raw:
        return None
    transform = BaseDeviceTransform.__new__(BaseDeviceTransform)
    device, _, platform = transform.prepared_device(raw)
    return device if platform else None


def _as_vrf_instances_node(device: dict[str, Any]) -> dict[str, Any]:
    """The device in the cleaned vrf_instances.gql shape: its activations become the rows of its own VLAN domain."""
    rows = [
        {"id": f"row-{act['vlan_id']}", "vlan_id": act["vlan_id"], "segment_deployment": {"segment": act["segment"]}}
        for act in device.get("segment_deployments") or []
        if act.get("vlan_id")
    ]
    capabilities = [dict(cap) for cap in device.get("capabilities") or []]
    domain = vlan_domain_id(device.get("id"), ((cap.get("typename"), cap.get("id")) for cap in capabilities))
    node = {**device, "capabilities": capabilities, "member_of_groups": [], "vlan_domain_segments": []}
    owner = next((cap for cap in capabilities if domain and cap.get("id") == domain), node)
    owner["vlan_domain_segments"] = rows
    return node


@pytest.mark.parametrize("path", INPUTS, ids=[path.parent.name for path in INPUTS])
def test_the_transform_and_the_generator_demand_the_same_vrfs(path: Path) -> None:
    """``vrf_requirements`` (config shape) equals ``device_vrf_facts(...).demand`` (vrf_instances.gql shape)."""
    device = _prepared(path)
    if device is None:
        pytest.skip("not a device config fixture")
    assert vrf_requirements(device).namespaces == device_vrf_facts(_as_vrf_instances_node(device)).demand


def test_the_goldens_include_devices_with_each_kind_of_demand() -> None:
    """The agreement above is not vacuous: some golden device demands a VRF through each source."""
    seen: dict[str, int] = {"rows": 0, "legs": 0, "addresses": 0}
    for path in INPUTS:
        device = _prepared(path)
        if device is None:
            continue
        inputs = device_vrf_facts(_as_vrf_instances_node(device)).inputs
        seen["rows"] += bool(inputs.row_namespaces - {DEFAULT_NAMESPACE})
        seen["legs"] += bool(inputs.leg_namespaces)
        seen["addresses"] += bool(inputs.address_namespaces - {DEFAULT_NAMESPACE})
    assert all(seen.values()), seen
