"""CheckCustomerQuota for the colocation and cloud footprints, the address block of a tier."""

from __future__ import annotations

from typing import Any, cast

import pytest

from checks.customers.customer_quota import CheckCustomerQuota


def _v(value: Any) -> dict[str, Any]:
    """A GraphQL attribute."""
    return {"value": value}


def _check() -> Any:
    """CheckCustomerQuota with errors and warnings captured instead of sent to Infrahub."""
    check = cast(Any, CheckCustomerQuota.__new__(CheckCustomerQuota))
    check._errors = []
    check._warnings = []
    check.log_error = lambda **kwargs: check._errors.append(kwargs)
    check.log_warning = lambda message: check._warnings.append(message)
    return check


def _run(payload: dict[str, Any]) -> Any:
    """The check after validating ``payload``."""
    check = _check()
    check.validate(payload)
    return check


def _colo(name: str = "C011-P-NY", max_segments: int = 2) -> dict[str, Any]:
    """One TopologyCustomerColocation row with its design."""
    return {
        "node": {
            "id": f"fp-{name}",
            "name": _v(name),
            "design": {"node": {"id": "design-S", "name": _v("S"), "max_segments": _v(max_segments)}},
        }
    }


def _cloud(
    name: str = "C003-P-AWS", max_segments: int = 2, max_virtual_networks: int = 1, segments: int = 0
) -> dict[str, Any]:
    """One TopologyCustomerCloud row: region ``region-1``, account ``acct-1``, ``segments`` cloud segments."""
    return {
        "node": {
            "id": f"fp-{name}",
            "name": _v(name),
            "parent": {"node": {"id": "region-1", "name": _v("eu-central-1")}},
            "account": {"node": {"id": "acct-1"}},
            "design": {
                "node": {
                    "id": "design-S",
                    "name": _v("S"),
                    "max_segments": _v(max_segments),
                    "max_virtual_networks": _v(max_virtual_networks),
                }
            },
            "network_segments": {"edges": [{"node": {"id": f"cs-{i}"}} for i in range(segments)]},
        }
    }


def _network(net_id: str, account: str = "acct-1", region: str = "region-1") -> dict[str, Any]:
    """One CloudVirtualNetwork row."""
    return {"node": {"id": net_id, "account": {"node": {"id": account}}, "region": {"node": {"id": region}}}}


def _vxlan(seg_id: str, *footprints: tuple[str, str]) -> dict[str, Any]:
    """A ManagedVxlanSegment naming (name, kind) footprints."""
    named = [{"node": {"id": f"fp-{name}", "__typename": kind, "name": _v(name)}} for name, kind in footprints]
    return {
        "node": {
            "id": seg_id,
            "name": _v(seg_id),
            "security_zone": {"node": None},
            "customer_deployments": {"edges": named},
        }
    }


def _template(
    name: str,
    max_segments: int,
    length: int,
    *,
    segment_length: int = 24,
    ipv6_length: int = 58,
    ipv6_segment_length: int = 64,
) -> dict[str, Any]:
    """A DC or colocation template row with its four tier numbers (the defaults hold any tier below)."""
    return {
        "node": {
            "id": f"tpl-{name}",
            "name": _v(name),
            "max_segments": _v(max_segments),
            "address_prefix_length": _v(length),
            "segment_prefix_length": _v(segment_length),
            "ipv6_prefix_length": _v(ipv6_length),
            "ipv6_segment_prefix_length": _v(ipv6_segment_length),
        }
    }


def _edges(*rows: dict[str, Any]) -> dict[str, Any]:
    return {"edges": list(rows)}


