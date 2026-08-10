"""Unit tests for firewall helper functions in transforms/helpers/firewall.py.

Covers:
  - get_firewall_zones()          — zone list from SecurityZone nodes
  - get_firewall_static_routes()  — static routes per zone interface
  - get_vrf_default_gateways()    — VRF → FW gateway IP map from activations
  - get_zone_policies()           — policy dicts with rules from SecurityPolicy nodes
  - get_customer_pbr_rules()      — default-redirect-to-firewall PBR rules per VLAN
  - get_border_leaf_pbr_rules()   — same, but SGT/prefix-matched, DC-wide (border-leaf)
  - get_firewall_contexts()       — per-tenant FirewallContext list from this device's interfaces
"""

from transforms.helpers.firewall import (
    _flatten_deployment_firewall_contexts,
    get_border_leaf_pbr_rules,
    get_customer_pbr_rules,
    get_firewall_contexts,
    get_firewall_static_routes,
    get_firewall_zones,
    get_vrf_default_gateways,
    get_zone_policies,
)

# ---------------------------------------------------------------------------
# Helpers — zone data
# ---------------------------------------------------------------------------


def _make_zone(
    *,
    name: str = "internal",
    trust_level: int | None = 100,
    zone_type: str | None = "internal",
    description: str | None = "Test zone",
    network_segments: list | None = None,
) -> dict:
    """Build a cleaned SecurityZone dict as returned by clean_data()."""
    zone: dict = {"name": name}
    if trust_level is not None:
        zone["trust_level"] = trust_level
    if zone_type is not None:
        zone["zone_type"] = zone_type
    if description is not None:
        zone["description"] = description
    if network_segments is not None:
        zone["network_segments"] = network_segments
    return zone


def _make_segment_with_prefix(prefix: str) -> dict:
    """Build a minimal segment dict that _get_segment_prefix_str() can parse."""
    return {"gateway": {"ip_prefix": {"prefix": prefix}}}


# ---------------------------------------------------------------------------
# Helpers — FW interface data
# ---------------------------------------------------------------------------


def _make_fw_interface(
    *,
    name: str = "eth0.10",
    zone_name: str | None = "internal",
    ip_addr: str | None = "10.0.0.1/30",
    ns_name: str | None = "VRF-INTERNAL",
) -> dict:
    """Build a cleaned DcimFirewallInterface dict."""
    iface: dict = {"name": name}
    if zone_name is not None:
        iface["security_zone"] = {"name": zone_name}
    if ip_addr is not None or ns_name is not None:
        ip_obj: dict = {}
        if ip_addr is not None:
            ip_obj["address"] = ip_addr
        if ns_name is not None:
            ip_obj["ip_namespace"] = {"name": ns_name}
        iface["ip_address"] = ip_obj
    return iface


# ---------------------------------------------------------------------------
# Helpers — activation / SegmentDeployment data
# ---------------------------------------------------------------------------


def _make_activation(
    *,
    seg_prefix: str | None = "10.0.1.0/24",
    ns_name: str | None = "VRF-INTERNAL",
    fw_ip: str | None = "10.0.0.1/30",
    zone_name: str | None = "internal",
) -> dict:
    """Build a minimal cleaned SegmentDeployment dict for get_vrf_default_gateways()."""
    prefix: dict = {}
    if seg_prefix is not None:
        prefix["prefix"] = seg_prefix
    if ns_name is not None:
        prefix["ip_namespace"] = {"name": ns_name}

    fw_iface: dict = {}
    if fw_ip is not None:
        fw_iface["ip_address"] = {"address": fw_ip}

    seg: dict = {"prefix": [prefix] if prefix else []}
    if zone_name is not None:
        seg["security_zone"] = {"name": zone_name, "firewall_interface": fw_iface}

    return {"segment": seg}


# ---------------------------------------------------------------------------
# Helpers — policy / rule data
# ---------------------------------------------------------------------------


def _make_policy(
    *,
    name: str = "east-west",
    default_action: str = "deny",
    enabled: bool = True,
    rules: list | None = None,
) -> dict:
    return {
        "name": name,
        "default_action": default_action,
        "enabled": enabled,
        "rules": rules or [],
    }


def _make_rule(
    *,
    index: int = 10,
    name: str | None = None,
    action: str = "permit",
    protocol: str = "tcp",
    port_start: int | None = 443,
    port_end: int | None = None,
    src_zone: str | None = None,
    dst_zone: str | None = None,
    src_segment: dict | None = None,
    dst_segment: dict | None = None,
    log: bool = False,
    disabled: bool = False,
    security_profile: dict | None = None,
    description: str | None = None,
) -> dict:
    rule: dict = {
        "index": index,
        "name": name or f"rule-{index}",
        "action": action,
        "protocol": protocol,
        "port_start": port_start,
        "port_end": port_end,
        "source_segment": src_segment,
        "destination_segment": dst_segment,
        "log": log,
        "disabled": disabled,
        "description": description or "",
    }
    if src_zone is not None:
        rule["source_zone"] = {"name": src_zone}
    if dst_zone is not None:
        rule["destination_zone"] = {"name": dst_zone}
    if security_profile is not None:
        rule["security_profile"] = security_profile
    return rule


# ===========================================================================
# get_firewall_zones()
# ===========================================================================


