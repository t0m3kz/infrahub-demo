"""Tests for device configuration transforms."""

from transforms.common import get_loopbacks


class TestGetLoopbacks:
    """Test loopback extraction from interface data."""

    def test_get_loopbacks_empty_interfaces(self) -> None:
        """Test that empty interfaces list returns empty dict."""
        result = get_loopbacks([])
        assert result == {}

    def test_get_loopbacks_single_loopback(self) -> None:
        """Test extraction of single loopback interface."""
        interfaces = [
            {
                "name": "Loopback0",
                "role": "loopback",
                "ip_addresses": ["1.127.1.3/32"],
                "description": None,
                "status": None,
                "interface_type": None,
                "mtu": None,
                "vlans": [],
            }
        ]
        result = get_loopbacks(interfaces)
        assert result == {"loopback0": "1.127.1.3/32"}

    def test_get_loopbacks_multiple_loopbacks(self) -> None:
        """Test extraction of multiple loopback interfaces."""
        interfaces = [
            {
                "name": "Loopback0",
                "role": "loopback",
                "ip_addresses": ["1.127.1.3/32"],
                "description": None,
                "status": None,
                "interface_type": None,
                "mtu": None,
                "vlans": [],
            },
            {
                "name": "Loopback1",
                "role": "loopback",
                "ip_addresses": ["1.200.1.3/32"],
                "description": None,
                "status": None,
                "interface_type": None,
                "mtu": None,
                "vlans": [],
            },
        ]
        result = get_loopbacks(interfaces)
        assert result == {
            "loopback0": "1.127.1.3/32",
            "loopback1": "1.200.1.3/32",
        }

    def test_get_loopbacks_mixed_interfaces(self) -> None:
        """Test extraction with loopback and non-loopback interfaces."""
        interfaces = [
            {
                "name": "Ethernet1",
                "role": "management",
                "ip_addresses": ["10.0.0.1/24"],
                "description": None,
                "status": None,
                "interface_type": None,
                "mtu": None,
                "vlans": [],
            },
            {
                "name": "Loopback0",
                "role": "loopback",
                "ip_addresses": ["1.127.1.3/32"],
                "description": None,
                "status": None,
                "interface_type": None,
                "mtu": None,
                "vlans": [],
            },
        ]
        result = get_loopbacks(interfaces)
        assert result == {"loopback0": "1.127.1.3/32"}

    def test_get_loopbacks_no_ip_address(self) -> None:
        """Test loopback without IP address returns empty dict."""
        interfaces = [
            {
                "name": "Loopback0",
                "role": "loopback",
                "ip_addresses": [],
                "description": None,
                "status": None,
                "interface_type": None,
                "mtu": None,
                "vlans": [],
            }
        ]
        result = get_loopbacks(interfaces)
        assert result == {}

    def test_get_loopbacks_case_insensitive(self) -> None:
        """Test that loopback names are normalized to lowercase."""
        interfaces = [
            {
                "name": "LOOPBACK0",
                "role": "loopback",
                "ip_addresses": ["1.127.1.3/32"],
                "description": None,
                "status": None,
                "interface_type": None,
                "mtu": None,
                "vlans": [],
            }
        ]
        result = get_loopbacks(interfaces)
        assert "loopback0" in result
        assert result["loopback0"] == "1.127.1.3/32"

    def test_get_loopbacks_non_loopback_role(self) -> None:
        """Test that non-loopback interfaces are skipped."""
        interfaces = [
            {
                "name": "Ethernet1",
                "role": "physical",
                "ip_addresses": ["10.0.0.1/24"],
                "description": None,
                "status": None,
                "interface_type": None,
                "mtu": None,
                "vlans": [],
            }
        ]
        result = get_loopbacks(interfaces)
        assert result == {}
