"""CheckCircuitPlacement: what the one session kind may carry per ``circuit_type``.

The schema leaves ip_namespace, password, cloud_endpoint and registered_prefixes optional on
TopologySessionCircuit, so the check holds them per circuit_type (handoff requires ip_namespace and refuses an owner;
dci and tunnel refuse the handoff-only fields; only a tunnel has an owner and registered_prefixes). Payload builders:
tests/unit/circuit_placement_check_helpers.py.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from checks.circuit_placement import CheckCircuitPlacement
from tests.unit.circuit_placement_check_helpers import DCI, HANDOFF, TUNNEL, _edges, _owner, _run

HANDOFF_ONLY: dict[str, Any] = {
    "ip_namespace": {"node": {"name": {"value": "PROD"}, "namespace_type": {"value": "prod"}}},
    "password": {"node": {"id": "key-1"}},
    "cloud_endpoint": {"node": {"id": "vif-1", "name": {"value": "VIF-1"}}},
}


class TestHandoffOnlyFields:
    """A dci and a tunnel set none of the handoff fields."""

    @pytest.mark.parametrize("circuit", [DCI, TUNNEL], ids=["dci", "tunnel"])
    @pytest.mark.parametrize("field", sorted(HANDOFF_ONLY))
    def test_a_handoff_field_on_another_type_fails(self, circuit: dict[str, Any], field: str) -> None:
        """Each of the four fields is refused on its own, naming the field."""
        noun = "Interconnect" if circuit is DCI else "Tunnel"
        messages = _run(
            interconnects=[{**circuit, field: HANDOFF_ONLY[field]}] if circuit is DCI else None,
            tunnels=[{**circuit, field: HANDOFF_ONLY[field]}] if circuit is TUNNEL else None,
        )
        assert len(messages) == 1
        assert messages[0].startswith(f"{noun} circuit") and f"sets {field}" in messages[0]

    @pytest.mark.parametrize("circuit", [DCI, TUNNEL], ids=["dci", "tunnel"])
    def test_a_circuit_without_them_passes(self, circuit: dict[str, Any]) -> None:
        """The reference circuits carry none of them (a null relationship is not set)."""
        clean = {
            **circuit,
            "ip_namespace": {"node": None},
            "password": {"node": None},
            "cloud_endpoint": {"node": None},
        }
        assert (
            _run(interconnects=[clean] if circuit is DCI else None, tunnels=[clean] if circuit is TUNNEL else None)
            == []
        )


class TestTunnelOnlyFields:
    """registered_prefixes and an owner belong to a tunnel."""

    def test_a_tunnel_may_register_prefixes(self) -> None:
        """A tunnel carries registered_prefixes."""
        assert _run(tunnels=[{**TUNNEL, "registered_prefixes": _edges([{"id": "pfx-1"}])}]) == []

    def test_a_dci_with_registered_prefixes_fails(self) -> None:
        """A DCI is underlay: it registers no prefix."""
        messages = _run(interconnects=[{**DCI, "registered_prefixes": _edges([{"id": "pfx-1"}])}])
        assert len(messages) == 1 and "sets registered_prefixes" in messages[0]

    def test_an_owned_dci_fails(self) -> None:
        """A DCI is shared platform, unowned."""
        messages = _run(interconnects=[{**DCI, "owner": _owner("C005")}])
        assert len(messages) == 1 and "owned by 'C005'" in messages[0]


class TestCircuitTypeValue:
    """A session circuit has one of the three circuit types."""

    def test_an_unknown_circuit_type_is_reported_against_the_session_kind(self) -> None:
        """A circuit_type outside dci, handoff and tunnel is one finding and no other rule runs on it."""
        check = cast(Any, CheckCircuitPlacement.__new__(CheckCircuitPlacement))
        findings: list[dict[str, Any]] = []
        check.log_error = lambda **kwargs: findings.append(kwargs)
        check.validate({"TopologySessionCircuit": _edges([{**HANDOFF, "circuit_type": {"value": "vpn"}}])})
        assert len(findings) == 1
        assert findings[0]["object_type"] == "TopologySessionCircuit" and "'vpn'" in findings[0]["message"]

    def test_findings_name_the_kind_not_the_type(self) -> None:
        """A finding on a session circuit is logged against TopologySessionCircuit, on a carrier circuit against its kind."""
        check = cast(Any, CheckCircuitPlacement.__new__(CheckCircuitPlacement))
        findings: list[dict[str, Any]] = []
        check.log_error = lambda **kwargs: findings.append(kwargs)
        check.validate({"TopologySessionCircuit": _edges([{**DCI, "owner": _owner("C005")}])})
        assert [f["object_type"] for f in findings] == ["TopologySessionCircuit"]
