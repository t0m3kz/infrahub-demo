"""Unit tests for BGP peer group building and BGP profile lookup.

Tests verify peer group assignment, route reflector client detection
and BGP profile selection from session data.
"""

import pytest

from tests.unit.bgp_transforms_helpers import _make_peering
from transforms.helpers.bgp import (
    _build_peer_groups,
    get_bgp_profile,
)
from utils.fabric_roles import RR_ROLES

# ============================================================================
# _build_peer_groups
# ============================================================================


def _session(
    session_type="EBGP",
    ttl=1,
    remote_as=None,
    rr_client=False,
    name="s",
    address_families=None,
    send_community=True,
    send_extended_community=False,
    remove_private_as=False,
    maximum_routes=None,
    local_pref=None,
    med=None,
    password=None,
):
    """Build a minimal session dict for peer group testing."""
    s = {
        "name": name,
        "session_type": session_type,
        "ttl": ttl,
        "bfd_enabled": True,
        "send_community": send_community,
        "send_extended_community": send_extended_community,
        "remove_private_as": remove_private_as,
        "maximum_routes": maximum_routes,
        "local_pref": local_pref,
        "med": med,
        "password": password,
        "route_reflector_client": rr_client,
    }
    if remote_as:
        s["remote_as"] = remote_as
    if address_families is not None:
        s["address_families"] = address_families
    return s