class TestGetFirewallZones:
    def test_none_returns_empty(self) -> None:
        assert get_firewall_zones(None) == []

    def test_empty_list_returns_empty(self) -> None:
        assert get_firewall_zones([]) == []

    def test_zone_without_name_is_skipped(self) -> None:
        result = get_firewall_zones([{"trust_level": 50, "zone_type": "dmz"}])
        assert result == []

    def test_single_zone_basic_fields(self) -> None:
        zone = _make_zone(name="internal", trust_level=100, zone_type="internal", description="Corp LAN")
        result = get_firewall_zones([zone])
        assert len(result) == 1
        assert result[0]["name"] == "internal"
        assert result[0]["trust_level"] == 100
        assert result[0]["zone_type"] == "internal"
        assert result[0]["description"] == "Corp LAN"
        assert result[0]["member_cidrs"] == []

    def test_zone_with_network_segments(self) -> None:
        segs = [
            _make_segment_with_prefix("10.0.1.0/24"),
            _make_segment_with_prefix("10.0.2.0/24"),
        ]
        zone = _make_zone(name="internal", network_segments=segs)
        result = get_firewall_zones([zone])
        assert result[0]["member_cidrs"] == ["10.0.1.0/24", "10.0.2.0/24"]

    def test_member_cidrs_sorted(self) -> None:
        segs = [
            _make_segment_with_prefix("192.168.0.0/24"),
            _make_segment_with_prefix("10.0.0.0/8"),
        ]
        zone = _make_zone(name="mixed", network_segments=segs)
        result = get_firewall_zones([zone])
        # sorted() on CIDR strings — "10..." < "192..."
        assert result[0]["member_cidrs"] == ["10.0.0.0/8", "192.168.0.0/24"]

    def test_zones_sorted_by_trust_level_descending(self) -> None:
        zones = [
            _make_zone(name="untrust", trust_level=0, zone_type="external"),
            _make_zone(name="dmz", trust_level=50, zone_type="dmz"),
            _make_zone(name="internal", trust_level=100, zone_type="internal"),
        ]
        result = get_firewall_zones(zones)
        assert [z["name"] for z in result] == ["internal", "dmz", "untrust"]

    def test_zone_missing_optional_trust_level_defaults_to_zero(self) -> None:
        zone = {"name": "bare-zone"}
        result = get_firewall_zones([zone])
        assert len(result) == 1
        assert result[0]["trust_level"] == 0

    def test_zone_missing_optional_zone_type_defaults_to_internal(self) -> None:
        zone = {"name": "bare-zone"}
        result = get_firewall_zones([zone])
        assert result[0]["zone_type"] == "internal"

    def test_zone_missing_optional_description_defaults_to_empty_string(self) -> None:
        zone = {"name": "no-desc"}
        result = get_firewall_zones([zone])
        assert result[0]["description"] == ""

    def test_segment_without_prefix_key_skipped_in_member_cidrs(self) -> None:
        """A network_segment that has no prefix data does not crash and is simply skipped."""
        zone = _make_zone(name="internal", network_segments=[{"name": "empty-seg"}])
        result = get_firewall_zones([zone])
        assert result[0]["member_cidrs"] == []

    def test_multiple_zones_with_same_trust_level_stable_order(self) -> None:
        """Two zones at the same trust_level both appear in the output."""
        zones = [
            _make_zone(name="a", trust_level=50),
            _make_zone(name="b", trust_level=50),
        ]
        result = get_firewall_zones(zones)
        assert len(result) == 2
        names = {z["name"] for z in result}
        assert names == {"a", "b"}


# ===========================================================================
# get_firewall_static_routes()
# ===========================================================================


class TestGetFirewallStaticRoutes:
    def test_empty_interfaces_returns_empty(self) -> None:
        assert get_firewall_static_routes([], []) == []

    def test_empty_zones_returns_empty(self) -> None:
        iface = _make_fw_interface(name="eth0.10", zone_name="internal")
        assert get_firewall_static_routes([iface], []) == []

    def test_interface_missing_zone_name_is_skipped(self) -> None:
        iface = _make_fw_interface(zone_name=None)
        zone = {"name": "internal", "trust_level": 100, "zone_type": "internal", "member_cidrs": ["10.0.0.0/8"]}
        assert get_firewall_static_routes([iface], [zone]) == []

    def test_interface_missing_ip_address_is_skipped(self) -> None:
        iface = {"name": "eth0.10", "security_zone": {"name": "internal"}}
        zone = {"name": "internal", "trust_level": 100, "zone_type": "internal", "member_cidrs": ["10.0.0.0/8"]}
        assert get_firewall_static_routes([iface], [zone]) == []

    def test_interface_missing_ns_name_is_skipped(self) -> None:
        iface = {
            "name": "eth0.10",
            "security_zone": {"name": "internal"},
            "ip_address": {"address": "10.0.0.1/30"},
        }
        zone = {"name": "internal", "trust_level": 100, "zone_type": "internal", "member_cidrs": ["10.0.0.0/8"]}
        assert get_firewall_static_routes([iface], [zone]) == []

    def test_single_interface_with_zone_cidr(self) -> None:
        """FW on .1/30 → leaf nexthop is .2."""
        iface = _make_fw_interface(
            name="eth0.10",
            zone_name="internal",
            ip_addr="10.99.99.1/30",
            ns_name="VRF-INTERNAL",
        )
        zone = {
            "name": "internal",
            "trust_level": 100,
            "zone_type": "internal",
            "member_cidrs": ["10.0.1.0/24"],
        }
        result = get_firewall_static_routes([iface], [zone])
        assert len(result) == 1
        assert result[0]["vrf"] == "VRF-INTERNAL"
        assert result[0]["destination"] == "10.0.1.0/24"
        assert result[0]["nexthop"] == "10.99.99.2"
        assert result[0]["interface"] == "eth0.10"

    def test_nexthop_is_other_host_in_slash30(self) -> None:
        """When FW is .2 in the /30, the leaf nexthop is .1."""
        iface = _make_fw_interface(
            name="eth0.20",
            zone_name="dmz",
            ip_addr="172.16.0.2/30",
            ns_name="VRF-DMZ",
        )
        zone = {"name": "dmz", "trust_level": 50, "zone_type": "dmz", "member_cidrs": ["192.168.1.0/24"]}
        result = get_firewall_static_routes([iface], [zone])
        assert result[0]["nexthop"] == "172.16.0.1"

    def test_one_route_per_zone_cidr(self) -> None:
        """One interface × N CIDRs in the zone → N route entries."""
        iface = _make_fw_interface(
            name="eth0.10",
            zone_name="internal",
            ip_addr="10.99.99.1/30",
            ns_name="VRF-INTERNAL",
        )
        zone = {
            "name": "internal",
            "trust_level": 100,
            "zone_type": "internal",
            "member_cidrs": ["10.0.1.0/24", "10.0.2.0/24", "10.0.3.0/24"],
        }
        result = get_firewall_static_routes([iface], [zone])
        assert len(result) == 3
        destinations = {r["destination"] for r in result}
        assert destinations == {"10.0.1.0/24", "10.0.2.0/24", "10.0.3.0/24"}

    def test_multiple_interfaces_produce_independent_routes(self) -> None:
        ifaces = [
            _make_fw_interface(name="eth0.10", zone_name="internal", ip_addr="10.0.0.1/30", ns_name="VRF-A"),
            _make_fw_interface(name="eth0.20", zone_name="dmz", ip_addr="10.0.1.1/30", ns_name="VRF-B"),
        ]
        zones = [
            {"name": "internal", "trust_level": 100, "zone_type": "internal", "member_cidrs": ["10.10.0.0/24"]},
            {"name": "dmz", "trust_level": 50, "zone_type": "dmz", "member_cidrs": ["192.168.0.0/24"]},
        ]
        result = get_firewall_static_routes(ifaces, zones)
        assert len(result) == 2
        vrfs = {r["vrf"] for r in result}
        assert vrfs == {"VRF-A", "VRF-B"}

    def test_routes_sorted_by_vrf_then_destination(self) -> None:
        ifaces = [
            _make_fw_interface(name="eth0.30", zone_name="external", ip_addr="10.2.0.1/30", ns_name="VRF-A"),
            _make_fw_interface(name="eth0.10", zone_name="internal", ip_addr="10.0.0.1/30", ns_name="VRF-A"),
        ]
        zones = [
            {"name": "internal", "trust_level": 100, "zone_type": "internal", "member_cidrs": ["10.10.1.0/24"]},
            {"name": "external", "trust_level": 0, "zone_type": "external", "member_cidrs": ["10.20.1.0/24"]},
        ]
        result = get_firewall_static_routes(ifaces, zones)
        # Both VRF-A — sorted by destination
        assert result[0]["destination"] < result[1]["destination"]

    def test_interface_with_invalid_ip_is_skipped(self) -> None:
        iface = {
            "name": "eth0.bad",
            "security_zone": {"name": "internal"},
            "ip_address": {"address": "not-an-ip", "ip_namespace": {"name": "VRF-INTERNAL"}},
        }
        zone = {"name": "internal", "trust_level": 100, "zone_type": "internal", "member_cidrs": ["10.0.0.0/8"]}
        result = get_firewall_static_routes([iface], [zone])
        assert result == []

    def test_zone_not_found_in_lookup_skips_interface(self) -> None:
        iface = _make_fw_interface(zone_name="nonexistent-zone")
        zone = {"name": "internal", "trust_level": 100, "zone_type": "internal", "member_cidrs": ["10.0.0.0/8"]}
        result = get_firewall_static_routes([iface], [zone])
        assert result == []


