from __future__ import annotations

from generators.helpers.ports import PortProfileHelper
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
