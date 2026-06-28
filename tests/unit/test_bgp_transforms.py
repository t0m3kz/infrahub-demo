"""Unit tests for BGP transform functions.

Tests verify correct BGP session building, peer group assignment,
and route reflector client detection from peering_interfaces data.
"""

from transforms.common import _build_peer_groups, _build_session_from_peering, get_bgp_profile

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
):
    peering = {
        "name": name,
        "session_type": session_type,
        "bfd_enabled": bfd,
        "send_community": "standard-extended",
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
            procs.append({"device": {"name": local_device}, "local_as": {"asn": local_asn}})
        if remote_asn is not None:
            procs.append({"device": {"name": remote_device}, "local_as": {"asn": remote_asn}})
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

    def test_invalid_peering_interfaces_returns_none(self):
        """Peering with wrong number of interfaces returns None."""
        peering = {
            "name": "bad",
            "session_type": "EBGP",
            "ttl": 1,
            "interface_capabilities": [{"device": {"name": "leaf-01"}}],  # only 1
        }
        session = _build_session_from_peering(
            peering,
            device_name="leaf-01",
            local_as=None,
            interfaces=None,
        )
        assert session is None

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

    def test_underlay_address_families_empty(self):
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
        assert session["address_families"] == []

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
                {"device": {"name": local_device}, "local_as": {"asn": 65001}},
                {"device": {"name": remote_device}, "local_as": {"asn": 65002}},
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
                {"device": {"name": "dc2-super-spine-01"}, "local_as": {"asn": 65002}},
                {"device": {"name": "dc1-super-spine-01"}, "local_as": {"asn": 65001}},
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


def _session(session_type="EBGP", ttl=1, remote_as=None, rr_client=False, name="s"):
    """Build a minimal session dict for peer group testing."""
    s = {
        "name": name,
        "session_type": session_type,
        "ttl": ttl,
        "bfd_enabled": True,
        "send_community": "standard-extended",
        "route_reflector_client": rr_client,
    }
    if remote_as:
        s["remote_as"] = remote_as
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
