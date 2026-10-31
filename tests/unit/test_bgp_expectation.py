"""utils/bgp_expectation.py: the per-session route expectation, on the failure cases of the 30_all demo."""

from __future__ import annotations

from typing import Any

import pytest

from generators import fabric_sizing
from utils.bgp_expectation import (
    DCI,
    DCI_UNDERLAY,
    EVPN,
    EXTERNAL,
    FABRIC,
    IPV4,
    IPV6,
    OVERLAY,
    TRANSIT,
    UNCLASSIFIED,
    UNDERLAY,
    VRF_INTERNAL,
    Device,
    DeviceAddrs,
    Entry,
    ExternalPolicy,
    Fabric,
    accepts,
    covers,
    evpn_route_bound,
    exact_entry,
    expect_session,
    host_entry,
    range_entry,
    session_class,
)
from utils.limits import BGP_MAX_PREFIX_CEILING, BGP_PREFIX_SLACK

POOL_V4 = "10.250.0.0/24"
POOL_V6 = "fd00:2500:1::/120"
VIP_DC10 = "fd00:2500:ff::10/128"
VIP_FR = "fd00:2500:ff::f1/128"


def _fabric(**overrides: Any) -> Fabric:
    """A small two-site fabric: a v4 and a v6 loopback pool, two VIPs, one PROD namespace with two segments."""
    spine = Device("spine1", "spine", DeviceAddrs(frozenset({"10.250.0.1/32"})))
    leaf = Device(
        "leaf1",
        "leaf",
        DeviceAddrs(frozenset({"10.250.0.11/32"})),
        namespaces=frozenset({"PROD"}),
    )
    bgw = Device(
        "bl-dc101101",
        "border-leaf",
        DeviceAddrs(frozenset({"fd00:2500:1::5/128"}), frozenset({VIP_DC10})),
        namespaces=frozenset({"PROD", "INTERNET"}),
        default_exit=frozenset({"INTERNET"}),
    )
    far_bgw = Device("eg-fr201", "edge", DeviceAddrs(frozenset({"fd00:2500:2::5/128"}), frozenset({VIP_FR})))
    values: dict[str, Any] = {
        "loopback_ranges": (POOL_V4, POOL_V6),
        "vips": frozenset({VIP_DC10, VIP_FR}),
        "devices": {d.name: d for d in (spine, leaf, bgw, far_bgw)},
        "namespace_prefixes": {
            "PROD": frozenset({"10.20.0.0/24", "10.20.1.0/24", "10.99.0.0/29"}),
            "INTERNET": frozenset({"203.0.113.0/24"}),
        },
        "default_exits": {"INTERNET": frozenset({"bl-dc101101"})},
        "transit_prefixes": frozenset({"10.99.0.0/29"}),
        "l2vni_count": 2,
    }
    values.update(overrides)
    return Fabric(**values)


def _underlay(remote: str = "spine1", afs: tuple[str, ...] = (IPV4,)) -> dict[str, object]:
    """A built underlay session (ttl 1) towards ``remote``."""
    return {"name": "u1", "ttl": 1, "peering_role": "underlay", "remote_device": remote, "address_families": list(afs)}


