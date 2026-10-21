"""Unit tests for CheckPhysicalCircuitPorts (the ports a physical circuit terminates on).

The payloads are raw GraphQL responses shaped like queries/validation/physical_circuit_ports.gql;
``validate`` runs them through ``clean_data``.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from checks.physical_circuit_ports import CheckPhysicalCircuitPorts


def _check() -> Any:
    """CheckPhysicalCircuitPorts with logging captured instead of sent to Infrahub."""
    check = cast(Any, CheckPhysicalCircuitPorts.__new__(CheckPhysicalCircuitPorts))
    findings: list[dict[str, Any]] = []
    check._captured = findings
    check.log_error = lambda **kwargs: findings.append(kwargs)
    return check


def _port(port_id: str, device: str, name: str, typename: str = "DcimPhysicalInterface") -> dict[str, dict[str, Any]]:
    """One end of a circuit, as the query selects it."""
    return {
        "node": {
            "id": port_id,
            "__typename": typename,
            "name": {"value": name},
            "device": {"node": {"name": {"value": device}}},
        }
    }


def _circuit(circuit_id: str, *ends: dict[str, Any]) -> dict[str, Any]:
    """One circuit row with its ends."""
    return {
        "node": {
            "id": f"id-{circuit_id}",
            "circuit_id": {"value": circuit_id},
            "capabilities": {"edges": list(ends)},
        }
    }


def _run(*circuits: dict[str, Any]) -> list[dict[str, Any]]:
    """Validate circuits and return the logged findings."""
    check = _check()
    check.validate({"TopologyPhysicalCircuit": {"edges": list(circuits)}})
    return check._captured


BL = _port("p-bl", "bl-dc101101", "Ethernet1/35")
EG = _port("p-eg", "eg-fr201", "Ethernet1/10")
BL2 = _port("p-bl2", "bl-dc101101", "Ethernet1/36")
EG2 = _port("p-eg2", "eg-fr202", "Ethernet1/10")


class TestCleanInventory:
    def test_empty_response_is_silent(self) -> None:
        """No physical circuit at all is nothing to validate."""
        check = _check()
        check.validate({})
        assert check._captured == []

    def test_a_circuit_with_two_physical_ends_is_silent(self) -> None:
        assert _run(_circuit("DF-DC10-EQXFR2", BL, EG)) == []

    def test_two_circuits_on_distinct_ports_are_each_fine(self) -> None:
        assert _run(_circuit("DF-A", BL, EG), _circuit("DF-B", BL2, EG2)) == []


class TestSharedPort:
    def test_a_port_that_ends_two_circuits_is_one_finding_naming_both(self) -> None:
        """The port and both circuits are named, and the finding points at the port."""
        findings = _run(_circuit("DF-A", BL, EG), _circuit("DF-B", BL, EG2))

        assert len(findings) == 1
        assert "bl-dc101101:Ethernet1/35" in findings[0]["message"]
        assert "DF-A" in findings[0]["message"] and "DF-B" in findings[0]["message"]
        assert (findings[0]["object_id"], findings[0]["object_type"]) == ("p-bl", "DcimPhysicalInterface")

    def test_each_shared_port_is_its_own_finding(self) -> None:
        findings = _run(_circuit("DF-A", BL, EG), _circuit("DF-B", BL, EG))

        assert sorted(f["object_id"] for f in findings) == ["p-bl", "p-eg"]


class TestEndKind:
    @pytest.mark.parametrize("typename", ["DcimVirtualInterface", "DcimLAGInterface", "TopologyVirtualCircuit"])
    def test_an_end_that_is_not_a_physical_interface_is_an_error(self, typename: str) -> None:
        findings = _run(_circuit("DF-A", BL, _port("p-v", "eg-fr201", "Vlan10", typename)))

        assert len(findings) == 1
        assert "DF-A" in findings[0]["message"] and typename in findings[0]["message"]
        assert findings[0]["object_type"] == "TopologyPhysicalCircuit"

    def test_a_non_physical_end_is_not_counted_as_a_shared_port(self) -> None:
        """The same virtual port on two circuits is reported once per circuit as the wrong kind, not as shared."""
        virtual = _port("p-v", "eg-fr201", "Vlan10", "DcimVirtualInterface")

        findings = _run(_circuit("DF-A", BL, virtual), _circuit("DF-B", EG, virtual))

        assert len(findings) == 2
        assert not any("terminates at most one" in f["message"] for f in findings)


class TestEndCount:
    def test_a_one_ended_circuit_is_an_error(self) -> None:
        """The schema cap does not see an interface-side write or a deleted device's port."""
        findings = _run(_circuit("INET-C001", BL))

        assert len(findings) == 1
        assert "INET-C001" in findings[0]["message"] and "1 end(s)" in findings[0]["message"]
        assert (findings[0]["object_id"], findings[0]["object_type"]) == ("id-INET-C001", "TopologyPhysicalCircuit")

    def test_a_circuit_without_ends_is_an_error(self) -> None:
        findings = _run(_circuit("INET-C001"))

        assert len(findings) == 1 and "0 end(s)" in findings[0]["message"]

    def test_a_three_ended_circuit_is_an_error(self) -> None:
        findings = _run(_circuit("DF-A", BL, EG, BL2))

        assert len(findings) == 1 and "3 end(s)" in findings[0]["message"]

    def test_a_circuit_without_circuit_id_is_named_by_its_object_id(self) -> None:
        row = _circuit("ignored", BL)
        row["node"]["circuit_id"] = {"value": None}

        findings = _run(row)

        assert len(findings) == 1 and "id-ignored" in findings[0]["message"]
