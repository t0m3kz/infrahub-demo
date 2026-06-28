"""Unit tests for shared iBGP overlay ASN across generators.

Tests verify that for ebgp-ibgp and ospf-ibgp routing strategies,
the overlay ASN is shared across all switches regardless of which
generator (pod, rack) creates the BGP processes.

The key invariant: all overlay BGP processes must reference the same ASN.
dc.py creates the shared AS; pod/rack generators look it up — no fallbacks.
"""

from unittest.mock import MagicMock

import pytest

from generators.helpers.routing import RoutingPlan, RoutingPlanInput, RoutingPlanner


def _make_pool() -> str:
    """Return pool ID string (planner uses string IDs, not SDK objects)."""
    return "pool-1"


def _make_loopback(name: str, device_id: str, role: str, ip: str) -> MagicMock:
    lb = MagicMock()
    lb.id = f"lb-{device_id}"
    lb.device.peer.name.value = name
    lb.device.peer.id = device_id
    lb.device.peer.role.value = role
    lb.ip_address.id = f"ip-{device_id}"
    lb.ip_address.display_label = f"{ip}/32"
    return lb


def _make_device_map(names_roles: list[tuple[str, str]]) -> dict[str, dict]:
    """Create a device_map dict for direct _plan_* calls."""
    return {
        name: {
            "id": f"dev-{name}",
            "role": role,
            "router_id": {"id": f"ip-dev-{name}"},
            "loopback_ip": f"10.0.0.{i + 1}",
        }
        for i, (name, role) in enumerate(names_roles)
    }


def _make_existing_overlay_bgp(device_name: str, as_id: str, as_asn: int = 65500) -> MagicMock:
    """Create a ManagedBGP SDK mock representing an existing overlay BGP process."""
    bgp = MagicMock()
    bgp.id = f"bgp-{device_name}-overlay"
    bgp.name.value = f"{device_name}-bgp-overlay"
    bgp.local_as.id = as_id
    bgp.capabilities.peers[0].name.value = device_name
    bgp.capabilities.peers[0].id = f"dev-{device_name}"
    return bgp


def _make_p2p_interface(iface_id: str, iface_name: str, device_id: str, cable_id: str) -> MagicMock:
    iface = MagicMock()
    iface.id = iface_id
    iface.name.value = iface_name
    iface.cable.id = cable_id
    iface.device.id = device_id
    return iface