# ===========================================================================
# get_vrf_default_gateways()
# ===========================================================================


def _make_exchange_leg(*, ip_addr: str, ns_name: str) -> dict:
    """Build a cleaned interface_capabilities leg (a peer interface on the same device)."""
    return {"ip_address": {"address": ip_addr, "ip_namespace": {"name": ns_name}}}


def _make_routed_exchange_capability(
    *,
    exchange_id: str = "exchange-1",
    legs: list[dict] | None = None,
) -> dict:
    """Build a cleaned TopologyRoutedExchange capability, as found in interface.interface_capabilities."""
    return {
        "typename": "TopologyRoutedExchange",
        "id": exchange_id,
        "interface_capabilities": legs or [],
    }


class TestGetVrfDefaultGateways:
    def test_none_returns_empty(self) -> None:
        assert get_vrf_default_gateways(None) == {}

    def test_empty_list_returns_empty(self) -> None:
        assert get_vrf_default_gateways([]) == {}

    def test_interface_with_no_capabilities_is_ignored(self) -> None:
        result = get_vrf_default_gateways([{"name": "Vlan10", "interface_capabilities": []}])
        assert result == {}

    def test_non_exchange_capability_is_ignored(self) -> None:
        iface = {"name": "Vlan10", "interface_capabilities": [{"typename": "ManagedVxlanSegment"}]}
        assert get_vrf_default_gateways([iface]) == {}

    def test_two_legs_produce_reciprocal_gateways(self) -> None:
        """Leg in VRF-A's nexthop is the leg's IP in VRF-Z, and vice versa."""
        legs = [
            _make_exchange_leg(ip_addr="10.1.99.1/30", ns_name="VRF-A"),
            _make_exchange_leg(ip_addr="10.2.99.1/30", ns_name="VRF-Z"),
        ]
        cap = _make_routed_exchange_capability(legs=legs)
        iface = {"name": "Vlan10", "interface_capabilities": [cap]}
        result = get_vrf_default_gateways([iface])
        assert result == {"VRF-A": "10.2.99.1", "VRF-Z": "10.1.99.1"}

    def test_same_exchange_seen_on_both_legs_counted_once(self) -> None:
        """The exchange capability appears on both of the device's own interfaces
        (it's a reverse-read of the same relation) — dedup by exchange id."""
        legs = [
            _make_exchange_leg(ip_addr="10.1.99.1/30", ns_name="VRF-A"),
            _make_exchange_leg(ip_addr="10.2.99.1/30", ns_name="VRF-Z"),
        ]
        cap = _make_routed_exchange_capability(exchange_id="exchange-1", legs=legs)
        ifaces = [
            {"name": "Vlan10", "interface_capabilities": [cap]},
            {"name": "Vlan20", "interface_capabilities": [cap]},
        ]
        result = get_vrf_default_gateways(ifaces)
        assert result == {"VRF-A": "10.2.99.1", "VRF-Z": "10.1.99.1"}

    def test_leg_missing_ip_is_skipped(self) -> None:
        legs = [
            {"ip_address": None},
            _make_exchange_leg(ip_addr="10.2.99.1/30", ns_name="VRF-Z"),
        ]
        cap = _make_routed_exchange_capability(legs=legs)
        result = get_vrf_default_gateways([{"name": "Vlan10", "interface_capabilities": [cap]}])
        assert result == {}

    def test_multiple_independent_exchanges_on_different_interfaces(self) -> None:
        cap_a = _make_routed_exchange_capability(
            exchange_id="exchange-a",
            legs=[
                _make_exchange_leg(ip_addr="10.1.0.1/30", ns_name="VRF-A"),
                _make_exchange_leg(ip_addr="10.1.0.2/30", ns_name="VRF-B"),
            ],
        )
        cap_b = _make_routed_exchange_capability(
            exchange_id="exchange-b",
            legs=[
                _make_exchange_leg(ip_addr="10.2.0.1/30", ns_name="VRF-C"),
                _make_exchange_leg(ip_addr="10.2.0.2/30", ns_name="VRF-D"),
            ],
        )
        ifaces = [
            {"name": "Vlan10", "interface_capabilities": [cap_a]},
            {"name": "Vlan20", "interface_capabilities": [cap_b]},
        ]
        result = get_vrf_default_gateways(ifaces)
        assert result == {
            "VRF-A": "10.1.0.2",
            "VRF-B": "10.1.0.1",
            "VRF-C": "10.2.0.2",
            "VRF-D": "10.2.0.1",
        }


