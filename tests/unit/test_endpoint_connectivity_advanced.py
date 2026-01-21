"""Unit tests for endpoint connectivity generator advanced features.

Tests:
- ConnectionFingerprint uniqueness and hashing
- InterfaceSpeedMatcher speed extraction and grouping (general-purpose utility)
- ConnectionValidator validation logic (endpoint-specific)
- Connection plan building
"""

from dataclasses import FrozenInstanceError

import pytest

from generators.helpers import ConnectionValidator, InterfaceSpeedMatcher
from generators.models import ConnectionFingerprint


class TestConnectionFingerprint:
    """Test ConnectionFingerprint dataclass behavior."""

    def test_create_fingerprint(self) -> None:
        """Test creating a connection fingerprint."""
        fp = ConnectionFingerprint(
            server_name="server-01",
            server_interface="eth0",
            switch_name="tor-1",
            switch_interface="Ethernet1/1",
        )

        assert fp.server_name == "server-01"
        assert fp.server_interface == "eth0"
        assert fp.switch_name == "tor-1"
        assert fp.switch_interface == "Ethernet1/1"

    def test_fingerprint_equality(self) -> None:
        """Test that identical fingerprints are equal."""
        fp1 = ConnectionFingerprint("server-01", "eth0", "tor-1", "Ethernet1/1")
        fp2 = ConnectionFingerprint("server-01", "eth0", "tor-1", "Ethernet1/1")

        assert fp1 == fp2
        assert hash(fp1) == hash(fp2)

    def test_fingerprint_uniqueness(self) -> None:
        """Test that different fingerprints are unique."""
        fp1 = ConnectionFingerprint("server-01", "eth0", "tor-1", "Ethernet1/1")
        fp2 = ConnectionFingerprint("server-01", "eth1", "tor-1", "Ethernet1/1")  # Different interface
        fp3 = ConnectionFingerprint("server-01", "eth0", "tor-2", "Ethernet1/1")  # Different switch

        assert fp1 != fp2
        assert fp1 != fp3
        assert fp2 != fp3
        assert len({fp1, fp2, fp3}) == 3  # All unique in set

    def test_fingerprint_frozen(self) -> None:
        """Test that fingerprints are immutable."""
        fp = ConnectionFingerprint("server-01", "eth0", "tor-1", "Ethernet1/1")

        with pytest.raises(FrozenInstanceError):
            fp.server_name = "server-02"  # type: ignore

    def test_fingerprint_repr(self) -> None:
        """Test string representation."""
        fp = ConnectionFingerprint("server-01", "eth0", "tor-1", "Ethernet1/1")
        repr_str = repr(fp)

        assert "server-01:eth0" in repr_str
        assert "tor-1:Ethernet1/1" in repr_str
        assert "→" in repr_str


