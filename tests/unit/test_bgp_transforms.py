"""Unit tests for BGP transform functions.

Tests verify correct BGP session building, peer group assignment,
and route reflector client detection from peering_interfaces data.
"""

import pytest

from transforms.common import _build_peer_groups, _build_session_from_peering, get_bgp_profile
from transforms.helpers.bgp import _extract_remote_asn_from_peering

# ============================================================================
# Helpers to build test data matching GraphQL response structure
# ============================================================================


def _make_peering_interfaces(
    local_name: str,
    local_ip: str,
    local_device: str,
    remote_name: str,
    remote_ip: str,
    remote_device: str,
    local_type: str = "DcimPhysicalInterface",
    remote_type: str = "DcimPhysicalInterface",
):
    """Build peering_interfaces list (already flattened from edges)."""
    return [
        {
            "typename": local_type,
            "name": local_name,
            "ip_address": {"address": local_ip},
            "device": {"name": local_device},
        },
        {
            "typename": remote_type,
            "name": remote_name,
            "ip_address": {"address": remote_ip},
            "device": {"name": remote_device},
        },
    ]


def _make_peering(
    *,
    name: str = "test-peering",
    session_type: str = "EBGP",
    ttl: int = 1,
    bfd: bool = True,
    route_reflector_client: bool = False,
    local_device: str = "leaf-01",
    local_ip: str = "10.1.0.1/31",
    remote_device: str = "spine-01",
    remote_ip: str = "10.1.0.0/31",
    local_iface_type: str = "DcimPhysicalInterface",
    remote_iface_type: str = "DcimPhysicalInterface",
    local_asn: int | None = None,
    remote_asn: int | None = None,
    maximum_routes: int | None = None,
    local_pref: int | None = None,
    med: int | None = None,
    send_extended_community: bool = False,
    remove_private_as: bool = False,
    password: str | None = None,
):
    peering = {
        "name": name,
        "session_type": session_type,
        "bfd_enabled": bfd,
        "send_community": True,
        "send_extended_community": send_extended_community,
        "maximum_routes": maximum_routes,
        "local_pref": local_pref,
        "med": med,
        "remove_private_as": remove_private_as,
        "password": {"password": password} if password is not None else None,
        "ttl": ttl,
        "route_reflector_client": route_reflector_client,
        "interface_capabilities": _make_peering_interfaces(
            local_name="Ethernet1",
            local_ip=local_ip,
            local_device=local_device,
            remote_name="Ethernet2",
            remote_ip=remote_ip,
            remote_device=remote_device,
            local_type=local_iface_type,
            remote_type=remote_iface_type,
        ),
    }
    # Add bgp_processes for remote ASN resolution (eBGP)
    if local_asn is not None or remote_asn is not None:
        procs = []
        if local_asn is not None:
            procs.append({"capabilities": [{"name": local_device}], "local_as": {"asn": local_asn}})
        if remote_asn is not None:
            procs.append({"capabilities": [{"name": remote_device}], "local_as": {"asn": remote_asn}})
        peering["bgp_processes"] = procs
    return peering


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
            "interface_capabilities": [{"device": {"name": "leaf-01"}}],  # only 1
        }
        with pytest.raises(ValueError, match="expected 2 interface_capabilities"):
            _build_session_from_peering(
                peering,
                device_name="leaf-01",
                local_as=None,
                interfaces=None,
            )

    def test_invalid_local_remote_mapping_raises_value_error(self):
        """Two interfaces that don't map local+remote for this device are malformed."""
        peering = {
            "name": "bad-mapping",
            "session_type": "IBGP",
            "ttl": 255,
            "interface_capabilities": [
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
# Helpers — circuit interface service (post-clean_data format)
# ============================================================================


def _make_circuit_iface(
    *,
    circuit_typename: str = "TopologyVirtualCircuit",
    local_device: str,
    local_iface: str,
    local_ip: str,
    remote_device: str,
    remote_iface: str,
    remote_ip: str,
) -> dict:
    """Build a device interface dict with a circuit in interface_capabilities.

    Circuits inherit ManagedGeneric and use identifier: interface_capabilities,
    so they appear in interface_capabilities alongside OSPF/segment services.
    The circuit uses a cardinality-many `interfaces` list (local + remote).
    """
    circuit = {
        "typename": circuit_typename,
        "interfaces": [
            {
                "name": local_iface,
                "ip_address": {"address": local_ip},
                "device": {"name": local_device},
            },
            {
                "name": remote_iface,
                "ip_address": {"address": remote_ip},
                "device": {"name": remote_device},
            },
        ],
    }
    return {
        "name": local_iface,
        "ip_address": {"address": local_ip},
        "cable": None,
        "device": {"name": local_device},
        "interface_capabilities": [circuit],
    }


# ============================================================================
# _build_session_from_peering — circuit service traversal (DCI)
# ============================================================================


class TestCircuitServiceTraversal:
    """TTL=1 underlay sessions where connectivity is expressed via interface
    services (ManagedVirtualCircuit / ManagedPhysicalCircuit) rather than a
    direct DcimCable between the two device interfaces."""

    def _dci_peering(self, local_device: str, remote_device: str) -> dict:
        return {
            "name": "dci-underlay--dc1-dc2-primary",
            "session_type": "EBGP",
            "bfd_enabled": True,
            "send_community": True,
            "ttl": 1,
            "route_reflector_client": False,
            "interface_capabilities": [
                {"name": "Ethernet1/31", "ip_address": None, "device": {"name": local_device}},
                {"name": "Ethernet25/1", "ip_address": None, "device": {"name": remote_device}},
            ],
            "bgp_processes": [
                {"capabilities": [{"name": local_device}], "local_as": {"asn": 65001}},
                {"capabilities": [{"name": remote_device}], "local_as": {"asn": 65002}},
            ],
        }

    def test_virtual_circuit_traversal_resolves_ips(self):
        """Session with TopologyVirtualCircuit interface service finds peer IP."""
        local_iface = _make_circuit_iface(
            circuit_typename="TopologyVirtualCircuit",
            local_device="dc1-super-spine-01",
            local_iface="Ethernet1/31",
            local_ip="fd00:2200::1/127",
            remote_device="dc2-super-spine-01",
            remote_iface="Ethernet25/1",
            remote_ip="fd00:2200::2/127",
        )
        peering = self._dci_peering("dc1-super-spine-01", "dc2-super-spine-01")
        session = _build_session_from_peering(
            peering,
            device_name="dc1-super-spine-01",
            local_as={"asn": 65001},
            interfaces=[local_iface],
        )
        assert session is not None
        assert session["local_ip"] == {"address": "fd00:2200::1/127"}
        assert session["remote_ip"] == {"address": "fd00:2200::2/127"}
        assert session["remote_device"] == "dc2-super-spine-01"
        assert session["remote_as"] == {"asn": 65002}

    def test_physical_circuit_traversal_resolves_ips(self):
        """Session with TopologyPhysicalCircuit interface service finds peer IP."""
        local_iface = _make_circuit_iface(
            circuit_typename="TopologyPhysicalCircuit",
            local_device="dc1-super-spine-01",
            local_iface="Ethernet1/31",
            local_ip="100.64.0.0/31",
            remote_device="dc2-super-spine-01",
            remote_iface="Ethernet25/1",
            remote_ip="100.64.0.1/31",
        )
        peering = self._dci_peering("dc1-super-spine-01", "dc2-super-spine-01")
        session = _build_session_from_peering(
            peering,
            device_name="dc1-super-spine-01",
            local_as={"asn": 65001},
            interfaces=[local_iface],
        )
        assert session is not None
        assert session["local_ip"] == {"address": "100.64.0.0/31"}
        assert session["remote_ip"] == {"address": "100.64.0.1/31"}

    def test_cable_takes_precedence_over_circuit(self):
        """When a cable is present it is preferred over circuit traversal."""
        cabled_iface = {
            "name": "Ethernet1",
            "ip_address": {"address": "10.0.0.1/31"},
            "device": {"name": "leaf-01"},
            "cable": {
                "endpoints": [
                    {
                        "name": "Ethernet1",
                        "ip_address": {"address": "10.0.0.1/31"},
                        "device": {"name": "leaf-01"},
                    },
                    {
                        "name": "Ethernet2",
                        "ip_address": {"address": "10.0.0.0/31"},
                        "device": {"name": "spine-01"},
                    },
                ]
            },
            "interface_capabilities": [
                # A circuit also present — should NOT be used
                {
                    "typename": "ManagedVirtualCircuit",
                    "topology_circuit": {
                        "connectors": [
                            {
                                "interface": {
                                    "name": "Ethernet1",
                                    "ip_address": {"address": "fd00::1/127"},
                                    "device": {"name": "leaf-01"},
                                }
                            },
                            {
                                "interface": {
                                    "name": "Ethernet2",
                                    "ip_address": {"address": "fd00::2/127"},
                                    "device": {"name": "spine-01"},
                                }
                            },
                        ]
                    },
                }
            ],
        }
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
            interfaces=[cabled_iface],
        )
        assert session is not None
        # IP comes from cable, not from the fd00::/127 circuit
        assert session.get("local_ip") == {"address": "10.0.0.1/31"}
        assert session.get("remote_ip") == {"address": "10.0.0.0/31"}

    def test_no_cable_no_circuit_returns_none(self):
        """TTL=1 session with no cable and no matching circuit is skipped."""
        bare_iface = {
            "name": "Ethernet1/31",
            "ip_address": {"address": "fd00::1/127"},
            "cable": None,
            "device": {"name": "dc1-super-spine-01"},
            "interface_capabilities": [],
        }
        peering = self._dci_peering("dc1-super-spine-01", "dc2-super-spine-01")
        session = _build_session_from_peering(
            peering,
            device_name="dc1-super-spine-01",
            local_as={"asn": 65001},
            interfaces=[bare_iface],
        )
        assert session is None

    def test_circuit_with_wrong_remote_skipped(self):
        """Circuit whose remote side doesn't match remote_device_name is ignored."""
        iface = _make_circuit_iface(
            circuit_typename="TopologyVirtualCircuit",
            local_device="dc1-super-spine-01",
            local_iface="Ethernet1/31",
            local_ip="fd00::1/127",
            remote_device="dc3-super-spine-01",  # wrong remote
            remote_iface="Ethernet25/1",
            remote_ip="fd00::2/127",
        )
        peering = self._dci_peering("dc1-super-spine-01", "dc2-super-spine-01")
        session = _build_session_from_peering(
            peering,
            device_name="dc1-super-spine-01",
            local_as={"asn": 65001},
            interfaces=[iface],
        )
        assert session is None

    def test_z_side_device_also_resolves(self):
        """The Z-side device resolves its peer via interface_capabilities circuit lookup."""
        circuit = {
            "typename": "TopologyVirtualCircuit",
            "interfaces": [
                {
                    "name": "Ethernet1/31",
                    "ip_address": {"address": "fd00:2200::1/127"},
                    "device": {"name": "dc1-super-spine-01"},
                },
                {
                    "name": "Ethernet25/1",
                    "ip_address": {"address": "fd00:2200::2/127"},
                    "device": {"name": "dc2-super-spine-01"},
                },
            ],
        }
        z_iface = {
            "name": "Ethernet25/1",
            "ip_address": {"address": "fd00:2200::2/127"},
            "cable": None,
            "device": {"name": "dc2-super-spine-01"},
            "interface_capabilities": [circuit],
        }
        peering = {
            "name": "dci-underlay--dc1-dc2-primary",
            "session_type": "EBGP",
            "bfd_enabled": True,
            "send_community": True,
            "ttl": 1,
            "route_reflector_client": False,
            "interface_capabilities": [
                {"name": "Ethernet25/1", "ip_address": None, "device": {"name": "dc2-super-spine-01"}},
                {"name": "Ethernet1/31", "ip_address": None, "device": {"name": "dc1-super-spine-01"}},
            ],
            "bgp_processes": [
                {"capabilities": [{"name": "dc2-super-spine-01"}], "local_as": {"asn": 65002}},
                {"capabilities": [{"name": "dc1-super-spine-01"}], "local_as": {"asn": 65001}},
            ],
        }
        session = _build_session_from_peering(
            peering,
            device_name="dc2-super-spine-01",
            local_as={"asn": 65002},
            interfaces=[z_iface],
        )
        assert session is not None
        assert session["local_ip"] == {"address": "fd00:2200::2/127"}
        assert session["remote_ip"] == {"address": "fd00:2200::1/127"}
        assert session["remote_device"] == "dc1-super-spine-01"


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

    def test_rr_client_on_spine(self):
        """Spine with RR-flagged iBGP sessions gets route_reflector_client on peer group."""
        sessions = [
            _session(session_type="IBGP", ttl=255, remote_as={"asn": 65000}, rr_client=True, name="o1"),
            _session(session_type="IBGP", ttl=255, remote_as={"asn": 65000}, rr_client=True, name="o2"),
        ]
        pgs = _build_peer_groups(sessions, device_role="spine")
        assert len(pgs) == 1
        assert pgs[0]["route_reflector_client"] is True

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

    def test_non_bgp_services_ignored(self):
        services = [
            {"typename": "ManagedOSPF", "name": "ospf-1"},
        ]
        result = get_bgp_profile(services, device_name="leaf-01")
        assert result == []