# ===========================================================================
# get_zone_policies()
# ===========================================================================


class TestGetZonePolicies:
    def test_none_returns_empty(self) -> None:
        assert get_zone_policies(None) == []

    def test_empty_list_returns_empty(self) -> None:
        assert get_zone_policies([]) == []

    def test_disabled_policy_is_skipped(self) -> None:
        policy = _make_policy(name="skipped", enabled=False, rules=[_make_rule()])
        assert get_zone_policies([policy]) == []

    def test_single_policy_basic_fields(self) -> None:
        policy = _make_policy(name="east-west", default_action="deny")
        result = get_zone_policies([policy])
        assert len(result) == 1
        assert result[0]["name"] == "east-west"
        assert result[0]["default_action"] == "deny"

    def test_implicit_deny_always_appended(self) -> None:
        policy = _make_policy(rules=[])
        result = get_zone_policies([policy])
        last = result[0]["rules"][-1]
        assert last["name"] == "implicit-deny-all"
        assert last["action"] == "deny"
        assert last["protocol"] == "ip"
        assert last["log"] is True

    def test_implicit_deny_seq_min_9990_when_no_rules(self) -> None:
        policy = _make_policy(rules=[])
        result = get_zone_policies([policy])
        assert result[0]["rules"][-1]["seq"] == 9990

    def test_implicit_deny_seq_above_last_rule(self) -> None:
        policy = _make_policy(rules=[_make_rule(index=100)])
        result = get_zone_policies([policy])
        last_seq = result[0]["rules"][-1]["seq"]
        assert last_seq >= 110

    def test_disabled_rule_is_skipped(self) -> None:
        rules = [_make_rule(index=10), _make_rule(index=20, disabled=True)]
        policy = _make_policy(rules=rules)
        result = get_zone_policies([policy])
        # One active rule + implicit deny
        non_deny = [r for r in result[0]["rules"] if r["name"] != "implicit-deny-all"]
        assert len(non_deny) == 1
        assert non_deny[0]["seq"] == 10

    def test_rules_sorted_by_index(self) -> None:
        rules = [_make_rule(index=30), _make_rule(index=10), _make_rule(index=20)]
        policy = _make_policy(rules=rules)
        result = get_zone_policies([policy])
        seqs = [r["seq"] for r in result[0]["rules"] if r["name"] != "implicit-deny-all"]
        assert seqs == [10, 20, 30]

    def test_rule_tcp_with_single_port(self) -> None:
        rule = _make_rule(protocol="tcp", port_start=443, port_end=None)
        result = get_zone_policies([_make_policy(rules=[rule])])
        r = result[0]["rules"][0]
        assert r["protocol"] == "tcp"
        assert r["dst_port"] == "eq 443"

    def test_rule_tcp_with_port_range(self) -> None:
        rule = _make_rule(protocol="tcp", port_start=8080, port_end=8090)
        result = get_zone_policies([_make_policy(rules=[rule])])
        assert result[0]["rules"][0]["dst_port"] == "range 8080 8090"

    def test_rule_protocol_any_maps_to_ip(self) -> None:
        rule = _make_rule(protocol="any", port_start=None)
        result = get_zone_policies([_make_policy(rules=[rule])])
        assert result[0]["rules"][0]["protocol"] == "ip"

    def test_rule_icmp_port_not_set(self) -> None:
        """Port is ignored for non-TCP/UDP protocols."""
        rule = _make_rule(protocol="icmp", port_start=8)
        result = get_zone_policies([_make_policy(rules=[rule])])
        assert result[0]["rules"][0]["dst_port"] is None

    def test_rule_udp_single_port(self) -> None:
        rule = _make_rule(protocol="udp", port_start=53, port_end=None)
        result = get_zone_policies([_make_policy(rules=[rule])])
        assert result[0]["rules"][0]["dst_port"] == "eq 53"

    def test_rule_src_zone_and_dst_zone(self) -> None:
        rule = _make_rule(src_zone="dmz", dst_zone="internal")
        result = get_zone_policies([_make_policy(rules=[rule])])
        r = result[0]["rules"][0]
        assert r["src_zone"] == "dmz"
        assert r["dst_zone"] == "internal"

    def test_rule_zone_fields_none_when_absent(self) -> None:
        rule = _make_rule()
        result = get_zone_policies([_make_policy(rules=[rule])])
        r = result[0]["rules"][0]
        assert r["src_zone"] is None
        assert r["dst_zone"] is None

    def test_rule_src_and_dst_segment_prefix(self) -> None:
        src_seg = _make_segment_with_prefix("10.1.0.0/24")
        dst_seg = _make_segment_with_prefix("10.2.0.0/24")
        rule = _make_rule(src_segment=src_seg, dst_segment=dst_seg, port_start=None)
        result = get_zone_policies([_make_policy(rules=[rule])])
        r = result[0]["rules"][0]
        assert r["src"] == "10.1.0.0/24"
        assert r["dst"] == "10.2.0.0/24"

    def test_rule_src_dst_none_when_no_segments(self) -> None:
        rule = _make_rule(src_segment=None, dst_segment=None)
        result = get_zone_policies([_make_policy(rules=[rule])])
        r = result[0]["rules"][0]
        assert r["src"] is None
        assert r["dst"] is None

    def test_rule_log_field(self) -> None:
        rule = _make_rule(log=True)
        result = get_zone_policies([_make_policy(rules=[rule])])
        assert result[0]["rules"][0]["log"] is True

    def test_rule_log_false_by_default(self) -> None:
        rule = _make_rule(log=False)
        result = get_zone_policies([_make_policy(rules=[rule])])
        assert result[0]["rules"][0]["log"] is False

    def test_rule_security_profile_extracted(self) -> None:
        rule = _make_rule(security_profile={"name": "strict-av"})
        result = get_zone_policies([_make_policy(rules=[rule])])
        assert result[0]["rules"][0]["security_profile"] == "strict-av"

    def test_rule_security_profile_none_when_absent(self) -> None:
        rule = _make_rule(security_profile=None)
        result = get_zone_policies([_make_policy(rules=[rule])])
        assert result[0]["rules"][0]["security_profile"] is None

    def test_multiple_policies_all_returned(self) -> None:
        policies = [
            _make_policy(name="policy-a", rules=[_make_rule(index=10)]),
            _make_policy(name="policy-b", rules=[_make_rule(index=20)]),
        ]
        result = get_zone_policies(policies)
        assert len(result) == 2
        names = {p["name"] for p in result}
        assert names == {"policy-a", "policy-b"}

    def test_mixed_enabled_disabled_policies(self) -> None:
        policies = [
            _make_policy(name="active", enabled=True, rules=[_make_rule()]),
            _make_policy(name="inactive", enabled=False, rules=[_make_rule()]),
        ]
        result = get_zone_policies(policies)
        assert len(result) == 1
        assert result[0]["name"] == "active"

    def test_implicit_deny_has_null_zones(self) -> None:
        policy = _make_policy(rules=[])
        result = get_zone_policies([policy])
        deny = result[0]["rules"][-1]
        assert deny["src_zone"] is None
        assert deny["dst_zone"] is None
        assert deny["src"] is None
        assert deny["dst"] is None

    def test_rule_port_range_same_start_end_uses_eq(self) -> None:
        """When port_end equals port_start the result is 'eq X', not 'range X X'."""
        rule = _make_rule(protocol="tcp", port_start=80, port_end=80)
        result = get_zone_policies([_make_policy(rules=[rule])])
        assert result[0]["rules"][0]["dst_port"] == "eq 80"

    def test_policy_enabled_key_missing_defaults_to_enabled(self) -> None:
        """A policy dict without an 'enabled' key is treated as enabled."""
        policy = {"name": "no-enabled-key", "default_action": "deny", "rules": []}
        result = get_zone_policies([policy])
        assert len(result) == 1
        assert result[0]["name"] == "no-enabled-key"


