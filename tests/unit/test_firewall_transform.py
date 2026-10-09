"""Unit + smoke tests for transforms/firewall.py.

Covers:
  - _collect_segment_policies()  — pure helper, deduplication logic
  - merge_policies()            — pure helper, merge + precedence logic
  - Firewall.transform()         — no-platform early-exit path
  - Firewall.transform()         — full Jinja2 render smoke tests (all four vendors)
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from transforms.config.firewall import Firewall, _collect_segment_policies
from transforms.helpers.policy import merge_policies

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
    """Cleaned SegmentDeployment activation dict; policies is the segment's
    own security_policy as a 0/1-item list."""
    return {
        "vlan_id": 10,
        "vni": 10010,
        "segment": {
            "name": seg_name,
            "security_zone": {"name": zone_name},
            "security_policy": policies[0] if policies else None,
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
        act2 = _activation_with_policies(seg_name="seg-02", policies=[pol_b])
        act3 = _activation_with_policies(seg_name="seg-03", policies=[pol_c])
        result = _collect_segment_policies([act1, act2, act3])
        assert len(result) == 3
        names = {p["name"] for p in result}
        assert names == {"pol-a", "pol-b", "pol-c"}


# ===========================================================================
# Class 2 — merge_policies
# ===========================================================================


class TestMergePolicies:
    def test_empty_both_returns_empty(self) -> None:
        assert merge_policies([], []) == []

    def test_global_only_returned(self) -> None:
        global_pol = _seg_policy("global-pol")
        result = merge_policies([global_pol], [])
        assert len(result) == 1
        assert result[0]["name"] == "global-pol"

    def test_segment_only_returned(self) -> None:
        seg_pol = _seg_policy("seg-pol")
        result = merge_policies([], [seg_pol])
        assert len(result) == 1
        assert result[0]["name"] == "seg-pol"

    def test_no_collision_both_returned(self) -> None:
        global_pol = _seg_policy("global-pol")
        seg_pol = _seg_policy("seg-pol")
        result = merge_policies([global_pol], [seg_pol])
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
        result = merge_policies([global_pol], [seg_pol])
        assert len(result) == 1
        merged = result[0]
        # Segment version wins — it has 'rules' content
        assert merged["rules"] == [{"seq": 10, "name": "seg-rule"}]

    def test_global_preserved_when_no_collision(self) -> None:
        global_pol_a = _seg_policy("global-a")
        seg_pol_b = _seg_policy("seg-b")
        result = merge_policies([global_pol_a], [seg_pol_b])
        names = {p["name"] for p in result}
        assert "global-a" in names
        assert "seg-b" in names

    def test_policy_without_name_or_id_skipped(self) -> None:
        """A policy dict with neither 'name' nor 'id' is not added to the merged output."""
        nameless = {"default_action": "deny", "enabled": True, "rules": []}
        valid = _seg_policy("valid-pol")
        result = merge_policies([nameless, valid], [])
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
                "zone_type": {"value": "internal"},
            }
        },
        "security_policy": {
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
                                "source_segment": {"node": None},
                                "destination_segment": {"node": None},
                                "security_profile": {"node": None},
                            }
                        }
                    ]
                },
            }
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


def _make_destination_prefix_policy_data(platform: str) -> dict[str, Any]:
    """Smoke data for a rule with NO destination_segment/zone — only a
    destination_prefixes selector, the shape a colocation-hosted cloud/
    partner/SaaS interconnect rule uses (see data/demos/30_all/
    08_interconnects/07_zone_policies/05_security_policy.yml)."""
    base = _make_smoke_data(platform)

    prefix_rule_node: dict[str, Any] = {
        "index": {"value": 10},
        "name": {"value": "nordix-prod-to-aws"},
        "action": {"value": "permit"},
        "protocol": {"value": "tcp"},
        "port_start": {"value": 443},
        "port_end": {"value": None},
        "log": {"value": False},
        "disabled": {"value": False},
        "apply_on_switch": {"value": False},
        "description": {"value": "Nordix web/app tier to AWS transit hub"},
        "source_segment": {"node": None},
        "destination_segment": {"node": None},
        "source_prefixes": {"edges": []},
        "destination_prefixes": {"edges": [{"node": {"id": "px-1", "prefix": {"value": "10.40.0.0/16"}}}]},
        "source_ip_addresses": {"edges": []},
        "destination_ip_addresses": {"edges": []},
        "security_profile": {"node": None},
    }
    prefix_policy_node: dict[str, Any] = {
        "name": {"value": "colo-fr-external-egress"},
        "default_action": {"value": "deny"},
        "enabled": {"value": True},
        "rules": {"edges": [{"node": prefix_rule_node}]},
    }
    base["SecurityPolicy"]["edges"].append({"node": prefix_policy_node})
    return base


def _make_protocol_port_policy_data(
    platform: str, *, protocol: str, port_start: int | None, port_end: int | None
) -> dict[str, Any]:
    """Smoke data for one rule with an arbitrary protocol/port combination —
    used to check PAN-OS/Junos service/application object rendering across
    single-port, port-range, and no-port (icmp/any) cases."""
    base = _make_smoke_data(platform)

    rule_node: dict[str, Any] = {
        "index": {"value": 10},
        "name": {"value": "proto-port-rule"},
        "action": {"value": "permit"},
        "protocol": {"value": protocol},
        "port_start": {"value": port_start},
        "port_end": {"value": port_end},
        "log": {"value": False},
        "disabled": {"value": False},
        "apply_on_switch": {"value": False},
        "description": {"value": ""},
        "source_segment": {"node": None},
        "destination_segment": {"node": None},
        "source_prefixes": {"edges": []},
        "destination_prefixes": {"edges": [{"node": {"id": "px-1", "prefix": {"value": "10.40.0.0/16"}}}]},
        "source_ip_addresses": {"edges": []},
        "destination_ip_addresses": {"edges": []},
        "security_profile": {"node": None},
    }
    policy_node: dict[str, Any] = {
        "name": {"value": "proto-port-policy"},
        "default_action": {"value": "deny"},
        "enabled": {"value": True},
        "rules": {"edges": [{"node": rule_node}]},
    }
    base["SecurityPolicy"]["edges"].append({"node": policy_node})
    return base


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
    async def test_smoke_render_juniper(self) -> None:
        fw = _make_fw()
        data = _make_smoke_data("juniper_junos")
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

    @pytest.mark.asyncio
    async def test_destination_prefix_rule_renders_cidr_on_paloalto(self) -> None:
        """A rule with only destination_prefixes (no zone/segment) — the shape
        used for colocation-hosted cloud/partner/SaaS interconnects — must
        still render a real destination match, not 'any'."""
        fw = _make_fw()
        data = _make_destination_prefix_policy_data("paloalto_panos")
        result = await fw.transform(data)
        assert "nordix-prod-to-aws" in result
        assert "10.40.0.0/16" in result
        for line in result.splitlines():
            if "rules nordix-prod-to-aws destination" in line:
                assert "any" not in line
                break
        else:
            pytest.fail("no 'destination' line found for rule 'nordix-prod-to-aws'")

    @pytest.mark.parametrize(
        "platform",
        [
            "paloalto_panos",
            "fortinet_fortios",
            "cisco_asa",
            "checkpoint_gaia",
            "juniper_junos",
        ],
    )
    @pytest.mark.asyncio
    async def test_destination_prefix_rule_renders_cidr_all_platforms(self, platform: str) -> None:
        """Same coverage as the PAN-OS-specific test above, across every
        vendor template — each renders the CIDR as a literal destination
        match rather than silently falling back to 'any'/'Any'. ASA takes
        an IPv4 network as "address netmask"."""
        fw = _make_fw()
        data = _make_destination_prefix_policy_data(platform)
        result = await fw.transform(data)
        expected = "10.40.0.0 255.255.0.0" if platform == "cisco_asa" else "10.40.0.0/16"
        assert expected in result

    @pytest.mark.asyncio
    async def test_single_port_renders_service_object_on_paloalto(self) -> None:
        """PAN-OS has no vendor-neutral dst_port string to fall back on — it
        builds its own named service object from raw_protocol/port_start."""
        fw = _make_fw()
        data = _make_protocol_port_policy_data("paloalto_panos", protocol="tcp", port_start=443, port_end=None)
        result = await fw.transform(data)
        assert "set service svc-tcp-443 protocol tcp port 443" in result
        assert "set rulebase security rules proto-port-rule service svc-tcp-443" in result

    @pytest.mark.asyncio
    async def test_port_range_renders_service_object_on_paloalto(self) -> None:
        fw = _make_fw()
        data = _make_protocol_port_policy_data("paloalto_panos", protocol="tcp", port_start=8080, port_end=8090)
        result = await fw.transform(data)
        assert "set service svc-tcp-8080-8090 protocol tcp port 8080-8090" in result
        assert "set rulebase security rules proto-port-rule service svc-tcp-8080-8090" in result

    @pytest.mark.asyncio
    async def test_icmp_falls_back_to_service_any_on_paloalto(self) -> None:
        """No port-based service makes sense for ICMP/any — must not emit a
        malformed service object with a missing port. The base smoke fixture
        carries its own unrelated tcp/443 segment rule, so this checks the
        proto-port-rule's own service line specifically, not the whole
        output — a global 'svc-' absence check would false-fail on that
        other rule's legitimate service object."""
        fw = _make_fw()
        data = _make_protocol_port_policy_data("paloalto_panos", protocol="icmp", port_start=None, port_end=None)
        result = await fw.transform(data)
        assert "set rulebase security rules proto-port-rule service any" in result
        for line in result.splitlines():
            if "rules proto-port-rule service" in line:
                assert "svc-" not in line

    @pytest.mark.asyncio
    async def test_single_port_renders_application_object_on_juniper(self) -> None:
        fw = _make_fw()
        data = _make_protocol_port_policy_data("juniper_junos", protocol="tcp", port_start=443, port_end=None)
        result = await fw.transform(data)
        assert "set applications application app-tcp-443 protocol tcp destination-port 443" in result
        assert "match application app-tcp-443" in result

    @pytest.mark.asyncio
    async def test_port_range_renders_application_object_on_juniper(self) -> None:
        fw = _make_fw()
        data = _make_protocol_port_policy_data("juniper_junos", protocol="udp", port_start=8080, port_end=8090)
        result = await fw.transform(data)
        assert "set applications application app-udp-8080-8090 protocol udp destination-port 8080-8090" in result
        assert "match application app-udp-8080-8090" in result

    @pytest.mark.asyncio
    async def test_icmp_falls_back_to_application_any_on_juniper(self) -> None:
        """See test_icmp_falls_back_to_service_any_on_paloalto's docstring —
        same base-fixture caveat applies here."""
        fw = _make_fw()
        data = _make_protocol_port_policy_data("juniper_junos", protocol="icmp", port_start=None, port_end=None)
        result = await fw.transform(data)
        matched = [line for line in result.splitlines() if "policy proto-port-rule match application" in line]
        assert matched == [
            "set security policies from-zone any to-zone any policy proto-port-rule match application any"
        ]

    @pytest.mark.asyncio
    async def test_single_port_renders_service_object_on_checkpoint(self) -> None:
        """Check Point's `service` parameter takes a service OBJECT NAME, not
        the Cisco-ACL-style dst_port string ('eq 443') — it needs its own
        named service, defined via `add service-tcp`, same shape as PAN-OS's
        `set service`."""
        fw = _make_fw()
        data = _make_protocol_port_policy_data("checkpoint_gaia", protocol="tcp", port_start=443, port_end=None)
        result = await fw.transform(data)
        assert "add service-tcp name svc-tcp-443 port 443" in result
        assert "service svc-tcp-443 track" in result

    @pytest.mark.asyncio
    async def test_port_range_renders_service_object_on_checkpoint(self) -> None:
        fw = _make_fw()
        data = _make_protocol_port_policy_data("checkpoint_gaia", protocol="udp", port_start=8080, port_end=8090)
        result = await fw.transform(data)
        assert "add service-udp name svc-udp-8080-8090 port 8080-8090" in result
        assert "service svc-udp-8080-8090 track" in result

    @pytest.mark.asyncio
    async def test_icmp_falls_back_to_service_any_on_checkpoint(self) -> None:
        fw = _make_fw()
        data = _make_protocol_port_policy_data("checkpoint_gaia", protocol="icmp", port_start=None, port_end=None)
        result = await fw.transform(data)
        matched = [line for line in result.splitlines() if 'name "proto-port-rule"' in line]
        assert len(matched) == 1
        assert "service Any" in matched[0]

    @pytest.mark.asyncio
    async def test_single_port_renders_service_object_on_fortinet(self) -> None:
        """FortiOS's `set service "ALL"` was previously hardcoded regardless
        of protocol/port — a named service object (config firewall service
        custom) is required, defined in its own top-level block ahead of
        config firewall policy since FortiOS config sections can't nest."""
        fw = _make_fw()
        data = _make_protocol_port_policy_data("fortinet_fortios", protocol="tcp", port_start=443, port_end=None)
        result = await fw.transform(data)
        assert 'edit "svc-tcp-443"' in result
        assert "set tcp-portrange 443" in result
        assert 'set service "svc-tcp-443"' in result

    @pytest.mark.asyncio
    async def test_port_range_renders_service_object_on_fortinet(self) -> None:
        fw = _make_fw()
        data = _make_protocol_port_policy_data("fortinet_fortios", protocol="udp", port_start=8080, port_end=8090)
        result = await fw.transform(data)
        assert 'edit "svc-udp-8080-8090"' in result
        assert "set udp-portrange 8080-8090" in result
        assert 'set service "svc-udp-8080-8090"' in result

    @pytest.mark.asyncio
    async def test_icmp_falls_back_to_service_all_on_fortinet(self) -> None:
        fw = _make_fw()
        data = _make_protocol_port_policy_data("fortinet_fortios", protocol="icmp", port_start=None, port_end=None)
        result = await fw.transform(data)
        lines = result.splitlines()
        name_idx = next(i for i, line in enumerate(lines) if 'set name "proto-port-rule"' in line)
        next_idx = next(i for i in range(name_idx, len(lines)) if lines[i].strip() == "next")
        assert any('set service "ALL"' in line for line in lines[name_idx:next_idx])

    @pytest.mark.asyncio
    async def test_shared_protocol_port_deduplicated_on_fortinet(self) -> None:
        """Two rules using the same protocol/port must produce exactly one
        service object, not one per rule."""
        base = _make_smoke_data("fortinet_fortios")
        rule_a = {
            "index": {"value": 10},
            "name": {"value": "rule-a"},
            "action": {"value": "permit"},
            "protocol": {"value": "tcp"},
            "port_start": {"value": 443},
            "port_end": {"value": None},
            "log": {"value": False},
            "disabled": {"value": False},
            "apply_on_switch": {"value": False},
            "description": {"value": ""},
            "source_segment": {"node": None},
            "destination_segment": {"node": None},
            "source_prefixes": {"edges": []},
            "destination_prefixes": {"edges": [{"node": {"id": "px-1", "prefix": {"value": "10.1.0.0/16"}}}]},
            "source_ip_addresses": {"edges": []},
            "destination_ip_addresses": {"edges": []},
            "security_profile": {"node": None},
        }
        rule_b = {**rule_a, "index": {"value": 20}, "name": {"value": "rule-b"}}
        policy_node = {
            "name": {"value": "dual-rule-policy"},
            "default_action": {"value": "deny"},
            "enabled": {"value": True},
            "rules": {"edges": [{"node": rule_a}, {"node": rule_b}]},
        }
        base["SecurityPolicy"]["edges"].append({"node": policy_node})

        fw = _make_fw()
        result = await fw.transform(base)
        assert result.count('edit "svc-tcp-443"') == 1

    @pytest.mark.parametrize(
        "platform",
        [
            "paloalto_panos",
            "fortinet_fortios",
            "cisco_asa",
            "checkpoint_gaia",
            "juniper_junos",
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
            "juniper_junos",
        ],
    )
    @pytest.mark.asyncio
    async def test_all_platforms_no_exception_on_empty_policies(self, platform: str) -> None:
        """Templates must render without exception when zone_policies is empty."""
        fw = _make_fw()
        data = _make_smoke_data(platform)
        data["SecurityPolicy"] = {"edges": []}
        # Strip the segment's security_policy from the interface_capabilities segment node
        iface_node = data["DcimPhysicalDevice"]["edges"][0]["node"]["interfaces"]["edges"][0]["node"]
        seg_node = iface_node["interface_capabilities"]["edges"][0]["node"]
        seg_node["security_policy"] = {"node": None}
        result = await fw.transform(data)
        assert isinstance(result, str)
        assert len(result) > 0


