"""Unit tests for the rule-planning and port helpers shared by the
application security generators (generators/helpers/rules.py, utils/ports.py)."""

from __future__ import annotations

from typing import Any

import pytest

from generators.helpers.rules import RulesPlanner
from utils.ports import PortProfileHelper


class TestRulesPlannerNamingAndPayload:
    def test_rule_name_normalizes_component_labels(self) -> None:
        """Without a dependency name, labels are lower-cased and space-dashed."""
        name = RulesPlanner.rule_name("myapp", {"name": "Payments API"}, {"name": "Internal API"})
        assert name == "myapp-payments-api-to-internal-api"

    def test_build_rule_payload_optional_fields(self) -> None:
        """Ports and expiry are set only when present; microsegmentation on either side applies on switch."""
        payload = RulesPlanner.build_rule_payload(
            policy_id="policy-1",
            rule_name="app-a-to-b",
            dep={"access_expires_at": "2026-12-31T00:00:00+00:00"},
            src_comp={"name": "a"},
            dst_comp={"name": "b"},
            src_seg={"id": "seg-a", "isolation_mode": "normal"},
            dst_seg={"id": "seg-b", "isolation_mode": "microsegmented"},
            protocol="tcp",
            port_start=443,
            port_end=None,
            cross_zone=True,
        )

        assert payload["policy"] == {"id": "policy-1"}
        assert payload["name"] == "app-a-to-b"
        assert payload["action"] == "permit"
        assert payload["protocol"] == "tcp"
        assert payload["log"] is True
        assert payload["disabled"] is False
        assert payload["apply_on_switch"] is True
        assert payload["port_start"] == 443
        assert "port_end" not in payload
        assert payload["expires_at"] == "2026-12-31T00:00:00+00:00"

    def test_build_rule_payload_without_expiry_or_microsegmentation(self) -> None:
        """No expiry key without access_expires_at; normal/missing isolation never applies on switch."""
        payload = RulesPlanner.build_rule_payload(
            policy_id="policy-1",
            rule_name="r",
            dep={},
            src_comp={},
            dst_comp={},
            src_seg={"id": "seg-a"},
            dst_seg={"id": "seg-b", "isolation_mode": "normal"},
            protocol="udp",
            port_start=None,
            port_end=None,
            cross_zone=False,
        )

        assert payload["apply_on_switch"] is False
        assert payload["log"] is False
        assert "expires_at" not in payload
        assert "port_start" not in payload


class TestParsePortSpec:
    """protocol/port or protocol/start-end; anything else raises, so a typo
    never widens into an any-port rule."""

    @pytest.mark.parametrize(
        ("spec", "expected"),
        [
            ("tcp/443", ("tcp", 443, None)),
            ("udp/53", ("udp", 53, None)),
            ("udp/30000-30010", ("udp", 30000, 30010)),
            ("tcp/1", ("tcp", 1, None)),
            ("tcp/65535", ("tcp", 65535, None)),
            ("tcp/1-65535", ("tcp", 1, 65535)),
            ("TCP/443", ("tcp", 443, None)),
            ("  udp/5000-5001 ", ("udp", 5000, 5001)),
        ],
        ids=["tcp", "udp", "range", "lowest", "highest", "full-range", "upper-case", "padded"],
    )
    def test_valid_spec_parses(self, spec: str, expected: tuple[str, int, int | None]) -> None:
        """Protocol is lower-cased, whitespace stripped, a single port has port_end None."""
        assert PortProfileHelper.parse_port_spec(spec) == expected

    @pytest.mark.parametrize(
        "spec",
        [
            "icmp/1",
            "any/443",
            "tcp/0",
            "tcp/65536",
            "tcp/0443",
            "tcp/443-0450",
            "tcp/0-10",
            "tcp/100-65536",
            "tcp/9000-8000",
            "tcp/443-443",
            "garbage",
            "tcp",
            "tcp/",
            "443",
            "tcp/abc",
            "tcp/443-",
            "tcp/443/udp",
            "tcp/123456",
            "",
            None,
        ],
        ids=[
            "icmp",
            "any-protocol",
            "zero",
            "above-65535",
            "leading-zero",
            "range-end-leading-zero",
            "range-from-zero",
            "range-above-65535",
            "descending-range",
            "empty-range",
            "garbage",
            "protocol-only",
            "missing-port",
            "port-only",
            "non-numeric",
            "open-range",
            "extra-segment",
            "too-many-digits",
            "empty",
            "none",
        ],
    )
    def test_invalid_spec_raises_value_error(self, spec: str | None) -> None:
        """Every malformed spec is rejected rather than guessed at."""
        with pytest.raises(ValueError, match="invalid port"):
            PortProfileHelper.parse_port_spec(spec)


