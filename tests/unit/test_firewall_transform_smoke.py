"""Unit + smoke tests for transforms/firewall.py."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from tests.unit.firewall_transform_helpers import (
    _SMOKE_SEGMENT_REF,
    _add_segment_rule,
    _make_fw,
    _make_smoke_data,
    _smoke_segment,
)

# Raw-data builder (for Firewall.transform() tests)


def _make_raw_data(
    device_name: str = "test-fw",
    platform_name: str | None = None,
    interfaces: list[dict] | None = None,
    zones: list[dict] | None = None,
) -> dict[str, Any]:
    """Build a minimal raw GraphQL response dict for the firewall_config query."""
    platform: dict[str, Any] = {}
    if platform_name is not None:
        platform = {"name": {"value": platform_name}}

    raw_ifaces = [{"node": iface} for iface in (interfaces or [])]

    # Build SecurityZone edges
    raw_zones = [{"node": _zone_to_raw(z)} for z in (zones or [])]

    return {
        "DcimPhysicalDevice": {
            "edges": [
                {
                    "node": {
                        "name": {"value": device_name},
                        "platform": {"node": platform} if platform else {"node": {}},
                        "interfaces": {"edges": raw_ifaces},
                        "capabilities": {"edges": []},
                        "role": {"value": "internal-firewall"},
                    }
                }
            ]
        },
        "SecurityZone": {"edges": raw_zones},
    }


def _zone_to_raw(zone: dict) -> dict:
    """Convert a cleaned zone dict to a minimal raw GQL node dict."""
    # Wrap scalar fields in {"value": ...} so clean_data works.
    raw: dict[str, Any] = {}
    for k, v in zone.items():
        if isinstance(v, (str, int, float, bool)) or v is None:
            raw[k] = {"value": v}
        else:
            raw[k] = v
    return raw


# Class 1 — Firewall.transform() — no-platform early exit


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


def _destination_prefixes() -> dict[str, Any]:
    """A routing-neutral IPAM prefix used as a rule selector."""
    return {
        "edges": [
            {
                "node": {
                    "id": "px-1",
                    "prefix": {"value": "10.40.0.0/16"},
                    "ip_namespace": {"node": {"name": {"value": "default"}}},
                }
            }
        ]
    }


def _make_destination_prefix_policy_data(platform: str) -> dict[str, Any]:
    """A standalone prefix selector whose policy rule supplies its destination zone."""
    base = _make_smoke_data(platform)

    prefix_rule_node: dict[str, Any] = {
        "id": "rule-nordix-prod-to-aws",
        "index": {"value": 20},
        "name": {"value": "nordix-prod-to-aws"},
        "action": {"value": "permit"},
        "protocol": {"value": "tcp"},
        "port_start": {"value": 443},
        "port_end": {"value": None},
        "log": {"value": False},
        "disabled": {"value": False},
        "apply_on_switch": {"value": False},
        "description": {"value": "Nordix web/app tier to AWS transit hub"},
        "source_segment": {"node": _SMOKE_SEGMENT_REF},
        "destination_segment": {"node": None},
        "destination_zone": {"node": {"name": {"value": "PROD"}}},
        "source_prefixes": {"edges": []},
        "destination_prefixes": _destination_prefixes(),
        "source_ip_addresses": {"edges": []},
        "destination_ip_addresses": {"edges": []},
        "security_profile": {"node": None},
    }
    _add_segment_rule(base, prefix_rule_node)
    return base


def _make_protocol_port_policy_data(
    platform: str, *, protocol: str, port_start: int | None, port_end: int | None
) -> dict[str, Any]:
    """Smoke data for one rule with an arbitrary protocol/port combination."""
    base = _make_smoke_data(platform)

    rule_node: dict[str, Any] = {
        "id": "rule-proto-port",
        "index": {"value": 20},
        "name": {"value": "proto-port-rule"},
        "action": {"value": "permit"},
        "protocol": {"value": protocol},
        "port_start": {"value": port_start},
        "port_end": {"value": port_end},
        "log": {"value": False},
        "disabled": {"value": False},
        "apply_on_switch": {"value": False},
        "description": {"value": ""},
        "source_segment": {"node": _SMOKE_SEGMENT_REF},
        "destination_segment": {"node": None},
        "destination_zone": {"node": {"name": {"value": "PROD"}}},
        "source_prefixes": {"edges": []},
        "destination_prefixes": _destination_prefixes(),
        "source_ip_addresses": {"edges": []},
        "destination_ip_addresses": {"edges": []},
        "security_profile": {"node": None},
    }
    _add_segment_rule(base, rule_node)
    return base


def _inbound_rule_node(name: str, *, policy: str, enabled: bool = True) -> dict[str, Any]:
    """A rule of another segment's policy whose destination is the smoke segment."""
    return {
        "id": f"rule-{name}",
        "index": {"value": 10},
        "name": {"value": name},
        "action": {"value": "permit"},
        "protocol": {"value": "tcp"},
        "port_start": {"value": 5432},
        "port_end": {"value": None},
        "log": {"value": False},
        "disabled": {"value": False},
        "apply_on_switch": {"value": False},
        "description": {"value": ""},
        "source_segment": {
            "node": {
                "id": "seg-peer",
                "name": {"value": "seg-peer"},
                "gateway": {"node": {"ip_prefix": {"node": {"prefix": {"value": "10.20.0.0/24"}}}}},
            }
        },
        "destination_segment": {"node": _SMOKE_SEGMENT_REF},
        "source_prefixes": {"edges": []},
        "destination_prefixes": {"edges": []},
        "source_ip_addresses": {"edges": []},
        "destination_ip_addresses": {"edges": []},
        "security_profile": {"node": None},
        "policy": {
            "node": {
                "id": f"pol-{policy}",
                "name": {"value": policy},
                "enabled": {"value": enabled},
                "default_action": {"value": "deny"},
                "segment": {"node": {"id": "seg-peer", "name": {"value": "seg-peer"}}},
            }
        },
    }