class TestColocationQuota:
    """A colocation footprint is metered on its segments, not on ports."""

    def test_at_the_limit_passes(self) -> None:
        """Two segments of a design that allows two."""
        payload = {
            "TopologyCustomerColocation": _edges(_colo()),
            "ManagedVxlanSegment": _edges(
                _vxlan("s1", ("C011-P-NY", "TopologyCustomerColocation")),
                _vxlan("s2", ("C011-P-NY", "TopologyCustomerColocation")),
            ),
        }
        check = _run(payload)
        assert check._errors == [] and check._warnings == []

    def test_over_the_limit_is_an_error(self) -> None:
        """A third segment is an ERROR naming the footprint, its design and the numbers."""
        segments = [_vxlan(f"s{i}", ("C011-P-NY", "TopologyCustomerColocation")) for i in range(3)]
        check = _run({"TopologyCustomerColocation": _edges(_colo()), "ManagedVxlanSegment": _edges(*segments)})

        (finding,) = check._errors
        assert "'C011-P-NY'" in finding["message"] and "3 segments" in finding["message"]
        assert "'S'" in finding["message"] and "max_segments" in finding["message"]
        assert finding["object_type"] == "TopologyCustomerColocation"


class TestCloudQuota:
    """A cloud footprint is metered on its cloud segments and the virtual networks of its account in its region."""

    def test_segments_at_the_limit_pass_and_over_fail(self) -> None:
        """Two cloud segments pass a design of two, three fail."""
        assert _run({"TopologyCustomerCloud": _edges(_cloud(segments=2))})._errors == []
        (finding,) = _run({"TopologyCustomerCloud": _edges(_cloud(segments=3))})._errors
        assert "3 segments" in finding["message"] and finding["object_type"] == "TopologyCustomerCloud"

    def test_virtual_networks_at_the_limit_pass_and_over_fail(self) -> None:
        """One network of a design that allows one passes; a second of the same account and region fails."""
        one = {"TopologyCustomerCloud": _edges(_cloud()), "CloudVirtualNetwork": _edges(_network("n1"))}
        assert _run(one)._errors == []
        two = {**one, "CloudVirtualNetwork": _edges(_network("n1"), _network("n2"))}
        (finding,) = _run(two)._errors
        assert "2 virtual networks" in finding["message"] and "max_virtual_networks" in finding["message"]
        assert "'S'" in finding["message"]

    def test_networks_of_another_account_or_region_are_not_counted(self) -> None:
        """Only the account's networks in the footprint's region count."""
        networks = _edges(_network("n1"), _network("n2", account="acct-2"), _network("n3", region="region-2"))
        check = _run({"TopologyCustomerCloud": _edges(_cloud()), "CloudVirtualNetwork": networks})
        assert check._errors == []

    def test_a_cloud_footprint_with_no_network_passes(self) -> None:
        """Nothing to count is within any quota."""
        assert _run({"TopologyCustomerCloud": _edges(_cloud(max_virtual_networks=1))})._errors == []


