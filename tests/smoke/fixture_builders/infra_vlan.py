"""Infrastructure-VLAN fixtures: a leaf with a transfer VLAN (a ManagedVlan capability) on a second port.

Each fixture is a NEW directory next to the segment-only ones, whose goldens stay byte-identical: an infrastructure
VLAN is the only thing that makes a template declare a VLAN with no segment behind it (no SVI, no VNI, no VRF).
"""

from __future__ import annotations

from tests.smoke.fixture_builders.common import _make_interface, _node, _v, write_data_fixtures
from tests.smoke.fixture_builders.fabric import build_device_data
from transforms.config.leaf import Leaf

LEAF_PLATFORMS = ("cisco_nxos", "arista_eos", "sonic", "dell_sonic")
TRANSFER_VLAN_ID = 20
SECOND_VLAN_ID = 30


def _infra_vlan(vlan_id: int, name: str, device_name: str) -> dict:
    """A ManagedVlan capability node as the interface capability fragment returns it."""
    return {
        "__typename": "ManagedVlan",
        "id": f"vlan-{vlan_id}",
        "name": _v(name),
        "description": _v(f"{name} L2"),
        "vlan_id": _v(vlan_id),
        "vlan_domain": _node({"id": f"dev-{device_name}"}),
    }


def build_infra_vlan_leaf_data(*, device_name: str, platform: str) -> dict:
    """A leaf with its segments plus Ethernet11 (one transfer VLAN, access) and Ethernet12 (two, trunk)."""
    data = build_device_data(
        device_name=device_name,
        role="leaf",
        platform=platform,
        scenario="ebgp_ibgp",
        include_segments=True,
    )
    transfer = _infra_vlan(TRANSFER_VLAN_ID, "transfer-fw", device_name)
    second = _infra_vlan(SECOND_VLAN_ID, "mgmt-oob", device_name)
    interfaces = data["DcimDevice"]["edges"][0]["node"]["interfaces"]["edges"]
    for name, description, vlans in (
        ("Ethernet11", "to firewall transfer", [transfer]),
        ("Ethernet12", "to oob switch", [transfer, second]),
    ):
        interfaces.append(
            {
                "node": _make_interface(
                    name=name, device_name=device_name, description=description, role="server", segments=vlans
                )
            }
        )
    return data


def write_fixtures() -> tuple[int, int]:
    """Write the leaf with infrastructure VLANs on every leaf platform."""
    fixtures = [
        (Leaf, f"leaf_{platform}_infra_vlan", build_infra_vlan_leaf_data(device_name="dc1-leaf-01", platform=platform))
        for platform in LEAF_PLATFORMS
    ]
    print("\nGenerating infrastructure VLAN fixtures (leaf):")
    return write_data_fixtures(fixtures)
