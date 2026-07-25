"""Unit tests for SecurityIntentRule validation check."""

from __future__ import annotations

from typing import Any

from checks.security_intent_rule import CheckSecurityIntentRule


def _check() -> Any:
    check = CheckSecurityIntentRule.__new__(CheckSecurityIntentRule)
    errors: list[str] = []
    check._captured_errors = errors
    check.log_error = lambda message: errors.append(message)
    return check


def _node(**overrides: Any) -> dict[str, Any]:
    base = {
        "name": {"value": "intent-1"},
        "match_mode": {"value": "ip_exact"},
        "protocol": {"value": "tcp"},
        "port_start": {"value": 443},
        "port_end": {"value": None},
        "policy": {"node": {"id": "p1"}},
        "source_zone": {"node": None},
        "destination_zone": {"node": None},
        "source_tag": {"node": None},
        "destination_tag": {"node": None},
        "source_segments": {"edges": []},
        "destination_segments": {"edges": []},
        "source_ip_addresses": {"edges": [{"node": {"id": "ip1"}}]},
        "destination_ip_addresses": {"edges": [{"node": {"id": "ip2"}}]},
        "source_prefixes": {"edges": []},
        "destination_prefixes": {"edges": []},
    }
    base.update(overrides)
    return base


def _payload(node: dict[str, Any]) -> dict[str, Any]:
    return {"SecurityIntentRule": {"edges": [{"node": node}]}}


class TestSecurityIntentRuleCheck:
    def test_ip_exact_missing_destination_ip_fails(self) -> None:
        check = _check()
        node = _node(destination_ip_addresses={"edges": []})

        check.validate(_payload(node))

        assert any("match_mode=ip_exact" in err for err in check._captured_errors)

    def test_label_mode_requires_both_tags(self) -> None:
        check = _check()
        node = _node(
            match_mode={"value": "label"},
            source_ip_addresses={"edges": []},
            destination_ip_addresses={"edges": []},
            source_tag={"node": {"id": "t1"}},
            destination_tag={"node": None},
        )

        check.validate(_payload(node))

        assert any("match_mode=label" in err for err in check._captured_errors)

    def test_hybrid_with_selector_on_each_side_passes(self) -> None:
        check = _check()
        node = _node(
            match_mode={"value": "hybrid"},
            source_ip_addresses={"edges": []},
            destination_ip_addresses={"edges": []},
            source_segments={"edges": [{"node": {"id": "s1"}}]},
            destination_tag={"node": {"id": "t2"}},
        )

        check.validate(_payload(node))

        assert check._captured_errors == []

    def test_protocol_any_with_ports_fails(self) -> None:
        check = _check()
        node = _node(protocol={"value": "any"}, port_start={"value": 53})

        check.validate(_payload(node))

        assert any("protocol=any" in err for err in check._captured_errors)

    def test_port_end_without_start_fails(self) -> None:
        check = _check()
        node = _node(port_start={"value": None}, port_end={"value": 8443})

        check.validate(_payload(node))

        assert any("port_end without port_start" in err for err in check._captured_errors)