class TestAddressBlockHoldsTheTier:
    """Rule (a): the IPv4 and IPv6 blocks have room for max_segments segments of the tier's size (DC and colocation)."""

    @pytest.mark.parametrize("kind", ["TopologyCustomerTemplateDC", "TopologyCustomerTemplateColo"])
    @pytest.mark.parametrize(("max_segments", "length"), [(2, 23), (6, 21), (8, 21), (24, 19), (1, 24)])
    def test_a_block_with_room_passes(self, kind: str, max_segments: int, length: int) -> None:
        """/23 holds 2 segments, /21 holds 8, /19 holds 32, a /24 holds one."""
        assert _run({kind: _edges(_template("T", max_segments, length))})._errors == []

    @pytest.mark.parametrize("kind", ["TopologyCustomerTemplateDC", "TopologyCustomerTemplateColo"])
    @pytest.mark.parametrize(("max_segments", "length"), [(3, 23), (9, 21), (2, 25), (33, 19)])
    def test_a_block_without_room_is_an_error_naming_the_tier(self, kind: str, max_segments: int, length: int) -> None:
        """One segment more than the block holds, and a block smaller than a /24, name the tier."""
        (finding,) = _run({kind: _edges(_template("XL", max_segments, length))})._errors
        assert "'XL'" in finding["message"] and f"/{length} address block" in finding["message"]
        assert finding["object_type"] == kind

    @pytest.mark.parametrize("kind", ["TopologyCustomerTemplateDC", "TopologyCustomerTemplateColo"])
    @pytest.mark.parametrize(
        ("segment_length", "max_segments", "fits"), [(26, 8, True), (26, 9, False), (24, 2, True), (24, 3, False)]
    )
    def test_the_block_holds_segments_of_the_tiers_own_size(
        self, kind: str, segment_length: int, max_segments: int, fits: bool
    ) -> None:
        """A /23 block holds 8 /26 segments (not 9) and 2 /24s (not 3): the tier's segment size decides."""
        template = _template("L", max_segments, 23, segment_length=segment_length)
        errors = _run({kind: _edges(template)})._errors
        assert (errors == []) is fits
        if not fits:
            assert f"/{segment_length}" in errors[0]["message"] and "'L'" in errors[0]["message"]

    @pytest.mark.parametrize("kind", ["TopologyCustomerTemplateDC", "TopologyCustomerTemplateColo"])
    @pytest.mark.parametrize(
        ("ipv6_length", "ipv6_segment_length", "max_segments", "fits"),
        [(60, 64, 16, True), (60, 64, 17, False), (58, 64, 64, True), (56, 64, 256, True), (56, 64, 257, False)],
    )
    def test_the_ipv6_block_holds_the_tiers_segments(
        self, kind: str, ipv6_length: int, ipv6_segment_length: int, max_segments: int, fits: bool
    ) -> None:
        """A /60 holds 16 /64s, a /58 holds 64, a /56 holds 256; the error names the tier."""
        template = _template("M", max_segments, 16, ipv6_length=ipv6_length, ipv6_segment_length=ipv6_segment_length)
        errors = [e for e in _run({kind: _edges(template)})._errors if "IPv6" in e["message"]]
        assert (errors == []) is fits
        if not fits:
            assert "'M'" in errors[0]["message"] and f"/{ipv6_length} IPv6 address block" in errors[0]["message"]

    @pytest.mark.parametrize("kind", ["TopologyCustomerTemplateDC", "TopologyCustomerTemplateColo"])
    def test_a_segment_larger_than_its_block_is_an_error_in_each_family(self, kind: str) -> None:
        """segment_prefix_length /22 in a /23 block, ipv6_segment_prefix_length /56 in a /58 block: both name the tier."""
        template = _template("S", 1, 23, segment_length=22, ipv6_length=58, ipv6_segment_length=56)
        messages = [finding["message"] for finding in _run({kind: _edges(template)})._errors]
        assert len(messages) == 2
        assert "segment_prefix_length /22 is larger than its /23 address block" in messages[0]
        assert "ipv6_segment_prefix_length /56 is larger than its /58 IPv6 address block" in messages[1]
        assert all("'S'" in message for message in messages)

    def test_a_cloud_template_is_judged_on_its_routed_range_only(self) -> None:
        """A cloud tier has no segment sizes: only its block against max_virtual_networks (not the DC numbers)."""
        assert _run({"TopologyCustomerTemplateCloud": _edges(_cloud_template("XL", 25, 12))})._errors == []


def _cloud_template(name: str, length: int, max_virtual_networks: int) -> dict[str, Any]:
    """A cloud template row."""
    return {
        "node": {
            "id": f"tpl-{name}",
            "name": _v(name),
            "address_prefix_length": _v(length),
            "max_virtual_networks": _v(max_virtual_networks),
        }
    }


class TestCloudTierRoutedRanges:
    """A cloud tier's block holds a /29 routed range per virtual network it allows."""

    @pytest.mark.parametrize(
        ("length", "networks", "fits"),
        [(28, 1, True), (28, 2, True), (28, 3, False), (27, 3, True), (26, 6, True), (25, 12, True), (25, 17, False)],
    )
    def test_the_block_holds_max_virtual_networks_slices(self, length: int, networks: int, fits: bool) -> None:
        """2^(29 - block) slices against max_virtual_networks; an ERROR naming the tier when they do not fit."""
        errors = _run({"TopologyCustomerTemplateCloud": _edges(_cloud_template("M", length, networks))})._errors
        assert (errors == []) is fits
        if not fits:
            message = errors[0]["message"]
            assert "'M'" in message and f"/{length} block" in message and f"{networks} virtual networks" in message

    def test_a_block_smaller_than_a_slice_holds_none(self) -> None:
        """A /30 block cannot hold a /29: zero slices."""
        (finding,) = _run({"TopologyCustomerTemplateCloud": _edges(_cloud_template("S", 30, 1))})._errors
        assert "holds 0 /29" in finding["message"]