class TestEntries:
    """Entry arithmetic: what a prefix-list line matches and what covers what."""

    def test_host_entry_strips_the_mask_to_a_host_route(self) -> None:
        """An interface address with any mask becomes its /32 or /128."""
        assert host_entry("10.0.0.1/24") == Entry("10.0.0.1/32")
        assert host_entry("fd00::1/64") == Entry("fd00::1/128")

    def test_range_entry_matches_down_to_the_host_length(self) -> None:
        """A pool range is ``le 32`` / ``le 128``."""
        assert range_entry(POOL_V4) == Entry(POOL_V4, 32)
        assert range_entry(POOL_V6) == Entry(POOL_V6, 128)

    @pytest.mark.parametrize(
        ("prefix", "expected"),
        [
            ("10.250.0.77/32", True),
            ("10.250.0.0/24", True),
            ("10.250.1.1/32", False),
            ("fd00:2500:1::5/128", False),
        ],
    )
    def test_accepts_follows_prefix_list_semantics(self, prefix: str, expected: bool) -> None:
        """A range entry matches inside the prefix, never another family or outside it."""
        assert accepts(frozenset({range_entry(POOL_V4)}), prefix) is expected

    def test_exact_entry_does_not_match_a_longer_prefix(self) -> None:
        """No ``le``: only the prefix itself."""
        entries = frozenset({exact_entry("10.20.0.0/24")})
        assert accepts(entries, "10.20.0.0/24")
        assert not accepts(entries, "10.20.0.0/25")

    def test_covers_requires_one_allowed_entry_to_contain_the_whole_sent_entry(self) -> None:
        """Sent is covered only when an allowed entry contains it down to its longest length."""
        allowed = frozenset({range_entry(POOL_V4)})
        assert covers(allowed, frozenset({host_entry("10.250.0.9/32")}))
        assert not covers(allowed, frozenset({host_entry("10.251.0.9/32")}))
        assert not covers(frozenset({exact_entry(POOL_V4)}), frozenset({range_entry(POOL_V4)}))


class TestClasses:
    """session_class: the key is peering_role, ttl, vrf, transit and external."""

    @pytest.mark.parametrize(
        ("session", "klass"),
        [
            ({"ttl": 1, "peering_role": "underlay"}, UNDERLAY),
            ({"ttl": 255, "peering_role": "overlay"}, OVERLAY),
            ({"ttl": 1, "peering_role": "dci-underlay"}, DCI_UNDERLAY),
            ({"ttl": 2, "peering_role": "dci"}, DCI),
            ({"ttl": 1, "peering_role": "fabric"}, FABRIC),
            ({"vrf": "PROD", "external": True}, EXTERNAL),
            ({"vrf": "INTERNET", "transit": True, "external": True}, TRANSIT),
            ({"vrf": "PROD"}, VRF_INTERNAL),
        ],
    )
    def test_each_session_lands_in_one_class(self, session: dict[str, object], klass: str) -> None:
        """The built-session keys decide the class."""
        assert session_class(session) == klass

    def test_the_transit_vrf_is_never_an_internal_vrf_session(self) -> None:
        """Transit eg-pa401/402: the internal rule must not see the INTERNET VRF, the transit class owns it."""
        fabric = _fabric()
        session = {"name": "t", "vrf": "INTERNET", "transit": True, "address_families": [IPV4], "remote_device": "isp"}
        expectation = expect_session(session, fabric.devices["eg-fr201"], fabric)
        assert expectation.klass == TRANSIT
        assert expectation.families[IPV4].inbound == frozenset({exact_entry("0.0.0.0/0")})
        assert expectation.families[IPV4].outbound == frozenset()

    def test_an_sdwan_far_end_is_left_out(self) -> None:
        """SD-WAN/LISP sessions are not in the first class set: no expectation, nothing to render or check."""
        fabric = _fabric(unmanaged=frozenset({"office-wan-1"}))
        session = {"name": "w", "vrf": "PROD", "remote_device": "office-wan-1", "address_families": [IPV4]}
        assert session_class(session, fabric) == UNCLASSIFIED
        assert expect_session(session, fabric.devices["leaf1"], fabric).families == {}