class TestInterfaceSpeedMatcher:
    """Test InterfaceSpeedMatcher speed extraction and grouping."""

    @pytest.mark.parametrize(
        "interface_type,expected_speed",
        [
            ("100gbase-x-qsfp28", 100),
            ("25gbase-x-sfp28", 25),
            ("10gbase-t", 10),
            ("40gbase-x-qsfpplus", 40),
            ("1000base-t", None),  # Should match as 1000, but pattern looks for XgbaseNone),  # No match
            ("unknown-type", None),
        ],
    )
    def test_extract_speed(self, interface_type: str, expected_speed: int | None) -> None:
        """Test speed extraction from interface types."""
        speed = InterfaceSpeedMatcher.extract_speed(interface_type)
        assert speed == expected_speed

    def test_extract_speed_case_insensitive(self) -> None:
        """Test speed extraction is case-insensitive."""
        assert InterfaceSpeedMatcher.extract_speed("100GBASE-X-QSFP28") == 100
        assert InterfaceSpeedMatcher.extract_speed("25GBase-X-SFP28") == 25
        assert InterfaceSpeedMatcher.extract_speed("10gBaSe-T") == 10

    def test_group_by_speed_empty(self) -> None:
        """Test grouping with empty inputs."""
        groups = InterfaceSpeedMatcher.group_by_speed([], [])
        assert groups == {}

    def test_group_by_speed_single_speed(self) -> None:
        """Test grouping with single speed group."""

        # Mock server interfaces
        class MockServerInterface:
            def __init__(self, name: str, interface_type: str):
                self.name = name
                self.interface_type = interface_type

        # Mock switch interfaces
        class MockSwitchInterface:
            def __init__(self, name: str, interface_type: str):
                self.name = name
                self.interface_type = MockAttribute(interface_type)

        class MockAttribute:
            def __init__(self, value: str):
                self.value = value

        server_intfs = [
            MockServerInterface("eth0", "100gbase-x-qsfp28"),
            MockServerInterface("eth1", "100gbase-x-qsfp28"),
        ]

        switch_intfs = [
            MockSwitchInterface("Ethernet1/1", "100gbase-x-qsfp28"),
            MockSwitchInterface("Ethernet1/2", "100gbase-x-qsfp28"),
        ]

        groups = InterfaceSpeedMatcher.group_by_speed(server_intfs, switch_intfs)

        assert len(groups) == 1
        assert 100 in groups
        server_group, switch_group = groups[100]
        assert len(server_group) == 2
        assert len(switch_group) == 2

    def test_group_by_speed_mixed_speeds(self) -> None:
        """Test grouping with multiple speed groups."""

        class MockServerInterface:
            def __init__(self, name: str, interface_type: str):
                self.name = name
                self.interface_type = interface_type

        class MockSwitchInterface:
            def __init__(self, name: str, interface_type: str):
                self.name = name
                self.interface_type = MockAttribute(interface_type)

        class MockAttribute:
            def __init__(self, value: str):
                self.value = value

        server_intfs = [
            MockServerInterface("eth0", "100gbase-x-qsfp28"),
            MockServerInterface("eth1", "100gbase-x-qsfp28"),
            MockServerInterface("eth2", "25gbase-x-sfp28"),
            MockServerInterface("eth3", "25gbase-x-sfp28"),
        ]

        switch_intfs = [
            MockSwitchInterface("Ethernet1/1", "100gbase-x-qsfp28"),
            MockSwitchInterface("Ethernet1/2", "100gbase-x-qsfp28"),
            MockSwitchInterface("Ethernet1/9", "25gbase-x-sfp28"),
            MockSwitchInterface("Ethernet1/10", "25gbase-x-sfp28"),
        ]

        groups = InterfaceSpeedMatcher.group_by_speed(server_intfs, switch_intfs)

        assert len(groups) == 2
        assert 100 in groups
        assert 25 in groups

        # Check 100G group
        server_100g, switch_100g = groups[100]
        assert len(server_100g) == 2
        assert len(switch_100g) == 2
        assert all("100gbase" in intf.interface_type for intf in server_100g)

        # Check 25G group
        server_25g, switch_25g = groups[25]
        assert len(server_25g) == 2
        assert len(switch_25g) == 2
        assert all("25gbase" in intf.interface_type for intf in server_25g)

    def test_group_by_speed_mismatched_speeds(self) -> None:
        """Test that only matching speeds are grouped."""

        class MockServerInterface:
            def __init__(self, name: str, interface_type: str):
                self.name = name
                self.interface_type = interface_type

        class MockSwitchInterface:
            def __init__(self, name: str, interface_type: str):
                self.name = name
                self.interface_type = MockAttribute(interface_type)

        class MockAttribute:
            def __init__(self, value: str):
                self.value = value

        # Servers have 100G, switches have 25G
        server_intfs = [
            MockServerInterface("eth0", "100gbase-x-qsfp28"),
            MockServerInterface("eth1", "100gbase-x-qsfp28"),
        ]

        switch_intfs = [
            MockSwitchInterface("Ethernet1/1", "25gbase-x-sfp28"),
            MockSwitchInterface("Ethernet1/2", "25gbase-x-sfp28"),
        ]

        groups = InterfaceSpeedMatcher.group_by_speed(server_intfs, switch_intfs)

        # No matching speed groups
        assert groups == {}