# ===========================================================================
# Class 5 — Address-family-aware rendering (IPv6 P2P links, IPv4 fw_interfaces)
# ===========================================================================
#
# FW-context P2P links default to IPv6 (/127, generators/topology/dc.py's
# _ensure_firewall_context_pools) — templates must branch on address family
# rather than hardcoding an IPv4-only command (e.g. PAN-OS "ipv4 addr" for
# an actual IPv6 address is simply wrong syntax). Renders templates directly
# (not through Firewall.transform()) since only the address-family branching
# is under test here, not the full data-shaping pipeline.


def _render_template(platform: str, **context: Any) -> str:
    fw = _make_fw()
    template = fw._load_template(platform)
    return template.render(**context)


class TestAddressFamilyAwareRendering:
    _FW_IFACE_V6 = {
        "name": "eth0",
        "vlan_id": None,
        "parent_interface": None,
        "ip_address": {"address": "fd00:2300::1/127"},
        "description": None,
        "security_zone": None,
    }
    _FW_IFACE_V4 = {
        "name": "eth1",
        "vlan_id": None,
        "parent_interface": None,
        "ip_address": {"address": "10.0.0.1/30"},
        "description": None,
        "security_zone": None,
    }
    _CONTEXT_V6 = {
        "name": "ctx-shared",
        "tenant_name": None,
        "vlan_id": 3000,
        "parent_interface": {"name": "eth1"},
        "ip_address": "fd00:2300::/127",
        "context_id": None,
    }
    _CONTEXT_V4 = {
        "name": "ctx-shared",
        "tenant_name": None,
        "vlan_id": 3000,
        "parent_interface": {"name": "eth1"},
        "ip_address": "100.65.0.0/31",
        "context_id": None,
    }

    @pytest.mark.parametrize(
        "platform,v6_marker,v4_marker",
        [
            ("checkpoint_gaia", "ipv6-address", "ipv4-address"),
            ("paloalto_panos", "ipv6 addr", "ipv4 addr"),
            ("fortinet_fortios", "set ip6-address", "set ip "),
            ("cisco_asa", "ipv6 address", "ip address"),
            ("juniper_junos", "family inet6 address", "family inet address"),
        ],
    )
    def test_fw_interface_v6_uses_v6_command(self, platform: str, v6_marker: str, v4_marker: str) -> None:
        out = _render_template(platform, name="fw1", fw_interfaces=[self._FW_IFACE_V6])
        assert v6_marker in out
        assert v4_marker not in out

    @pytest.mark.parametrize(
        "platform,v6_marker,v4_marker",
        [
            ("checkpoint_gaia", "ipv6-address", "ipv4-address"),
            ("paloalto_panos", "ipv6 addr", "ipv4 addr"),
            ("fortinet_fortios", "set ip6-address", "set ip "),
            ("cisco_asa", "ipv6 address", "ip address"),
            ("juniper_junos", "family inet6 address", "family inet address"),
        ],
    )
    def test_fw_interface_v4_uses_v4_command(self, platform: str, v6_marker: str, v4_marker: str) -> None:
        out = _render_template(platform, name="fw1", fw_interfaces=[self._FW_IFACE_V4])
        assert v4_marker in out
        assert v6_marker not in out

    @pytest.mark.parametrize(
        "platform,v6_marker,v4_marker",
        [
            ("checkpoint_gaia", "ipv6-address", "ipv4-address"),
            ("paloalto_panos", "ipv6 addr", "ipv4 addr"),
            ("fortinet_fortios", "set ip6-address", "set ip "),
            ("cisco_asa", "ipv6 address", "ip address"),
        ],
    )
    def test_context_v6_uses_v6_command(self, platform: str, v6_marker: str, v4_marker: str) -> None:
        out = _render_template(platform, name="fw1", contexts=[self._CONTEXT_V6])
        assert v6_marker in out
        assert v4_marker not in out

    @pytest.mark.parametrize(
        "platform,v6_marker,v4_marker",
        [
            ("checkpoint_gaia", "ipv6-address", "ipv4-address"),
            ("paloalto_panos", "ipv6 addr", "ipv4 addr"),
            ("fortinet_fortios", "set ip6-address", "set ip "),
            ("cisco_asa", "ipv6 address", "ip address"),
        ],
    )
    def test_context_v4_uses_v4_command(self, platform: str, v6_marker: str, v4_marker: str) -> None:
        out = _render_template(platform, name="fw1", contexts=[self._CONTEXT_V4])
        assert v4_marker in out
        assert v6_marker not in out


