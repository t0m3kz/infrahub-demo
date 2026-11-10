"""Hosted termination follows the real cleaned GraphQL NIC -> physical host -> switch shape."""

from pathlib import Path
from typing import Any

import pytest
from graphql import FragmentDefinitionNode, parse

from tests.smoke.fixture_builders.vrf import interface_vrf_capability, routing_vrf
from tests.unit.encapsulation_helpers import raw_encapsulation
from tests.unit.firewall_transform_helpers import _make_fw
from transforms.helpers.firewall_contexts import get_firewall_contexts
from transforms.helpers.firewall_terminated import terminated_interfaces
from utils.data_cleaning import clean_data


def test_facing_cable_fragment_is_a_definition_not_an_interface_field(root_dir: Path) -> None:
    """GraphQL parses an accidentally nested ``fragment`` as a field: require the actual top-level definition."""
    document = parse((root_dir / "queries/fragments/firewall_cabling.gql").read_text())
    names = {
        definition.name.value for definition in document.definitions if isinstance(definition, FragmentDefinitionNode)
    }
    assert "FirewallFacingCableFields" in names


def _payload(
    *, cabled: bool = True, far_device: str | None = "leaf", own_device: str | None = "host"
) -> dict[str, Any]:
    """A raw firewall query response with a virtual uplink and one addressed context leg."""
    segment = {
        "id": "segment",
        "name": {"value": "terminated"},
        "gateway": {"node": {"address": {"value": "10.90.2.1/24"}}},
        "security_zone": {"node": {"name": {"value": "PROD"}}},
        "segment_deployments": {
            "edges": [
                {
                    "node": {
                        "vlans": {
                            "edges": [
                                {"node": {"vlan_id": {"value": 150}, "device": {"node": {"id": "leaf"}}}},
                                {"node": {"vlan_id": {"value": 999}, "device": {"node": {"id": "host"}}}},
                            ]
                        },
                    }
                }
            ]
        },
    }
    context = {
        "__typename": "ManagedFirewallContext",
        "id": "context",
        "name": {"value": "dedicated"},
        "vlan_id": {"value": 3000},
        "terminated_segments": {"edges": [{"node": segment}]},
        "served_deployments": {
            "edges": [{"node": {"id": "footprint", "network_segments": {"edges": [{"node": segment}]}}}]
        },
    }
    cable = {
        "node": {
            "endpoints": {
                "edges": [
                    {"node": {"device": {"node": {"id": "host"}}}},
                    {"node": {"device": {"node": {"id": far_device}}} if far_device else {}},
                ]
            }
        }
    }
    return {
        "DcimDevice": {
            "edges": [
                {
                    "node": {
                        "id": "vm",
                        "name": {"value": "vm-firewall"},
                        "role": {"value": "internal-firewall"},
                        "shared_capabilities": {"edges": [{"node": context}]},
                        "interfaces": {
                            "edges": [
                                {
                                    "node": {
                                        "__typename": "DcimVirtualInterface",
                                        "name": {"value": "eth1"},
                                        "role": {"value": "uplink"},
                                        **raw_encapsulation(
                                            None,
                                            {
                                                "name": {"value": "eth3"},
                                                "device": {"node": {"id": own_device}}
                                                if own_device
                                                else {"node": None},
                                                "cable": cable if cabled else {"node": None},
                                            },
                                        ),
                                        "capabilities": {"edges": []},
                                    }
                                },
                                {
                                    "node": {
                                        **interface_vrf_capability(routing_vrf("PROD")),
                                        "__typename": "DcimVirtualInterface",
                                        "name": {"value": "eth1.3000"},
                                        **raw_encapsulation(
                                            3000, {"name": {"value": "eth1"}, "device": {"node": {"id": "vm"}}}
                                        ),
                                        "device": {"node": {"id": "vm"}},
                                        "ip_address": {
                                            "node": {
                                                "address": {"value": "10.250.0.5/29"},
                                                "ip_namespace": {"node": {"name": {"value": "default"}}},
                                            }
                                        },
                                        "capabilities": {"edges": []},
                                    }
                                },
                            ]
                        },
                    }
                }
            ]
        }
    }


def _render(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Run the production context selector and termination derivation on a raw query payload."""
    device = clean_data(payload)["DcimDevice"][0]
    ha = {"name": "pair"}
    contexts = get_firewall_contexts(device, ha)
    return terminated_interfaces(contexts, device["interfaces"], ha, device["id"], device["name"])


def test_hosted_termination_uses_switch_vlan_not_chassis_vlan() -> None:
    """The virtual trunk keeps its name and takes the facing switch's VLAN, never its host's row."""
    (interface,) = _render(_payload())
    assert interface["name"] == "eth1.150"
    assert interface["parent_interface"] == {"name": "eth1"}
    assert interface["virtual_ip"] == "10.90.2.1/24"
    assert interface["security_zone"] == {"name": "PROD"}


@pytest.mark.parametrize(
    "options",
    [
        {"cabled": False},
        {"far_device": None},
        {"far_device": "host"},
        {"own_device": None},
    ],
    ids=["uncabled-host", "patch-panel-end", "host-only-end", "missing-host-id"],
)
def test_hosted_termination_without_proven_facing_switch_renders_nothing(options: dict[str, Any]) -> None:
    """Null relationships and nonmatching fragments cannot produce a guessed VLAN or gateway."""
    assert _render(_payload(**options)) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("platform", "membership", "forbidden"),
    [
        (
            "paloalto_panos",
            (
                "set vsys dedicated import network interface eth1.150",
                "set network virtual-router vr-dedicated interface eth1.150",
                "set vsys dedicated zone PROD network layer3 eth1.150",
            ),
            "set zone PROD network layer3 eth1.150",
        ),
        (
            "juniper_junos",
            (
                "set tenants dedicated interfaces eth1 unit 150 family inet address 10.90.2.1/24",
                "set tenants dedicated security zones security-zone PROD interfaces eth1.150",
                "set tenants dedicated routing-instances ri-dedicated interface eth1.150",
            ),
            "set security zones security-zone PROD interfaces eth1.150",
        ),
        (
            "checkpoint_gaia",
            ("set virtual-system dedicated interface eth1.150",),
            "set virtual-system other interface eth1.150",
        ),
    ],
)
async def test_full_config_attaches_terminated_gateway_to_its_policy_context(
    platform: str, membership: tuple[str, ...], forbidden: str
) -> None:
    """A terminated gateway belongs to its policy/routing context, not the shared appliance's root table."""
    payload = _payload()
    payload["DcimDevice"]["edges"][0]["node"]["platform"] = {"node": {"name": {"value": platform}}}
    rendered = await _make_fw().transform(payload)
    for line in membership:
        assert line in rendered.splitlines()
    assert forbidden not in rendered.splitlines()