def _attachment(name: str, network_id: str | None, prefixes: int = 0) -> dict[str, Any]:
    """A CloudGatewayAttachment row with ``prefixes`` allowed_prefixes."""
    return {
        "node": {
            "id": f"att-{name}",
            "name": _v(name),
            "virtual_network": {"node": {"id": network_id} if network_id else None},
            "allowed_prefixes": {"edges": [{"node": {"id": f"p{i}"}} for i in range(prefixes)]},
        }
    }


class TestAttachmentsWithoutRange:
    """WARNING: an attachment to a footprint's network with no allowed_prefixes (neither authored nor allocated)."""

    @staticmethod
    def _payload(*attachments: dict[str, Any]) -> dict[str, Any]:
        return {
            "TopologyCustomerCloud": _edges(_cloud()),
            "CloudVirtualNetwork": _edges(_network("n1"), _network("n2", account="acct-other")),
            "CloudGatewayAttachment": _edges(*attachments),
        }

    def test_a_bare_attachment_of_a_footprints_network_warns(self) -> None:
        """Names the attachment and the footprint; it is a WARNING, not an ERROR."""
        check = _run(self._payload(_attachment("tgwa-a", "n1")))

        (message,) = check._warnings
        assert "'tgwa-a'" in message and "'C003-P-AWS'" in message and "allowed_prefixes" in message
        assert check._errors == []

    def test_an_attachment_with_prefixes_does_not_warn(self) -> None:
        """Authored, or the routed range the generator wrote: either is a range."""
        assert _run(self._payload(_attachment("tgwa-a", "n1", prefixes=1)))._warnings == []

    @pytest.mark.parametrize("network_id", ["n2", None, "n-unknown"])
    def test_an_attachment_of_no_footprint_network_does_not_warn(self, network_id: str | None) -> None:
        """A network of another account, an attachment to a gateway, an unknown network: not this check's business."""
        assert _run(self._payload(_attachment("tgwa-a", network_id)))._warnings == []


class TestCloudSegmentBlock:
    """Rule (b): a cloud segment's cidr_block is at least as large as its host_count needs."""

    @staticmethod
    def _segment(host_count: int | None, cidr_block: str | None, ip_version: str = "ipv4") -> dict[str, Any]:
        node = {
            "id": "cs-1",
            "name": _v("c003-web"),
            "host_count": _v(host_count),
            "cidr_block": _v(cidr_block),
            "ip_version": _v(ip_version),
        }
        return {"CloudNetworkSegment": _edges({"node": node})}

    def test_a_cloud_segment_must_be_ipv4(self) -> None:
        """A cloud segment carries a provider cidr_block: an ipv6 one is an ERROR naming it."""
        (finding,) = _run(self._segment(None, "10.0.0.0/24", ip_version="ipv6"))._errors
        assert "'c003-web'" in finding["message"] and "must be IPv4" in finding["message"]
        assert finding["object_type"] == "CloudNetworkSegment"
        assert _run(self._segment(None, "10.0.0.0/24"))._errors == []

    @pytest.mark.parametrize(
        ("host_count", "cidr_block"),
        [(100, "10.0.0.0/25"), (100, "10.0.0.0/24"), (5, "10.0.0.8/29"), (253, "10.0.1.0/24"), (254, "10.0.0.0/23")],
    )
    def test_a_block_that_holds_the_hosts_passes(self, host_count: int, cidr_block: str) -> None:
        """100 hosts need a /25; 5 a /29; 253 a /24; 254 a /23."""
        assert _run(self._segment(host_count, cidr_block))._errors == []

    @pytest.mark.parametrize(
        ("host_count", "cidr_block", "needs"),
        [(100, "10.0.0.0/26", 25), (6, "10.0.0.0/29", 28), (254, "10.0.0.0/24", 23)],
    )
    def test_a_smaller_block_is_an_error(self, host_count: int, cidr_block: str, needs: int) -> None:
        """The message names the segment, its block and the length the host count needs."""
        (finding,) = _run(self._segment(host_count, cidr_block))._errors
        assert "'c003-web'" in finding["message"] and cidr_block in finding["message"]
        assert f"/{needs}" in finding["message"]
        assert finding["object_type"] == "CloudNetworkSegment"

    def test_without_a_host_count_or_a_block_nothing_is_judged(self) -> None:
        """The provider assigns the subnet; only a stated host_count is checked against a stated block."""
        assert _run(self._segment(None, "10.0.0.0/29"))._errors == []
        assert _run(self._segment(100, None))._errors == []


