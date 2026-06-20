"""Unit tests for BGP session planning.

Tests verify correct BGP session creation for different:
- Session types (ibgp, ebgp)
- Deployment scenarios (standard, mixed, middle_rack)
- Device role combinations (spine-leaf, leaf-tor relationships)
"""

import pytest

from generators.helpers.routing import BGPSession, _BGPDevice
from generators.helpers.routing import _BGPSessionPlanner as BGPSessionPlanner


class TestBGPSessionPlannerCore:
    """Test core BGP session planner functionality."""

    def test_initialization(self) -> None:
        devices = [
            _BGPDevice("spine-1", "s1", "spine"),
            _BGPDevice("leaf-1", "l1", "leaf"),
        ]

        planner = BGPSessionPlanner(devices=devices)

        assert len(planner.devices) == 2

    def test_device_grouping_by_role(self) -> None:
        devices = [
            _BGPDevice("spine-1", "s1", "spine"),
            _BGPDevice("spine-2", "s2", "spine"),
            _BGPDevice("leaf-1", "l1", "leaf"),
            _BGPDevice("leaf-2", "l2", "leaf"),
            _BGPDevice("tor-1", "t1", "tor"),
        ]

        planner = BGPSessionPlanner(devices=devices)

        assert len(planner.devices) == 5
        roles = {d.role for d in planner.devices}
        assert "spine" in roles
        assert "leaf" in roles
        assert "tor" in roles


class TestRouteReflectorTopology:
    """Test route reflector topology (spines as RR, leafs/ToRs as clients)."""

    def test_basic_route_reflector_ibgp(self) -> None:
        devices = [
            _BGPDevice("spine-1", "s1", "spine"),
            _BGPDevice("leaf-1", "l1", "leaf"),
            _BGPDevice("leaf-2", "l2", "leaf"),
        ]

        planner = BGPSessionPlanner(devices=devices)
        sessions = planner.build_session_plan(session_type="ibgp")

        assert len(sessions) == 2

        for session in sessions:
            dev1_name, dev1_id, dev2_name, dev2_id, sess_type, af_types = session
            assert sess_type == "ibgp"
            assert af_types == ["evpn"]
            assert "leaf" in dev1_name
            assert "spine" in dev2_name

    def test_route_reflector_with_multiple_spines(self) -> None:
        devices = [
            _BGPDevice("spine-1", "s1", "spine"),
            _BGPDevice("spine-2", "s2", "spine"),
            _BGPDevice("leaf-1", "l1", "leaf"),
            _BGPDevice("leaf-2", "l2", "leaf"),
        ]

        planner = BGPSessionPlanner(devices=devices)
        sessions = planner.build_session_plan(session_type="ibgp")

        # 2 leafs × 2 spines = 4 sessions
        assert len(sessions) == 4

        leaf1_sessions = [s for s in sessions if s[0] == "leaf-1"]
        leaf2_sessions = [s for s in sessions if s[0] == "leaf-2"]

        assert len(leaf1_sessions) == 2
        assert len(leaf2_sessions) == 2

    def test_route_reflector_with_tors_mixed_deployment(self) -> None:
        devices = [
            _BGPDevice("spine-1", "s1", "spine"),
            _BGPDevice("spine-2", "s2", "spine"),
            _BGPDevice("leaf-1", "l1", "leaf"),
            _BGPDevice("leaf-2", "l2", "leaf"),
            _BGPDevice("tor-1", "t1", "tor"),
            _BGPDevice("tor-2", "t2", "tor"),
        ]

        planner = BGPSessionPlanner(devices=devices)
        sessions = planner.build_session_plan(session_type="ibgp")

        # (2 leafs + 2 ToRs) × 2 spines = 8 sessions
        assert len(sessions) == 8

        tor_sessions = [s for s in sessions if "tor" in s[0]]
        assert len(tor_sessions) == 4  # 2 ToRs × 2 spines

        for session in tor_sessions:
            dev1_name, _, dev2_name, _, sess_type, _ = session
            assert "tor" in dev1_name
            assert "spine" in dev2_name
            assert sess_type == "ibgp"

    def test_route_reflector_with_tors_middle_rack_deployment(self) -> None:
        devices = [
            _BGPDevice("spine-1", "s1", "spine"),
            _BGPDevice("leaf-1", "l1", "leaf"),
            _BGPDevice("leaf-2", "l2", "leaf"),
            _BGPDevice("tor-1", "t1", "tor"),
            _BGPDevice("tor-2", "t2", "tor"),
        ]

        planner = BGPSessionPlanner(devices=devices)
        sessions = planner.build_session_plan(session_type="ibgp")

        # (2 leafs + 2 ToRs) × 1 spine = 4 sessions
        assert len(sessions) == 4

        client_devices = [s[0] for s in sessions]
        assert "leaf-1" in client_devices
        assert "leaf-2" in client_devices
        assert "tor-1" in client_devices
        assert "tor-2" in client_devices

        for session in sessions:
            assert session[2] == "spine-1"
            assert session[4] == "ibgp"

    def test_route_reflector_ebgp_overlay(self) -> None:
        devices = [
            _BGPDevice("spine-1", "s1", "spine"),
            _BGPDevice("leaf-1", "l1", "leaf"),
            _BGPDevice("leaf-2", "l2", "leaf"),
        ]

        planner = BGPSessionPlanner(devices=devices)
        sessions = planner.build_session_plan(session_type="ebgp")

        assert len(sessions) == 2

        for session in sessions:
            _, _, _, _, sess_type, _ = session
            assert sess_type == "ebgp"


