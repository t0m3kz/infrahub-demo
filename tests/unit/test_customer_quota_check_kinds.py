"""CheckCustomerQuota for the colocation and cloud footprints, the address block of a tier, the cloud segment block
and the mixed-kind warning (the data center segment and port quota is in test_customer_quota_check.py).

The payloads are raw GraphQL responses shaped like queries/validation/customer_quota.gql; ``validate`` runs them
through ``clean_data``.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from checks.customer_quota import CheckCustomerQuota


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


def _template(name: str, max_segments: int, length: int) -> dict[str, Any]:
    """A DC or colocation template row."""
    return {
        "node": {
            "id": f"tpl-{name}",
            "name": _v(name),
            "max_segments": _v(max_segments),
            "address_prefix_length": _v(length),
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
    """Rule (a): a /<address_prefix_length> has room for max_segments /24 segments (data center and colocation)."""

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

    def test_the_cloud_template_is_not_judged(self) -> None:
        """A cloud template's length is the footprint's routed range, not a block for its segments."""
        payload = {"TopologyCustomerTemplateCloud": _edges(_template("XL", 24, 25))}
        assert _run(payload)._errors == []


class TestCloudSegmentBlock:
    """Rule (b): a cloud segment's cidr_block is at least as large as its host_count needs."""

    @staticmethod
    def _segment(host_count: int | None, cidr_block: str | None) -> dict[str, Any]:
        node = {"id": "cs-1", "name": _v("c003-web"), "host_count": _v(host_count), "cidr_block": _v(cidr_block)}
        return {"CloudNetworkSegment": _edges({"node": node})}

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