class TestFormatPortSpec:
    @pytest.mark.parametrize(
        ("port", "expected"),
        [(("tcp", 443, None), "tcp/443"), (("udp", 30000, 30010), "udp/30000-30010")],
        ids=["single", "range"],
    )
    def test_format_port_spec(self, port: tuple[str, int, int | None], expected: str) -> None:
        """A PortSpec formats back to the spec it was written as."""
        assert PortProfileHelper.format_port_spec(port) == expected

    @pytest.mark.parametrize("spec", ["tcp/443", "udp/30000-30010", "tcp/1", "udp/1-65535"])
    def test_parse_and_format_round_trip(self, spec: str) -> None:
        """parse_port_spec and format_port_spec are inverses on canonical specs."""
        assert PortProfileHelper.format_port_spec(PortProfileHelper.parse_port_spec(spec)) == spec


class TestResolveDependencyPorts:
    """A dependency opens its own ports, else every port of its target component."""

    def test_dependency_ports_are_used(self) -> None:
        """Explicit dependency ports narrow the target's."""
        dep = {"ports": ["tcp/6379"]}
        target = {"ports": ["tcp/6379", "udp/30000-30010"]}
        assert PortProfileHelper.resolve_dependency_ports(dep, target) == [("tcp", 6379, None)]

    def test_empty_dependency_ports_fall_back_to_target_ports(self) -> None:
        """No dependency ports: every port the target lists, in its order."""
        target = {"ports": ["tcp/8443", "udp/30000-30010"]}
        assert PortProfileHelper.resolve_dependency_ports({"ports": []}, target) == [
            ("tcp", 8443, None),
            ("udp", 30000, 30010),
        ]

    def test_missing_dependency_ports_fall_back_to_target_ports(self) -> None:
        """A dependency with ports None (unset List attribute) also falls back."""
        assert PortProfileHelper.resolve_dependency_ports({"ports": None}, {"ports": ["tcp/443"]}) == [
            ("tcp", 443, None)
        ]

    @pytest.mark.parametrize(
        ("dep", "target"),
        [({}, None), ({"ports": []}, {}), ({"ports": None}, {"ports": None}), ({}, {"ports": []})],
        ids=["no-target", "target-without-ports", "both-none", "both-empty"],
    )
    def test_no_ports_anywhere_resolves_to_empty(self, dep: dict[str, Any], target: dict[str, Any] | None) -> None:
        """Nothing to open is an empty list, which the caller skips — never an any-port rule."""
        assert PortProfileHelper.resolve_dependency_ports(dep, target) == []

    def test_duplicates_are_dropped_keeping_first_order(self) -> None:
        """The same port written twice (case, whitespace) opens one rule."""
        dep = {"ports": ["udp/53", "tcp/443", " TCP/443", "udp/53", "tcp/8443"]}
        assert PortProfileHelper.resolve_dependency_ports(dep) == [
            ("udp", 53, None),
            ("tcp", 443, None),
            ("tcp", 8443, None),
        ]

    def test_malformed_dependency_port_raises(self) -> None:
        """One bad port fails the whole dependency, not just that port."""
        with pytest.raises(ValueError, match="icmp/1"):
            PortProfileHelper.resolve_dependency_ports({"ports": ["tcp/443", "icmp/1"]})

    def test_malformed_target_port_raises_on_fallback(self) -> None:
        """A bad port on the target fails a dependency that falls back to it."""
        with pytest.raises(ValueError, match="tcp/http"):
            PortProfileHelper.resolve_dependency_ports({"ports": []}, {"ports": ["tcp/http"]})

    def test_malformed_target_port_is_ignored_when_dependency_lists_ports(self) -> None:
        """The target's ports are not parsed when the dependency names its own."""
        assert PortProfileHelper.resolve_dependency_ports({"ports": ["tcp/443"]}, {"ports": ["bogus"]}) == [
            ("tcp", 443, None)
        ]

    def test_rules_planner_resolve_ports_delegates(self) -> None:
        """RulesPlanner.resolve_ports is the same resolution the generators call."""
        dep = {"ports": []}
        target = {"ports": ["tcp/5432"]}
        assert RulesPlanner.resolve_ports(dep, target) == PortProfileHelper.resolve_dependency_ports(dep, target)