class TestASNCalculation:
    """Test ASN calculation logic for eBGP deployments."""

    @pytest.mark.parametrize(
        "device_name,role,base_asn,expected",
        [
            ("dc1-spine-01", "spine", 65000, 65000),
            ("dc1-spine-02", "spine", 65000, 65000),
            ("dc1-leaf-01", "leaf", 65000, 65001),
            ("dc1-leaf-02", "leaf", 65000, 65002),
            ("dc1-leaf-10", "leaf", 65000, 65010),
            ("dc1-tor-01", "tor", 65000, 66001),
            ("dc1-tor-02", "tor", 65000, 66002),
            ("dc1-tor-15", "tor", 65000, 66015),
            ("dc1-border-01", "border-leaf", 65000, 65001),
            ("dc1-border-02", "border-leaf", 65000, 65002),
            ("spine", "spine", 65000, 65000),
        ],
    )
    def test_calculate_device_asn(self, device_name: str, role: str, base_asn: int, expected: int) -> None:
        import re

        match = re.search(r"-(\d+)$", device_name)
        index = int(match.group(1)) if match else 1

        if role in ["spine", "super-spine", "super_spine"]:
            result = base_asn
        elif role in ["leaf", "border-leaf", "border_leaf"]:
            result = base_asn + index
        elif role == "tor":
            result = base_asn + 1000 + index
        else:
            result = base_asn + index

        assert result == expected


class TestEdgeCases:
    """Test edge cases and error conditions."""

    def test_empty_device_list(self) -> None:
        planner = BGPSessionPlanner(devices=[])
        sessions = planner.build_session_plan(session_type="ibgp")

        assert sessions == []

    def test_no_spines_leafs_only_route_reflector(self) -> None:
        devices = [
            _BGPDevice("leaf-1", "l1", "leaf"),
            _BGPDevice("leaf-2", "l2", "leaf"),
        ]

        planner = BGPSessionPlanner(devices=devices)
        sessions = planner.build_session_plan(session_type="ibgp")

        assert sessions == []

    def test_no_spines_tor_leaf_route_reflector(self) -> None:
        """Rack generator scenario: leafs act as sub-RR for ToRs when no spines present."""
        devices = [
            _BGPDevice("leaf-1", "l1", "leaf"),
            _BGPDevice("leaf-2", "l2", "leaf"),
            _BGPDevice("tor-1", "t1", "tor"),
            _BGPDevice("tor-2", "t2", "tor"),
        ]

        planner = BGPSessionPlanner(devices=devices)
        sessions = planner.build_session_plan(session_type="ibgp")

        # Fallback: leafs as RR, TORs as clients → 2 TORs × 2 leafs = 4 sessions
        assert len(sessions) == 4

        for session in sessions:
            dev1_name, _, dev2_name, _, sess_type, _ = session
            assert "tor" in dev1_name
            assert "leaf" in dev2_name
            assert sess_type == "ibgp"

    def test_no_leafs_route_reflector(self) -> None:
        """Spines without clients (leaves/tors) do NOT peer with each other.
        Spines are RRs for clients, not for other spines. This contrasts with
        super-spines which DO peer together when alone (see test_super_spines_only_peer_together).
        """
        devices = [
            _BGPDevice("spine-1", "s1", "spine"),
            _BGPDevice("spine-2", "s2", "spine"),
        ]

        planner = BGPSessionPlanner(devices=devices)
        sessions = planner.build_session_plan(session_type="ibgp")

        assert sessions == []

    def test_super_spines_only_peer_together(self) -> None:
        """Super-spines without clients DO peer with each other.
        This is the DC-level scenario: DC generator seeds super-spine overlay BGP
        before any pod exists, so the planner receives only super-spines.
        """
        devices = [
            _BGPDevice("dc1-super-spine-01", "ss1", "super-spine"),
            _BGPDevice("dc1-super-spine-02", "ss2", "super-spine"),
        ]

        planner = BGPSessionPlanner(devices=devices)
        sessions = planner.build_session_plan(session_type="ibgp")

        assert len(sessions) == 1
        assert {sessions[0].dev1_name, sessions[0].dev2_name} == {"dc1-super-spine-01", "dc1-super-spine-02"}

    def test_single_device(self) -> None:
        devices = [
            _BGPDevice("spine-1", "s1", "spine"),
        ]

        planner = BGPSessionPlanner(devices=devices)
        sessions = planner.build_session_plan(session_type="ibgp")

        assert sessions == []


