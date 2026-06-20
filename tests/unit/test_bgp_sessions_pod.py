"""Unit tests for BGP Session Planner - Pod Deployment Scenarios.

Tests BGP session creation for pod deployments where ToRs connect
directly to Spines (without Leafs in between).
"""

import pytest

from generators.helpers.routing import _BGPDevice
from generators.helpers.routing import _BGPSessionPlanner as BGPSessionPlanner


class TestPodDeploymentSpinesTors:
    """Test BGP sessions for Pod deployments with only Spines and ToRs."""

    def test_route_reflector_pod_spines_tors_only(self) -> None:
        """Spines act as RRs, ToRs are clients — 4 ToRs × 2 Spines = 8 sessions."""
        devices = [
            _BGPDevice("pod1-spine-01", "spine-1", "spine"),
            _BGPDevice("pod1-spine-02", "spine-2", "spine"),
            _BGPDevice("pod1-tor-01", "tor-1", "tor"),
            _BGPDevice("pod1-tor-02", "tor-2", "tor"),
            _BGPDevice("pod1-tor-03", "tor-3", "tor"),
            _BGPDevice("pod1-tor-04", "tor-4", "tor"),
        ]

        planner = BGPSessionPlanner(devices=devices)
        sessions = planner.build_session_plan(session_type="ibgp")

        assert len(sessions) == 8

        tor_names = {"pod1-tor-01", "pod1-tor-02", "pod1-tor-03", "pod1-tor-04"}
        spine_names = {"pod1-spine-01", "pod1-spine-02"}

        for dev1_name, _, dev2_name, _, session_type, _ in sessions:
            assert (dev1_name in tor_names and dev2_name in spine_names) or (
                dev1_name in spine_names and dev2_name in tor_names
            )
            assert session_type == "ibgp"

    def test_single_spine_multiple_tors_pod(self) -> None:
        """Single Spine RR with 4 ToR clients — 4 sessions."""
        devices = [
            _BGPDevice("pod1-spine-01", "spine-1", "spine"),
            _BGPDevice("pod1-tor-01", "tor-1", "tor"),
            _BGPDevice("pod1-tor-02", "tor-2", "tor"),
            _BGPDevice("pod1-tor-03", "tor-3", "tor"),
            _BGPDevice("pod1-tor-04", "tor-4", "tor"),
        ]

        planner = BGPSessionPlanner(devices=devices)
        sessions = planner.build_session_plan(session_type="ibgp")

        assert len(sessions) == 4

        spine_name = "pod1-spine-01"
        for dev1_name, _, dev2_name, _, _, _ in sessions:
            assert spine_name in [dev1_name, dev2_name]


class TestPodMixedDeployment:
    """Test BGP sessions for Pod deployments with mixed device types."""

    def test_pod_with_spines_leafs_and_tors(self) -> None:
        """All clients (Leafs + ToRs) peer with Spines — (2+2) × 2 = 8 sessions."""
        devices = [
            _BGPDevice("pod1-spine-01", "spine-1", "spine"),
            _BGPDevice("pod1-spine-02", "spine-2", "spine"),
            _BGPDevice("pod1-leaf-01", "leaf-1", "leaf"),
            _BGPDevice("pod1-leaf-02", "leaf-2", "leaf"),
            _BGPDevice("pod1-tor-01", "tor-1", "tor"),
            _BGPDevice("pod1-tor-02", "tor-2", "tor"),
        ]

        planner = BGPSessionPlanner(devices=devices)
        sessions = planner.build_session_plan(session_type="ibgp")

        assert len(sessions) == 8

        spine_names = {"pod1-spine-01", "pod1-spine-02"}
        client_names = {"pod1-leaf-01", "pod1-leaf-02", "pod1-tor-01", "pod1-tor-02"}

        for dev1_name, _, dev2_name, _, _, _ in sessions:
            assert (dev1_name in spine_names and dev2_name in client_names) or (
                dev1_name in client_names and dev2_name in spine_names
            )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
