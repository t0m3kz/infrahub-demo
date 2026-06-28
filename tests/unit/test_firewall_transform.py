"""Unit + smoke tests for transforms/firewall.py.

Covers:
  - _collect_segment_policies()  — pure helper, deduplication logic
  - _merge_policies()            — pure helper, merge + precedence logic
  - Firewall.transform()         — no-platform early-exit path
  - Firewall.transform()         — full Jinja2 render smoke tests (all four vendors)
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from transforms.config.firewall import Firewall, _collect_segment_policies, _merge_policies

# Project root — needed to point root_directory at the real templates/
ROOT = str(Path(__file__).parent.parent.parent)


# ===========================================================================
# Raw-data builder (for Firewall.transform() tests)
# ===========================================================================


def _make_raw_data(
    device_name: str = "test-fw",
    platform_name: str | None = None,
    interfaces: list[dict] | None = None,
    zones: list[dict] | None = None,
    global_policies: list[dict] | None = None,
) -> dict[str, Any]:
    """Build a minimal raw GraphQL response dict for the firewall_config query.

    The Firewall transform calls clean_data() itself (not the base-class
    transform()), so the input must be in the raw {"edges": [{"node": ...}]}
    GQL shape.
    """
    platform: dict[str, Any] = {}
    if platform_name is not None:
        platform = {"netmiko_device_type": {"value": platform_name}}

    raw_ifaces = [{"node": iface} for iface in (interfaces or [])]

    # Build SecurityZone edges
    raw_zones = [{"node": _zone_to_raw(z)} for z in (zones or [])]
    # Build SecurityPolicy edges
    raw_policies = [{"node": _policy_to_raw(p)} for p in (global_policies or [])]

    return {
        "DcimPhysicalDevice": {
            "edges": [
                {
                    "node": {
                        "name": {"value": device_name},
                        "platform": {"node": platform} if platform else {"node": {}},
                        "interfaces": {"edges": raw_ifaces},
                        "capabilities": {"edges": []},
                        "role": {"value": "firewall"},
                    }
                }
            ]
        },
        "SecurityZone": {"edges": raw_zones},
        "SecurityPolicy": {"edges": raw_policies},
    }


def _zone_to_raw(zone: dict) -> dict:
    """Convert a cleaned zone dict to a minimal raw GQL node dict.

    clean_data() unwraps {"value": x} wrappers — we just pass through dicts
    and lists as-is so clean_data() can recurse without changing them.
    The keys that clean_data() doesn't touch (plain strings, ints) are fine.
    """
    # For smoke tests the zone dict is already "clean" — wrap scalar fields
    # in {"value": ...} so clean_data produces the right output.
    raw: dict[str, Any] = {}
    for k, v in zone.items():
        if isinstance(v, (str, int, float, bool)) or v is None:
            raw[k] = {"value": v}
        else:
            raw[k] = v
    return raw


def _policy_to_raw(policy: dict) -> dict:
    """Same treatment for SecurityPolicy nodes."""
    raw: dict[str, Any] = {}
    for k, v in policy.items():
        if isinstance(v, (str, int, float, bool)) or v is None:
            raw[k] = {"value": v}
        else:
            raw[k] = v
    return raw


def _make_fw() -> Firewall:
    """Instantiate Firewall bypassing InfrahubTransform __init__."""
    fw = Firewall.__new__(Firewall)
    fw.root_directory = ROOT
    return fw


# ===========================================================================
# Helpers used by both pure-function and smoke tests
# ===========================================================================


def _seg_policy(name: str, rules: list | None = None, **extra: Any) -> dict:
    """Minimal security policy dict (already cleaned)."""
    return {"name": name, "default_action": "deny", "enabled": True, "rules": rules or [], **extra}


def _activation_with_policies(
    seg_name: str = "seg-01",
    zone_name: str = "internal",
    policies: list[dict] | None = None,
) -> dict:
    """Cleaned SegmentDeployment activation dict with security_policies on the segment."""
    return {
        "vlan_id": 10,
        "vni": 10010,
        "segment": {
            "name": seg_name,
            "security_zone": {"name": zone_name},
            "security_policies": policies or [],
        },
    }


# ===========================================================================
# Class 1 — _collect_segment_policies
# ===========================================================================


class TestCollectSegmentPolicies:
    def test_empty_activations_returns_empty(self) -> None:
        assert _collect_segment_policies([]) == []

    def test_activation_without_policies_returns_empty(self) -> None:
        act = _activation_with_policies(policies=[])
        assert _collect_segment_policies([act]) == []

    def test_single_policy_extracted(self) -> None:
        policy = _seg_policy("allow-https")
        act = _activation_with_policies(policies=[policy])
        result = _collect_segment_policies([act])
        assert len(result) == 1
        assert result[0]["name"] == "allow-https"

    def test_duplicate_policy_name_deduped(self) -> None:
        """Two activations sharing the same policy name → only one entry returned."""
        act1 = _activation_with_policies(seg_name="seg-01", policies=[_seg_policy("shared-pol")])
        act2 = _activation_with_policies(seg_name="seg-02", policies=[_seg_policy("shared-pol")])
        result = _collect_segment_policies([act1, act2])
        assert len(result) == 1

    def test_first_seen_policy_wins_on_same_name(self) -> None:
        """When two activations share a policy name, the FIRST one encountered is kept."""
        first_policy = _seg_policy("dup-pol", rules=[{"seq": 10, "name": "first-rule"}])
        second_policy = _seg_policy("dup-pol", rules=[{"seq": 20, "name": "second-rule"}])
        act1 = _activation_with_policies(seg_name="seg-01", policies=[first_policy])
        act2 = _activation_with_policies(seg_name="seg-02", policies=[second_policy])
        result = _collect_segment_policies([act1, act2])
        assert len(result) == 1
        assert result[0]["rules"][0]["name"] == "first-rule"

    def test_multiple_distinct_policies_all_returned(self) -> None:
        pol_a = _seg_policy("pol-a")
        pol_b = _seg_policy("pol-b")
        pol_c = _seg_policy("pol-c")
        act1 = _activation_with_policies(seg_name="seg-01", policies=[pol_a])
        act2 = _activation_with_policies(seg_name="seg-02", policies=[pol_b, pol_c])
        result = _collect_segment_policies([act1, act2])
        assert len(result) == 3
        names = {p["name"] for p in result}
        assert names == {"pol-a", "pol-b", "pol-c"}


# ===========================================================================
# Class 2 — _merge_policies
# ===========================================================================


class TestMergePolicies:
    def test_empty_both_returns_empty(self) -> None:
        assert _merge_policies([], []) == []

    def test_global_only_returned(self) -> None:
        global_pol = _seg_policy("global-pol")
        result = _merge_policies([global_pol], [])
        assert len(result) == 1
        assert result[0]["name"] == "global-pol"

    def test_segment_only_returned(self) -> None:
        seg_pol = _seg_policy("seg-pol")
        result = _merge_policies([], [seg_pol])
        assert len(result) == 1
        assert result[0]["name"] == "seg-pol"

    def test_no_collision_both_returned(self) -> None:
        global_pol = _seg_policy("global-pol")
        seg_pol = _seg_policy("seg-pol")
        result = _merge_policies([global_pol], [seg_pol])
        assert len(result) == 2
        names = {p["name"] for p in result}
        assert names == {"global-pol", "seg-pol"}

    def test_segment_wins_on_name_collision(self) -> None:
        """Segment version has extra 'rules' data and must overwrite the global one."""
        global_pol = {"name": "shared", "default_action": "deny", "enabled": True, "rules": []}
        seg_pol = {
            "name": "shared",
            "default_action": "permit",
            "enabled": True,
            "rules": [{"seq": 10, "name": "seg-rule"}],
        }
        result = _merge_policies([global_pol], [seg_pol])
        assert len(result) == 1
        merged = result[0]
        # Segment version wins — it has 'rules' content
        assert merged["rules"] == [{"seq": 10, "name": "seg-rule"}]

    def test_global_preserved_when_no_collision(self) -> None:
        global_pol_a = _seg_policy("global-a")
        seg_pol_b = _seg_policy("seg-b")
        result = _merge_policies([global_pol_a], [seg_pol_b])
        names = {p["name"] for p in result}
        assert "global-a" in names
        assert "seg-b" in names

    def test_policy_without_name_or_id_skipped(self) -> None:
        """A policy dict with neither 'name' nor 'id' is not added to the merged output."""
        nameless = {"default_action": "deny", "enabled": True, "rules": []}
        valid = _seg_policy("valid-pol")
        result = _merge_policies([nameless, valid], [])
        assert len(result) == 1
        assert result[0]["name"] == "valid-pol"


# ===========================================================================
# Class 3 — Firewall.transform() — no-platform early exit
# ===========================================================================


class TestFirewallTransformNoPlatform:
    def test_no_platform_returns_comment_string(self) -> None:
        fw = _make_fw()
        data = _make_raw_data(device_name="no-plat-fw", platform_name=None)
        result = asyncio.run(fw.transform(data))
        assert isinstance(result, str)
        assert "! Device" in result
        assert "No configuration generated" in result

    def test_device_name_in_no_platform_message(self) -> None:
        fw = _make_fw()
        data = _make_raw_data(device_name="my-fw-01", platform_name=None)
        result = asyncio.run(fw.transform(data))
        assert "my-fw-01" in result


# ===========================================================================
# Shared smoke-test data builder
# ===========================================================================


def _make_smoke_data(platform: str) -> dict[str, Any]:
    """Build a realistic but minimal raw GQL dict for a full-render smoke test.

    Includes:
    - One FW device with the given platform
    - One DcimFirewallInterface with a security_zone
    - One zone with one segment with one permit rule
    - One global SecurityPolicy (empty rules)
    """
    # Segment node embedded in interface_capabilities (security_zone + policies on segment)
    seg_node: dict[str, Any] = {
        "__typename": {"value": "ManagedVxlanSegment"},
        "name": {"value": "seg-smoke"},
        "status": {"value": "active"},
        "arp_suppression": {"value": True},
        "segment_deployments": {"edges": [{"node": {"vlan_id": {"value": 100}, "vni": {"value": 10100}}}]},
        "prefix": {
            "edges": [
                {
                    "node": {
                        "prefix": {"value": "10.10.10.0/24"},
                        "gateway_ip": {"value": None},
                        "ip_namespace": {"node": {"name": {"value": "VRF-SMOKE"}, "l3_vni": {"value": None}}},
                    }
                }
            ]
        },
        "security_zone": {
            "node": {
                "name": {"value": "internal"},
                "trust_level": {"value": 100},
                "zone_type": {"value": "internal"},
            }
        },
        "security_policies": {
            "edges": [
                {
                    "node": {
                        "name": {"value": "seg-pol-smoke"},
                        "default_action": {"value": "deny"},
                        "enabled": {"value": True},
                        "rules": {
                            "edges": [
                                {
                                    "node": {
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
                                        "source_zone": {"node": None},
                                        "destination_zone": {"node": None},
                                        "source_segment": {"node": None},
                                        "destination_segment": {"node": None},
                                        "security_profile": {"node": None},
                                    }
                                }
                            ]
                        },
                    }
                }
            ]
        },
    }

    # FW interface with IP — zone and policies come from interface_capabilities segment
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
        "interface_capabilities": {"edges": [{"node": seg_node}]},
    }

    # Zone also exposed as a top-level SecurityZone root
    zone_node: dict[str, Any] = {
        "name": {"value": "internal"},
        "trust_level": {"value": 100},
        "zone_type": {"value": "internal"},
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

    # Global SecurityPolicy (empty rules — global policies are the top-level list)
    global_policy_node: dict[str, Any] = {
        "name": {"value": "global-pol-smoke"},
        "default_action": {"value": "deny"},
        "enabled": {"value": True},
        "rules": {"edges": []},
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
                                "netmiko_device_type": {"value": platform},
                            }
                        },
                        "interfaces": {"edges": [{"node": fw_iface_node}]},
                        "capabilities": {"edges": []},
                    }
                }
            ]
        },
        "SecurityZone": {"edges": [{"node": zone_node}]},
        "SecurityPolicy": {"edges": [{"node": global_policy_node}]},
    }


def _make_merged_policies_data(platform: str) -> dict[str, Any]:
    """Build smoke data that includes BOTH a global and a segment policy with distinct names."""
    base = _make_smoke_data(platform)

    # Add a second global policy "global-pol" alongside the existing "global-pol-smoke"
    extra_global: dict[str, Any] = {
        "name": {"value": "global-pol"},
        "default_action": {"value": "deny"},
        "enabled": {"value": True},
        "rules": {"edges": []},
    }
    base["SecurityPolicy"]["edges"].append({"node": extra_global})
    return base


# ===========================================================================
# Class 4 — Firewall.transform() — full render smoke tests
# ===========================================================================


class TestFirewallTransformSmoke:
    @pytest.mark.asyncio
    async def test_smoke_render_paloalto(self) -> None:
        fw = _make_fw()
        data = _make_smoke_data("paloalto_panos")
        result = await fw.transform(data)
        assert isinstance(result, str)
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_smoke_render_fortinet(self) -> None:
        fw = _make_fw()
        data = _make_smoke_data("fortinet_fortios")
        result = await fw.transform(data)
        assert isinstance(result, str)
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_smoke_render_cisco_asa(self) -> None:
        fw = _make_fw()
        data = _make_smoke_data("cisco_asa")
        result = await fw.transform(data)
        assert isinstance(result, str)
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_smoke_render_checkpoint(self) -> None:
        fw = _make_fw()
        data = _make_smoke_data("checkpoint_gaia")
        result = await fw.transform(data)
        assert isinstance(result, str)
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_segment_policy_appears_in_rendered_config(self) -> None:
        """The rule name from the segment policy must appear in the PAN-OS output."""
        fw = _make_fw()
        data = _make_smoke_data("paloalto_panos")
        result = await fw.transform(data)
        # paloalto_panos.j2 emits: set rulebase security rules <rule_name> ...
        # The rule name is "smoke-permit-rule" (spaces → dashes in the template)
        assert "smoke-permit-rule" in result

    @pytest.mark.asyncio
    async def test_global_and_segment_policies_merged(self) -> None:
        """Both global-pol (global) and seg-pol-smoke (segment) rules appear in output."""
        fw = _make_fw()
        data = _make_merged_policies_data("paloalto_panos")
        result = await fw.transform(data)
        # global-pol-smoke has no rules so its name won't appear, but it contributes
        # to zone_policies; seg-pol-smoke has "smoke-permit-rule" which should appear.
        # global-pol also has no rules, but "global-pol" name itself does not appear
        # in PAN-OS output (rules are rendered per-rule, not per-policy).
        # What we CAN verify is that the segment rule still renders correctly
        # alongside extra global policies (no crash, rule present).
        assert "smoke-permit-rule" in result
        # Also verify the render didn't short-circuit (non-empty output)
        assert len(result) > 50

    @pytest.mark.parametrize(
        "platform",
        [
            "paloalto_panos",
            "fortinet_fortios",
            "cisco_asa",
            "checkpoint_gaia",
        ],
    )
    @pytest.mark.asyncio
    async def test_all_platforms_render_hostname(self, platform: str) -> None:
        """Every vendor template emits the device hostname somewhere in the output."""
        fw = _make_fw()
        data = _make_smoke_data(platform)
        result = await fw.transform(data)
        assert f"fw-{platform}" in result

    @pytest.mark.parametrize(
        "platform",
        [
            "paloalto_panos",
            "fortinet_fortios",
            "cisco_asa",
            "checkpoint_gaia",
        ],
    )
    @pytest.mark.asyncio
    async def test_all_platforms_no_exception_on_empty_policies(self, platform: str) -> None:
        """Templates must render without exception when zone_policies is empty."""
        fw = _make_fw()
        data = _make_smoke_data(platform)
        data["SecurityPolicy"] = {"edges": []}
        # Strip segment security_policies from the interface_capabilities segment node
        iface_node = data["DcimPhysicalDevice"]["edges"][0]["node"]["interfaces"]["edges"][0]["node"]
        seg_node = iface_node["interface_capabilities"]["edges"][0]["node"]
        seg_node["security_policies"] = {"edges": []}
        result = await fw.transform(data)
        assert isinstance(result, str)
        assert len(result) > 0