# ===========================================================================
# get_customer_pbr_rules()
# ===========================================================================


def _make_pbr_activation(
    *,
    vlan_id: int = 100,
    deployment_id: str | None = "dep-a",
    security_policies: list | None = None,
    environment: str | None = None,
) -> dict:
    """deployment_id models VxlanSegment.customer_deployments (TopologyCustomer
    ids) — the field FirewallContext.tenant is actually matched against, NOT
    segment.owner (an OrganizationCustomer, a different kind with different
    ids that never equals FirewallContext.tenant.id)."""
    seg: dict = {
        "id": "seg-1",
        "name": "seg-1",
        "customer_name": "web",
        "customer_deployments": [{"id": deployment_id}] if deployment_id else [],
    }
    if environment is not None:
        seg["environment"] = environment
    if security_policies is not None:
        seg["security_policies"] = security_policies
    return {"vlan_id": vlan_id, "segment": seg}


def _make_pbr_rule(*, action: str = "permit", dst_prefix: str | None = "10.0.2.0/24", disabled: bool = False) -> dict:
    rule: dict = {"action": action, "disabled": disabled}
    if dst_prefix:
        rule["destination_segment"] = {"gateway": {"ip_prefix": {"prefix": dst_prefix}}}
    return rule


def _make_context_leg(*, fw_ip: str = "10.65.0.0/30", tenant_id: str | None = None) -> dict:
    """A ManagedFirewallContext dict as returned by
    _flatten_deployment_firewall_contexts() — device-scoped (this device's
    own deployment's firewall-role devices), not the rendered device's own
    interfaces. The firewall's OWN leg is what provides the nexthop, found
    by filtering interface_capabilities legs to the one on a device with
    role="firewall"."""
    ctx: dict = {
        "interface_capabilities": [{"device": {"role": "firewall"}, "ip_address": {"address": fw_ip}}],
    }
    if tenant_id:
        ctx["tenant"] = {"id": tenant_id}
    return ctx


# ===========================================================================
# _flatten_deployment_firewall_contexts()
# ===========================================================================


def _make_ha_capability(*, contexts: list[dict] | None = None) -> dict:
    return {"typename": "ManagedFirewallHA", "contexts": contexts or []}


def _make_fw_device(*, contexts: list[dict] | None = None, extra_capability: dict | None = None) -> dict:
    capabilities = [_make_ha_capability(contexts=contexts)]
    if extra_capability:
        capabilities.append(extra_capability)
    return {"capabilities": capabilities}


class TestFlattenDeploymentFirewallContexts:
    def test_none_deployment_returns_empty(self) -> None:
        assert _flatten_deployment_firewall_contexts(None) == []

    def test_empty_deployment_returns_empty(self) -> None:
        assert _flatten_deployment_firewall_contexts({}) == []

    def test_dc_tier_device_contexts_extracted(self) -> None:
        """DC-tier devices (border-leaf, firewall, spine) have contexts directly
        under deployment.devices — no parent hop needed."""
        ctx = {"id": "ctx-1"}
        deployment = {"devices": [_make_fw_device(contexts=[ctx])]}
        assert _flatten_deployment_firewall_contexts(deployment) == [ctx]

    def test_pod_tier_device_contexts_extracted_via_parent(self) -> None:
        """Pod-tier devices (leaf/tor/access-leaf) have their deployment set to
        the POD, so contexts live under deployment.parent.devices instead."""
        ctx = {"id": "ctx-1"}
        deployment = {"devices": [], "parent": {"devices": [_make_fw_device(contexts=[ctx])]}}
        assert _flatten_deployment_firewall_contexts(deployment) == [ctx]

    def test_non_firewall_ha_capability_ignored(self) -> None:
        deployment = {
            "devices": [_make_fw_device(contexts=[{"id": "ctx-1"}], extra_capability={"typename": "ManagedBGP"})]
        }
        result = _flatten_deployment_firewall_contexts(deployment)
        assert result == [{"id": "ctx-1"}]

    def test_dedup_across_dc_and_pod_tier_paths(self) -> None:
        """A context reachable both directly and via parent must not double-count."""
        ctx = {"id": "ctx-1"}
        deployment = {
            "devices": [_make_fw_device(contexts=[ctx])],
            "parent": {"devices": [_make_fw_device(contexts=[ctx])]},
        }
        result = _flatten_deployment_firewall_contexts(deployment)
        assert result == [ctx]

    def test_multiple_contexts_across_devices(self) -> None:
        ctx_a, ctx_b = {"id": "ctx-a"}, {"id": "ctx-b"}
        deployment = {"devices": [_make_fw_device(contexts=[ctx_a]), _make_fw_device(contexts=[ctx_b])]}
        result = _flatten_deployment_firewall_contexts(deployment)
        assert {c["id"] for c in result} == {"ctx-a", "ctx-b"}


