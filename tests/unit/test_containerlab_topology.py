"""Tests for Containerlab topology rendering."""

from __future__ import annotations

from pathlib import Path

import jinja2
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _endpoint(device_name: str, interface_name: str) -> dict:
    return {
        "node": {
            "name": {"value": interface_name},
            "device": {
                "node": {
                    "name": {"value": device_name},
                    "platform": {"node": {"containerlab_os": {"value": "sonic-vm"}}},
                }
            },
        }
    }


def _interface(device_name: str, interface_name: str, peer_name: str) -> dict:
    cable_endpoints = [
        _endpoint(device_name, interface_name),
        _endpoint(peer_name, interface_name),
    ]
    return {
        "node": {
            "name": {"value": interface_name},
            "cable": {"node": {"endpoints": {"edges": cable_endpoints}}},
        }
    }


def test_sonic_vm_topology_uses_real_cable_endpoints_and_port_mapping() -> None:
    """Render SONiC VM ports as valid Containerlab node/interface endpoints."""
    devices = []
    for device_name, peer_name in (("leaf-a", "leaf-b"), ("leaf-b", "leaf-a")):
        devices.append(
            {
                "node": {
                    "name": {"value": device_name},
                    "platform": {"node": {"containerlab_os": {"value": "sonic-vm"}}},
                    "device_type": {"node": {"name": {"value": "sonic-vm"}}},
                    "software_image": {"node": {"containerlab_image": {"value": "vrnetlab/vr-sonic:202405"}}},
                    "primary_address": {"node": {"address": {"value": "172.20.0.11/24"}}},
                    "interfaces": {
                        "edges": [
                            _interface(device_name, "Ethernet0", peer_name),
                            _interface(device_name, "Ethernet4", peer_name),
                        ]
                    },
                }
            }
        )

    template = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(PROJECT_ROOT / "templates")),
        trim_blocks=True,
        lstrip_blocks=True,
    ).get_template("clab_topology.j2")
    rendered = template.render(
        data={
            "TopologyDeployment": {"edges": [{"node": {"name": {"value": "sonic-lab"}, "devices": {"edges": devices}}}]}
        },
    )
    topology = next(yaml.safe_load_all(rendered))

    assert topology["topology"]["nodes"]["leaf-a"]["kind"] == "sonic-vm"
    assert topology["topology"]["nodes"]["leaf-a"]["image"] == "vrnetlab/vr-sonic:202405"
    assert topology["topology"]["links"] == [
        {"endpoints": ["leaf-a:eth1", "leaf-b:eth1"]},
        {"endpoints": ["leaf-a:eth2", "leaf-b:eth2"]},
    ]