class TestUnderlay:
    """The underlay carries loopbacks and VIPs only."""

    def test_a_leaf_accepts_the_whole_pool_from_a_spine_and_announces_its_own_loopback(self) -> None:
        """A spine relays, so the leaf's inbound is the range, its outbound only its own /32."""
        fabric = _fabric()
        policy = expect_session(_underlay(), fabric.devices["leaf1"], fabric).families[IPV4]
        assert policy.inbound == frozenset({range_entry(POOL_V4)})
        assert policy.outbound == frozenset({host_entry("10.250.0.11/32")})
        assert not policy.default_in
        assert not policy.default_out

    def test_a_spine_announces_the_whole_pool_it_relays(self) -> None:
        """A spine passes the learned loopbacks on: its outbound is the pool, not its own address."""
        fabric = _fabric()
        policy = expect_session(_underlay("leaf1"), fabric.devices["spine1"], fabric).families[IPV4]
        assert policy.outbound == policy.inbound == frozenset({range_entry(POOL_V4)})

    def test_a_super_spine_relays_like_a_spine(self) -> None:
        """Every RR_ROLES tier relays, including the super-spine of a back-to-back-less fabric."""
        fabric = _fabric()
        device = Device("ss1", "super-spine", DeviceAddrs(frozenset({"10.250.0.2/32"})))
        policy = expect_session(_underlay("spine1"), device, fabric).families[IPV4]
        assert policy.outbound == frozenset({range_entry(POOL_V4)})

    def test_the_ipv6_vip_is_accepted_in_the_ipv6_family_only(self) -> None:
        """An IPv6 VIP with an IPv4 loopback pool never leaks into the IPv4 list, and the IPv6 list carries it."""
        fabric = _fabric()
        v4 = expect_session(_underlay(), fabric.devices["leaf1"], fabric).families[IPV4]
        v6 = expect_session(_underlay(afs=(IPV6,)), fabric.devices["leaf1"], fabric).families[IPV6]
        assert not any(e.family == IPV6 for e in v4.inbound or ())
        assert host_entry(VIP_DC10) in (v6.inbound or ())
        assert host_entry(VIP_FR) in (v6.inbound or ())

    def test_the_multisite_vip_on_two_loopbacks_is_one_entry(self) -> None:
        """The VIP of a site is on two devices' loopback100 (the FR2 edge pair): the union holds it once."""
        fabric = _fabric(vips=frozenset({VIP_FR, VIP_FR, VIP_DC10}))
        policy = expect_session(_underlay(afs=(IPV6,)), fabric.devices["leaf1"], fabric).families[IPV6]
        assert sorted(e.prefix for e in policy.inbound or () if e.le is None) == sorted([VIP_FR, VIP_DC10])

    def test_a_border_gateway_announces_its_loopback_and_its_vip(self) -> None:
        """The BGW's own set is its VTEP loopback plus the shared VIP (RM-CONN-2-BGP's match)."""
        fabric = _fabric()
        policy = expect_session(_underlay("spine1", (IPV6,)), fabric.devices["bl-dc101101"], fabric).families[IPV6]
        assert policy.outbound == frozenset({host_entry("fd00:2500:1::5/128"), host_entry(VIP_DC10)})

    def test_a_family_without_a_pool_is_not_derivable_rather_than_deny_all(self) -> None:
        """No IPv6 range: the family is None (rendered unprotected, reported), never an empty deny-all list."""
        fabric = _fabric(loopback_ranges=(POOL_V4,))
        policy = expect_session(_underlay(afs=(IPV6,)), fabric.devices["leaf1"], fabric).families[IPV6]
        assert policy.inbound is None
        assert policy.outbound is None
        assert policy.max_prefix is None

    def test_mlag_peers_with_one_asn_get_the_same_expectation(self) -> None:
        """The ASN is not an input: two MLAG peers sharing one ASN differ only by their own loopback."""
        fabric = _fabric()
        peer_a = {**_underlay(), "remote_as": {"asn": 65001}}
        peer_b = {**_underlay(), "remote_as": {"asn": 65001}, "name": "u2"}
        leaf_b = Device("leaf2", "leaf", DeviceAddrs(frozenset({"10.250.0.12/32"})))
        a = expect_session(peer_a, fabric.devices["leaf1"], fabric).families[IPV4]
        b = expect_session(peer_b, leaf_b, fabric).families[IPV4]
        assert a.inbound == b.inbound
        assert a.outbound != b.outbound


