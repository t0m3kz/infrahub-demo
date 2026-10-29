"""Shared builders and renderers for the Firewall transform tests (test_firewall_transform_*.py)."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from transforms.config.firewall import Firewall

# Project root — needed to point root_directory at the real templates/
ROOT = str(Path(__file__).parent.parent.parent)


def _make_fw() -> Firewall:
    """Instantiate Firewall bypassing InfrahubTransform __init__."""
    fw = Firewall.__new__(Firewall)
    fw.root_directory = ROOT
    return fw


# ===========================================================================
# Shared smoke-test data builder
# ===========================================================================


# The smoke segment as a rule references it (PolicyRuleSegmentFields).
_SMOKE_SEGMENT_REF: dict[str, Any] = {"id": "seg-smoke", "name": {"value": "seg-smoke"}}


def _make_smoke_data(platform: str) -> dict[str, Any]:
    """Build a realistic but minimal raw GQL dict for a full-render smoke test.

    Includes:
    - One FW device with the given platform
    - One DcimFirewallInterface with a security_zone
    - One zone with one segment whose own policy has one permit rule
    """
    # Segment node embedded in capabilities (security_zone + policies on segment)
    seg_node: dict[str, Any] = {
        "__typename": "ManagedVxlanSegment",
        "id": "seg-smoke",
        "name": {"value": "seg-smoke"},
        "status": {"value": "active"},
        "arp_suppression": {"value": True},
        "segment_deployments": {"edges": [{"node": {"vlan_id": {"value": 100}, "vni": {"value": 10100}}}]},
        "gateway": {"node": None},
        "prefix": {
            "edges": [
                {
                    "node": {
                        "prefix": {"value": "10.10.10.0/24"},
                        "ip_namespace": {"node": {"name": {"value": "VRF-SMOKE"}, "l3_vni": {"value": None}}},
                    }
                }
            ]
        },
        "security_zone": {
            "node": {
                "name": {"value": "internal"},
                "trust_level": {"value": 100},
            }
        },
        "inbound_rules": {"edges": []},
        "security_policy": {
            "node": {
                "id": "pol-smoke",
                "name": {"value": "seg-pol-smoke"},
                "segment": {"node": {"id": "seg-smoke", "name": {"value": "seg-smoke"}}},
                "default_action": {"value": "deny"},
                "enabled": {"value": True},
                "rules": {
                    "edges": [
                        {
                            "node": {
                                "id": "rule-smoke-permit",
                                "index": {"value": 10},
                                "name": {"value": "smoke-permit-rule"},
                                "action": {"value": "permit"},
                                "protocol": {"value": "tcp"},
                                "port_start": {"value": 443},
                                "port_end": {"value": None},
                                "log": {"value": False},
                                "disabled": {"value": False},
                                "apply_on_switch": {"value": False},
                                "description": {"value": "HTTPS"},
                                "source_segment": {"node": _SMOKE_SEGMENT_REF},
                                "destination_segment": {"node": None},
                                "security_profile": {"node": None},
                            }
                        }
                    ]
                },
            }
        },
    }

    # FW interface with IP — zone and policies come from capabilities segment
    fw_iface_node: dict[str, Any] = {
        "__typename": {"value": "DcimVirtualInterface"},
        "name": {"value": "eth0.100"},
        "description": {"value": "Smoke test interface"},
        "status": {"value": "active"},
        "role": {"value": None},
        "parent_interface": {"node": {"name": {"value": "eth0"}}},
        "ip_address": {
            "node": {
                "address": {"value": "10.99.99.1/30"},
                "ip_namespace": {"node": {"name": {"value": "VRF-SMOKE"}}},
            }
        },
        "ha_domain": {"node": None},
        "capabilities": {"edges": [{"node": seg_node}]},
    }

    # Zone also exposed as a top-level SecurityZone root
    zone_node: dict[str, Any] = {
        "name": {"value": "internal"},
        "trust_level": {"value": 100},
        "description": {"value": "Internal zone"},
        "network_segments": {
            "edges": [
                {
                    "node": {
                        "name": {"value": "seg-smoke"},
                        "prefix": {
                            "edges": [
                                {
                                    "node": {
                                        "prefix": {"value": "10.10.10.0/24"},
                                    }
                                }
                            ]
                        },
                    }
                }
            ]
        },
    }

    return {
        "DcimPhysicalDevice": {
            "edges": [
                {
                    "node": {
                        "name": {"value": f"fw-{platform}"},
                        "role": {"value": "firewall"},
                        "platform": {
                            "node": {
                                "name": {"value": platform},
                            }
                        },
                        "interfaces": {"edges": [{"node": fw_iface_node}]},
                        "capabilities": {"edges": []},
                    }
                }
            ]
        },
        "SecurityZone": {"edges": [{"node": zone_node}]},
    }


def _smoke_segment(data: dict[str, Any]) -> dict[str, Any]:
    """The segment the smoke firewall carries on eth0.100."""
    iface = data["DcimPhysicalDevice"]["edges"][0]["node"]["interfaces"]["edges"][0]["node"]
    return iface["capabilities"]["edges"][0]["node"]


def _add_segment_rule(data: dict[str, Any], rule_node: dict[str, Any]) -> None:
    """Append a rule to the carried segment's own policy (every rule lives in
    its source segment's policy)."""
    _smoke_segment(data)["security_policy"]["node"]["rules"]["edges"].append({"node": rule_node})


