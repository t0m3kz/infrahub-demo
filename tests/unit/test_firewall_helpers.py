"""Unit tests for firewall zones, static routes and exchange transits in transforms/helpers/firewall.py."""

from unittest.mock import patch

from tests.unit.firewall_helpers_shared import _make_pbr_activation, _make_segment_with_prefix
from transforms.helpers.firewall import (
    _service_context_caps,
    get_exchange_routes,
    get_exchange_transits,
    get_firewall_static_routes,
    get_firewall_zones,
)
from transforms.helpers.firewall_pbr import get_customer_pbr_rules
from utils.exchange_transit import EXCHANGE_PEERS, exchange_peer_zones

# ---------------------------------------------------------------------------
# Helpers — zone data
# ---------------------------------------------------------------------------


def _make_zone(
    *,
    name: str = "internal",
    trust_level: int | None = 100,
    description: str | None = "Test zone",
    network_segments: list | None = None,
) -> dict:
    """Build a cleaned SecurityZone dict as returned by clean_data()."""
    zone: dict = {"name": name}
    if trust_level is not None:
        zone["trust_level"] = trust_level
    if description is not None:
        zone["description"] = description
    if network_segments is not None:
        zone["network_segments"] = network_segments
    return zone


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
    """Build a minimal cleaned SegmentDeployment dict for the firewall static-route helpers."""
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


# ===========================================================================
# get_firewall_zones()
# ===========================================================================


class TestGetFirewallZones:
    def test_none_returns_empty(self) -> None:
        assert get_firewall_zones(None) == []

    def test_empty_list_returns_empty(self) -> None:
        assert get_firewall_zones([]) == []

    def test_zone_without_name_is_skipped(self) -> None:
        result = get_firewall_zones([{"trust_level": 50}])
        assert result == []

    def test_single_zone_basic_fields(self) -> None:
        zone = _make_zone(name="internal", trust_level=100, description="Corp LAN")
        result = get_firewall_zones([zone])
        assert result == [{"name": "internal", "trust_level": 100, "description": "Corp LAN", "member_cidrs": []}]

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
            _make_zone(name="untrust", trust_level=0),
            _make_zone(name="dmz", trust_level=50),
            _make_zone(name="internal", trust_level=100),
        ]
        result = get_firewall_zones(zones)
        assert [z["name"] for z in result] == ["internal", "dmz", "untrust"]

    def test_zone_missing_optional_trust_level_defaults_to_zero(self) -> None:
        zone = {"name": "bare-zone"}
        result = get_firewall_zones([zone])
        assert len(result) == 1
        assert result[0]["trust_level"] == 0

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
        zone = {"name": "internal", "trust_level": 100, "member_cidrs": ["10.0.0.0/8"]}
        assert get_firewall_static_routes([iface], [zone]) == []

    def test_interface_missing_ip_address_is_skipped(self) -> None:
        iface = {"name": "eth0.10", "security_zone": {"name": "internal"}}
        zone = {"name": "internal", "trust_level": 100, "member_cidrs": ["10.0.0.0/8"]}
        assert get_firewall_static_routes([iface], [zone]) == []

    def test_interface_missing_ns_name_is_skipped(self) -> None:
        iface = {
            "name": "eth0.10",
            "security_zone": {"name": "internal"},
            "ip_address": {"address": "10.0.0.1/30"},
        }
        zone = {"name": "internal", "trust_level": 100, "member_cidrs": ["10.0.0.0/8"]}
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
        zone = {"name": "dmz", "trust_level": 50, "member_cidrs": ["192.168.1.0/24"]}
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
            {"name": "internal", "trust_level": 100, "member_cidrs": ["10.10.0.0/24"]},
            {"name": "dmz", "trust_level": 50, "member_cidrs": ["192.168.0.0/24"]},
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
            {"name": "internal", "trust_level": 100, "member_cidrs": ["10.10.1.0/24"]},
            {"name": "external", "trust_level": 0, "member_cidrs": ["10.20.1.0/24"]},
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
        zone = {"name": "internal", "trust_level": 100, "member_cidrs": ["10.0.0.0/8"]}
        result = get_firewall_static_routes([iface], [zone])
        assert result == []

    def test_zone_not_found_in_lookup_skips_interface(self) -> None:
        iface = _make_fw_interface(zone_name="nonexistent-zone")
        zone = {"name": "internal", "trust_level": 100, "member_cidrs": ["10.0.0.0/8"]}
        result = get_firewall_static_routes([iface], [zone])
        assert result == []


