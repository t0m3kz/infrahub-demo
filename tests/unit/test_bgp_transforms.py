"""Unit tests for BGP transform functions.

Tests verify correct BGP session building, peer group assignment,
and route reflector client detection from peering_interfaces data.
"""

import pytest

from tests.unit.bgp_transforms_helpers import _make_peering
from transforms.helpers.bgp import (
    _build_session_from_peering,
    _extract_remote_asn_from_peering,
)

# ============================================================================
# _build_session_from_peering
# ============================================================================


class TestBuildSessionFromPeering:
    """Test _build_session_from_peering() local/remote detection and field mapping."""

    def test_basic_ebgp_underlay(self):
        peering = _make_peering(
            session_type="EBGP",
            ttl=1,
            local_device="leaf-01",
            remote_device="spine-01",
            local_asn=65001,
            remote_asn=65000,
        )
        session = _build_session_from_peering(
            peering,
            device_name="leaf-01",
            local_as={"asn": 65001},
            interfaces=None,
        )
        assert session is not None
        assert session["session_type"] == "EBGP"
        assert session["remote_as"] == {"asn": 65000}
        assert session["remote_device"] == "spine-01"

    def test_basic_ibgp_overlay(self):
        peering = _make_peering(
            session_type="IBGP",
            ttl=255,
            local_device="leaf-01",
            remote_device="spine-01",
            local_ip="10.0.0.1/32",
            remote_ip="10.0.0.100/32",
            local_iface_type="DcimVirtualInterface",
            remote_iface_type="DcimVirtualInterface",
            route_reflector_client=True,
        )
        local_as = {"asn": 65000}
        session = _build_session_from_peering(
            peering,
            device_name="leaf-01",
            local_as=local_as,
            interfaces=None,
        )
        assert session is not None
        assert session["session_type"] == "IBGP"
        assert session["remote_as"] == local_as
        assert session["route_reflector_client"] is True

    def test_local_remote_detection_by_device_name(self):
        peering = _make_peering(
            session_type="IBGP",
            ttl=255,
            local_device="spine-01",
            remote_device="leaf-01",
            local_ip="10.0.0.100/32",
            remote_ip="10.0.0.1/32",
            local_iface_type="DcimVirtualInterface",
            remote_iface_type="DcimVirtualInterface",
        )
        session = _build_session_from_peering(
            peering,
            device_name="spine-01",
            local_as={"asn": 65000},
            interfaces=None,
        )
        assert session is not None
        assert session["remote_device"] == "leaf-01"
        assert session["remote_ip"] == {"address": "10.0.0.1/32"}

    def test_ebgp_without_remote_as_returns_none(self):
        """eBGP session without resolved remote_as should be skipped."""
        peering = _make_peering(
            session_type="EBGP",
            ttl=1,
            local_device="leaf-01",
            remote_device="spine-01",
            # No bgp_processes — remote ASN can't be resolved
        )
        session = _build_session_from_peering(
            peering,
            device_name="leaf-01",
            local_as={"asn": 65001},
            interfaces=None,
        )
        assert session is None

    def test_ibgp_without_bgp_processes_uses_local_as(self):
        """iBGP session should use local_as as remote_as regardless of bgp_processes."""
        peering = _make_peering(
            session_type="IBGP",
            ttl=255,
            local_device="leaf-01",
            remote_device="spine-01",
            local_iface_type="DcimVirtualInterface",
            remote_iface_type="DcimVirtualInterface",
        )
        local_as = {"asn": 65000}
        session = _build_session_from_peering(
            peering,
            device_name="leaf-01",
            local_as=local_as,
            interfaces=None,
        )
        assert session is not None
        assert session["remote_as"] == local_as

    def test_invalid_peering_interfaces_raises_value_error(self):
        """Malformed peerings must fail loudly instead of being silently dropped."""
        peering = {
            "name": "bad",
            "session_type": "EBGP",
            "ttl": 1,
            "capabilities": [{"device": {"name": "leaf-01"}}],  # only 1
        }
        with pytest.raises(ValueError, match="expected 2 capabilities"):
            _build_session_from_peering(
                peering,
                device_name="leaf-01",
                local_as={"asn": 65001},
                interfaces=None,
            )

    def test_invalid_local_remote_mapping_raises_value_error(self):
        """Two interfaces that don't map local+remote for this device are malformed."""
        peering = {
            "name": "bad-mapping",
            "session_type": "IBGP",
            "ttl": 255,
            "capabilities": [
                {"device": {"name": "spine-01"}, "ip_address": {"address": "10.0.0.1/32"}},
                {"device": {"name": "spine-02"}, "ip_address": {"address": "10.0.0.2/32"}},
            ],
        }
        with pytest.raises(ValueError, match="missing local/remote interface mapping"):
            _build_session_from_peering(
                peering,
                device_name="leaf-01",
                local_as={"asn": 65000},
                interfaces=None,
            )

    def test_overlay_address_families_evpn(self):
        peering = _make_peering(
            session_type="IBGP",
            ttl=255,
            local_device="leaf-01",
            remote_device="spine-01",
            local_iface_type="DcimVirtualInterface",
            remote_iface_type="DcimVirtualInterface",
        )
        session = _build_session_from_peering(
            peering,
            device_name="leaf-01",
            local_as={"asn": 65000},
            interfaces=None,
        )
        assert session is not None
        assert session["address_families"] == ["evpn"]

    def test_underlay_address_families_ipv4(self):
        """Underlay session with an IPv4 neighbor resolves to ['ipv4'], never empty."""
        peering = _make_peering(
            session_type="EBGP",
            ttl=1,
            local_device="leaf-01",
            remote_device="spine-01",
            local_asn=65001,
            remote_asn=65000,
        )
        session = _build_session_from_peering(
            peering,
            device_name="leaf-01",
            local_as={"asn": 65001},
            interfaces=None,
        )
        assert session is not None
        assert session["address_families"] == ["ipv4"]

    def test_underlay_address_families_ipv6(self):
        """Underlay session with an IPv6 neighbor resolves to ['ipv6']."""
        peering = _make_peering(
            session_type="EBGP",
            ttl=1,
            local_device="leaf-01",
            remote_device="spine-01",
            local_ip="fd00:2100::1/127",
            remote_ip="fd00:2100::0/127",
            local_asn=65001,
            remote_asn=65000,
        )
        session = _build_session_from_peering(
            peering,
            device_name="leaf-01",
            local_as={"asn": 65001},
            interfaces=None,
        )
        assert session is not None
        assert session["address_families"] == ["ipv6"]

    def test_explicit_schema_address_families_can_combine_ipv4_and_evpn(self):
        """Explicit schema config (e.g. unnumbered iBGP carrying both AFs) is passed through as-is."""
        peering = _make_peering(
            session_type="IBGP",
            ttl=1,
            local_device="leaf-01",
            remote_device="spine-01",
        )
        peering["address_families"] = [
            {"afi": "ipv4", "safi": "unicast"},
            {"afi": "l2vpn", "safi": "evpn"},
        ]
        session = _build_session_from_peering(
            peering,
            device_name="leaf-01",
            local_as={"asn": 65000},
            interfaces=None,
        )
        assert session is not None
        assert session["address_families"] == ["ipv4", "evpn"]

    def test_security_fields_pass_through(self):
        """maximum_routes/local_pref/med/send_extended_community/remove_private_as/password
        are threaded from the peering node into the session dict verbatim."""
        peering = _make_peering(
            session_type="EBGP",
            ttl=1,
            local_device="leaf-01",
            remote_device="spine-01",
            local_asn=65001,
            remote_asn=65000,
            maximum_routes=100,
            local_pref=200,
            med=50,
            send_extended_community=True,
            remove_private_as=True,
            password="s3cr3t",
        )
        session = _build_session_from_peering(
            peering,
            device_name="leaf-01",
            local_as={"asn": 65001},
            interfaces=None,
        )
        assert session is not None
        assert session["maximum_routes"] == 100
        assert session["local_pref"] == 200
        assert session["med"] == 50
        assert session["send_extended_community"] is True
        assert session["remove_private_as"] is True
        assert session["password"] == "s3cr3t"

    def test_security_fields_default_none(self):
        peering = _make_peering(
            session_type="EBGP",
            ttl=1,
            local_device="leaf-01",
            remote_device="spine-01",
            local_asn=65001,
            remote_asn=65000,
        )
        session = _build_session_from_peering(
            peering,
            device_name="leaf-01",
            local_as={"asn": 65001},
            interfaces=None,
        )
        assert session is not None
        assert session["maximum_routes"] is None
        assert session["local_pref"] is None
        assert session["med"] is None
        assert session["send_extended_community"] is False
        assert session["remove_private_as"] is False
        assert session["password"] is None

    def test_route_reflector_client_default_false(self):
        peering = _make_peering(
            session_type="IBGP",
            ttl=255,
            local_device="leaf-01",
            remote_device="spine-01",
            local_iface_type="DcimVirtualInterface",
            remote_iface_type="DcimVirtualInterface",
        )
        # No route_reflector_client in peering data
        del peering["route_reflector_client"]
        session = _build_session_from_peering(
            peering,
            device_name="leaf-01",
            local_as={"asn": 65000},
            interfaces=None,
        )
        assert session is not None
        assert session["route_reflector_client"] is False