class TestBuildPeerGroups:
    """Test _build_peer_groups() peer group assignment and RR client detection."""

    def test_underlay_peer_group_created(self):
        sessions = [
            _session(session_type="EBGP", ttl=1, name="u1"),
            _session(session_type="EBGP", ttl=1, name="u2"),
        ]
        pgs = _build_peer_groups(sessions)
        assert len(pgs) == 1
        assert pgs[0]["name"] == "UNDERLAY-PEERS"
        assert sessions[0]["peer_group"] == "UNDERLAY-PEERS"
        assert sessions[1]["peer_group"] == "UNDERLAY-PEERS"

    def test_single_underlay_gets_peer_group(self):
        sessions = [_session(session_type="EBGP", ttl=1)]
        pgs = _build_peer_groups(sessions)
        assert len(pgs) == 1
        assert pgs[0]["name"] == "UNDERLAY-PEERS"
        assert sessions[0]["peer_group"] == "UNDERLAY-PEERS"

    def test_underlay_peer_group_address_families_defaults_to_ipv4(self):
        """No explicit AFs on sessions (legacy shape) — peer group falls back to ipv4."""
        sessions = [_session(session_type="EBGP", ttl=1, name="u1")]
        pgs = _build_peer_groups(sessions)
        assert pgs[0]["address_families"] == ["ipv4"]

    def test_underlay_peer_group_address_families_ipv6(self):
        """Underlay sessions resolved to ipv6 — peer group reflects that, not a hardcoded ipv4."""
        sessions = [
            _session(session_type="EBGP", ttl=1, name="u1", address_families=["ipv6"]),
            _session(session_type="EBGP", ttl=1, name="u2", address_families=["ipv6"]),
        ]
        pgs = _build_peer_groups(sessions)
        assert pgs[0]["address_families"] == ["ipv6"]

    def test_underlay_peer_group_address_families_mixed_ipv4_ipv6(self):
        """Mixed-family underlay sessions — peer group activates both."""
        sessions = [
            _session(session_type="EBGP", ttl=1, name="u1", address_families=["ipv4"]),
            _session(session_type="EBGP", ttl=1, name="u2", address_families=["ipv6"]),
        ]
        pgs = _build_peer_groups(sessions)
        assert pgs[0]["address_families"] == ["ipv4", "ipv6"]

    def test_ibgp_overlay_peer_group_created(self):
        sessions = [
            _session(session_type="IBGP", ttl=255, remote_as={"asn": 65000}, name="o1"),
            _session(session_type="IBGP", ttl=255, remote_as={"asn": 65000}, name="o2"),
        ]
        pgs = _build_peer_groups(sessions)
        assert len(pgs) == 1
        assert pgs[0]["name"] == "EVPN-PEERS"
        assert pgs[0]["remote_as"] == 65000

    def test_ebgp_overlay_peer_group_created(self):
        sessions = [
            _session(session_type="EBGP", ttl=255, name="eo1"),
            _session(session_type="EBGP", ttl=255, name="eo2"),
        ]
        pgs = _build_peer_groups(sessions)
        assert len(pgs) == 1
        assert pgs[0]["name"] == "EVPN-OVERLAY"

    @pytest.mark.parametrize("role", ["spine", "super-spine", "hyper-spine", "border-spine"])
    def test_ebgp_overlay_relay_sets_next_hop_unchanged(self, role: str) -> None:
        """A relaying tier must not rewrite the EVPN next-hop.

        Under ebgp-ebgp the spine re-advertises EVPN routes, and normal eBGP
        behaviour is to replace the next-hop with its own address. A spine has
        no `interface nve1`, so remote VTEPs would build a VXLAN tunnel to a
        device that cannot terminate it — sessions up, routes present, traffic
        silently blackholed (RFC 8365 §5.1.2.1).
        """
        sessions = [
            _session(session_type="EBGP", ttl=255, name="eo1"),
            _session(session_type="EBGP", ttl=255, name="eo2"),
        ]
        pgs = _build_peer_groups(sessions, device_role=role)
        assert pgs[0]["next_hop_unchanged"] is True

    @pytest.mark.parametrize("role", ["spine", "super-spine", "hyper-spine", "border-spine"])
    def test_ebgp_overlay_relay_retains_all_route_targets(self, role: str) -> None:
        """A spine holds no VRF and no L2VNI, so it has no import route-target
        and would otherwise discard the EVPN NLRI it exists to relay."""
        sessions = [_session(session_type="EBGP", ttl=255, name="eo1")]
        pgs = _build_peer_groups(sessions, device_role=role)
        assert pgs[0]["retain_route_target_all"] is True

    @pytest.mark.parametrize("role", ["leaf", "border-leaf", "tor", "access-leaf"])
    def test_ebgp_overlay_vtep_does_not_set_relay_knobs(self, role: str) -> None:
        """A VTEP originates its own routes with itself as the next-hop, so
        preserving an inherited next-hop there would be wrong."""
        sessions = [_session(session_type="EBGP", ttl=255, name="eo1")]
        pgs = _build_peer_groups(sessions, device_role=role)
        assert pgs[0]["next_hop_unchanged"] is False
        assert pgs[0]["retain_route_target_all"] is False

    def test_ebgp_overlay_relay_knobs_default_off_without_a_role(self) -> None:
        """No role given (the _build_peer_groups default) must not silently
        enable relay behaviour on a device that may be a VTEP."""
        sessions = [_session(session_type="EBGP", ttl=255, name="eo1")]
        pgs = _build_peer_groups(sessions)
        assert pgs[0]["next_hop_unchanged"] is False
        assert pgs[0]["retain_route_target_all"] is False

    def test_rr_client_on_spine(self):
        """Spine with RR-flagged iBGP sessions gets route_reflector_client on peer group."""
        sessions = [
            _session(session_type="IBGP", ttl=255, remote_as={"asn": 65000}, rr_client=True, name="o1"),
            _session(session_type="IBGP", ttl=255, remote_as={"asn": 65000}, rr_client=True, name="o2"),
        ]
        pgs = _build_peer_groups(sessions, device_role="spine")
        assert len(pgs) == 1
        assert pgs[0]["route_reflector_client"] is True

    @pytest.mark.parametrize("role", sorted(RR_ROLES))
    def test_every_rr_role_with_rr_flagged_sessions_is_a_route_reflector(self, role: str) -> None:
        """One definition (utils/fabric_roles.RR_ROLES) decides which roles reflect, hyper-spine included."""
        sessions = [
            _session(session_type="IBGP", ttl=255, remote_as={"asn": 65000}, rr_client=True, name="o1"),
            _session(session_type="IBGP", ttl=255, remote_as={"asn": 65000}, rr_client=True, name="o2"),
        ]
        pgs = _build_peer_groups(sessions, device_role=role)
        assert pgs[0]["route_reflector_client"] is True
        assert pgs[0]["next_hop_unchanged"] is True

    def test_hyper_spine_profile_gets_rr_peer_group_and_cluster_id(self) -> None:
        """get_bgp_profile for a hyper-spine yields the same RR peer group a super-spine gets."""
        peerings = [
            _make_peering(
                name=f"overlay-{i}",
                session_type="IBGP",
                ttl=255,
                local_device="hs-01",
                remote_device=f"leaf-0{i}",
                local_ip="10.0.0.200/32",
                remote_ip=f"10.0.0.{i}/32",
                local_iface_type="DcimVirtualInterface",
                remote_iface_type="DcimVirtualInterface",
                route_reflector_client=True,
            )
            for i in (1, 2)
        ]
        service = TestGetBgpProfile()._make_service(peerings, local_asn=65000, router_id="10.0.0.200/32")
        bgp = get_bgp_profile([service], device_name="hs-01", device_role="hyper-spine")[0]
        assert bgp["peer_groups"][0]["route_reflector_client"] is True
        assert bgp["cluster_id"] == "10.0.0.200"

    def test_rr_client_not_on_non_rr_role(self):
        sessions = [_session(session_type="IBGP", ttl=255, remote_as={"asn": 65000}, rr_client=True, name="o1")]
        assert _build_peer_groups(sessions, device_role="l2-leaf")[0]["route_reflector_client"] is False

    def test_rr_client_on_super_spine(self):
        sessions = [
            _session(session_type="IBGP", ttl=255, remote_as={"asn": 65000}, rr_client=True, name="o1"),
            _session(session_type="IBGP", ttl=255, remote_as={"asn": 65000}, rr_client=True, name="o2"),
        ]
        pgs = _build_peer_groups(sessions, device_role="super-spine")
        assert pgs[0]["route_reflector_client"] is True

    def test_rr_client_not_on_leaf(self):
        """Leaf sees same RR-flagged peerings but should NOT set route_reflector_client."""
        sessions = [
            _session(session_type="IBGP", ttl=255, remote_as={"asn": 65000}, rr_client=True, name="o1"),
            _session(session_type="IBGP", ttl=255, remote_as={"asn": 65000}, rr_client=True, name="o2"),
        ]
        pgs = _build_peer_groups(sessions, device_role="leaf")
        assert len(pgs) == 1
        assert pgs[0]["route_reflector_client"] is False

    def test_rr_client_not_on_tor(self):
        sessions = [
            _session(session_type="IBGP", ttl=255, remote_as={"asn": 65000}, rr_client=True, name="o1"),
            _session(session_type="IBGP", ttl=255, remote_as={"asn": 65000}, rr_client=True, name="o2"),
        ]
        pgs = _build_peer_groups(sessions, device_role="tor")
        assert pgs[0]["route_reflector_client"] is False

    def test_no_rr_flag_no_rr_client_even_on_spine(self):
        """Spine without RR-flagged peerings should not have route_reflector_client."""
        sessions = [
            _session(session_type="IBGP", ttl=255, remote_as={"asn": 65000}, rr_client=False, name="o1"),
            _session(session_type="IBGP", ttl=255, remote_as={"asn": 65000}, rr_client=False, name="o2"),
        ]
        pgs = _build_peer_groups(sessions, device_role="spine")
        assert pgs[0]["route_reflector_client"] is False

    def test_mixed_sessions_multiple_peer_groups(self):
        """Mix of underlay eBGP + overlay iBGP creates separate peer groups."""
        sessions = [
            _session(session_type="EBGP", ttl=1, name="u1"),
            _session(session_type="EBGP", ttl=1, name="u2"),
            _session(session_type="IBGP", ttl=255, remote_as={"asn": 65000}, rr_client=True, name="o1"),
            _session(session_type="IBGP", ttl=255, remote_as={"asn": 65000}, rr_client=True, name="o2"),
        ]
        pgs = _build_peer_groups(sessions, device_role="spine")
        names = {pg["name"] for pg in pgs}
        assert names == {"UNDERLAY-PEERS", "EVPN-PEERS"}

    def test_underlay_peer_group_aggregates_send_extended_community_and_remove_private_as(self):
        """If ANY grouped session wants send_extended_community/remove_private_as, the group gets it."""
        sessions = [
            _session(session_type="EBGP", ttl=1, name="u1", send_extended_community=False, remove_private_as=False),
            _session(session_type="EBGP", ttl=1, name="u2", send_extended_community=True, remove_private_as=True),
        ]
        pgs = _build_peer_groups(sessions)
        assert pgs[0]["send_extended_community"] is True
        assert pgs[0]["remove_private_as"] is True

    def test_underlay_peer_group_no_extended_community_when_no_session_wants_it(self):
        sessions = [
            _session(session_type="EBGP", ttl=1, name="u1", send_extended_community=False, remove_private_as=False),
        ]
        pgs = _build_peer_groups(sessions)
        assert pgs[0]["send_extended_community"] is False
        assert pgs[0]["remove_private_as"] is False

    def test_per_neighbor_fields_not_present_on_peer_group_dict(self):
        """maximum_routes/local_pref/med/password are per-neighbor only, never aggregated onto the group."""
        sessions = [
            _session(session_type="EBGP", ttl=1, name="u1", maximum_routes=100, local_pref=200, med=50, password="x"),
        ]
        pgs = _build_peer_groups(sessions)
        assert "maximum_routes" not in pgs[0]
        assert "local_pref" not in pgs[0]
        assert "med" not in pgs[0]
        assert "password" not in pgs[0]

    def test_ibgp_remote_as_from_peer_group(self):
        """iBGP sessions in a peer group get remote_as_from_peer_group flag."""
        sessions = [
            _session(session_type="IBGP", ttl=255, remote_as={"asn": 65000}, name="o1"),
            _session(session_type="IBGP", ttl=255, remote_as={"asn": 65000}, name="o2"),
        ]
        _build_peer_groups(sessions)
        assert sessions[0].get("remote_as_from_peer_group") is True
        assert sessions[1].get("remote_as_from_peer_group") is True