class TestGetCustomerPbrRules:
    def test_none_activations_returns_empty(self) -> None:
        assert get_customer_pbr_rules(None, [_make_context_leg()]) == []

    def test_no_firewall_context_leg_returns_empty(self) -> None:
        activations = [_make_pbr_activation(security_policies=[])]
        assert get_customer_pbr_rules(activations, []) == []

    def test_segment_without_security_policies_key_is_skipped(self) -> None:
        """Missing 'security_policies' key (not queried) means no PBR rule — same
        gate get_acls() uses."""
        activations = [_make_pbr_activation(security_policies=None)]
        contexts = [_make_context_leg()]
        assert get_customer_pbr_rules(activations, contexts) == []

    def test_shared_context_nexthop_used_when_no_tenant(self) -> None:
        activations = [_make_pbr_activation(deployment_id="dep-a", security_policies=[])]
        contexts = [_make_context_leg(fw_ip="10.65.0.0/30")]
        result = get_customer_pbr_rules(activations, contexts)
        assert len(result) == 1
        assert result[0]["fw_nexthop"] == "10.65.0.0"
        assert result[0]["vlan_id"] == 100
        assert result[0]["bypass_prefixes"] == []

    def test_dedicated_context_nexthop_preferred_over_shared(self) -> None:
        activations = [_make_pbr_activation(deployment_id="dep-a", security_policies=[])]
        contexts = [
            _make_context_leg(fw_ip="10.65.0.0/30"),  # shared
            _make_context_leg(fw_ip="10.66.0.0/30", tenant_id="dep-a"),  # dedicated
        ]
        result = get_customer_pbr_rules(activations, contexts)
        assert result[0]["fw_nexthop"] == "10.66.0.0"

    def test_dedicated_context_for_other_tenant_not_used(self) -> None:
        """A dedicated context for a DIFFERENT deployment must not leak as this
        segment's nexthop — falls back to shared (or nothing) instead."""
        activations = [_make_pbr_activation(deployment_id="dep-a", security_policies=[])]
        contexts = [_make_context_leg(fw_ip="10.66.0.0/30", tenant_id="dep-b")]
        assert get_customer_pbr_rules(activations, contexts) == []

    def test_stretched_vxlan_segment_matches_by_any_of_its_deployments(self) -> None:
        """VxlanSegment.customer_deployments is cardinality many (stretch across
        DCs) — a dedicated context keyed to ANY one of them must match, not
        just the first."""
        activations = [
            {
                "vlan_id": 100,
                "segment": {
                    "id": "seg-1",
                    "name": "seg-1",
                    "customer_name": "web",
                    "customer_deployments": [{"id": "dep-a"}, {"id": "dep-b"}],
                    "security_policies": [],
                },
            }
        ]
        contexts = [_make_context_leg(fw_ip="10.66.0.0/30", tenant_id="dep-b")]
        result = get_customer_pbr_rules(activations, contexts)
        assert result[0]["fw_nexthop"] == "10.66.0.0"

    def test_owner_org_id_never_matches_tenant_deployment_id(self) -> None:
        """Regression: FirewallContext.tenant peers TopologyCustomer (a
        deployment footprint), not OrganizationCustomer (the org) — segment.owner
        must never be used for this match even if it happens to look like an id."""
        activations = [
            {
                "vlan_id": 100,
                "segment": {
                    "id": "seg-1",
                    "name": "seg-1",
                    "customer_name": "web",
                    "owner": {"id": "dep-a"},
                    "customer_deployments": [],
                    "security_policies": [],
                },
            }
        ]
        contexts = [_make_context_leg(fw_ip="10.66.0.0/30", tenant_id="dep-a")]
        assert get_customer_pbr_rules(activations, contexts) == []

    def test_context_without_firewall_leg_is_skipped(self) -> None:
        """inline connectivity_mode never allocates an IP on the firewall leg —
        a context with no addressed firewall-role leg contributes no nexthop."""
        activations = [_make_pbr_activation(deployment_id="dep-a", security_policies=[])]
        contexts = [{"interface_capabilities": [{"device": {"role": "firewall"}, "ip_address": {}}]}]
        assert get_customer_pbr_rules(activations, contexts) == []

    def test_permit_rule_becomes_bypass_prefix(self) -> None:
        policies = [{"enabled": True, "rules": [_make_pbr_rule(dst_prefix="10.0.2.0/24")]}]
        activations = [_make_pbr_activation(security_policies=policies)]
        contexts = [_make_context_leg()]
        result = get_customer_pbr_rules(activations, contexts)
        assert result[0]["bypass_prefixes"] == ["10.0.2.0/24"]

    def test_deny_rule_is_not_a_bypass(self) -> None:
        policies = [{"enabled": True, "rules": [_make_pbr_rule(action="deny", dst_prefix="10.0.2.0/24")]}]
        activations = [_make_pbr_activation(security_policies=policies)]
        contexts = [_make_context_leg()]
        result = get_customer_pbr_rules(activations, contexts)
        assert result[0]["bypass_prefixes"] == []

    def test_disabled_rule_is_not_a_bypass(self) -> None:
        policies = [{"enabled": True, "rules": [_make_pbr_rule(disabled=True)]}]
        activations = [_make_pbr_activation(security_policies=policies)]
        contexts = [_make_context_leg()]
        result = get_customer_pbr_rules(activations, contexts)
        assert result[0]["bypass_prefixes"] == []

    def test_disabled_policy_contributes_no_bypass(self) -> None:
        policies = [{"enabled": False, "rules": [_make_pbr_rule()]}]
        activations = [_make_pbr_activation(security_policies=policies)]
        contexts = [_make_context_leg()]
        result = get_customer_pbr_rules(activations, contexts)
        assert result[0]["bypass_prefixes"] == []

    def test_cross_owner_permit_still_bypasses(self) -> None:
        """A permit rule bypasses PBR regardless of whether source/destination
        share an owner — intra- and inter-customer traffic use one model."""
        policies = [{"enabled": True, "rules": [_make_pbr_rule(dst_prefix="10.9.0.0/24")]}]
        activations = [_make_pbr_activation(deployment_id="dep-a", security_policies=policies)]
        contexts = [_make_context_leg()]
        result = get_customer_pbr_rules(activations, contexts)
        assert "10.9.0.0/24" in result[0]["bypass_prefixes"]

    def test_duplicate_vlan_deduplicated(self) -> None:
        activations = [
            _make_pbr_activation(vlan_id=100, security_policies=[]),
            _make_pbr_activation(vlan_id=100, security_policies=[]),
        ]
        contexts = [_make_context_leg()]
        result = get_customer_pbr_rules(activations, contexts)
        assert len(result) == 1

    def test_results_sorted_by_vlan_id(self) -> None:
        activations = [
            _make_pbr_activation(vlan_id=200, security_policies=[]),
            _make_pbr_activation(vlan_id=100, security_policies=[]),
        ]
        contexts = [_make_context_leg()]
        result = get_customer_pbr_rules(activations, contexts)
        assert [r["vlan_id"] for r in result] == [100, 200]

    def test_customer_name_and_environment_passed_through(self) -> None:
        activations = [_make_pbr_activation(security_policies=[], environment="s")]
        contexts = [_make_context_leg()]
        result = get_customer_pbr_rules(activations, contexts)
        assert result[0]["customer_name"] == "web"
        assert result[0]["environment"] == "s"

    def test_environment_none_when_absent(self) -> None:
        activations = [_make_pbr_activation(security_policies=[])]
        contexts = [_make_context_leg()]
        result = get_customer_pbr_rules(activations, contexts)
        assert result[0]["environment"] is None