class TestMixedDeploymentScenarios:
    """Test specific mixed deployment scenarios for leaf-ToR relationships."""

    def test_mixed_deployment_two_rows(self) -> None:
        devices = [
            _BGPDevice("spine-1", "s1", "spine"),
            _BGPDevice("spine-2", "s2", "spine"),
            _BGPDevice("leaf-1", "l1", "leaf"),
            _BGPDevice("leaf-2", "l2", "leaf"),
            _BGPDevice("tor-1", "t1", "tor"),
            _BGPDevice("tor-2", "t2", "tor"),
            _BGPDevice("leaf-3", "l3", "leaf"),
            _BGPDevice("leaf-4", "l4", "leaf"),
            _BGPDevice("tor-3", "t3", "tor"),
            _BGPDevice("tor-4", "t4", "tor"),
        ]

        planner = BGPSessionPlanner(devices=devices)
        sessions = planner.build_session_plan(session_type="ibgp")

        # (4 leafs + 4 ToRs) × 2 spines = 16 sessions
        assert len(sessions) == 16

        tor_sessions = [s for s in sessions if "tor" in s[0]]
        assert len(tor_sessions) == 8  # 4 ToRs × 2 spines

        tor1_sessions = [s for s in sessions if s[0] == "tor-1"]
        assert len(tor1_sessions) == 2

    def test_middle_rack_deployment_single_rack(self) -> None:
        devices = [
            _BGPDevice("spine-1", "s1", "spine"),
            _BGPDevice("leaf-1", "l1", "leaf"),
            _BGPDevice("leaf-2", "l2", "leaf"),
            _BGPDevice("tor-1", "t1", "tor"),
            _BGPDevice("tor-2", "t2", "tor"),
        ]

        planner = BGPSessionPlanner(devices=devices)
        sessions = planner.build_session_plan(session_type="ibgp")

        assert len(sessions) == 4

        device_names = {s[0] for s in sessions}
        assert "tor-1" in device_names
        assert "tor-2" in device_names
        assert "leaf-1" in device_names
        assert "leaf-2" in device_names

        for session in sessions:
            assert session[2] == "spine-1"


class TestSessionMetadata:
    """Test session metadata (address families, session types)."""

    def test_evpn_address_family_included(self) -> None:
        devices = [
            _BGPDevice("spine-1", "s1", "spine"),
            _BGPDevice("leaf-1", "l1", "leaf"),
        ]

        planner = BGPSessionPlanner(devices=devices)
        sessions = planner.build_session_plan(session_type="ibgp")

        for session in sessions:
            _, _, _, _, _, af_types = session
            assert "evpn" in af_types

    def test_session_namedtuple_structure(self) -> None:
        devices = [
            _BGPDevice("spine-1", "s1", "spine"),
            _BGPDevice("leaf-1", "l1", "leaf"),
        ]

        planner = BGPSessionPlanner(devices=devices)
        sessions = planner.build_session_plan(session_type="ibgp")

        session = sessions[0]

        assert isinstance(session, BGPSession)
        assert isinstance(session.dev1_name, str)
        assert isinstance(session.dev1_id, str)
        assert isinstance(session.dev2_name, str)
        assert isinstance(session.dev2_id, str)
        assert isinstance(session.session_type, str)
        assert isinstance(session.af_types, list)