# ===========================================================================
# get_exchange_transits() / get_exchange_routes()
# ===========================================================================


def _ns(name: str, ns_type: str, l3_vni: int) -> dict:
    return {"name": name, "namespace_type": ns_type, "l3_vni": l3_vni}


_PROD = _ns("PROD", "prod", 50001)
_INET = _ns("INTERNET", "internet", 50003)
_NONPROD = _ns("NON-PROD", "non_prod", 50002)


def _leg(address: str, namespace: dict, exchanges: list[dict] | None = None, name: str = "eth1.3000") -> dict:
    return {
        "name": name,
        "device": {"name": "dc-fw-1", "role": "firewall"},
        "ip_address": {"address": address, "ip_namespace": namespace},
        "capabilities": exchanges or [],
    }


def _exchange(a: dict, z: dict, tenant: str | None = None, exchange_id: str = "x1") -> dict:
    return {
        "id": exchange_id,
        "gateway": {"id": "ctx-1", "tenant": {"id": tenant} if tenant else None},
    }


def _context(legs: list[dict], *, tenant: str | None = None, served: list[str] | None = None, vlan: int = 3001) -> dict:
    return {
        "typename": "ManagedFirewallContext",
        "id": "ctx-1",
        "name": "dc11-fw-shared",
        "vlan_id": vlan,
        "tenant": {"id": tenant} if tenant else None,
        "served_deployments": [{"id": d} for d in served or []],
        "capabilities": legs,
    }


def _bl_port(ctx: dict, role: str = "firewall") -> list[dict]:
    return [{"name": "Ethernet1/10", "role": role, "capabilities": [ctx]}]


def _shared_context(*, tenant: str | None = None, served: list[str] | None = None) -> dict:
    """PROD (/29 100.66.0.0) and INTERNET (/29 100.66.32.0) legs of one context, one exchange."""
    exchange = _exchange(_PROD, _INET, tenant=tenant)
    legs = [
        _leg("100.66.0.5/29", _PROD, [exchange]),
        _leg("100.66.0.6/29", _PROD, [exchange]),
        _leg("100.66.32.5/29", _INET, [exchange], name="eth1.3400"),
        _leg("100.66.32.6/29", _INET, [exchange], name="eth1.3400"),
    ]
    return _context(legs, tenant=tenant, served=served)


def _border_transits(interfaces: list[dict] | None) -> list[dict]:
    """The transits a border device derives from the contexts on its firewall-role ports."""
    return get_exchange_transits(_service_context_caps(interfaces))


class TestGetExchangeTransits:
    def test_no_interfaces_returns_empty(self) -> None:
        assert _border_transits(None) == []

    def test_port_without_firewall_role_is_ignored(self) -> None:
        assert _border_transits(_bl_port(_shared_context(), role="uplink")) == []

    def test_one_transit_per_leg_namespace_with_band_vlan_and_vni(self) -> None:
        transits = _border_transits(_bl_port(_shared_context()))
        assert [t["vlan_id"] for t in transits] == [3001, 3401]
        assert [t["vni"] for t in transits] == [64001, 64401]
        assert all(t["transit_context"] == "dc11-fw-shared" for t in transits)

    def test_gateway_is_the_anycast_offset_of_the_slash29(self) -> None:
        prod = _border_transits(_bl_port(_shared_context()))[0]["segment"]
        assert prod["name"] == "XCHG-dc11-fw-shared-PROD"
        assert prod["gateway"]["address"] == "100.66.0.1/29"
        assert prod["gateway"]["ip_prefix"] == {
            "prefix": "100.66.0.0/29",
            "ip_namespace": {"name": "PROD", "l3_vni": 50001, "namespace_type": "prod"},
        }
        assert prod["terminate_inline"] is False

    def test_default_namespace_legacy_leg_is_skipped(self) -> None:
        legacy = _context([_leg("10.65.0.1/30", {"name": "default", "namespace_type": None, "l3_vni": None})])
        assert _border_transits(_bl_port(legacy)) == []

    def test_same_context_on_two_ports_is_counted_once(self) -> None:
        ports = _bl_port(_shared_context()) + _bl_port(_shared_context())
        assert len(_border_transits(ports)) == 2


