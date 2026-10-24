"""Shared renderers and transform builders for the ``test_base_device_transform_*`` modules."""

from __future__ import annotations

from pathlib import Path

import jinja2

from transforms.common import BaseDeviceTransform, _loopback_name
from transforms.config.tor import ToR


def _render_leaf(platform: str, **overrides: object) -> str:
    """Render leafs/<platform>.j2 with an empty leaf context plus overrides."""
    template_dir = Path(__file__).parents[2] / "templates" / "configs"
    template = jinja2.Environment(loader=jinja2.FileSystemLoader(str(template_dir))).get_template(
        f"leafs/{platform}.j2"
    )
    context: dict[str, object] = {
        "hostname": "test-leaf",
        "vlans": [],
        "interfaces": [],
        "loopback_name": _loopback_name([], platform),
        "acls": [],
        "vxlan": None,
        "vrf_routes": [],
        "bgp": [],
        "ospf": [],
        "mlag": None,
        **overrides,
    }
    return template.render(**context)


def _make_transform(device_role: str = "") -> BaseDeviceTransform:
    """Instantiate BaseDeviceTransform bypassing InfrahubTransform __init__."""
    t = BaseDeviceTransform.__new__(BaseDeviceTransform)
    t.device_role = device_role
    t.template_subdir = "leafs"
    t.root_directory = "/fake/root"
    return t


def _make_tor() -> ToR:
    t = ToR.__new__(ToR)
    t.root_directory = "/fake/root"
    return t


def _device_data(
    *,
    platform: str | None = "arista_eos",
    name: str = "leaf-01",
    role: str = "leaf",
    interfaces: list | None = None,
    device_capabilities: list | None = None,
) -> dict:
    return {
        "name": name,
        "role": role,
        "platform": {"netmiko_device_type": platform} if platform else None,
        "interfaces": interfaces or [],
        "capabilities": device_capabilities or [],
    }
