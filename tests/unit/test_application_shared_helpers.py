from __future__ import annotations

from generators.helpers.ports import PortProfileHelper, PortsPlanner
from generators.helpers.rules import RulePlanningHelper


class TestRulePlanningHelper:
    def test_flow_rule_name_normalizes_parts(self) -> None:
        name = RulePlanningHelper.flow_rule_name("myapp", "Payments API", "Internal API")
        assert name == "myapp-payments-api-to-internal-api"

    def test_flow_rule_description_uses_explicit_description(self) -> None:
        description = RulePlanningHelper.flow_rule_description(
            explicit_description="Allow frontend to backend",
            src_name="frontend",
            src_type="frontend",
            dst_name="backend",
            dst_type="backend",
        )
        assert description == "Allow frontend to backend"

    def test_flow_rule_description_builds_default(self) -> None:
        description = RulePlanningHelper.flow_rule_description(
            explicit_description=None,
            src_name="frontend",
            src_type="frontend",
            dst_name="backend",
            dst_type="backend",
        )
        assert description == "Auto-generated: frontend (frontend) -> backend (backend)"

    def test_source_segment_policy_name(self) -> None:
        assert RulePlanningHelper.source_segment_policy_name("seg-a") == "seg-seg-a-egress"

    def test_build_policy_rule_payload(self) -> None:
        payload = RulePlanningHelper.build_policy_rule_payload(
            policy_id="policy-1",
            rule_name="app-a-to-b",
            protocol="tcp",
            source_segment_id="seg-a",
            destination_segment_id="seg-b",
            source_isolation_mode="normal",
            destination_isolation_mode="microsegmented",
            description="test-description",
            log=True,
            port_start=443,
            expires_at="2026-12-31T00:00:00+00:00",
            extra_fields={"source_zone": {"id": "zone-a"}},
        )

        assert payload["policy"] == {"id": "policy-1"}
        assert payload["name"] == "app-a-to-b"
        assert payload["protocol"] == "tcp"
        assert payload["log"] is True
        assert payload["apply_on_switch"] is True
        assert payload["port_start"] == 443
        assert payload["expires_at"] == "2026-12-31T00:00:00+00:00"
        assert payload["source_zone"] == {"id": "zone-a"}


class TestPortProfileHelper:
    def test_resolve_dependency_rule_port_defaults_protocol_to_tcp(self) -> None:
        dep = {"port_start": 443, "port_end": None}
        assert PortProfileHelper.resolve_dependency_rule_port(dep) == ("tcp", 443, None)

    def test_resolve_dependency_rule_port_returns_none_when_empty(self) -> None:
        assert PortProfileHelper.resolve_dependency_rule_port({}) is None

    def test_derive_service_port_tuple_skips_icmp(self) -> None:
        assert (
            PortProfileHelper.derive_service_port_tuple(
                port_start=80,
                port_end=None,
                protocol_raw="icmp",
            )
            is None
        )

    def test_derive_service_port_tuple_normalizes_tls_to_tcp(self) -> None:
        assert PortProfileHelper.derive_service_port_tuple(
            port_start=443,
            port_end=None,
            protocol_raw="tls",
        ) == (443, None, "tcp")

    def test_vip_protocol_for_service_port_preserves_existing_behavior(self) -> None:
        assert PortProfileHelper.vip_protocol_for_service_port("tcp", 80) == "http"
        assert PortProfileHelper.vip_protocol_for_service_port("tcp", 443) == "https"
        assert PortProfileHelper.vip_protocol_for_service_port("tcp", 8443) == "tcp"
        assert PortProfileHelper.vip_protocol_for_service_port("tcp_udp", 53) == "tcp"

    def test_health_check_type_for_vip(self) -> None:
        assert PortProfileHelper.health_check_type_for_vip("http") == "http"
        assert PortProfileHelper.health_check_type_for_vip("https") == "ssl"
        assert PortProfileHelper.health_check_type_for_vip("udp") == "tcp"


class TestPortsPlanner:
    def test_derive_ports_from_vips(self) -> None:
        ports, warnings = PortsPlanner.derive_ports_from_vips(
            [
                {"port": 443, "protocol": "https"},
                {"port": 80, "protocol": "http"},
                {"port": 1234, "protocol": "unknown"},
            ]
        )

        assert (443, None, "tcp") in ports
        assert (80, None, "tcp") in ports
        assert len(warnings) == 1

    def test_planner_maps_vip_protocol_and_health_check(self) -> None:
        assert PortsPlanner.to_vip_protocol("tcp", 443) == "https"
        assert PortsPlanner.health_check_type_for_vip("https") == "ssl"
