"""Unit tests for OSPF configuration helpers.

Covers:
- get_ospf() — process extraction, router_id/process_id/reference_bandwidth passthrough
"""

from __future__ import annotations

from transforms.helpers.ospf import get_ospf


def _ospf_service(
    *,
    process_id: str = "1",
    router_id: str = "10.0.0.1/32",
    reference_bandwidth: int = 100000,
) -> dict:
    return {
        "typename": "ManagedOSPF",
        "process_id": process_id,
        "router_id": {"address": router_id},
        "reference_bandwidth": reference_bandwidth,
    }


class TestGetOspfNoServices:
    def test_empty_capabilities_returns_empty_list(self) -> None:
        assert get_ospf([]) == []

    def test_no_ospf_typename_returns_empty_list(self) -> None:
        caps = [{"typename": "ManagedBGP"}, {"typename": "ManagedMLAG"}]
        assert get_ospf(caps) == []


class TestGetOspfExtraction:
    def test_returns_process_id(self) -> None:
        result = get_ospf([_ospf_service(process_id="42")])
        assert result[0]["process_id"] == "42"

    def test_returns_router_id_address_without_prefix_stripping(self) -> None:
        """router_id in the returned config is the raw address (prefix stripped in templates)."""
        result = get_ospf([_ospf_service(router_id="10.0.0.5/32")])
        assert result[0]["router_id"] == "10.0.0.5/32"

    def test_returns_reference_bandwidth(self) -> None:
        result = get_ospf([_ospf_service(reference_bandwidth=40000)])
        assert result[0]["reference_bandwidth"] == 40000

    def test_multiple_services_sorted_by_process_id(self) -> None:
        services = [
            _ospf_service(process_id="2", router_id="10.0.0.2/32"),
            _ospf_service(process_id="1", router_id="10.0.0.1/32"),
        ]
        result = get_ospf(services)
        assert [c["process_id"] for c in result] == ["1", "2"]

    def test_non_ospf_capabilities_ignored(self) -> None:
        caps = [
            {"typename": "ManagedBGP"},
            _ospf_service(process_id="1"),
        ]
        result = get_ospf(caps)
        assert len(result) == 1
        assert result[0]["process_id"] == "1"


class TestGetOspfReferenceBandwidthFromInterfaces:
    """reference_bandwidth is derived from the fastest physical interface when available."""

    def test_derived_from_fastest_interface_overrides_schema_default(self) -> None:
        interfaces = [
            {"interface_type": "100gbase-x-qsfp28"},
            {"interface_type": "25gbase-x-sfp28"},
        ]
        result = get_ospf([_ospf_service(reference_bandwidth=100000)], interfaces)
        assert result[0]["reference_bandwidth"] == 100000  # 100 Gbps -> 100000 Mbps

    def test_400g_interface_overrides_schema_default(self) -> None:
        interfaces = [{"interface_type": "400gbase-x-qsfpdd"}]
        result = get_ospf([_ospf_service(reference_bandwidth=100000)], interfaces)
        assert result[0]["reference_bandwidth"] == 400000

    def test_no_interfaces_falls_back_to_schema_value(self) -> None:
        result = get_ospf([_ospf_service(reference_bandwidth=40000)], [])
        assert result[0]["reference_bandwidth"] == 40000

    def test_none_interfaces_falls_back_to_schema_value(self) -> None:
        result = get_ospf([_ospf_service(reference_bandwidth=40000)], None)
        assert result[0]["reference_bandwidth"] == 40000

    def test_interfaces_without_recognizable_speed_fall_back_to_schema_value(self) -> None:
        interfaces = [{"interface_type": "other"}, {"interface_type": None}]
        result = get_ospf([_ospf_service(reference_bandwidth=40000)], interfaces)
        assert result[0]["reference_bandwidth"] == 40000
