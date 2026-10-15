"""Unit tests for the "outside" address space and the unresolved-endpoint rule check."""

from __future__ import annotations

import ipaddress

import pytest

from transforms.helpers.policy import is_unresolved_segment_endpoint
from utils.exchange_transit import is_public_address
from utils.internet_space import NON_INTERNET_PREFIXES


class TestNonInternetPrefixes:
    def test_every_entry_is_a_network(self) -> None:
        for prefix in NON_INTERNET_PREFIXES:
            ipaddress.ip_network(prefix)

    @pytest.mark.parametrize(
        "address",
        [
            "10.1.2.3",
            "172.16.0.1",
            "192.168.1.1",
            "100.66.32.4",
            "100.64.0.1",
            "169.254.1.1",
            "127.0.0.1",
            "224.0.0.5",
            "198.18.0.1",
            "198.19.255.254",
        ],
    )
    def test_internal_addresses_are_covered(self, address: str) -> None:
        assert any(ipaddress.ip_address(address) in ipaddress.ip_network(p) for p in NON_INTERNET_PREFIXES)

    @pytest.mark.parametrize("address", ["8.8.8.8", "192.0.2.10", "198.51.100.7", "203.0.113.1", "1.1.1.1"])
    def test_public_and_documentation_addresses_are_not_covered(self, address: str) -> None:
        assert not any(ipaddress.ip_address(address) in ipaddress.ip_network(p) for p in NON_INTERNET_PREFIXES)

    @pytest.mark.parametrize(
        "address", ["198.18.0.1", "198.19.1.1", "100.64.0.1", "100.66.32.5", "10.0.0.1", "192.0.0.9"]
    )
    def test_snat_check_and_leaf_acl_agree_on_internal_space(self, address: str) -> None:
        """One list: an address the leaf ACL treats as internal is never an acceptable SNAT address."""
        assert any(ipaddress.ip_address(address) in ipaddress.ip_network(p) for p in NON_INTERNET_PREFIXES)
        assert not is_public_address(address)

    @pytest.mark.parametrize("address", ["8.8.8.8", "192.0.2.10", "198.51.100.7", "203.0.113.1", "198.20.0.1"])
    def test_snat_check_accepts_what_the_leaf_acl_calls_internet(self, address: str) -> None:
        assert not any(ipaddress.ip_address(address) in ipaddress.ip_network(p) for p in NON_INTERNET_PREFIXES)
        assert is_public_address(address)

    def test_there_is_a_single_non_public_definition(self) -> None:
        """exchange_transit re-uses the constant instead of keeping a second list."""
        import utils.exchange_transit as exchange_transit

        assert exchange_transit.NON_INTERNET_PREFIXES is NON_INTERNET_PREFIXES
        assert not hasattr(exchange_transit, "NON_PUBLIC_PREFIXES")


_INTERNET = {"id": "inet", "security_zone": {"name": "INTERNET-ZONE"}}


class TestIsUnresolvedSegmentEndpoint:
    def test_segment_without_prefix_is_unresolved(self) -> None:
        assert is_unresolved_segment_endpoint({"destination_segment": _INTERNET}, "destination")

    def test_zoned_elsewhere_prefix_less_segment_is_not_the_internet(self) -> None:
        """A cloud segment (cidr_block, no gateway prefix) must not be taken for the outside."""
        cloud = {"id": "c", "security_zone": {"name": "PROD-ZONE"}}
        assert not is_unresolved_segment_endpoint({"destination_segment": cloud}, "destination")
        assert not is_unresolved_segment_endpoint({"destination_segment": {"id": "c"}}, "destination")

    def test_segment_with_prefix_is_resolved(self) -> None:
        segment = {"id": "x", "gateway": {"ip_prefix": {"prefix": "10.0.0.0/24"}}}
        assert not is_unresolved_segment_endpoint({"destination_segment": segment}, "destination")

    def test_no_segment_is_any_not_outside(self) -> None:
        assert not is_unresolved_segment_endpoint({"destination_segment": None}, "destination")
        assert not is_unresolved_segment_endpoint({}, "source")

    def test_selector_resolves_the_side(self) -> None:
        rule = {"destination_segment": _INTERNET, "destination_prefixes": [{"prefix": "8.8.8.0/24"}]}
        assert not is_unresolved_segment_endpoint(rule, "destination")

    def test_sides_are_independent(self) -> None:
        rule = {"source_segment": _INTERNET, "destination_segment": None}
        assert is_unresolved_segment_endpoint(rule, "source")
        assert not is_unresolved_segment_endpoint(rule, "destination")