# ===========================================================================
# get_firewall_contexts()
# ===========================================================================


def _make_fw_context_interface(
    *,
    iface_name: str = "ethernet1/1.3000",
    ip_addr: str | None = "10.65.0.1/30",
    parent_name: str = "ethernet1/1",
    context_id: str = "ctx-1",
    context_name: str = "dc10-shared",
    vlan_id: int = 3000,
    tenant_name: str | None = None,
) -> dict:
    cap = {
        "typename": "ManagedFirewallContext",
        "id": context_id,
        "name": context_name,
        "vlan_id": vlan_id,
        "context_id": None,
        "tenant": {"name": tenant_name} if tenant_name else {},
    }
    iface: dict = {
        "name": iface_name,
        "parent_interface": {"name": parent_name},
        "interface_capabilities": [cap],
    }
    if ip_addr:
        iface["ip_address"] = {"address": ip_addr}
    return iface


class TestGetFirewallContexts:
    def test_none_returns_empty(self) -> None:
        assert get_firewall_contexts(None) == []

    def test_empty_list_returns_empty(self) -> None:
        assert get_firewall_contexts([]) == []

    def test_interface_without_context_capability_ignored(self) -> None:
        iface = {"name": "eth0", "interface_capabilities": [{"typename": "ManagedVxlanSegment"}]}
        assert get_firewall_contexts([iface]) == []

    def test_single_context_extracted(self) -> None:
        result = get_firewall_contexts([_make_fw_context_interface()])
        assert len(result) == 1
        assert result[0]["name"] == "dc10-shared"
        assert result[0]["vlan_id"] == 3000
        assert result[0]["sub_interface"] == "ethernet1/1.3000"
        assert result[0]["parent_interface"] == {"name": "ethernet1/1"}
        assert result[0]["ip_address"] == "10.65.0.1/30"
        assert result[0]["tenant_name"] is None

    def test_dedicated_context_carries_tenant_name(self) -> None:
        result = get_firewall_contexts([_make_fw_context_interface(tenant_name="C005-P-DC10")])
        assert result[0]["tenant_name"] == "C005-P-DC10"

    def test_context_without_ip_still_extracted(self) -> None:
        """inline connectivity_mode contexts have no dedicated p2p IP."""
        result = get_firewall_contexts([_make_fw_context_interface(ip_addr=None)])
        assert len(result) == 1
        assert result[0]["ip_address"] is None

    def test_duplicate_context_id_deduplicated(self) -> None:
        ifaces = [
            _make_fw_context_interface(context_id="ctx-1"),
            _make_fw_context_interface(context_id="ctx-1", iface_name="ethernet1/2.3000"),
        ]
        assert len(get_firewall_contexts(ifaces)) == 1

    def test_multiple_contexts_sorted_by_name(self) -> None:
        ifaces = [
            _make_fw_context_interface(context_id="ctx-b", context_name="dc10-b-dedicated"),
            _make_fw_context_interface(context_id="ctx-a", context_name="dc10-a-dedicated"),
        ]
        result = get_firewall_contexts(ifaces)
        assert [c["name"] for c in result] == ["dc10-a-dedicated", "dc10-b-dedicated"]


# ===========================================================================
# get_border_leaf_pbr_rules()
# ===========================================================================


def _make_dc_activation(
    *,
    vni: int = 10100,
    seg_id: str = "seg-1",
    customer_name: str = "web",
    deployment_id: str | None = "dep-a",
    sgt: int | None = None,
    gateway_prefix: str | None = "10.10.1.0/24",
    environment: str | None = None,
) -> dict:
    """Models one _flatten_deployment_segment_activations() entry — border-leaf's
    DC-wide segment_deployments traversal, NOT the per-interface
    _collect_activations_from_interfaces() a leaf uses. Keyed by vni now
    (not vlan_id — local VLAN ID is per-VLAN-domain, no longer DC-wide)."""
    seg: dict = {
        "id": seg_id,
        "name": seg_id,
        "customer_name": customer_name,
        "customer_deployments": [{"id": deployment_id}] if deployment_id else [],
    }
    if sgt is not None:
        seg["security_tag"] = {"name": "web-tier", "group_id": sgt}
    if gateway_prefix is not None:
        seg["gateway"] = {"ip_prefix": {"prefix": gateway_prefix}}
    if environment is not None:
        seg["environment"] = environment
    return {"vni": vni, "segment": seg}


