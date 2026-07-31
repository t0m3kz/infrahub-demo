"""Unit tests for shared underlay/overlay RoutingPassword wiring in RoutingPlanner.

Tests verify that when a password_id is supplied, the planner attaches a
``password: {"id": ...}`` relationship ref to every underlay eBGP peering,
OSPF underlay interface, and overlay BGP peering it creates — and that it's
omitted entirely (not set to None/empty) when no password_id is supplied,
mirroring the existing evpn_af_id conditional-add pattern.
"""

from unittest.mock import MagicMock

from generators.helpers.routing import RoutingPlan, RoutingPlanner


def _make_p2p_interface(iface_id: str, iface_name: str, device_id: str, cable_id: str) -> MagicMock:
    iface = MagicMock()
    iface.id = iface_id
    iface.name.value = iface_name
    iface.cable.id = cable_id
    iface.device.id = device_id
    return iface


def _device_map_entry(name: str, device_id: str, role: str, ip: str) -> dict:
    return {name: {"id": device_id, "role": role, "router_id": {"id": f"ip-{device_id}"}, "loopback_ip": ip}}


class TestEbgpUnderlayPassword:
    def _plan_pair(self, password_id: str = "") -> RoutingPlan:
        device_map: dict = {}
        device_map.update(_device_map_entry("leaf-1", "l1", "leaf", "10.0.0.1"))
        device_map.update(_device_map_entry("spine-1", "s1", "spine", "10.0.0.2"))
        interfaces = [
            _make_p2p_interface("if1", "Ethernet1", "l1", "c1"),
            _make_p2p_interface("if2", "Ethernet1", "s1", "c1"),
        ]
        plan = RoutingPlan()
        planner = RoutingPlanner(deployment_id="dc-1")
        planner._plan_ebgp_underlay(
            plan,
            device_map=device_map,
            interfaces=interfaces,
            existing_as_by_device={},
            asn_pool="pool-1",
            password_id=password_id,
        )
        return plan

    def test_password_ref_attached_when_id_provided(self) -> None:
        plan = self._plan_pair(password_id="pw-underlay-1")
        assert len(plan.bgp_peerings) == 1
        assert plan.bgp_peerings[0]["password"] == {"id": "pw-underlay-1"}

    def test_password_key_omitted_when_no_id(self) -> None:
        plan = self._plan_pair(password_id="")
        assert len(plan.bgp_peerings) == 1
        assert "password" not in plan.bgp_peerings[0]


class TestOspfUnderlayPassword:
    def _plan_pair(self, password_id: str = "") -> RoutingPlan:
        device_map: dict = {}
        device_map.update(_device_map_entry("leaf-1", "l1", "leaf", "10.0.0.1"))
        iface = _make_p2p_interface("if1", "Ethernet1", "l1", "c1")
        plan = RoutingPlan()
        planner = RoutingPlanner(deployment_id="dc-1")
        planner._plan_ospf_underlay(
            plan,
            device_map=device_map,
            interfaces=[iface],
            deployment_name="dc1",
            existing_area_id="area-0-id",
            password_id=password_id,
        )
        return plan

    def test_password_ref_attached_when_id_provided(self) -> None:
        plan = self._plan_pair(password_id="pw-underlay-1")
        assert len(plan.ospf_interfaces) == 1
        assert plan.ospf_interfaces[0]["password"] == {"id": "pw-underlay-1"}

    def test_password_key_omitted_when_no_id(self) -> None:
        plan = self._plan_pair(password_id="")
        assert len(plan.ospf_interfaces) == 1
        assert "password" not in plan.ospf_interfaces[0]


class TestOverlayPeeringPassword:
    def _plan_pair(self, password_id: str = "") -> RoutingPlan:
        device_map = {
            "spine-1": {"id": "s1", "role": "spine", "router_id": {"id": "ip-s1"}, "loopback_ip": "10.0.0.1"},
            "leaf-1": {"id": "l1", "role": "leaf", "router_id": {"id": "ip-l1"}, "loopback_ip": "10.0.0.2"},
        }
        bgp_procs = [
            {"name": "spine-1-bgp-overlay", "capabilities": [{"id": "s1"}]},
            {"name": "leaf-1-bgp-overlay", "capabilities": [{"id": "l1"}]},
        ]
        plan = RoutingPlan()
        planner = RoutingPlanner(deployment_id="dc-1")
        planner._plan_overlay_peerings(
            plan,
            overlay_type="ibgp",
            bgp_processes=bgp_procs,
            device_map=device_map,
            password_id=password_id,
        )
        return plan

    def test_password_ref_attached_when_id_provided(self) -> None:
        plan = self._plan_pair(password_id="pw-overlay-1")
        assert len(plan.bgp_peerings) == 1
        assert plan.bgp_peerings[0]["password"] == {"id": "pw-overlay-1"}

    def test_password_key_omitted_when_no_id(self) -> None:
        plan = self._plan_pair(password_id="")
        assert len(plan.bgp_peerings) == 1
        assert "password" not in plan.bgp_peerings[0]


class TestBuildRoutingPlanPasswordPassthrough:
    """End-to-end: RoutingPlanInput.underlay_password_id/overlay_password_id
    reach the respective peerings/interfaces via build_routing_plan()."""

    def _make_loopback(self, name: str, device_id: str, role: str, ip: str) -> MagicMock:
        lb = MagicMock()
        lb.id = f"lb-{device_id}"
        lb.device.peer.name.value = name
        lb.device.peer.id = device_id
        lb.device.peer.role.value = role
        lb.ip_address.id = f"ip-{device_id}"
        lb.ip_address.display_label = f"{ip}/32"
        return lb

    def test_underlay_and_overlay_password_ids_flow_through(self) -> None:
        from generators.helpers.routing import RoutingPlanInput

        loopbacks = [
            self._make_loopback("leaf-1", "l1", "leaf", "10.0.0.1"),
            self._make_loopback("spine-1", "s1", "spine", "10.0.0.2"),
        ]
        interfaces = [
            _make_p2p_interface("if1", "Ethernet1", "l1", "c1"),
            _make_p2p_interface("if2", "Ethernet1", "s1", "c1"),
        ]
        # routing_strategy must be set via the constructor, not attribute
        # assignment after — RoutingOptions.design is typed as the
        # HasRoutingStrategy Protocol, and isinstance() against a
        # runtime_checkable Protocol uses inspect.getattr_static, which only
        # sees attributes MagicMock pre-registers from constructor kwargs.
        design = MagicMock(routing_strategy="ebgp-ebgp")
        design.model_dump = MagicMock(return_value={})

        planner = RoutingPlanner(deployment_id="dc-1")
        plan = planner.build_routing_plan(
            RoutingPlanInput(
                bottom_devices=["leaf-1", "spine-1"],
                top_devices=[],
                interfaces=interfaces,
                loopback_interfaces=loopbacks,
                options={"design": design, "asn_pool": "pool-1", "overlay_as_id": "as-overlay-1"},
                routing_strategy="ebgp-ibgp",
                deployment_name="dc1",
                underlay_password_id="pw-underlay-1",
                overlay_password_id="pw-overlay-1",
            )
        )

        underlay_peerings = [p for p in plan.bgp_peerings if p["name"].startswith("underlay--")]
        overlay_peerings = [p for p in plan.bgp_peerings if p["name"].startswith("overlay-evpn--")]

        assert underlay_peerings and all(p["password"] == {"id": "pw-underlay-1"} for p in underlay_peerings)
        assert overlay_peerings and all(p["password"] == {"id": "pw-overlay-1"} for p in overlay_peerings)