# ============================================================================
# _extract_remote_asn_from_peering
# ============================================================================


class TestExtractRemoteAsnFromPeering:
    """bgp_processes entries carry the owning device via `capabilities`
    (ManagedBGP -> DcimCapabilities), not a `device` field. These tests pin
    that shape down directly, independent of the higher-level session-building
    tests, since a regression here silently drops every eBGP session."""

    def test_matches_remote_device(self):
        peering = {
            "bgp_processes": [
                {"capabilities": [{"name": "leaf-01"}], "local_as": {"asn": 65001}},
                {"capabilities": [{"name": "spine-01"}], "local_as": {"asn": 65000}},
            ]
        }
        assert _extract_remote_asn_from_peering(peering, "spine-01") == 65000

    def test_no_matching_device_returns_none(self):
        peering = {
            "bgp_processes": [
                {"capabilities": [{"name": "leaf-01"}], "local_as": {"asn": 65001}},
                {"capabilities": [{"name": "spine-01"}], "local_as": {"asn": 65000}},
            ]
        }
        assert _extract_remote_asn_from_peering(peering, "spine-99") is None

    def test_missing_bgp_processes_returns_none(self):
        assert _extract_remote_asn_from_peering({}, "spine-01") is None

    def test_bgp_processes_not_a_list_returns_none(self):
        assert _extract_remote_asn_from_peering({"bgp_processes": None}, "spine-01") is None

    def test_missing_capabilities_on_process_returns_none(self):
        """A bgp_process with no capabilities (device unresolved) can't match."""
        peering = {"bgp_processes": [{"local_as": {"asn": 65000}}]}
        assert _extract_remote_asn_from_peering(peering, "spine-01") is None

    def test_empty_capabilities_list_returns_none(self):
        peering = {"bgp_processes": [{"capabilities": [], "local_as": {"asn": 65000}}]}
        assert _extract_remote_asn_from_peering(peering, "spine-01") is None

    def test_missing_local_as_on_matched_process_returns_none(self):
        peering = {"bgp_processes": [{"capabilities": [{"name": "spine-01"}]}]}
        assert _extract_remote_asn_from_peering(peering, "spine-01") is None

    def test_stops_at_first_matching_process(self):
        """Two bgp_processes both claiming the same device — first match wins."""
        peering = {
            "bgp_processes": [
                {"capabilities": [{"name": "spine-01"}], "local_as": {"asn": 65000}},
                {"capabilities": [{"name": "spine-01"}], "local_as": {"asn": 99999}},
            ]
        }
        assert _extract_remote_asn_from_peering(peering, "spine-01") == 65000