# ============================================================================
# get_bgp_profile (integration)
# ============================================================================


class TestGetBgpProfile:
    """Integration tests for get_bgp_profile()."""

    def _make_service(self, peerings, local_asn=65001, router_id="10.0.0.1/32"):
        return {
            "typename": "ManagedBGP",
            "name": "bgp-fabric",
            "status": "active",
            "multipath": True,
            "graceful_restart": True,
            "confederation_identifier": None,
            "local_as": {"asn": local_asn},
            "router_id": {"address": router_id},
            "peerings": peerings,
        }

    def test_empty_services(self):
        result = get_bgp_profile([], device_name="leaf-01")
        assert result == []

    def test_ibgp_overlay_sessions(self):
        peerings = [
            _make_peering(
                name="overlay-1",
                session_type="IBGP",
                ttl=255,
                local_device="leaf-01",
                remote_device="spine-01",
                local_ip="10.0.0.1/32",
                remote_ip="10.0.0.100/32",
                local_iface_type="DcimVirtualInterface",
                remote_iface_type="DcimVirtualInterface",
                route_reflector_client=True,
            ),
            _make_peering(
                name="overlay-2",
                session_type="IBGP",
                ttl=255,
                local_device="leaf-01",
                remote_device="spine-02",
                local_ip="10.0.0.1/32",
                remote_ip="10.0.0.101/32",
                local_iface_type="DcimVirtualInterface",
                remote_iface_type="DcimVirtualInterface",
                route_reflector_client=True,
            ),
        ]
        service = self._make_service(peerings, local_asn=65000)
        result = get_bgp_profile([service], device_name="leaf-01", device_role="leaf")

        assert len(result) == 1
        bgp = result[0]
        assert bgp["local_as"] == {"asn": 65000}
        assert len(bgp["sessions"]) == 2
        assert len(bgp["peer_groups"]) == 1
        assert bgp["peer_groups"][0]["name"] == "EVPN-PEERS"
        # Leaf should NOT have route_reflector_client
        assert bgp["peer_groups"][0]["route_reflector_client"] is False

    def test_spine_gets_rr_client_on_peer_group(self):
        peerings = [
            _make_peering(
                name="overlay-1",
                session_type="IBGP",
                ttl=255,
                local_device="spine-01",
                remote_device="leaf-01",
                local_ip="10.0.0.100/32",
                remote_ip="10.0.0.1/32",
                local_iface_type="DcimVirtualInterface",
                remote_iface_type="DcimVirtualInterface",
                route_reflector_client=True,
            ),
            _make_peering(
                name="overlay-2",
                session_type="IBGP",
                ttl=255,
                local_device="spine-01",
                remote_device="leaf-02",
                local_ip="10.0.0.100/32",
                remote_ip="10.0.0.2/32",
                local_iface_type="DcimVirtualInterface",
                remote_iface_type="DcimVirtualInterface",
                route_reflector_client=True,
            ),
        ]
        service = self._make_service(peerings, local_asn=65000, router_id="10.0.0.100/32")
        result = get_bgp_profile([service], device_name="spine-01", device_role="spine")

        bgp = result[0]
        assert bgp["peer_groups"][0]["route_reflector_client"] is True

    def test_ebgp_sessions_skipped_without_bgp_processes(self):
        """eBGP peerings without bgp_processes can't resolve remote ASN."""
        peerings = [
            _make_peering(
                name="underlay-1",
                session_type="EBGP",
                ttl=1,
                local_device="leaf-01",
                remote_device="spine-01",
                # No local_asn/remote_asn — no bgp_processes
            ),
        ]
        service = self._make_service(peerings)
        result = get_bgp_profile([service], device_name="leaf-01")

        assert len(result) == 1
        assert len(result[0]["sessions"]) == 0

    def test_ebgp_sessions_included_with_bgp_processes(self):
        peerings = [
            _make_peering(
                name="underlay-1",
                session_type="EBGP",
                ttl=1,
                local_device="leaf-01",
                remote_device="spine-01",
                local_asn=65001,
                remote_asn=65000,
            ),
            _make_peering(
                name="underlay-2",
                session_type="EBGP",
                ttl=1,
                local_device="leaf-01",
                remote_device="spine-02",
                remote_ip="10.1.0.2/31",
                local_asn=65001,
                remote_asn=65000,
            ),
        ]
        service = self._make_service(peerings)
        result = get_bgp_profile([service], device_name="leaf-01")

        assert len(result) == 1
        assert len(result[0]["sessions"]) == 2
        assert result[0]["sessions"][0]["remote_as"] == {"asn": 65000}

    def test_merged_bgp_processes_same_asn(self):
        """Two BGP processes with the same ASN are merged."""
        peering_underlay = _make_peering(
            name="underlay",
            session_type="EBGP",
            ttl=1,
            local_device="leaf-01",
            remote_device="spine-01",
            local_asn=65001,
            remote_asn=65000,
        )
        peering_overlay = _make_peering(
            name="overlay",
            session_type="IBGP",
            ttl=255,
            local_device="leaf-01",
            remote_device="spine-01",
            local_ip="10.0.0.1/32",
            remote_ip="10.0.0.100/32",
            local_iface_type="DcimVirtualInterface",
            remote_iface_type="DcimVirtualInterface",
        )
        services = [
            self._make_service([peering_underlay], local_asn=65001),
            self._make_service([peering_overlay], local_asn=65001),
        ]
        result = get_bgp_profile(services, device_name="leaf-01")
        # Merged into single BGP config
        assert len(result) == 1
        assert len(result[0]["sessions"]) == 2

    def test_mixed_capabilities_processes_only_managed_bgp(self):
        peerings = [
            _make_peering(
                name="underlay-1",
                session_type="EBGP",
                ttl=1,
                local_device="leaf-01",
                remote_device="spine-01",
                local_asn=65001,
                remote_asn=65000,
            ),
        ]
        services = [
            {"typename": "ManagedOSPF", "name": "ospf-1"},
            self._make_service(peerings, local_asn=65001),
        ]
        result = get_bgp_profile(services, device_name="leaf-01")
        assert len(result) == 1
        assert result[0]["name"] == "bgp-fabric"

    def test_sessions_include_local_as_for_templates(self):
        """Each built session should expose local_as for peering templates."""
        peerings = [
            _make_peering(
                name="overlay-1",
                session_type="IBGP",
                ttl=255,
                local_device="leaf-01",
                remote_device="spine-01",
                local_ip="10.0.0.1/32",
                remote_ip="10.0.0.100/32",
                local_iface_type="DcimVirtualInterface",
                remote_iface_type="DcimVirtualInterface",
            ),
        ]
        service = self._make_service(peerings, local_asn=65000)
        result = get_bgp_profile([service], device_name="leaf-01", device_role="leaf")

        assert len(result) == 1
        assert len(result[0]["sessions"]) == 1
        assert result[0]["sessions"][0]["local_as"] == {"asn": 65000}