class TestSizing:
    """Max-prefix comes from the model, sized by generators/fabric_sizing.py."""

    @staticmethod
    def _dc(super_spines: int) -> dict[str, object]:
        pod = {
            "max_spines_per_pod": 4,
            "max_rows": 2,
            "max_network_racks_per_row": 1,
            "max_leafs_per_network_rack": 2,
            "max_compute_racks_per_row": 4,
            "max_tors_per_compute_rack": 2,
            "max_border_leafs_per_pod": 2,
        }
        return {
            "max_super_spines_per_fabric": super_spines,
            "max_hyper_spines_per_fabric": 0,
            "max_border_leafs_per_fabric": 2,
            "max_pods": 3,
            "pod_templates": [pod],
        }

    @pytest.mark.parametrize("super_spines", [0, 2])
    def test_the_pool_sized_by_the_design_is_the_underlay_limit(self, super_spines: int) -> None:
        """The limit is the capacity of the pool fabric_sizing derived (with its headroom), plus the VIPs of the family.

        super_spines=0 is the back-to-back pod fabric (``pod_links``).
        """
        dc = self._dc(super_spines)
        bits = fabric_sizing.dc_loopback_bits(dc)
        pool = f"10.250.0.0/{fabric_sizing.host_bits_to_prefix_length(bits, ipv6=False)}"
        fabric = _fabric(loopback_ranges=(pool,), vips=frozenset())
        policy = expect_session(_underlay(), fabric.devices["leaf1"], fabric).families[IPV4]
        assert policy.max_prefix == 2**bits

    def test_a_pool_above_the_vendor_ceiling_yields_no_limit(self) -> None:
        """An IPv6 /64 pool is not a limit: None, not a rendered number."""
        fabric = _fabric(loopback_ranges=("fd00:2500:1::/64",))
        policy = expect_session(_underlay(afs=(IPV6,)), fabric.devices["leaf1"], fabric).families[IPV6]
        assert policy.max_prefix is None
        assert 2**64 > BGP_MAX_PREFIX_CEILING

    def test_the_evpn_bound_counts_type_3_5_and_2(self) -> None:
        """Type 3 VTEPs x L2 VNIs, type 5 the prefixes, type 2 twice the usable IPv4 hosts."""
        prefixes = frozenset({exact_entry("10.20.0.0/24"), exact_entry("10.20.1.0/25")})
        assert evpn_route_bound(10, 3, prefixes) == 10 * 3 + 2 + 2 * (254 + 126)

    def test_ipv6_host_space_is_not_counted_in_the_evpn_bound(self) -> None:
        """A /64 segment adds one type-5 route and no host bound."""
        assert evpn_route_bound(4, 2, frozenset({exact_entry("fd00:20::/64")})) == 4 * 2 + 1


class TestPairs:
    """Fabric iBGP and DCI underlay: the far border gateway's own addresses."""

    def test_dci_underlay_accepts_the_far_loopback_and_vip_and_announces_its_own(self) -> None:
        """E1 on the two ends: what one BGW announces is what the other accepts."""
        fabric = _fabric()
        session = {
            "name": "d1",
            "ttl": 1,
            "peering_role": "dci-underlay",
            "remote_device": "eg-fr201",
            "address_families": [IPV6],
        }
        near = expect_session(session, fabric.devices["bl-dc101101"], fabric).families[IPV6]
        assert near.inbound == frozenset({host_entry("fd00:2500:2::5/128"), host_entry(VIP_FR)})
        assert near.outbound == frozenset({host_entry("fd00:2500:1::5/128"), host_entry(VIP_DC10)})
        assert near.max_prefix == 2 + BGP_PREFIX_SLACK
        far_session = {**session, "remote_device": "bl-dc101101"}
        far = expect_session(far_session, fabric.devices["eg-fr201"], fabric).families[IPV6]
        assert near.outbound is not None
        assert far.inbound is not None
        assert covers(far.inbound, near.outbound)

    def test_an_unknown_far_end_is_not_derivable(self) -> None:
        """A far end the facts do not hold gives None, not an empty list."""
        fabric = _fabric()
        session = {
            "name": "d",
            "ttl": 1,
            "peering_role": "fabric",
            "remote_device": "ghost",
            "address_families": [IPV6],
        }
        assert expect_session(session, fabric.devices["bl-dc101101"], fabric).families[IPV6].inbound is None


