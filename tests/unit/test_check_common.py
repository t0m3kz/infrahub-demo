"""Unit tests for helper validators in checks.common."""

from __future__ import annotations

from checks.common import (
    validate_interfaces,
    validate_management_services,
    validate_routing_password,
)


class TestValidateInterfaces:
    def test_no_interfaces_returns_error(self) -> None:
        """Device without interfaces should fail validation."""
        errors = validate_interfaces({"interfaces": []})
        assert len(errors) == 1
        assert "no interfaces" in errors[0].lower()

    def test_loopback_without_ip_returns_error(self) -> None:
        """Loopback must have at least one IP field set."""
        errors = validate_interfaces(
            {
                "interfaces": [
                    {
                        "name": "Loopback0",
                        "role": "loopback",
                        "ip_addresses": [],
                    }
                ]
            }
        )
        assert len(errors) == 1
        assert "Loopback0" in errors[0]

    def test_non_loopback_without_ip_is_allowed(self) -> None:
        """Only loopback interfaces are checked for IP presence."""
        errors = validate_interfaces(
            {
                "interfaces": [
                    {
                        "name": "Ethernet1",
                        "role": "access",
                    }
                ]
            }
        )
        assert errors == []


class TestValidateManagementServices:
    def test_missing_required_capabilities_returns_errors(self) -> None:
        """AAA, NTP and Syslog are mandatory management capabilities."""
        errors = validate_management_services({"capabilities": []})
        assert len(errors) == 3
        assert any("ManagedAAA" in error for error in errors)
        assert any("ManagedNTP" in error for error in errors)
        assert any("ManagedSyslog" in error for error in errors)

    def test_capability_without_servers_returns_error(self) -> None:
        """Present capability with empty servers list should fail."""
        errors = validate_management_services(
            {
                "capabilities": [
                    {"typename": "ManagedAAA", "servers": []},
                    {"typename": "ManagedNTP", "servers": [{"host": "1.1.1.1"}]},
                    {"typename": "ManagedSyslog", "servers": [{"host": "2.2.2.2"}]},
                ]
            }
        )
        assert len(errors) == 1
        assert "ManagedAAA has no servers" in errors[0]

    def test_all_required_services_with_servers_pass(self) -> None:
        """All required management capabilities with servers should pass."""
        errors = validate_management_services(
            {
                "capabilities": [
                    {"typename": "ManagedAAA", "servers": [{"host": "1.1.1.1"}]},
                    {"typename": "ManagedNTP", "servers": [{"host": "2.2.2.2"}]},
                    {"typename": "ManagedSyslog", "servers": [{"host": "3.3.3.3"}]},
                ]
            }
        )
        assert errors == []


class TestValidateRoutingPassword:
    def test_missing_bgp_and_ospf_passwords_return_errors(self) -> None:
        """Both BGP peerings and OSPF interface configs require passwords."""
        errors = validate_routing_password(
            {
                "capabilities": [
                    {
                        "typename": "ManagedBGP",
                        "peerings": [
                            {"name": "underlay-peer-1", "password": ""},
                        ],
                    }
                ],
                "interfaces": [
                    {
                        "name": "Ethernet1",
                        "interface_capabilities": [
                            {"typename": "RoutingOSPFInterface", "password": ""},
                        ],
                    }
                ],
            }
        )
        assert len(errors) == 2
        assert any("BGP peering 'underlay-peer-1'" in error for error in errors)
        assert any("OSPF interface config on 'Ethernet1'" in error for error in errors)

    def test_valid_passwords_pass(self) -> None:
        """No findings when all routing auth secrets are set."""
        errors = validate_routing_password(
            {
                "capabilities": [
                    {
                        "typename": "ManagedBGP",
                        "peerings": [
                            {"name": "overlay-peer-1", "password": "set"},
                        ],
                    }
                ],
                "interfaces": [
                    {
                        "name": "Ethernet2",
                        "interface_capabilities": [
                            {"typename": "RoutingOSPFInterface", "password": "set"},
                        ],
                    }
                ],
            }
        )
        assert errors == []