class TestOverlayASNSharing:
    """Test that iBGP overlay requires and uses a shared ASN (no fallbacks)."""

    def test_ibgp_overlay_requires_overlay_as_id(self) -> None:
        device_map = _make_device_map([("spine-1", "spine"), ("leaf-1", "leaf")])

        planner = RoutingPlanner(deployment_id="dc-1")
        plan = RoutingPlan()

        planner._plan_overlay_processes(
            plan,
            device_map=device_map,
            overlay_as_id="shared-as-id",
        )

        assert len(plan.bgp_processes) == 2
        for bgp in plan.bgp_processes:
            assert bgp["local_as"] == {"id": "shared-as-id"}

    def test_ibgp_overlay_all_processes_reference_same_as(self) -> None:
        device_map = _make_device_map(
            [
                ("spine-1", "spine"),
                ("spine-2", "spine"),
                ("leaf-1", "leaf"),
                ("leaf-2", "leaf"),
            ]
        )

        planner = RoutingPlanner(deployment_id="dc-1")
        existing_as_id = "existing-overlay-as-uuid"
        plan = RoutingPlan()

        planner._plan_overlay_processes(
            plan,
            device_map=device_map,
            overlay_as_id=existing_as_id,
        )

        assert len(plan.bgp_processes) == 4
        for bgp in plan.bgp_processes:
            assert bgp["local_as"] == {"id": existing_as_id}

    def test_ebgp_ibgp_plan_reuses_overlay_as(self) -> None:
        loopbacks = [
            _make_loopback("spine-1", "s1", "spine", "10.0.0.1"),
            _make_loopback("leaf-1", "l1", "leaf", "10.0.0.2"),
        ]
        pool = _make_pool()
        existing_as_id = "shared-ibgp-asn-uuid"
        interfaces = [
            _make_p2p_interface("if1", "Ethernet1/1", "s1", "c1"),
            _make_p2p_interface("if2", "Ethernet1/1", "l1", "c1"),
        ]

        planner = RoutingPlanner(deployment_id="dc-1")
        result = planner.build_routing_plan(
            RoutingPlanInput(
                bottom_devices=["spine-1", "leaf-1"],
                underlay=[],
                interfaces=interfaces,
                loopback_interfaces=loopbacks,
                options={"asn_pool": pool, "overlay_as_id": existing_as_id},
                routing_strategy="ebgp-ibgp",
                deployment_name="dc1",
            )
        )

        overlay_bgps = [p for p in result.bgp_processes if p["name"].endswith("-bgp-overlay")]
        assert len(overlay_bgps) == 2

        for bgp in overlay_bgps:
            assert bgp["local_as"] == {"id": existing_as_id}

    def test_ebgp_ibgp_without_overlay_as_id_raises(self) -> None:
        loopbacks = [_make_loopback("spine-1", "s1", "spine", "10.0.0.1")]
        pool = _make_pool()
        interfaces = [
            _make_p2p_interface("if1", "Ethernet1/1", "s1", "c1"),
            _make_p2p_interface("if2", "Ethernet1/1", "s1-peer", "c1"),
        ]
        loopbacks.append(_make_loopback("peer-1", "s1-peer", "spine", "10.0.0.2"))

        planner = RoutingPlanner(deployment_id="dc-1")

        with pytest.raises(ValueError, match="overlay_as_id is required"):
            planner.build_routing_plan(
                RoutingPlanInput(
                    bottom_devices=["spine-1", "peer-1"],
                    interfaces=interfaces,
                    loopback_interfaces=loopbacks,
                    options={"asn_pool": pool},
                    routing_strategy="ebgp-ibgp",
                    deployment_name="dc1",
                )
            )

    def test_ospf_ibgp_without_ospf_area_raises(self) -> None:
        loopbacks = [_make_loopback("spine-1", "s1", "spine", "10.0.0.1")]

        planner = RoutingPlanner(deployment_id="dc-1")

        with pytest.raises(ValueError, match="existing_ospf_area is required"):
            planner.build_routing_plan(
                RoutingPlanInput(
                    bottom_devices=["spine-1"],
                    loopback_interfaces=loopbacks,
                    options={"overlay_as_id": "some-as-id"},
                    routing_strategy="ospf-ibgp",
                    deployment_name="dc1",
                )
            )

    def test_ospf_ibgp_plan_reuses_overlay_as(self) -> None:
        loopbacks = [
            _make_loopback("spine-1", "s1", "spine", "10.0.0.1"),
            _make_loopback("leaf-1", "l1", "leaf", "10.0.0.2"),
        ]
        existing_as_id = "shared-ospf-ibgp-asn"
        interfaces = [
            _make_p2p_interface("if1", "Ethernet1/1", "s1", "c1"),
            _make_p2p_interface("if2", "Ethernet1/1", "l1", "c1"),
        ]

        planner = RoutingPlanner(deployment_id="dc-1")
        result = planner.build_routing_plan(
            RoutingPlanInput(
                bottom_devices=["spine-1", "leaf-1"],
                interfaces=interfaces,
                loopback_interfaces=loopbacks,
                options={
                    "overlay_as_id": existing_as_id,
                    "ospf_area_id": "area-0-id",
                },
                routing_strategy="ospf-ibgp",
                deployment_name="dc1",
            )
        )

        overlay_bgps = [p for p in result.bgp_processes if p["name"].endswith("-bgp-overlay")]
        assert len(overlay_bgps) == 2

        for bgp in overlay_bgps:
            assert bgp["local_as"] == {"id": existing_as_id}

        assert len(result.autonomous_systems) == 0

    def test_ebgp_ebgp_ignores_overlay_as_id(self) -> None:
        loopbacks = [
            _make_loopback("spine-1", "s1", "spine", "10.0.0.1"),
            _make_loopback("leaf-1", "l1", "leaf", "10.0.0.2"),
        ]
        pool = _make_pool()
        interfaces = [
            _make_p2p_interface("if1", "Ethernet1/1", "s1", "c1"),
            _make_p2p_interface("if2", "Ethernet1/1", "l1", "c1"),
        ]

        planner = RoutingPlanner(deployment_id="dc-1")
        result = planner.build_routing_plan(
            RoutingPlanInput(
                bottom_devices=["spine-1", "leaf-1"],
                interfaces=interfaces,
                loopback_interfaces=loopbacks,
                options={"asn_pool": pool, "overlay_as_id": "should-be-ignored"},
                routing_strategy="ebgp-ebgp",
                deployment_name="dc1",
            )
        )

        overlay_bgps = [p for p in result.bgp_processes if p["name"].endswith("-bgp-overlay")]
        for bgp in overlay_bgps:
            assert bgp["local_as"].get("id") != "should-be-ignored"

    def test_multi_generator_simulation(self) -> None:
        """All overlay BGP processes share one ASN across multiple generator calls."""
        saved_overlay_as_id = "overlay-as-65500"

        # First generator: super-spines + spines
        ss_device_map = _make_device_map(
            [
                ("ss-1", "super-spine"),
                ("spine-1", "spine"),
                ("spine-2", "spine"),
            ]
        )

        planner = RoutingPlanner(deployment_id="dc-1")
        first_plan = RoutingPlan()

        planner._plan_overlay_processes(
            first_plan,
            device_map=ss_device_map,
            overlay_as_id=saved_overlay_as_id,
        )

        assert len(first_plan.bgp_processes) == 3

        # Second generator: leafs + spines (spines again for peering)
        leaf_device_map = _make_device_map(
            [
                ("leaf-1", "leaf"),
                ("leaf-2", "leaf"),
                ("spine-1", "spine"),
                ("spine-2", "spine"),
            ]
        )

        second_plan = RoutingPlan()

        planner._plan_overlay_processes(
            second_plan,
            device_map=leaf_device_map,
            overlay_as_id=saved_overlay_as_id,
        )

        assert len(second_plan.bgp_processes) == 4

        all_bgps = first_plan.bgp_processes + second_plan.bgp_processes
        assert len(all_bgps) == 7
        for bgp in all_bgps:
            assert bgp["local_as"] == {"id": saved_overlay_as_id}

    def test_overlay_peerings_include_remote_super_spine_from_overlay_as(self) -> None:
        """Use existing_bgp overlay entries to include remote super-spines."""
        loopbacks = [
            _make_loopback("spine-1", "dev-spine-1", "spine", "10.0.0.1"),
            _make_loopback("ss-1", "dev-ss-1", "super-spine", "10.0.0.2"),
        ]

        existing_overlay = [
            _make_existing_overlay_bgp("spine-1", as_id="as-ibgp-shared", as_asn=65500),
            _make_existing_overlay_bgp("ss-1", as_id="as-ibgp-shared", as_asn=65500),
        ]

        interfaces = [
            _make_p2p_interface("if1", "Ethernet1/1", "dev-spine-1", "c1"),
            _make_p2p_interface("if2", "Ethernet1/1", "dev-ss-1", "c1"),
        ]

        planner = RoutingPlanner(deployment_id="dc-1")

        class _Design:
            routing_strategy = "ospf-ibgp"
            bgp_topology = "route_reflector"

            def model_dump(self) -> dict:
                return {}

        result = planner.build_routing_plan(
            RoutingPlanInput(
                bottom_devices=["spine-1"],
                top_devices=["ss-1"],
                interfaces=interfaces,
                loopback_interfaces=loopbacks,
                overlay=existing_overlay,
                options={
                    "overlay_as_id": "as-ibgp-shared",
                    "ospf_area_id": "area-0-id",
                    "design": _Design(),
                },
                routing_strategy="ospf-ibgp",
                deployment_name="dc1",
            )
        )

        overlay_peerings = [p for p in result.bgp_peerings if p["name"].startswith("overlay")]
        assert overlay_peerings, "Expected overlay peering between spine and remote super-spine"
        assert any(
            p["name"] == "overlay-evpn--spine-1--ss-1" or p["name"] == "overlay-evpn--ss-1--spine-1"
            for p in overlay_peerings
        )