class TestGetExchangeRoutes:
    @staticmethod
    def _with_snat(ctx: dict, address: str | None = "192.0.2.5/32") -> dict:
        return {**ctx, "snat_address": {"address": address} if address else None}

    def test_shared_context_gets_default_route_in_tenant_vrf_to_its_vip(self) -> None:
        routes = get_exchange_routes(_bl_port(_shared_context(served=["dep-a"])))
        assert {"vrf": "PROD", "l3_vni": 50001, "prefix": "0.0.0.0/0", "nexthop": "100.66.0.4"} in routes

    def test_dedicated_context_gets_no_default_route(self) -> None:
        routes = get_exchange_routes(_bl_port(_shared_context(tenant="dep-a", served=["dep-a"])))
        assert [r for r in routes if r["prefix"] == "0.0.0.0/0"] == []

    def test_internet_vrf_returns_the_snat_host_route_to_internet_vip(self) -> None:
        context = self._with_snat(_shared_context(served=["dep-a"]))
        routes = get_exchange_routes(_bl_port(context))
        internet = [r for r in routes if r["vrf"] == "INTERNET"]
        assert internet == [{"vrf": "INTERNET", "l3_vni": 50003, "prefix": "192.0.2.5/32", "nexthop": "100.66.32.4"}]

    def test_internet_vrf_carries_no_tenant_prefix(self) -> None:
        context = self._with_snat(_shared_context(served=["dep-a"]))
        internet = [r for r in get_exchange_routes(_bl_port(context)) if r["vrf"] == "INTERNET"]
        assert all(r["prefix"].endswith("/32") for r in internet)

    def test_context_without_snat_emits_nothing_in_internet(self) -> None:
        routes = get_exchange_routes(_bl_port(_shared_context(served=["dep-a"])))
        assert [r for r in routes if r["vrf"] == "INTERNET"] == []

    def test_each_context_routes_its_own_snat_to_its_own_vip(self) -> None:
        shared = self._with_snat(_shared_context(served=["dep-a"]), "192.0.2.5/32")
        other = {**self._with_snat(_shared_context(tenant="dep-b", served=["dep-b"]), "192.0.2.6/32"), "id": "ctx-2"}
        routes = get_exchange_routes(_bl_port(shared) + _bl_port(other))
        assert {(r["prefix"], r["nexthop"]) for r in routes if r["vrf"] == "INTERNET"} == {
            ("192.0.2.5/32", "100.66.32.4"),
            ("192.0.2.6/32", "100.66.32.4"),
        }

    def test_no_default_route_in_internet_vrf(self) -> None:
        routes = get_exchange_routes(_bl_port(self._with_snat(_shared_context(served=["dep-a"]))))
        assert [r for r in routes if r["vrf"] == "INTERNET" and r["prefix"] == "0.0.0.0/0"] == []

    def test_snat_route_only_for_an_internet_z_leg(self) -> None:
        """A MANAGEMENT peer (in EXCHANGE_PEERS) is not source-NATed: no /32 in its VRF."""
        mgmt = _ns("MANAGEMENT", "management", 50004)
        exchange = _exchange(_PROD, mgmt)
        legs = [_leg("100.66.0.5/29", _PROD, [exchange]), _leg("100.66.48.5/29", mgmt, [exchange], name="eth1.3600")]
        context = self._with_snat(_context(legs))
        with patch.dict(EXCHANGE_PEERS, {"prod": ("internet", "management")}):
            routes = get_exchange_routes(_bl_port(context))
        assert [r for r in routes if r["vrf"] == "MANAGEMENT"] == []
        assert [r["prefix"] for r in routes] == ["0.0.0.0/0"]

    def test_prod_is_never_paired_with_non_prod(self) -> None:
        exchange = _exchange(_PROD, _NONPROD)
        legs = [_leg("100.66.0.5/29", _PROD, [exchange]), _leg("100.66.16.5/29", _NONPROD, [exchange])]
        assert get_exchange_routes(_bl_port(self._with_snat(_context(legs)))) == []

    def test_routes_are_sorted_and_empty_without_interfaces(self) -> None:
        assert get_exchange_routes(None) == []
        routes = get_exchange_routes(_bl_port(self._with_snat(_shared_context(served=["dep-a"]))))
        assert routes == sorted(routes, key=lambda r: (r["vrf"], r["prefix"], r["nexthop"]))


def _three_leg_context(*, tenant: str | None = None, served: list[str] | None = None) -> dict:
    """PROD carries two exchanges (with INTERNET and with MANAGEMENT); each peer leg carries its own."""
    mgmt = _ns("MANAGEMENT", "management", 50004)
    to_internet = _exchange(_PROD, _INET, tenant=tenant, exchange_id="x-inet")
    to_mgmt = _exchange(_PROD, mgmt, tenant=tenant, exchange_id="x-mgmt")
    legs = [
        _leg("100.66.0.5/29", _PROD, [to_internet, to_mgmt]),
        _leg("100.66.32.5/29", _INET, [to_internet], name="eth1.3400"),
        _leg("100.66.48.5/29", mgmt, [to_mgmt], name="eth1.3600"),
    ]
    return _context(legs, tenant=tenant, served=served)