# ===========================================================================
# Class 3 — Address-family-aware rendering (IPv6 P2P links, IPv4 fw_interfaces)
# ===========================================================================
#
# FW-context P2P links default to IPv6 (/127, generators/topology/dc.py's
# FirewallContext legs) — templates must branch on address family
# rather than hardcoding an IPv4-only command (e.g. PAN-OS "ipv4 addr" for
# an actual IPv6 address is simply wrong syntax). Renders templates directly
# (not through Firewall.transform()) since only the address-family branching
# is under test here, not the full data-shaping pipeline.


def _render_template(platform: str, **context: Any) -> str:
    fw = _make_fw()
    template = fw._load_template(platform)
    return template.render(**context)


def _make_context_smoke_data(platform: str, *, serves_segment: bool) -> dict[str, Any]:
    """Smoke data whose firewall also has a dedicated context. The context
    serves the carried segment only when one of its served deployments
    lists it among its network_segments."""
    data = _make_smoke_data(platform)
    device = data["DcimPhysicalDevice"]["edges"][0]["node"]
    segment = _smoke_segment(data)
    served_segment = {
        "id": segment["id"],
        "security_policy": copy.deepcopy(segment["security_policy"]),
        "inbound_rules": {"edges": []},
    }
    context_iface: dict[str, Any] = {
        "__typename": {"value": "DcimVirtualInterface"},
        "name": {"value": "eth1.3005"},
        "description": {"value": None},
        "status": {"value": "active"},
        "role": {"value": None},
        "parent_interface": {"node": {"name": {"value": "eth1"}}},
        "ip_address": {"node": {"address": {"value": "100.65.0.4/31"}, "ip_namespace": {"node": None}}},
        "ha_domain": {"node": None},
        "capabilities": {
            "edges": [
                {
                    "node": {
                        "__typename": "ManagedFirewallContext",
                        "id": "ctx-c005",
                        "name": {"value": "c005-dedicated"},
                        "vlan_id": {"value": 3005},
                        "context_id": {"value": None},
                        "tenant": {"node": {"id": "dep-c005", "name": {"value": "C005-P-DC12"}}},
                        "served_deployments": {
                            "edges": [
                                {
                                    "node": {
                                        "id": "dep-c005",
                                        "network_segments": {
                                            "edges": [{"node": served_segment}] if serves_segment else []
                                        },
                                    }
                                }
                            ]
                        },
                    }
                }
            ]
        },
    }
    device["interfaces"]["edges"].append({"node": context_iface})
    return data