# ===========================================================================
# Per-context security policies
# ===========================================================================

_CTX_RULE = {
    "seq": 100,
    "name": "c005-web-to-api",
    "action": "permit",
    "protocol": "tcp",
    "raw_protocol": "tcp",
    "port_start": 8443,
    "port_end": None,
    "src_zone": "PROD-ZONE",
    "dst_zone": "PROD-ZONE",
    "src": "10.5.1.0/24",
    "dst": "10.5.2.0/24",
    "dst_port": "eq 8443",
    "log": False,
    "description": "",
    "security_profile": None,
}


def _policy_context(**extra: Any) -> dict[str, Any]:
    return {
        "id": "ctx-c005",
        "tenant_id": "dep-c005",
        "name": "c005-dedicated",
        "tenant_name": "C005-P-DC12",
        "vlan_id": 3005,
        "parent_interface": {"name": "eth1"},
        "ip_address": "100.65.0.4/31",
        "context_id": None,
        "policies": [{"name": "seg-c005-web-egress", "default_action": "deny", "rules": [_CTX_RULE]}],
        **extra,
    }


class TestPerContextPolicyRendering:
    """Rules placed in a context render inside that VDOM/vsys/tenant/context, not at root."""

    @pytest.mark.parametrize(
        "platform,scope_marker,rule_marker",
        [
            ("fortinet_fortios", 'config vdom\n    edit "c005-dedicated"', 'set name "c005-web-to-api"'),
            (
                "paloalto_panos",
                "vsys c005-dedicated",
                "set vsys c005-dedicated rulebase security rules c005-web-to-api",
            ),
            ("juniper_junos", "tenant system c005-dedicated", "set tenants c005-dedicated security policies"),
            ("checkpoint_gaia", "set virtual-system c005-dedicated", 'name "c005-web-to-api"'),
            ("cisco_asa", "changeto context c005-dedicated", "access-list customer-pbr-in extended permit"),
        ],
    )
    def test_context_policies_render_in_the_context_scope(
        self, platform: str, scope_marker: str, rule_marker: str
    ) -> None:
        out = _render_template(platform, name="fw1", contexts=[_policy_context()], zone_policies=[])
        assert rule_marker in out.split(scope_marker, 1)[1]

    @pytest.mark.parametrize(
        "platform,root_marker",
        [
            ("fortinet_fortios", "config firewall policy"),
            ("paloalto_panos", "set rulebase security rules"),
            ("juniper_junos", "set security policies"),
            ("checkpoint_gaia", "add access-rule"),
            ("cisco_asa", "access-list PROD-ZONE-in"),
        ],
    )
    def test_context_policies_do_not_leak_into_root(self, platform: str, root_marker: str) -> None:
        out = _render_template(platform, name="fw1", contexts=[_policy_context()], zone_policies=[])
        before_context = out.split("c005-dedicated", 1)[0]
        assert root_marker not in before_context
        if platform in ("paloalto_panos", "juniper_junos"):
            assert root_marker not in out

    def test_asa_binds_the_context_acl_to_its_customer_pbr_interface(self) -> None:
        out = _render_template("cisco_asa", name="fw1", contexts=[_policy_context()])
        assert (
            "access-list customer-pbr-in extended permit tcp 10.5.1.0 255.255.255.0 10.5.2.0 255.255.255.0 eq 8443"
            in out
        )
        assert "access-group customer-pbr-in in interface customer-pbr" in out

    def test_asa_skips_the_access_group_without_a_context_interface(self) -> None:
        """No nameif customer-pbr exists to bind to when the sub-interface is not configured."""
        out = _render_template("cisco_asa", name="fw1", contexts=[_policy_context(ip_address=None)])
        assert "access-list customer-pbr-in" in out
        assert "access-group customer-pbr-in" not in out

    @pytest.mark.parametrize(
        "platform", ["fortinet_fortios", "paloalto_panos", "juniper_junos", "checkpoint_gaia", "cisco_asa"]
    )
    def test_context_without_policies_renders_no_policy_section(self, platform: str) -> None:
        out = _render_template(platform, name="fw1", contexts=[_policy_context(policies=[])])
        assert "c005-web-to-api" not in out