# ============================================================================
# _build_session_from_peering — direct IP fallback (no cable)
# ============================================================================


class TestDirectIpFallback:
    """TTL=1 underlay sessions where the peering's own capabilities
    already carry both sides' IPs directly (e.g. a VTI/sub-interface with no
    DcimCable and no separate TopologyCircuit object to traverse — external
    BGP peering over a fabric/MPLS-VPN virtual circuit handoff)."""

    def _direct_ip_peering(
        self,
        local_device: str,
        local_iface: str,
        local_ip: str,
        remote_device: str,
        remote_iface: str,
        remote_ip: str,
        remote_asn: int = 6695,
    ) -> dict:
        return {
            "name": "ext-underlay--local-remote",
            "session_type": "EBGP",
            "bfd_enabled": False,
            "send_community": True,
            "ttl": 1,
            "route_reflector_client": False,
            "capabilities": [
                {"name": remote_iface, "ip_address": {"address": remote_ip}, "device": {"name": remote_device}},
                {"name": local_iface, "ip_address": {"address": local_ip}, "device": {"name": local_device}},
            ],
            "bgp_processes": [
                {"capabilities": [{"name": remote_device}], "local_as": {"asn": remote_asn}},
                {"capabilities": [{"name": local_device}], "local_as": {"asn": 65000}},
            ],
        }

    def test_direct_ip_resolves_without_cable_or_circuit(self):
        """No cable, no circuit on the local interface — IP comes straight from the peering."""
        bare_iface = {
            "name": "VTI-FR2-DE-CIX",
            "ip_address": {"address": "10.255.0.24/31"},
            "cable": None,
            "device": {"name": "FR2-EDGE-01"},
            "capabilities": [],
        }
        peering = self._direct_ip_peering(
            local_device="FR2-EDGE-01",
            local_iface="VTI-FR2-DE-CIX",
            local_ip="10.255.0.24/31",
            remote_device="EXT-INET-DE-CIX-FR-01",
            remote_iface="VTI-DE-CIX-FR2",
            remote_ip="10.255.0.25/31",
        )
        session = _build_session_from_peering(
            peering,
            device_name="FR2-EDGE-01",
            local_as={"asn": 65000},
            interfaces=[bare_iface],
        )
        assert session is not None
        assert session["local_ip"] == {"address": "10.255.0.24/31"}
        assert session["remote_ip"] == {"address": "10.255.0.25/31"}
        assert session["remote_as"] == {"asn": 6695}
        assert session["remote_device"] == "EXT-INET-DE-CIX-FR-01"

    def test_cable_still_takes_precedence_over_direct_ip(self):
        """A cable connecting the two sides is preferred even when the peering
        capabilities also carry an IP directly."""
        cabled_iface = {
            "name": "Ethernet1/1",
            "ip_address": {"address": "10.0.0.1/31"},
            "device": {"name": "FR6-EDGE-01"},
            "cable": {
                "endpoints": [
                    {
                        "name": "Ethernet1/1",
                        "ip_address": {"address": "10.0.0.1/31"},
                        "device": {"name": "FR6-EDGE-01"},
                    },  # noqa: E501
                    {
                        "name": "Ethernet1/1",
                        "ip_address": {"address": "10.0.0.0/31"},
                        "device": {"name": "EXT-INET-BT-01"},
                    },  # noqa: E501
                ]
            },
            "capabilities": [],
        }
        peering = self._direct_ip_peering(
            local_device="FR6-EDGE-01",
            local_iface="Ethernet1/1",
            local_ip="10.255.0.26/31",  # deliberately different from the cable IP
            remote_device="EXT-INET-BT-01",
            remote_iface="Ethernet1/1",
            remote_ip="10.255.0.27/31",
            remote_asn=5400,
        )
        session = _build_session_from_peering(
            peering,
            device_name="FR6-EDGE-01",
            local_as={"asn": 65000},
            interfaces=[cabled_iface],
        )
        assert session is not None
        # Cable-derived IPs win over the peering's own direct IPs
        assert session["local_ip"] == {"address": "10.0.0.1/31"}
        assert session["remote_ip"] == {"address": "10.0.0.0/31"}

    def test_no_ip_anywhere_still_returns_none(self):
        """Direct-IP fallback does not manufacture a session when the peering
        interfaces themselves have no IP either (still a legitimate skip)."""
        bare_iface = {
            "name": "Ethernet1/31",
            "ip_address": {"address": "fd00::1/127"},
            "cable": None,
            "device": {"name": "dc1-super-spine-01"},
            "capabilities": [],
        }
        peering = {
            "name": "dci-underlay--dc1-dc2-primary",
            "session_type": "EBGP",
            "bfd_enabled": True,
            "send_community": True,
            "ttl": 1,
            "route_reflector_client": False,
            "capabilities": [
                {"name": "Ethernet1/31", "ip_address": None, "device": {"name": "dc1-super-spine-01"}},
                {"name": "Ethernet25/1", "ip_address": None, "device": {"name": "dc2-super-spine-01"}},
            ],
            "bgp_processes": [
                {"capabilities": [{"name": "dc1-super-spine-01"}], "local_as": {"asn": 65001}},
                {"capabilities": [{"name": "dc2-super-spine-01"}], "local_as": {"asn": 65002}},
            ],
        }
        session = _build_session_from_peering(
            peering,
            device_name="dc1-super-spine-01",
            local_as={"asn": 65001},
            interfaces=[bare_iface],
        )
        assert session is None