class TestGetBorderLeafPbrRules:
    def test_none_activations_returns_empty(self) -> None:
        assert get_border_leaf_pbr_rules(None, [_make_context_leg()], "cisco_nxos") == []

    def test_no_firewall_context_returns_empty(self) -> None:
        activations = [_make_dc_activation()]
        assert get_border_leaf_pbr_rules(activations, [], "cisco_nxos") == []

    def test_cisco_platform_matches_by_tag_when_sgt_present(self) -> None:
        activations = [_make_dc_activation(sgt=10)]
        contexts = [_make_context_leg()]
        result = get_border_leaf_pbr_rules(activations, contexts, "cisco_nxos")
        assert len(result) == 1
        assert result[0]["match_by_tag"] is True
        assert result[0]["sgt"] == 10
        assert result[0]["source_prefixes"] == []

    def test_arista_platform_matches_by_tag_when_sgt_present(self) -> None:
        activations = [_make_dc_activation(sgt=20)]
        contexts = [_make_context_leg()]
        result = get_border_leaf_pbr_rules(activations, contexts, "arista_eos")
        assert result[0]["match_by_tag"] is True
        assert result[0]["sgt"] == 20
        assert result[0]["sgt_name"] == "web-tier"

    def test_sgt_name_none_when_not_matching_by_tag(self) -> None:
        """A platform without hardware tag-matching (or a segment with no
        SGT at all) must not carry a stale sgt_name through — the template
        renders match_by_tag as the single source of truth."""
        activations = [_make_dc_activation(sgt=10)]
        contexts = [_make_context_leg()]
        result = get_border_leaf_pbr_rules(activations, contexts, "dell_sonic")
        assert result[0]["match_by_tag"] is False
        assert result[0]["sgt_name"] is None

    def test_sonic_platform_never_matches_by_tag_even_with_sgt(self) -> None:
        """SONiC has no hardware SGT/security-group primitive — always falls
        back to prefix matching, same as .dev/scenariusze.txt's own
        SONiC-BORDER-LEAF section."""
        activations = [_make_dc_activation(sgt=10, gateway_prefix="10.10.1.0/24")]
        contexts = [_make_context_leg()]
        result = get_border_leaf_pbr_rules(activations, contexts, "dell_sonic")
        assert result[0]["match_by_tag"] is False
        assert result[0]["sgt"] is None
        assert result[0]["source_prefixes"] == ["10.10.1.0/24"]

    def test_cisco_platform_falls_back_to_prefix_when_no_sgt(self) -> None:
        activations = [_make_dc_activation(sgt=None, gateway_prefix="10.10.1.0/24")]
        contexts = [_make_context_leg()]
        result = get_border_leaf_pbr_rules(activations, contexts, "cisco_nxos")
        assert result[0]["match_by_tag"] is False
        assert result[0]["source_prefixes"] == ["10.10.1.0/24"]

    def test_no_tag_and_no_prefix_is_skipped(self) -> None:
        """Nothing to match this segment's traffic by — must not emit an
        unreachable rule."""
        activations = [_make_dc_activation(sgt=None, gateway_prefix=None)]
        contexts = [_make_context_leg()]
        assert get_border_leaf_pbr_rules(activations, contexts, "dell_sonic") == []

    def test_dedicated_context_nexthop_preferred_over_shared(self) -> None:
        activations = [_make_dc_activation(deployment_id="dep-a", sgt=10)]
        contexts = [
            _make_context_leg(fw_ip="10.65.0.0/30"),  # shared
            _make_context_leg(fw_ip="10.66.0.0/30", tenant_id="dep-a"),  # dedicated
        ]
        result = get_border_leaf_pbr_rules(activations, contexts, "cisco_nxos")
        assert result[0]["fw_nexthop"] == "10.66.0.0"

    def test_shared_context_used_when_no_dedicated_tenant_match(self) -> None:
        activations = [_make_dc_activation(deployment_id="dep-a", sgt=10)]
        contexts = [_make_context_leg(fw_ip="10.65.0.0/30")]
        result = get_border_leaf_pbr_rules(activations, contexts, "cisco_nxos")
        assert result[0]["fw_nexthop"] == "10.65.0.0"

    def test_multiple_segments_different_customers_all_present(self) -> None:
        activations = [
            _make_dc_activation(vni=10100, seg_id="seg-1", customer_name="web", sgt=10),
            _make_dc_activation(vni=10200, seg_id="seg-2", customer_name="db", sgt=20),
        ]
        contexts = [_make_context_leg()]
        result = get_border_leaf_pbr_rules(activations, contexts, "cisco_nxos")
        assert sorted(r["customer_name"] for r in result) == ["db", "web"]

    def test_duplicate_vni_deduplicated(self) -> None:
        activations = [_make_dc_activation(vni=10100, sgt=10), _make_dc_activation(vni=10100, sgt=10)]
        contexts = [_make_context_leg()]
        result = get_border_leaf_pbr_rules(activations, contexts, "cisco_nxos")
        assert len(result) == 1

    def test_customer_name_and_environment_passed_through(self) -> None:
        activations = [_make_dc_activation(sgt=10, environment="production")]
        contexts = [_make_context_leg()]
        result = get_border_leaf_pbr_rules(activations, contexts, "cisco_nxos")
        assert result[0]["customer_name"] == "web"
        assert result[0]["environment"] == "production"

    def test_same_customer_multiple_segments_grouped_into_one_prefix_rule(self) -> None:
        """Two segments, same customer, same fw_nexthop, both prefix-fallback
        — must merge into ONE rule with both prefixes, not two separate rules."""
        activations = [
            _make_dc_activation(vni=10100, seg_id="seg-1", customer_name="web", gateway_prefix="10.10.1.0/24"),
            _make_dc_activation(vni=10200, seg_id="seg-2", customer_name="web", gateway_prefix="10.10.2.0/24"),
        ]
        contexts = [_make_context_leg()]
        result = get_border_leaf_pbr_rules(activations, contexts, "dell_sonic")
        assert len(result) == 1
        assert result[0]["source_prefixes"] == ["10.10.1.0/24", "10.10.2.0/24"]

    def test_same_customer_multiple_sgts_get_separate_rules(self) -> None:
        """Two segments, same customer, both tag-matched but different SGTs
        — each SGT keeps its own rule (no confirmed multi-value match syntax)."""
        activations = [
            _make_dc_activation(vni=10100, seg_id="seg-1", customer_name="web", sgt=10),
            _make_dc_activation(vni=10200, seg_id="seg-2", customer_name="web", sgt=20),
        ]
        contexts = [_make_context_leg()]
        result = get_border_leaf_pbr_rules(activations, contexts, "cisco_nxos")
        assert len(result) == 2
        assert sorted(r["sgt"] for r in result) == [10, 20]
        assert all(r["customer_name"] == "web" for r in result)

    def test_mixed_tag_and_prefix_same_customer_both_present(self) -> None:
        """One customer with segments on both a tag-capable leaf and a
        non-tagging leaf — gets one merged prefix rule AND its own tag rule."""
        activations = [
            _make_dc_activation(vni=10100, seg_id="seg-1", customer_name="web", sgt=10),
            _make_dc_activation(
                vni=10200, seg_id="seg-2", customer_name="web", sgt=None, gateway_prefix="10.10.2.0/24"
            ),
        ]
        contexts = [_make_context_leg()]
        result = get_border_leaf_pbr_rules(activations, contexts, "cisco_nxos")
        assert len(result) == 2
        by_tag = {r["match_by_tag"]: r for r in result}
        assert by_tag[True]["sgt"] == 10
        assert by_tag[False]["source_prefixes"] == ["10.10.2.0/24"]