class TestOverlay:
    """EVPN: type-5 prefixes of the member namespaces, a default only from an exit, never the transit /29."""

    @staticmethod
    def _overlay(remote: str) -> dict[str, object]:
        return {
            "name": "o1",
            "ttl": 255,
            "peering_role": "overlay",
            "remote_device": remote,
            "address_families": [EVPN],
        }

    def test_a_leaf_accepts_the_prefixes_of_its_namespaces_without_the_transit_29(self) -> None:
        """The firewall/LB transit /29 is a connected prefix of the border and never a type-5 route."""
        fabric = _fabric()
        policy = expect_session(self._overlay("spine1"), fabric.devices["leaf1"], fabric).families[EVPN]
        assert policy.inbound == frozenset({exact_entry("10.20.0.0/24"), exact_entry("10.20.1.0/24")})

    def test_the_internet_default_comes_from_a_border_through_a_relay_only(self) -> None:
        """INTERNET 0/0: accepted from the exit and from a spine relaying it, not from a plain leaf."""
        fabric = _fabric()
        leaf = fabric.devices["leaf1"]
        border = Device("bl-dc101101", "border-leaf", namespaces=frozenset({"PROD", "INTERNET"}))
        from_spine = expect_session(self._overlay("spine1"), Device("x", "leaf", namespaces=border.namespaces), fabric)
        from_leaf = expect_session(self._overlay("leaf1"), Device("x", "leaf", namespaces=border.namespaces), fabric)
        assert from_spine.families[EVPN].default_in
        assert not from_leaf.families[EVPN].default_in
        assert not expect_session(self._overlay("spine1"), leaf, fabric).families[EVPN].default_in
        out = expect_session(self._overlay("spine1"), fabric.devices["bl-dc101101"], fabric)
        assert out.families[EVPN].default_out

    def test_the_limit_is_the_loose_upper_bound(self) -> None:
        """Type 3 + 5 + 2 over the member namespaces and the L2 VNIs."""
        fabric = _fabric()
        policy = expect_session(self._overlay("spine1"), fabric.devices["leaf1"], fabric).families[EVPN]
        prefixes = policy.inbound or frozenset()
        vteps = 256 + 256
        assert policy.max_prefix == evpn_route_bound(vteps, 2, prefixes)


class TestVrfAndExternal:
    """Internal VRF sessions are namespace-scoped; external and transit keep their policy and only get a limit."""

    def test_an_internal_vrf_session_carries_only_its_namespace(self) -> None:
        """PROD prefixes in a PROD session, none of INTERNET, and the transit /29 stays out."""
        fabric = _fabric()
        session = {"name": "v", "vrf": "PROD", "remote_device": "leaf1", "address_families": [IPV4]}
        policy = expect_session(session, fabric.devices["bl-dc101101"], fabric).families[IPV4]
        assert policy.inbound == frozenset({exact_entry("10.20.0.0/24"), exact_entry("10.20.1.0/24")})
        assert policy.max_prefix == 2 + BGP_PREFIX_SLACK

    def test_an_external_session_keeps_the_exchange_policy_and_gets_a_derived_limit(self) -> None:
        """A hosted CloudNetworkAppliance session is external already: in/out come from the ownership rule."""
        fabric = _fabric(
            external={
                "Ethernet1/1.100": ExternalPolicy(frozenset({"172.16.0.0/16"}), frozenset({"10.20.0.0/24"})),
            }
        )
        session = {
            "name": "x",
            "vrf": "PROD",
            "external": True,
            "local_interface": "Ethernet1/1.100",
            "remote_device": "aws-ne",
            "address_families": [IPV4],
        }
        policy = expect_session(session, fabric.devices["bl-dc101101"], fabric).families[IPV4]
        assert policy.inbound == frozenset({exact_entry("172.16.0.0/16")})
        assert policy.outbound == frozenset({exact_entry("10.20.0.0/24")})
        assert policy.max_prefix == 1 + BGP_PREFIX_SLACK

    def test_an_external_session_without_owner_accepts_nothing(self) -> None:
        """No registered owner: empty both ways (deny all), the limit is the slack."""
        fabric = _fabric()
        session = {"name": "x", "vrf": "PROD", "external": True, "local_interface": "e", "address_families": [IPV4]}
        policy = expect_session(session, fabric.devices["bl-dc101101"], fabric).families[IPV4]
        assert policy.inbound == frozenset()
        assert policy.max_prefix == BGP_PREFIX_SLACK
