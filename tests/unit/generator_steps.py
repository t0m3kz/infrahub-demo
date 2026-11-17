"""The steps of the segment reconcile (generators/segments/segment_vlan.py, segment_ports.py) and the VRF pass
(generators/capabilities/vrf.py) are module functions that take the generator first. A test that stubs one on its
generator (``gen._port_devices = AsyncMock(...)``, called without the generator) gets the stub through
``generator_steps``: each function looks for an instance attribute of the old name on the generator it is given and calls
that, with the arguments it would have had as a method.
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable
from typing import Any

import pytest

from generators.applications import app_instance_segment
from generators.connectivity import context_routes
from generators.decommission import pod
from generators.segments import segment, segment_deployment_ports, segment_ports, segment_vlan

# (module, function name, attribute name a test stubs it by)
_STEPS: tuple[tuple[Any, str, str], ...] = (
    (segment_vlan, "_choose_vlan_ids", "_choose_vlan_ids"),
    (segment_vlan, "_read_vlan_ids", "_read_vlan_ids"),
    (segment_vlan, "_write_segment_vlans", "_write_segment_vlans"),
    (segment_vlan, "fetch_segment_vlan_state", "_fetch_segment_vlan_state"),
    (segment_vlan, "_fetch_segment_ports", "_fetch_segment_ports"),
    (segment_vlan, "_segment_vlan_devices", "_segment_vlan_devices"),
    (segment_vlan, "_read_devices", "_read_devices"),
    (segment_vlan, "_shared_vlan_id", "_shared_vlan_id"),
    (segment_vlan, "reconcile_segment_vlans", "_reconcile_segment_vlans_locked"),
    (segment_ports, "_segment_components", "_segment_components"),
    (segment_ports, "_derived_segment_port_ids", "_derived_segment_port_ids"),
    (segment_ports, "_port_devices", "_port_devices"),
    (segment_ports, "_reconcile_segment_port_rows", "_reconcile_segment_port_rows"),
    (segment_ports, "_customer_facing_target_ids", "_customer_facing_target_ids"),
    (segment_ports, "_customer_facing_target_id", "_customer_facing_target_id"),
    (segment_ports, "fetch_segment_vlan_state", "_fetch_segment_vlan_state"),
    (segment_ports, "reconcile_segment_vlans", "_reconcile_segment_vlans_locked"),
    (segment_ports, "reconcile_vrfs", "reconcile_vrfs"),
    (pod, "reconcile_vrfs", "reconcile_vrfs"),
    (pod, "prune_empty_vrfs", "prune_empty_vrfs"),
    (context_routes, "reconcile_vrfs", "reconcile_vrfs"),
    (segment, "reconcile_vxlan_segment", "reconcile_vxlan_segment"),
    (segment_deployment_ports, "reconcile_vxlan_segment", "reconcile_vxlan_segment"),
    (app_instance_segment, "reconcile_vxlan_segment", "reconcile_vxlan_segment"),
)


def _stubbed(real: Callable[..., Any], attribute: str) -> Callable[..., Any]:
    """``real``, or the stub the generator carries under ``attribute``."""
    if inspect.iscoroutinefunction(real):

        @functools.wraps(real)
        async def call(gen: Any, *args: Any, **kwargs: Any) -> Any:
            stub = vars(gen).get(attribute)
            return await (stub(*args, **kwargs) if stub is not None else real(gen, *args, **kwargs))

        return call

    @functools.wraps(real)
    def call_sync(gen: Any, *args: Any, **kwargs: Any) -> Any:
        stub = vars(gen).get(attribute)
        return stub(*args, **kwargs) if stub is not None else real(gen, *args, **kwargs)

    return call_sync


@pytest.fixture
def generator_steps(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let a test stub the steps of the segment reconcile and the VRF pass on its generator."""
    for module, name, attribute in _STEPS:
        monkeypatch.setattr(module, name, _stubbed(getattr(module, name), attribute))