class TestMixedKindWarning:
    """Rule (c): a WARNING when the footprints of one segment are of different kinds."""

    def test_two_kinds_warn(self) -> None:
        """A segment in a data center and a colocation footprint warns and names both kinds."""
        segment = _vxlan("stretch", ("C005-P-DC10", "TopologyCustomerDC"), ("C005-P-FR", "TopologyCustomerColocation"))
        check = _run({"ManagedVxlanSegment": _edges(segment)})

        (warning,) = check._warnings
        assert "'stretch'" in warning and "TopologyCustomerDC" in warning and "TopologyCustomerColocation" in warning
        assert check._errors == []

    def test_one_kind_does_not_warn(self) -> None:
        """Two data center footprints are one kind."""
        segment = _vxlan("stretch", ("C005-P-DC10", "TopologyCustomerDC"), ("C005-P-DC12", "TopologyCustomerDC"))
        assert _run({"ManagedVxlanSegment": _edges(segment)})._warnings == []


class TestSegmentIpVersion:
    """Rule (d): a VXLAN segment's gateway prefix is of its ip_version, and an IPv6 segment has no host_count."""

    @staticmethod
    def _segment(ip_version: str, prefix: str | None, host_count: int | None = None) -> dict[str, Any]:
        node = {
            "id": "seg-1",
            "name": _v("c001-app"),
            "ip_version": _v(ip_version),
            "host_count": _v(host_count),
            "gateway": {"node": {"id": "gw-1", "ip_prefix": {"node": {"id": "p-1", "prefix": _v(prefix)}}}}
            if prefix
            else {"node": None},
            "security_zone": {"node": None},
            "customer_deployments": {"edges": []},
        }
        return {"ManagedVxlanSegment": _edges({"node": node})}

    @pytest.mark.parametrize(
        ("ip_version", "prefix"), [("ipv4", "10.1.0.0/24"), ("ipv6", "fd5a:1760::/64"), ("ipv4", None), ("ipv6", None)]
    )
    def test_a_gateway_of_the_segments_family_or_none_passes(self, ip_version: str, prefix: str | None) -> None:
        """No gateway yet is not judged; the family of a gateway must be the segment's."""
        check = _run(self._segment(ip_version, prefix))
        assert check._errors == [] and check._warnings == []

    @pytest.mark.parametrize(("ip_version", "prefix"), [("ipv4", "fd5a:1760::/64"), ("ipv6", "10.1.0.0/24")])
    def test_a_gateway_of_the_other_family_is_an_error(self, ip_version: str, prefix: str) -> None:
        """The error names the segment, the prefix and the stated version."""
        (finding,) = _run(self._segment(ip_version, prefix))._errors
        assert "'c001-app'" in finding["message"] and prefix in finding["message"]
        assert f"not {ip_version.replace('ip', 'IP')}" in finding["message"]
        assert finding["object_type"] == "ManagedVxlanSegment"

    def test_a_host_count_on_an_ipv6_segment_is_a_warning(self) -> None:
        """It sizes nothing there: a WARNING, never an error; an IPv4 segment keeps its host_count silently."""
        check = _run(self._segment("ipv6", "fd5a:1760::/64", host_count=50))
        assert check._errors == []
        (warning,) = check._warnings
        assert "'c001-app'" in warning and "host_count 50" in warning and "IPv6" in warning
        assert _run(self._segment("ipv4", "10.1.0.0/26", host_count=50))._warnings == []
