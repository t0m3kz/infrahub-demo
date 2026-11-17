"""The transform's VRF demand and the generator's agree on every smoke golden input."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from transforms.common import BaseDeviceTransform
from transforms.helpers.vrf import vrf_requirements
from utils.limits import DEFAULT_NAMESPACE
from utils.service_contexts import SERVICE_PORTS_KEY, device_services
from utils.vrf_demand import device_vrf_facts

CONFIGS = Path(__file__).resolve().parents[2] / "smoke" / "configs"


def _prepared(path: Path) -> dict[str, Any] | None:
    """The cleaned device of a smoke input with its segment activations, or None for a non-device fixture."""
    raw = json.loads(path.read_text())
    if "DcimDevice" not in raw and "DcimPhysicalDevice" not in raw:
        return None
    transform = BaseDeviceTransform.__new__(BaseDeviceTransform)
    device, _, platform = transform.prepared_device(raw)
    return device if platform else None


def _as_vrf_instances_node(device: dict[str, Any]) -> dict[str, Any]:
    """The device in the cleaned vrf_instances.gql shape: its activations become its own rows."""
    rows = [
        {"id": f"row-{act['vlan_id']}", "vlan_id": act["vlan_id"], "segment_deployment": {"segment": act["segment"]}}
        for act in device.get("segment_deployments") or []
        if act.get("vlan_id")
    ]
    capabilities = [dict(cap) for cap in device.get("capabilities") or []]
    ports = device_services(device).get(SERVICE_PORTS_KEY) or []
    node = {**device, "capabilities": capabilities, "member_of_groups": [], "vlans": rows, SERVICE_PORTS_KEY: ports}
    return node


# Only device config inputs: cloud load-balancer and proxy inputs carry no device and no VRF.
INPUTS = [path for path in sorted(CONFIGS.glob("*/input.json")) if _prepared(path) is not None]


@pytest.mark.parametrize("path", INPUTS, ids=[path.parent.name for path in INPUTS])
def test_the_transform_and_the_generator_demand_the_same_vrfs(path: Path) -> None:
    """``vrf_requirements`` (config shape) equals ``device_vrf_facts(...).demand`` (vrf_instances.gql shape)."""
    device = _prepared(path)
    assert device is not None
    assert vrf_requirements(device).namespaces == device_vrf_facts(_as_vrf_instances_node(device)).demand


def test_the_goldens_include_devices_with_each_kind_of_demand() -> None:
    """The agreement above is not vacuous: some golden device demands a VRF through each source."""
    seen: dict[str, int] = {"rows": 0, "legs": 0, "addresses": 0}
    for path in INPUTS:
        device = _prepared(path)
        assert device is not None
        inputs = device_vrf_facts(_as_vrf_instances_node(device)).inputs
        seen["rows"] += bool(inputs.row_namespaces - {DEFAULT_NAMESPACE})
        seen["legs"] += bool(inputs.leg_namespaces)
        seen["addresses"] += bool(inputs.address_namespaces - {DEFAULT_NAMESPACE})
    assert all(seen.values()), seen