class TestConnectionValidator:
    """Test ConnectionValidator validation logic."""

    def test_validate_plan_success(self) -> None:
        """Test validation of valid connection plan."""
        plan = [
            ConnectionFingerprint("server-01", "eth0", "tor-1", "Ethernet1/1"),
            ConnectionFingerprint("server-01", "eth1", "tor-2", "Ethernet1/2"),  # Different switch interface
        ]

        is_valid, message = ConnectionValidator.validate_plan(plan, min_connections=2)

        assert is_valid is True
        assert "2 connections" in message

    def test_validate_plan_insufficient_connections(self) -> None:
        """Test validation fails with insufficient connections."""
        plan = [
            ConnectionFingerprint("server-01", "eth0", "tor-1", "Ethernet1/1"),
        ]

        is_valid, message = ConnectionValidator.validate_plan(plan, min_connections=2)

        assert is_valid is False
        assert "Insufficient connections" in message
        assert "1 < 2" in message

    def test_validate_plan_too_many_connections(self) -> None:
        """Test validation fails with too many connections."""
        plan = [
            ConnectionFingerprint("server-01", "eth0", "tor-1", "Ethernet1/1"),
            ConnectionFingerprint("server-01", "eth1", "tor-2", "Ethernet1/1"),
            ConnectionFingerprint("server-01", "eth2", "tor-1", "Ethernet1/2"),
            ConnectionFingerprint("server-01", "eth3", "tor-2", "Ethernet1/2"),
            ConnectionFingerprint("server-01", "eth4", "tor-1", "Ethernet1/3"),
        ]

        is_valid, message = ConnectionValidator.validate_plan(plan, max_connections=4)

        assert is_valid is False
        assert "Too many connections" in message
        assert "5 > 4" in message

    def test_validate_plan_duplicate_server_interfaces(self) -> None:
        """Test validation detects duplicate server interfaces."""
        plan = [
            ConnectionFingerprint("server-01", "eth0", "tor-1", "Ethernet1/1"),
            ConnectionFingerprint("server-01", "eth0", "tor-2", "Ethernet1/2"),  # Duplicate eth0
        ]

        is_valid, message = ConnectionValidator.validate_plan(plan)

        assert is_valid is False
        assert "Duplicate server interfaces" in message
        assert "eth0" in message

    def test_validate_plan_duplicate_switch_interfaces(self) -> None:
        """Test validation detects duplicate switch interfaces."""
        plan = [
            ConnectionFingerprint("server-01", "eth0", "tor-1", "Ethernet1/1"),
            ConnectionFingerprint("server-01", "eth1", "tor-1", "Ethernet1/1"),  # Duplicate Ethernet1/1
        ]

        is_valid, message = ConnectionValidator.validate_plan(plan)

        assert is_valid is False
        assert "Duplicate switch endpoints detected" in message
        assert "Ethernet1/1" in message

    def test_validate_plan_default_min_connections(self) -> None:
        """Test default minimum connections is 2 (dual-homing)."""
        plan = [
            ConnectionFingerprint("server-01", "eth0", "tor-1", "Ethernet1/1"),
        ]

        is_valid, message = ConnectionValidator.validate_plan(plan)  # No min_connections specified

        assert is_valid is False  # Default min_connections=2

    def test_validate_plan_no_max_limit(self) -> None:
        """Test validation succeeds when no max limit specified."""
        plan = [
            ConnectionFingerprint("server-01", f"eth{i}", f"tor-{i % 2 + 1}", f"Ethernet1/{i + 1}") for i in range(10)
        ]

        is_valid, message = ConnectionValidator.validate_plan(plan, min_connections=2)

        assert is_valid is True
        assert "10 connections" in message


class TestConnectionPlanBuilding:
    """Test connection plan building logic (integration with generator)."""

    def test_plan_deduplication(self) -> None:
        """Test that adding duplicate fingerprints to set works correctly."""
        planned_connections: set[ConnectionFingerprint] = set()

        fp1 = ConnectionFingerprint("server-01", "eth0", "tor-1", "Ethernet1/1")
        fp2 = ConnectionFingerprint("server-01", "eth0", "tor-1", "Ethernet1/1")  # Duplicate
        fp3 = ConnectionFingerprint("server-01", "eth1", "tor-2", "Ethernet1/1")  # Different

        planned_connections.add(fp1)
        assert len(planned_connections) == 1

        planned_connections.add(fp2)  # Should not add duplicate
        assert len(planned_connections) == 1

        planned_connections.add(fp3)  # Should add different connection
        assert len(planned_connections) == 2

    def test_plan_idempotency_check(self) -> None:
        """Test checking if connection already planned."""
        planned_connections: set[ConnectionFingerprint] = {
            ConnectionFingerprint("server-01", "eth0", "tor-1", "Ethernet1/1"),
            ConnectionFingerprint("server-01", "eth1", "tor-2", "Ethernet1/1"),
        }

        # Check existing connection
        new_fp = ConnectionFingerprint("server-01", "eth0", "tor-1", "Ethernet1/1")
        assert new_fp in planned_connections

        # Check non-existing connection
        new_fp2 = ConnectionFingerprint("server-01", "eth2", "tor-1", "Ethernet1/2")
        assert new_fp2 not in planned_connections
