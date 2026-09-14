"""Unit tests for get_capabilities() transform helper.

Capabilities are derived from device_capabilities (ManagedBGP/ManagedOSPF),
not from separate capability profile nodes.
"""

import pytest

from transforms.common import get_capabilities


class TestGetCapabilities:
    @pytest.mark.parametrize(
        "data,bgp,ospf",
        [
            ({}, False, False),
            ({"name": "leaf-1"}, False, False),
            ({"capabilities": None}, False, False),
            ({"capabilities": [{"typename": "ManagedBGP", "name": "bgp"}]}, True, False),
            ({"capabilities": [{"typename": "ManagedOSPF", "name": "ospf"}]}, False, True),
            (
                {
                    "capabilities": [
                        {"typename": "ManagedBGP", "name": "bgp"},
                        {"typename": "ManagedOSPF", "name": "ospf"},
                    ]
                },
                True,
                True,
            ),
            ({"capabilities": [{"typename": "ManagedDHCP", "name": "dhcp"}]}, False, False),
        ],
    )
    def test_get_capabilities(self, data: dict, bgp: bool, ospf: bool) -> None:
        result = get_capabilities(data)
        assert result["bgp_enabled"] is bgp
        assert result["ospf_enabled"] is ospf