# Class 2 — Firewall.transform() — full render smoke tests


class TestFirewallTransformSmoke:
    @pytest.mark.asyncio
    async def test_segment_policy_appears_in_rendered_config(self) -> None:
        """The rule name from the segment policy must appear in the PAN-OS output."""
        fw = _make_fw()
        data = _make_smoke_data("paloalto_panos")
        result = await fw.transform(data)
        # paloalto_panos.j2: `set rulebase security rules <rule_name>`, spaces become dashes.
        assert "smoke-permit-rule" in result

    @pytest.mark.asyncio
    async def test_inbound_rule_of_a_carried_segment_renders(self) -> None:
        """The ingress leg:"""
        data = _make_smoke_data("paloalto_panos")
        _smoke_segment(data)["inbound_rules"]["edges"].append(
            {"node": _inbound_rule_node("peer-to-db", policy="zz-peer")}
        )
        result = await _make_fw().transform(data)
        assert "set rulebase security rules peer-to-db source 10.20.0.0/24" in result
        assert result.index("rules smoke-permit-rule ") < result.index("rules peer-to-db ")

    @pytest.mark.asyncio
    async def test_inbound_rule_of_a_disabled_policy_is_skipped(self) -> None:
        data = _make_smoke_data("paloalto_panos")
        inbound = _inbound_rule_node("peer-to-db", policy="zz-peer", enabled=False)
        _smoke_segment(data)["inbound_rules"]["edges"].append({"node": inbound})
        result = await _make_fw().transform(data)
        assert "peer-to-db" not in result

    @pytest.mark.asyncio
    async def test_rule_reached_twice_renders_once(self) -> None:
        """A rule from the carried segment to itself is its egress rule and its inbound rule:"""
        data = _make_smoke_data("paloalto_panos")
        segment = _smoke_segment(data)
        egress = segment["security_policy"]["node"]["rules"]["edges"][0]["node"]
        policy = {k: v for k, v in segment["security_policy"]["node"].items() if k != "rules"}
        segment["inbound_rules"]["edges"].append({"node": {**egress, "policy": {"node": policy}}})
        result = await _make_fw().transform(data)
        assert result.count("set rulebase security rules smoke-permit-rule action") == 1

    @pytest.mark.asyncio
    async def test_no_root_policy_list_is_read(self) -> None:
        """Only the served segments' policies render: a stray SecurityPolicy root is ignored."""
        data = _make_smoke_data("paloalto_panos")
        stray = {
            "name": {"value": "stray"},
            "enabled": {"value": True},
            "rules": {"edges": [{"node": _inbound_rule_node("stray-rule", policy="stray")}]},
        }
        data["SecurityPolicy"] = {"edges": [{"node": stray}]}
        result = await _make_fw().transform(data)
        assert "stray-rule" not in result

    @pytest.mark.asyncio
    async def test_destination_prefix_rule_renders_cidr_on_paloalto(self) -> None:
        """A rule with only destination_prefixes (no zone/segment)."""
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
            "checkpoint_gaia",
            "juniper_junos",
        ],
    )
    @pytest.mark.asyncio
    async def test_destination_prefix_rule_renders_cidr_all_platforms(self, platform: str) -> None:
        """Same coverage as the PAN-OS-specific test above, across every vendor template."""
        fw = _make_fw()
        data = _make_destination_prefix_policy_data(platform)
        result = await fw.transform(data)
        assert "10.40.0.0/16" in result

    @pytest.mark.asyncio
    async def test_single_port_renders_service_object_on_paloalto(self) -> None:
        """PAN-OS has no vendor-neutral dst_port string to fall back on."""
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
        """No port-based service makes sense for ICMP/any."""
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
        """See test_icmp_falls_back_to_service_any_on_paloalto's docstring — same base-fixture caveat applies here."""
        fw = _make_fw()
        data = _make_protocol_port_policy_data("juniper_junos", protocol="icmp", port_start=None, port_end=None)
        result = await fw.transform(data)
        matched = [line for line in result.splitlines() if "policy proto-port-rule match application" in line]
        assert matched == [
            "set security policies from-zone any to-zone PROD policy proto-port-rule match application any"
        ]

    @pytest.mark.asyncio
    async def test_single_port_renders_service_object_on_checkpoint(self) -> None:
        """Check Point's `service` parameter takes a service OBJECT NAME."""
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
        assert len(matched) == 2
        service_lines = [line for line in matched if line.startswith("add access-rule ")]
        assert len(service_lines) == 1
        assert "service Any" in service_lines[0]
        assert 'set access-rule layer "Network" name "proto-port-rule" destination PROD' in matched

    @pytest.mark.parametrize(
        "platform",
        [
            "paloalto_panos",
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
            "checkpoint_gaia",
            "juniper_junos",
        ],
    )
    @pytest.mark.asyncio
    async def test_all_platforms_no_exception_on_empty_policies(self, platform: str) -> None:
        """Templates must render without exception when zone_policies is empty."""
        fw = _make_fw()
        data = _make_smoke_data(platform)
        _smoke_segment(data)["security_policy"] = {"node": None}
        result = await fw.transform(data)
        assert isinstance(result, str)
        assert len(result) > 0