class TestManagementExchangeOnTheBorderLeaf:
    def test_management_leg_becomes_a_transit_in_its_own_vrf_slot(self) -> None:
        transits = _border_transits(_bl_port(_three_leg_context()))
        assert [(t["vlan_id"], t["vni"]) for t in transits] == [(3001, 64001), (3401, 64401), (3601, 64601)]
        segment = transits[-1]["segment"]
        assert segment["name"] == "XCHG-dc11-fw-shared-MANAGEMENT"
        assert segment["gateway"]["address"] == "100.66.48.1/29"
        assert segment["gateway"]["ip_prefix"]["ip_namespace"] == {
            "name": "MANAGEMENT",
            "l3_vni": 50004,
            "namespace_type": "management",
        }

    def test_management_needs_no_return_route_and_the_default_route_is_not_duplicated(self) -> None:
        context = {**_three_leg_context(served=["dep-a"]), "snat_address": {"address": "192.0.2.5/32"}}
        routes = get_exchange_routes(_bl_port(context))
        assert [r for r in routes if r["vrf"] == "MANAGEMENT"] == []
        assert routes == [
            {"vrf": "INTERNET", "l3_vni": 50003, "prefix": "192.0.2.5/32", "nexthop": "100.66.32.4"},
            {"vrf": "PROD", "l3_vni": 50001, "prefix": "0.0.0.0/0", "nexthop": "100.66.0.4"},
        ]


class TestExchangePeerZones:
    def test_peer_zones_are_the_zones_of_the_exchange_peer_types(self) -> None:
        assert exchange_peer_zones() == {"INTERNET-ZONE", "MANAGEMENT-ZONE"}

    def test_follows_exchange_peers(self) -> None:
        with patch.dict(EXCHANGE_PEERS, {"prod": ("internet",), "non_prod": ("internet",)}):
            assert exchange_peer_zones() == {"INTERNET-ZONE"}


class TestLeafPbrSkipsManagementSegments:
    def _management_activation(self) -> dict:
        act = _make_pbr_activation(deployment_id="dep-a", security_policies=[{"enabled": True, "rules": []}])
        act["segment"]["gateway"] = {
            "ip_prefix": {
                "prefix": "10.250.0.0/24",
                "ip_namespace": {"name": "MANAGEMENT", "namespace_type": "management"},
            }
        }
        return act

    def test_segment_in_the_management_namespace_gets_no_redirect(self) -> None:
        ctx = _shared_context(served=["dep-a"])
        assert get_customer_pbr_rules([self._management_activation()], [ctx]) == []

    def test_tenant_segment_beside_it_keeps_its_redirect(self) -> None:
        ctx = _shared_context(served=["dep-a"])
        tenant = _make_pbr_activation(
            vlan_id=101, deployment_id="dep-a", security_policies=[{"enabled": True, "rules": []}]
        )
        tenant["segment"]["gateway"] = {
            "ip_prefix": {"prefix": "10.0.1.0/24", "ip_namespace": {"name": "PROD", "namespace_type": "prod"}}
        }
        rules = get_customer_pbr_rules([self._management_activation(), tenant], [ctx])
        assert [r["vlan_id"] for r in rules] == [101]


class TestLeafPbrNexthopIsContextLegVip:
    def test_nexthop_is_the_vip_of_the_leg_in_the_segment_namespace(self) -> None:
        ctx = _shared_context(served=["dep-a"])
        act = _make_pbr_activation(deployment_id="dep-a", security_policies=[{"enabled": True, "rules": []}])
        act["segment"]["gateway"] = {"ip_prefix": {"prefix": "10.0.1.0/24", "ip_namespace": {"name": "PROD"}}}
        rules = get_customer_pbr_rules([act], [ctx])
        assert rules[0]["fw_nexthop"] == "100.66.0.4"

    def test_segment_without_a_leg_in_its_namespace_gets_no_rule(self) -> None:
        ctx = _shared_context(served=["dep-a"])
        act = _make_pbr_activation(deployment_id="dep-a", security_policies=[{"enabled": True, "rules": []}])
        act["segment"]["gateway"] = {"ip_prefix": {"prefix": "10.0.1.0/24", "ip_namespace": {"name": "NON-PROD"}}}
        assert get_customer_pbr_rules([act], [ctx]) == []