def _make_context_smoke_data(platform: str, *, tenant_matches: bool) -> dict[str, Any]:
    """Smoke data whose segment rule names the segment and whose firewall has a
    dedicated context; the context serves the segment only when its tenant is
    one of the segment's customer deployments."""
    data = _make_smoke_data(platform)
    device = data["DcimPhysicalDevice"]["edges"][0]["node"]
    fw_iface = device["interfaces"]["edges"][0]["node"]
    seg_node = fw_iface["interface_capabilities"]["edges"][0]["node"]
    seg_node["id"] = "seg-smoke"
    seg_node["customer_deployments"] = {"edges": [{"node": {"id": "dep-c005"}}]}
    rule = seg_node["security_policy"]["node"]["rules"]["edges"][0]["node"]
    rule["source_segment"] = {"node": {"id": "seg-smoke", "name": {"value": "seg-smoke"}}}
    context_iface: dict[str, Any] = {
        "__typename": {"value": "DcimVirtualInterface"},
        "name": {"value": "eth1.3005"},
        "description": {"value": None},
        "status": {"value": "active"},
        "role": {"value": None},
        "parent_interface": {"node": {"name": {"value": "eth1"}}},
        "ip_address": {"node": {"address": {"value": "100.65.0.4/31"}, "ip_namespace": {"node": None}}},
        "ha_domain": {"node": None},
        "interface_capabilities": {
            "edges": [
                {
                    "node": {
                        "__typename": "ManagedFirewallContext",
                        "id": "ctx-c005",
                        "name": {"value": "c005-dedicated"},
                        "vlan_id": {"value": 3005},
                        "context_id": {"value": None},
                        "tenant": {
                            "node": {
                                "id": "dep-c005" if tenant_matches else "dep-other",
                                "name": {"value": "C005-P-DC12"},
                            }
                        },
                    }
                }
            ]
        },
    }
    device["interfaces"]["edges"].append({"node": context_iface})
    return data


class TestFirewallTransformContextPlacement:
    @pytest.mark.asyncio
    async def test_segment_rule_moves_into_its_tenants_context(self) -> None:
        """The rule renders under the vsys and no longer in the root rulebase."""
        out = await _make_fw().transform(_make_context_smoke_data("paloalto_panos", tenant_matches=True))
        assert "set vsys c005-dedicated rulebase security rules smoke-permit-rule" in out
        assert "set rulebase security rules smoke-permit-rule" not in out

    @pytest.mark.asyncio
    async def test_rule_of_an_unserved_segment_stays_in_root(self) -> None:
        """A dedicated context of another tenant does not take the rule."""
        out = await _make_fw().transform(_make_context_smoke_data("paloalto_panos", tenant_matches=False))
        assert "set rulebase security rules smoke-permit-rule" in out
        assert "set vsys c005-dedicated rulebase" not in out
