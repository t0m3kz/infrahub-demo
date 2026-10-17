"""Unit tests for utils/segment_addressing.py: the one segment-prefix resolver."""

from __future__ import annotations

from typing import Any

import pytest

from utils.segment_addressing import classifier_prefix, segment_prefix


def _gw(prefix: str | None) -> dict[str, Any]:
    """A segment dict whose gateway sits in `prefix`."""
    return {"gateway": {"ip_prefix": {"prefix": prefix}}}


class TestSegmentPrefix:
    """Gateway prefix wins, IPv4 cidr_block falls back, else None."""

    def test_gateway_prefix_is_returned(self) -> None:
        """An on-prem segment resolves to its gateway prefix."""
        assert segment_prefix(_gw("192.168.1.0/24")) == "192.168.1.0/24"

    def test_cloud_segment_falls_back_to_cidr_block(self) -> None:
        """A cloud segment has no gateway: cidr_block is its prefix."""
        assert segment_prefix({"cidr_block": "10.0.1.0/24"}) == "10.0.1.0/24"

    def test_gateway_wins_over_cidr_block(self) -> None:
        """When both exist the gateway prefix is used."""
        seg = {"cidr_block": "172.16.0.0/12", **_gw("10.0.0.0/8")}
        assert segment_prefix(seg) == "10.0.0.0/8"

    def test_empty_gateway_prefix_falls_back_to_cidr_block(self) -> None:
        """A gateway without a prefix does not hide the cidr_block."""
        assert segment_prefix({"cidr_block": "10.0.0.0/8", **_gw(None)}) == "10.0.0.0/8"

    @pytest.mark.parametrize(
        "seg",
        [{}, {"gateway": {}}, {"gateway": None}, {"cidr_block": None}, {"cidr_block": ""}, _gw(None)],
    )
    def test_no_address_returns_none(self, seg: dict[str, Any]) -> None:
        """A segment with neither gateway prefix nor cidr_block resolves to nothing."""
        assert segment_prefix(seg) is None

    def test_ipv6_gateway_prefix_only_for_ipv6_family(self) -> None:
        """An IPv6 gateway prefix is not an IPv4 answer, and vice versa."""
        seg = _gw("2001:db8::/64")
        assert segment_prefix(seg) is None
        assert segment_prefix(seg, family="ipv6") == "2001:db8::/64"
        assert segment_prefix(_gw("10.0.0.0/24"), family="ipv6") is None

    def test_cidr_block_is_ipv4_only(self) -> None:
        """cidr_block never answers an IPv6 request, and an IPv6 block is not IPv4."""
        assert segment_prefix({"cidr_block": "10.0.0.0/24"}, family="ipv6") is None
        assert segment_prefix({"cidr_block": "2001:db8::/64"}) is None


class TestSegmentDeploymentIdsCoverCloud:
    """_segment_deployment_ids reads a cloud segment's customer_deployment like a VLAN's."""

    def test_cloud_segment_customer_deployment_is_listed(self) -> None:
        """A CloudNetworkSegment (customer_deployment, cardinality one) yields its deployment id."""
        from transforms.helpers.firewall import _segment_deployment_ids

        seg = {"cidr_block": "10.0.0.0/24", "customer_deployment": {"id": "cust-1"}}
        assert _segment_deployment_ids(seg) == ["cust-1"]

    def test_cloud_segment_without_deployment_yields_nothing(self) -> None:
        """No customer_deployment: no ids."""
        from transforms.helpers.firewall import _segment_deployment_ids

        assert _segment_deployment_ids({"cidr_block": "10.0.0.0/24", "customer_deployment": None}) == []


class TestClassifierPrefix:
    """The one prefix a segment is classified by (MSS-G, the segmentation matrix): IPv4, else IPv6."""

    def test_ipv4_prefix_is_the_classifier(self) -> None:
        assert classifier_prefix(_gw("10.0.0.0/24")) == "10.0.0.0/24"

    def test_ipv6_only_segment_is_classified_by_its_ipv6_prefix(self) -> None:
        assert classifier_prefix(_gw("2001:db8::/64")) == "2001:db8::/64"

    def test_cloud_cidr_block_is_the_classifier(self) -> None:
        assert classifier_prefix({"cidr_block": "10.0.1.0/24"}) == "10.0.1.0/24"

    def test_segment_without_a_prefix_has_no_classifier(self) -> None:
        assert classifier_prefix({}) is None
        assert classifier_prefix(_gw(None)) is None
