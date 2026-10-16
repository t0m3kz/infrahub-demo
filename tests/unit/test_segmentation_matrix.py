"""Unit tests for the group contract builder and its platform views (transforms/helpers/group_policy.py)
and the segmentation_matrix artifact (transforms/topology/segmentation_matrix.py)."""

from __future__ import annotations

import asyncio
import json
from itertools import combinations
from pathlib import Path
from typing import Any

import pytest

from transforms.helpers.group_policy import (
    REPLIES,
    UNAWARE,
    UNCLASSIFIED,
    get_vrf_contracts,
    segment_groups,
    segment_security_view,
    vxlan_gpo_view,
)
from transforms.topology.segmentation_matrix import SegmentationMatrix

# ---------------------------------------------------------------------------
# Cleaned builder input (SegmentGroupPolicyFields after clean_data)
# ---------------------------------------------------------------------------


def _rule(
    name: str,
    dst: str | None,
    *,
    protocol: str = "tcp",
    port_start: int | None = 443,
    port_end: int | None = None,
    action: str = "permit",
    disabled: bool = False,
    src_tag: str | None = None,
    dst_tag: str | None = None,
    index: int = 100,
) -> dict[str, Any]:
    """A cleaned rule of a segment's own policy."""
    return {
        "id": f"id-{name}",
        "index": index,
        "name": name,
        "action": action,
        "protocol": protocol,
        "port_start": port_start,
        "port_end": port_end,
        "disabled": disabled,
        "destination_segment": {"id": dst} if dst else None,
        "source_security_tag": {"id": src_tag} if src_tag else None,
        "destination_security_tag": {"id": dst_tag} if dst_tag else None,
    }


def _seg(
    seg_id: str,
    *rules: dict[str, Any],
    customer: str = "C005",
    vrf: str = "PROD",
    namespace_type: str = "prod",
    kind: str = "ManagedVxlanSegment",
    isolation: str = "normal",
    sites: tuple[str, ...] = ("dc10",),
    tagged: bool = True,
    enabled: bool = True,
    zone: str = "PROD-ZONE",
) -> dict[str, Any]:
    """A cleaned segment: a routed tenant VXLAN segment named and tagged after ``seg_id`` by default."""
    segment: dict[str, Any] = {
        "id": seg_id,
        "typename": kind,
        "name": seg_id,
        "isolation_mode": isolation,
        "owner": {"org_id": customer},
        "security_zone": {"name": zone},
        "security_tag": {"id": f"tag-{seg_id}", "name": seg_id, "group_id": 100 + len(seg_id)} if tagged else None,
        "gateway": {
            "ip_prefix": {
                "prefix": f"10.0.{len(seg_id)}.0/24",
                "ip_namespace": {"name": vrf, "namespace_type": namespace_type},
            }
        },
        "security_policy": {
            "name": f"seg-{seg_id}-egress",
            "enabled": enabled,
            "default_action": "deny",
            "rules": list(rules),
        },
        "segment_deployments": [{"vni": 10000 + len(seg_id), "deployment": {"id": site}} for site in sites],
    }
    return segment


def _component_tag(tag_id: str, group_id: int, segment: str) -> dict[str, Any]:
    """A cleaned component SecurityTag whose component lives in ``segment``."""
    return {
        "id": tag_id,
        "name": f"{tag_id}.example",
        "group_id": group_id,
        "components": [{"network_segment": {"id": segment}}],
    }


def _pairs(rows: list[dict[str, Any]]) -> set[tuple[str, str]]:
    return {(row["src"], row["dst"]) for row in rows}


def _row(rows: list[dict[str, Any]], src: str, dst: str) -> dict[str, Any]:
    return next(row for row in rows if (row["src"], row["dst"]) == (src, dst))


TCP_443 = [{"protocol": "tcp", "port_start": 443, "port_end": 443}]


class TestGroups:
    """A segment group per tagged routed tenant segment, a component group per tag of a microsegmented one."""

    def test_segment_group_carries_customer_vrf_and_intra_group(self) -> None:
        by_segment, _ = segment_groups([_seg("c008-portal", customer="C008")], [])
        (group,) = by_segment["c008-portal"]
        assert group.as_dict() == {
            "name": "c008-portal",
            "group_id": 111,
            "kind": "segment",
            "segment": "c008-portal",
            "vrf": "PROD",
            "customer": "C008",
            "intra_group": "permit",
        }

    def test_microsegmented_segment_denies_intra_group_and_groups_its_components(self) -> None:
        tags = [_component_tag("k-web", 20, "c005-web"), _component_tag("k-session", 21, "c005-web")]
        by_segment, by_tag = segment_groups([_seg("c005-web", isolation="microsegmented")], tags)
        assert [(g.name, g.kind, g.intra_group) for g in by_segment["c005-web"]] == [
            ("c005-web", "segment", "deny"),
            ("k-session.example", "component", "permit"),
            ("k-web.example", "component", "permit"),
        ]
        assert set(by_tag) == {"k-web", "k-session"}

    def test_component_tags_of_a_normal_segment_are_no_group(self) -> None:
        """A tag left on a component of a normal segment classifies nothing."""
        by_segment, by_tag = segment_groups([_seg("c005-api")], [_component_tag("k-old", 20, "c005-api")])
        assert [g.name for g in by_segment["c005-api"]] == ["c005-api"]
        assert by_tag == {}

    @pytest.mark.parametrize(
        "segment",
        [
            _seg("infra", vrf="MANAGEMENT", namespace_type="management", zone="MANAGEMENT-ZONE"),
            _seg("internet", kind="ManagedExternalSegment", zone="INTERNET-ZONE"),
            _seg("untagged", tagged=False),
        ],
        ids=["management", "external", "untagged"],
    )
    def test_no_group_for_a_segment_that_is_not_a_tagged_routed_tenant(self, segment: dict[str, Any]) -> None:
        assert segment_groups([segment], [])[0] == {}


class TestVrfContracts:
    """The rules, 1:1, as permit rows between groups; everything else is denied."""

    def test_rule_between_two_customers_is_the_only_cross_customer_row(self) -> None:
        """C008 -> C005 on tcp/443: one row, plus the same entries on the two service legs."""
        rows = get_vrf_contracts(
            [
                _seg("c008-portal", _rule("portal-to-web", "c005-web"), customer="C008"),
                _seg("c005-web"),
                _seg("c007-db", customer="C007"),
            ],
            [],
        )
        assert _pairs(rows) == {("c008-portal", "c005-web"), ("c008-portal", UNCLASSIFIED), (UNCLASSIFIED, "c005-web")}
        row = _row(rows, "c008-portal", "c005-web")
        assert row == {
            "vrf": "PROD",
            "src": "c008-portal",
            "dst": "c005-web",
            "entries": TCP_443,
            "rules": ["portal-to-web"],
            "cross_customer": True,
        }
        assert _row(rows, "c008-portal", UNCLASSIFIED)["cross_customer"] is False

    def test_no_rule_no_row_even_inside_one_customer(self) -> None:
        """Two segments of one customer without a rule are denied, as L0 denies them."""
        assert get_vrf_contracts([_seg("c005-a"), _seg("c005-b")], []) == []

    def test_intra_segment_rule_of_a_normal_segment_adds_nothing(self) -> None:
        """Intra-group traffic is permitted already."""
        assert get_vrf_contracts([_seg("c005-a", _rule("a-to-a", "c005-a"))], []) == []

    def test_intra_segment_component_rule_is_one_forward_row(self) -> None:
        """web -> session on tcp/6379, nothing back, nothing between the segment tag and the components."""
        tags = [_component_tag("k-web", 20, "c005-web"), _component_tag("k-session", 21, "c005-web")]
        rule = _rule("web-to-session", "c005-web", port_start=6379, src_tag="k-web", dst_tag="k-session")
        rows = get_vrf_contracts([_seg("c005-web", rule, isolation="microsegmented")], tags)
        assert [(r["src"], r["dst"], r["entries"]) for r in rows] == [
            ("k-web.example", "k-session.example", [{"protocol": "tcp", "port_start": 6379, "port_end": 6379}])
        ]

    def test_segment_level_rule_inside_a_microsegmented_segment_opens_its_groups(self) -> None:
        """No tags on the rule: every group of the segment, the denied segment tag to itself included."""
        tags = [_component_tag("k-web", 20, "c005-web")]
        rows = get_vrf_contracts([_seg("c005-web", _rule("any-to-any", "c005-web"), isolation="microsegmented")], tags)
        assert _pairs(rows) == {
            ("c005-web", "c005-web"),
            ("c005-web", "k-web.example"),
            ("k-web.example", "c005-web"),
        }

    def test_component_dependency_into_a_microsegmented_segment_opens_that_component_only(self) -> None:
        """The rule names the target's tag: one row to it and its service leg, nothing to its neighbours."""
        tags = [_component_tag("k-web", 20, "c005-web"), _component_tag("k-session", 21, "c005-web")]
        segments = [
            _seg("c008-portal", _rule("portal-to-web", "c005-web", dst_tag="k-web"), customer="C008"),
            _seg("c005-web", isolation="microsegmented"),
        ]
        rows = get_vrf_contracts(segments, tags)
        assert _pairs(rows) == {
            ("c008-portal", "k-web.example"),
            ("c008-portal", UNCLASSIFIED),
            (UNCLASSIFIED, "k-web.example"),
        }

    def test_component_dependency_out_of_a_microsegmented_segment_opens_that_component_only(self) -> None:
        """The source side names its component: the segment's other groups stay closed."""
        tags = [_component_tag("k-web", 20, "c005-web"), _component_tag("k-session", 21, "c005-web")]
        segments = [
            _seg("c005-web", _rule("web-to-backend", "c005-backend", src_tag="k-web"), isolation="microsegmented"),
            _seg("c005-backend"),
        ]
        rows = get_vrf_contracts(segments, tags)
        assert {src for src, _dst in _pairs(rows)} == {"k-web.example", UNCLASSIFIED}

    def test_a_tag_of_another_segment_on_a_side_is_ignored(self) -> None:
        """A tag that is no component of that side's segment cannot narrow it (the check reports it)."""
        tags = [_component_tag("k-web", 20, "c005-web"), _component_tag("k-other", 22, "c005-other")]
        segments = [
            _seg("c008-portal", _rule("portal-to-web", "c005-web", dst_tag="k-other"), customer="C008"),
            _seg("c005-web", isolation="microsegmented"),
        ]
        dsts = {dst for src, dst in _pairs(get_vrf_contracts(segments, tags)) if src == "c008-portal"}
        assert dsts == {"c005-web", "k-web.example", UNCLASSIFIED}

    def test_rule_into_a_microsegmented_segment_reaches_every_group_of_it(self) -> None:
        """A hand-written rule names no component: segment-level, like the SVI ACL it mirrors (the check warns)."""
        tags = [_component_tag("k-web", 20, "c005-web"), _component_tag("k-session", 21, "c005-web")]
        segments = [
            _seg("c008-portal", _rule("portal-to-web", "c005-web"), customer="C008"),
            _seg("c005-web", isolation="microsegmented"),
        ]
        rows = get_vrf_contracts(segments, tags)
        assert {dst for src, dst in _pairs(rows) if src == "c008-portal"} == {
            "c005-web",
            "k-web.example",
            "k-session.example",
            UNCLASSIFIED,
        }
        assert all(row["cross_customer"] for row in rows if row["src"] == "c008-portal" and row["dst"] != UNCLASSIFIED)

    @pytest.mark.parametrize(
        "outside",
        [
            _seg("internet", kind="ManagedExternalSegment", zone="INTERNET-ZONE"),
            _seg("infra", vrf="MANAGEMENT", namespace_type="management", zone="MANAGEMENT-ZONE"),
            _seg("c009-untagged", tagged=False),
            _seg("c010-dev", vrf="NON-PROD", namespace_type="non_prod", zone="NONPROD-ZONE"),
        ],
        ids=["internet", "management", "untagged", "other-vrf"],
    )
    def test_rule_to_anything_outside_the_vrfs_groups_is_a_service_leg(self, outside: dict[str, Any]) -> None:
        """The internet, MANAGEMENT, an untagged segment, another VRF: the tagged side to UNCLASSIFIED only."""
        rows = get_vrf_contracts([_seg("c006-erp", _rule("erp-out", outside["id"])), outside], [])
        rows = [row for row in rows if row["vrf"] == "PROD"]
        assert _pairs(rows) == {("c006-erp", UNCLASSIFIED)}

    def test_rule_to_a_prefix_selector_is_a_service_leg(self) -> None:
        """No destination segment (cloud, partner CIDR)."""
        rows = get_vrf_contracts([_seg("c001-nordix", _rule("to-aws", None))], [])
        assert _pairs(rows) == {("c001-nordix", UNCLASSIFIED)}

    def test_rule_from_the_internet_arrives_on_a_service_leg(self) -> None:
        """A rule held by the External segment's policy into a tenant segment."""
        internet = _seg("internet", _rule("publish", "c005-web"), kind="ManagedExternalSegment", zone="INTERNET-ZONE")
        rows = get_vrf_contracts([internet, _seg("c005-web")], [])
        assert _pairs(rows) == {(UNCLASSIFIED, "c005-web")}

    def test_rule_across_vrfs_lands_on_each_vrfs_service_leg(self) -> None:
        """PROD -> NON-PROD (never authorized, but data): no direct row, one leg per VRF."""
        dev = _seg("c008-dev", vrf="NON-PROD", namespace_type="non_prod", zone="NONPROD-ZONE")
        rows = get_vrf_contracts([_seg("c008-portal", _rule("prod-to-dev", "c008-dev")), dev], [])
        assert {(row["vrf"], row["src"], row["dst"]) for row in rows} == {
            ("PROD", "c008-portal", UNCLASSIFIED),
            ("NON-PROD", UNCLASSIFIED, "c008-dev"),
        }

    @pytest.mark.parametrize(
        ("rule", "enabled"),
        [
            (_rule("off", "c005-web", disabled=True), True),
            (_rule("deny", "c005-web", action="deny"), True),
            (_rule("policy-off", "c005-web"), False),
        ],
        ids=["disabled-rule", "deny-rule", "disabled-policy"],
    )
    def test_inactive_and_deny_rules_are_no_rows(self, rule: dict[str, Any], enabled: bool) -> None:
        """A deny is the default already; a disabled rule or policy is no rule."""
        assert get_vrf_contracts([_seg("c008-portal", rule, enabled=enabled), _seg("c005-web")], []) == []

    def test_entries_merge_sorted_and_any_collapses(self) -> None:
        """Two rules of one pair merge into one row; an ``any`` rule makes it permit-all."""
        segments = [
            _seg(
                "c008-portal",
                _rule("udp-range", "c005-web", protocol="udp", port_start=30000, port_end=30010),
                _rule("https", "c005-web"),
            ),
            _seg("c005-web"),
        ]
        row = _row(get_vrf_contracts(segments, []), "c008-portal", "c005-web")
        assert row["entries"] == [
            {"protocol": "tcp", "port_start": 443, "port_end": 443},
            {"protocol": "udp", "port_start": 30000, "port_end": 30010},
        ]
        assert row["rules"] == ["https", "udp-range"]
        segments[0]["security_policy"]["rules"].append(_rule("all", "c005-web", protocol="any", port_start=None))
        row = _row(get_vrf_contracts(segments, []), "c008-portal", "c005-web")
        assert row["entries"] == [{"protocol": "any", "port_start": None, "port_end": None}]

    def test_local_rows_of_any_partition_add_up_to_all_rows(self) -> None:
        """The leaf/DC scope: every 2-partition of the groups reproduces the full row set, no row twice-different."""
        tags = [_component_tag("k-web", 20, "c005-web"), _component_tag("k-session", 21, "c005-web")]
        segments = [
            _seg("c008-portal", _rule("portal-to-web", "c005-web"), _rule("out", None), customer="C008"),
            _seg(
                "c005-web",
                _rule("web-to-session", "c005-web", port_start=6379, src_tag="k-web", dst_tag="k-session"),
                _rule("web-to-backend", "c005-backend", port_start=8443),
                isolation="microsegmented",
            ),
            _seg("c005-backend", sites=("dc10", "dc12")),
        ]
        full = get_vrf_contracts(segments, tags)
        names = sorted({group.name for groups in segment_groups(segments, tags)[0].values() for group in groups})
        key = lambda rows: sorted(json.dumps(row, sort_keys=True) for row in rows)  # noqa: E731
        for size in range(len(names) + 1):
            for part in combinations(names, size):
                local, rest = set(part), set(names) - set(part)
                union = {json.dumps(row, sort_keys=True) for row in get_vrf_contracts(segments, tags, local=local)}
                union |= {json.dumps(row, sort_keys=True) for row in get_vrf_contracts(segments, tags, local=rest)}
                assert sorted(union) == key(full)


# ---------------------------------------------------------------------------
# Platform views
# ---------------------------------------------------------------------------

RESERVED = {"unclassified": 0, "unaware": 15}


def _fleet() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """C008 (DC10) -> C005 web (microsegmented, DC10) and C005 web -> backend (stretched DC10 <-> DC12)."""
    tags = [_component_tag("k-web", 20, "c005-web"), _component_tag("k-session", 21, "c005-web")]
    segments = [
        _seg("c008-portal", _rule("portal-to-web", "c005-web"), customer="C008"),
        _seg(
            "c005-web",
            _rule("web-to-session", "c005-web", port_start=6379, src_tag="k-web", dst_tag="k-session"),
            _rule("web-to-backend", "c005-backend", port_start=8443),
            isolation="microsegmented",
        ),
        _seg("c005-backend", sites=("dc10", "dc12")),
        _seg("c005-db", sites=("dc12",)),
    ]
    return segments, tags


def _groups(segments: list[dict[str, Any]], tags: list[dict[str, Any]]) -> list[Any]:
    return [group for groups in segment_groups(segments, tags)[0].values() for group in groups]


def _gpo(rows: list[dict[str, Any]], groups: list[Any], reference: set[str], **kwargs: Any) -> dict[str, Any]:
    return vxlan_gpo_view(groups, rows, reserved=kwargs.pop("reserved", RESERVED), reference_sites=reference, **kwargs)


def _ids(view: dict[str, Any]) -> set[tuple[int | None, int | None]]:
    return {(row["src_group_id"], row["dst_group_id"]) for row in view["contracts"]}


class TestVxlanGpoView:
    """Group IDs from the tags, reserved groups from the profile, the policy-unaware rule from the site verdicts."""

    def test_rows_by_group_id_with_the_reserved_unclassified_group(self) -> None:
        segments, tags = _fleet()
        view = _gpo(get_vrf_contracts(segments, tags), _groups(segments, tags), {"dc10", "dc12"})
        assert view["default_action"] == "deny"
        assert view["reserved"] == RESERVED
        assert view["replies"] == REPLIES
        portal, web = 111, 108
        assert (portal, web) in _ids(view)
        assert (portal, 0) in _ids(view) and (0, web) in _ids(view)
        assert (20, 21) in _ids(view) and (21, 20) not in _ids(view)
        assert not any(row["src"] == UNAWARE or row["dst"] == UNAWARE for row in view["contracts"])

    def test_stretched_group_arrives_as_the_unaware_group_too(self) -> None:
        """DC12 is policy-unaware: web -> backend also to 15, and backend's other half both ways, permit-all."""
        segments, tags = _fleet()
        view = _gpo(get_vrf_contracts(segments, tags), _groups(segments, tags), {"dc10"})
        contracts = {(row["src"], row["dst"]): row for row in view["contracts"]}
        assert contracts[("c005-web", UNAWARE)]["dst_group_id"] == 15
        assert contracts[("c005-web", UNAWARE)]["entries"] == [
            {"protocol": "tcp", "port_start": 8443, "port_end": 8443}
        ]
        assert ("c005-web", "c005-backend") in contracts
        permit_all = [{"protocol": "any", "port_start": None, "port_end": None}]
        assert contracts[(UNAWARE, "c005-backend")]["entries"] == permit_all
        assert contracts[("c005-backend", UNAWARE)]["entries"] == permit_all

    def test_group_on_no_reference_site_is_only_the_unaware_group(self) -> None:
        """c005-db lives on DC12 only: its own ID is never on a policy-aware wire."""
        segments, tags = _fleet()
        segments[1]["security_policy"]["rules"].append(_rule("web-to-db", "c005-db", port_start=5432))
        view = _gpo(get_vrf_contracts(segments, tags), _groups(segments, tags), {"dc10"})
        dsts = {row["dst"] for row in view["contracts"] if row["src"] == "c005-web"}
        assert UNAWARE in dsts and "c005-db" not in dsts
        assert not any(row["src"] == "c005-db" or row["dst"] == "c005-db" for row in view["contracts"])

    def test_row_between_two_reserved_groups_is_dropped(self) -> None:
        """backend -> UNCLASSIFIED would also become 15 -> 0: no group of ours on either end."""
        segments, tags = _fleet()
        segments[2]["security_policy"]["rules"].append(_rule("backend-out", None))
        view = _gpo(get_vrf_contracts(segments, tags), _groups(segments, tags), {"dc10"})
        assert (15, 0) not in _ids(view)
        assert ("c005-backend", UNCLASSIFIED) in {(row["src"], row["dst"]) for row in view["contracts"]}

    def test_stretched_microsegmented_segment_tag_gets_no_intra_group_row(self) -> None:
        """Its intra-group traffic is denied: the unaware half gets no permit-all to it."""
        segments = [_seg("c005-mq", isolation="microsegmented", sites=("dc10", "dc12"))]
        view = _gpo(get_vrf_contracts(segments, []), _groups(segments, []), {"dc10"})
        assert view["contracts"] == []

    def test_reserved_group_the_profile_does_not_name_renders_as_none(self) -> None:
        """No number in code: a row to UNCLASSIFIED has no ID until the profile names one."""
        segments, tags = _fleet()
        view = _gpo(
            get_vrf_contracts(segments, tags),
            _groups(segments, tags),
            {"dc10", "dc12"},
            reserved={"unclassified": None, "unaware": None},
        )
        assert (111, None) in _ids(view)

    def test_local_scope_limits_the_intra_group_rows(self) -> None:
        """Only the listed groups' stretched halves are added."""
        segments, tags = _fleet()
        view = _gpo([], _groups(segments, tags), {"dc10"}, local={"c008-portal"})
        assert view["contracts"] == []


class TestSegmentSecurityView:
    """MSS-G: segments by prefix, policies under the destination, UNCLASSIFIED as the fallback."""

    def test_policies_live_under_the_destination_segment(self) -> None:
        segments, tags = _fleet()
        rows = get_vrf_contracts(segments, tags)
        view = segment_security_view(_groups(segments, tags), rows, {"c005-web": "10.5.10.0/24"})
        web = next(item for item in view["segments"] if item["name"] == "c005-web")
        assert web["prefix"] == "10.5.10.0/24"
        assert web["intra_segment"] == "deny"
        assert web["policies"] == [{"from": "c008-portal", "entries": TCP_443, "rules": ["portal-to-web"]}]
        assert web["fallback"] == {"entries": TCP_443, "rules": ["portal-to-web"]}
        assert view["default_action"] == "deny"

    def test_component_pair_inside_one_segment_is_unsupported(self) -> None:
        """A prefix cannot split two components of one segment."""
        segments, tags = _fleet()
        view = segment_security_view(_groups(segments, tags), get_vrf_contracts(segments, tags), {})
        assert [
            item
            for item in view["unsupported"]
            if item["src"] == "k-web.example" and item["dst"] == "k-session.example"
        ] == [
            {
                "src": "k-web.example",
                "dst": "k-session.example",
                "rules": ["web-to-session"],
                "reason": "component groups inside one segment are not expressible by prefix",
            }
        ]

    def test_a_row_naming_a_component_is_unsupported_not_widened_to_its_segment(self) -> None:
        """D4: a rule into one component (cross-segment) must not open the whole microsegmented segment.

        The row (and the service leg from UNCLASSIFIED into the component) is listed as unsupported; the
        segment gets neither a policy nor a fallback from it. The leg out of the portal is unaffected.
        """
        tags = [_component_tag("k-web", 20, "c005-web"), _component_tag("k-session", 21, "c005-web")]
        segments = [
            _seg("c008-portal", _rule("portal-to-web", "c005-web", dst_tag="k-web"), customer="C008"),
            _seg("c005-web", isolation="microsegmented"),
        ]
        view = segment_security_view(_groups(segments, tags), get_vrf_contracts(segments, tags), {})
        web = next(item for item in view["segments"] if item["name"] == "c005-web")
        assert web["policies"] == [] and web["fallback"] is None
        assert [(item["src"], item["dst"]) for item in view["unsupported"]] == [
            ("c008-portal", "k-web.example"),
            (UNCLASSIFIED, "k-web.example"),
        ]
        assert all(item["rules"] == ["portal-to-web"] for item in view["unsupported"])
        portal = next(item for item in view["segments"] if item["name"] == "c008-portal")
        assert portal["to_unclassified"] == {"entries": TCP_443, "rules": ["portal-to-web"]}

    def test_a_row_from_a_component_is_unsupported_not_widened_to_its_segment(self) -> None:
        """D4: a component's dependency on another segment does not become the whole segment's."""
        tags = [_component_tag("k-web", 20, "c005-web"), _component_tag("k-session", 21, "c005-web")]
        segments = [
            _seg("c005-web", _rule("web-to-backend", "c005-backend", src_tag="k-web"), isolation="microsegmented"),
            _seg("c005-backend"),
        ]
        view = segment_security_view(_groups(segments, tags), get_vrf_contracts(segments, tags), {})
        backend = next(item for item in view["segments"] if item["name"] == "c005-backend")
        assert backend["policies"] == []
        assert backend["fallback"] == {"entries": TCP_443, "rules": ["web-to-backend"]}
        assert [(item["src"], item["dst"]) for item in view["unsupported"]] == [("k-web.example", "c005-backend")]
        web = next(item for item in view["segments"] if item["name"] == "c005-web")
        assert web["to_unclassified"] is None, "the component's leg out is not the segment's"

    def test_a_segment_level_rule_keeps_its_segment_row_and_lists_the_component_rows_as_unsupported(self) -> None:
        """A hand-written rule into a microsegmented segment: the segment row stays, the per-component copies are not expressible."""
        segments, tags = _fleet()
        view = segment_security_view(_groups(segments, tags), get_vrf_contracts(segments, tags), {})
        web = next(item for item in view["segments"] if item["name"] == "c005-web")
        assert web["policies"] == [{"from": "c008-portal", "entries": TCP_443, "rules": ["portal-to-web"]}]
        assert {(item["src"], item["dst"]) for item in view["unsupported"]} >= {
            ("c008-portal", "k-web.example"),
            ("c008-portal", "k-session.example"),
            (UNCLASSIFIED, "k-web.example"),
        }

    def test_rows_to_unclassified_are_no_policy_but_kept_as_the_segments_outbound_for_its_replies(self) -> None:
        """MSS-G only enforces traffic to a segment; the reply of a flow out of a segment is traffic into it."""
        segments, tags = _fleet()
        view = segment_security_view(_groups(segments, tags), get_vrf_contracts(segments, tags), {})
        backend = next(item for item in view["segments"] if item["name"] == "c005-backend")
        assert backend["policies"] == [
            {
                "from": "c005-web",
                "entries": [{"protocol": "tcp", "port_start": 8443, "port_end": 8443}],
                "rules": ["web-to-backend"],
            }
        ]
        assert all(policy["from"] != UNCLASSIFIED for item in view["segments"] for policy in item["policies"])
        web = next(item for item in view["segments"] if item["name"] == "c005-web")
        assert web["to_unclassified"] == {
            "entries": [{"protocol": "tcp", "port_start": 8443, "port_end": 8443}],
            "rules": ["web-to-backend"],
        }
        assert backend["to_unclassified"] is None

    def test_only_local_segments_and_the_segments_their_rows_name_are_listed(self) -> None:
        """A ``from`` line needs its source defined: a remote source is listed too; an unrelated remote segment is not."""
        segments, tags = _fleet()
        rows = get_vrf_contracts(segments, tags, local={"c005-backend"})
        view = segment_security_view(_groups(segments, tags), rows, {}, local={"c005-backend"})
        assert [item["name"] for item in view["segments"]] == ["c005-backend", "c005-web"]
        assert [
            item["name"]
            for item in segment_security_view(_groups(segments, tags), [], {}, local={"c008-portal"})["segments"]
        ] == ["c008-portal"]

    def test_a_remote_destination_of_a_local_source_is_listed_with_its_policy(self) -> None:
        """A row from a local segment to a remote one is a policy under the remote destination."""
        segments, tags = _fleet()
        rows = get_vrf_contracts(segments, tags, local={"c008-portal"})
        view = segment_security_view(_groups(segments, tags), rows, {}, local={"c008-portal"})
        web = next(item for item in view["segments"] if item["name"] == "c005-web")
        assert web["policies"] == [{"from": "c008-portal", "entries": TCP_443, "rules": ["portal-to-web"]}]


# ---------------------------------------------------------------------------
# The artifact
# ---------------------------------------------------------------------------


def _v(value: Any) -> dict[str, Any]:
    return {"value": value}


def _device(
    name: str, role: str, device_type: str, version: str, memory_gb: int = 32, platform: str = "cisco_nxos"
) -> dict[str, Any]:
    """One raw DeviceGroupPolicyFields edge."""
    return {
        "node": {
            "name": _v(name),
            "role": _v(role),
            "platform": {"node": {"name": _v(platform)}},
            "device_type": {"node": {"name": _v(device_type), "memory_gb": _v(memory_gb)}},
            "software_image": {"node": {"version": _v(version)}},
        }
    }


def _profile_edge(
    name: str, kind: str, platform: str, device_types: tuple[str, ...], **reserved: int
) -> dict[str, Any]:
    return {
        "node": {
            "name": _v(name),
            "group_policy_kind": _v(kind),
            "min_release": _v("10.6(1)F" if platform == "cisco_nxos" else "4.36.2F"),
            "min_memory_gb": _v(24 if platform == "cisco_nxos" else None),
            "unclassified_group_id": _v(reserved.get("unclassified")),
            "unaware_group_id": _v(reserved.get("unaware")),
            "platform": {"node": {"name": _v(platform)}},
            "device_types": {"edges": [{"node": {"name": _v(item)}} for item in device_types]},
        }
    }


NXOS_PROFILE = _profile_edge(
    "reference-cisco-nxos", "vxlan_gpo", "cisco_nxos", ("N9K-C93180YC-FX3", "N9K-C9364C-GX"), unclassified=0, unaware=15
)
EOS_PROFILE = _profile_edge("reference-arista-eos", "segment_security", "arista_eos", ("DCS-7280SR3-48YC8",))


def _raw_rule(name: str, dst: str | None, port: int, *, src_tag: str | None = None, dst_tag: str | None = None) -> dict:
    return {
        "node": {
            "id": f"rule-{name}",
            "index": _v(100),
            "name": _v(name),
            "action": _v("permit"),
            "protocol": _v("tcp"),
            "port_start": _v(port),
            "port_end": _v(None),
            "disabled": _v(False),
            "apply_on_switch": _v(True),
            "source_segment": {"node": None},
            "destination_segment": {"node": {"id": dst} if dst else None},
            "source_security_tag": {"node": {"id": src_tag} if src_tag else None},
            "destination_security_tag": {"node": {"id": dst_tag} if dst_tag else None},
        }
    }


def _raw_segment(
    seg_id: str,
    name: str,
    customer: str,
    *rules: dict[str, Any],
    sites: tuple[str, ...] = ("dc10",),
    isolation: str = "normal",
    tag: tuple[str, int] | None = None,
    kind: str = "ManagedVxlanSegment",
    namespace: tuple[str, str] = ("PROD", "prod"),
) -> dict[str, Any]:
    """One raw SegmentGroupPolicyFields edge."""
    node: dict[str, Any] = {
        "id": seg_id,
        "__typename": kind,
        "name": _v(name),
        "isolation_mode": _v(isolation),
        "owner": {"node": {"org_id": _v(customer)}},
        "security_zone": {"node": {"name": _v("PROD-ZONE" if namespace[1] == "prod" else "MANAGEMENT-ZONE")}},
        "security_tag": {"node": {"id": f"tag-{seg_id}", "name": _v(tag[0]), "group_id": _v(tag[1])}}
        if tag
        else {"node": None},
        "gateway": {
            "node": {
                "ip_prefix": {
                    "node": {
                        "prefix": _v(f"10.9.{len(name)}.0/24"),
                        "ip_namespace": {"node": {"name": _v(namespace[0]), "namespace_type": _v(namespace[1])}},
                    }
                }
            }
        },
        "security_policy": {
            "node": {
                "name": _v(f"seg-{name}-egress"),
                "enabled": _v(True),
                "default_action": _v("deny"),
                "rules": {"edges": list(rules)},
            }
        },
        "inline_service": {"node": None},
    }
    if kind == "ManagedVxlanSegment":
        node["stretch_scope"] = _v("local" if len(sites) == 1 else "dc_pair")
        node["segment_deployments"] = {
            "edges": [
                {"node": {"vni": _v(10100 + n), "deployment": {"node": {"id": site, "name": _v(site.upper())}}}}
                for n, site in enumerate(sites)
            ]
        }
    else:
        node["vlan_id"] = _v(250)
        node["customer_deployment"] = {
            "node": {"id": f"fp-{seg_id}", "parent": {"node": {"id": sites[0], "__typename": "TopologyDataCenter"}}}
        }
    return {"node": node}


def _raw_component_tag(tag_id: str, group_id: int, segment: str) -> dict[str, Any]:
    return {
        "node": {
            "id": tag_id,
            "name": _v(f"{tag_id}.example"),
            "group_id": _v(group_id),
            "components": {
                "edges": [{"node": {"fqdn": _v(f"{tag_id}.example"), "network_segment": {"node": {"id": segment}}}}]
            },
        }
    }


def _payload(
    *,
    border_version: str = "10.6(3)F",
    access_leaf: bool = True,
    profiles: list[dict] | None = None,
    enforce: bool | None = True,
    dc12_enforce: bool | None = False,
) -> dict:
    """A raw segmentation_matrix response for DC10 (FX3/GX, an EX access leaf by default) next to a legacy DC12.

    ``enforce`` / ``dc12_enforce`` are the data centers' ``group_policy_enforce`` (None: the key is absent).

    Segments: C008's portal (DC10) calling C005's microsegmented web (DC10, components web and session,
    one intra-segment rule), C005's backend stretched DC10 <-> DC12, a remote DC12 segment, the untagged
    INFRA MANAGEMENT VLAN segment and an untagged C007 tenant segment in DC10.
    """
    pod_devices = [_device("lf-01", "leaf", "N9K-C93180YC-FX3", "10.6(1)F")]
    if access_leaf:
        pod_devices.append(_device("al-01", "access-leaf", "N9K-C93180YC-EX", "10.3(3)", memory_gb=24))
    segments = [
        _raw_segment(
            "s-portal",
            "c008-trade-portal-local-dc10-p",
            "C008",
            _raw_rule("portal-to-web", "s-web", 443, dst_tag="k-web"),
            tag=("c008-trade-portal-local-dc10-p", 40),
        ),
        _raw_segment(
            "s-web",
            "c005-web-frontend-local-dc10-p",
            "C005",
            _raw_rule("web-to-session", "s-web", 6379, src_tag="k-web", dst_tag="k-session"),
            _raw_rule("web-to-backend", "s-backend", 8443),
            isolation="microsegmented",
            tag=("c005-web-frontend-local-dc10-p", 41),
        ),
        _raw_segment(
            "s-backend",
            "c005-app-backend-stretch-p",
            "C005",
            sites=("dc10", "dc12"),
            tag=("c005-app-backend-stretch-p", 42),
        ),
        _raw_segment(
            "s-remote",
            "c010-remote-p",
            "C010",
            _raw_rule("remote-to-remote", "s-remote2", 22),
            sites=("dc12",),
            tag=("c010-remote-p", 43),
        ),
        _raw_segment("s-remote2", "c010-remote2-p", "C010", sites=("dc12",), tag=("c010-remote2-p", 44)),
        _raw_segment(
            "s-infra",
            "infra-core-dns-ntp-p",
            "INFRA",
            kind="ManagedVlanSegment",
            namespace=("MANAGEMENT", "management"),
        ),
        _raw_segment("s-c007", "c007-untagged-p", "C007"),
    ]

    def flag(value: bool | None) -> dict:
        return {} if value is None else {"group_policy_enforce": _v(value)}

    return {
        "target": {"edges": [{"node": {"id": "dc10", "name": _v("DC10"), **flag(enforce)}}]},
        "TopologyDataCenter": {
            "edges": [
                {
                    "node": {
                        "id": "dc10",
                        "name": _v("DC10"),
                        **flag(enforce),
                        "devices": {"edges": [_device("bl-01", "border-leaf", "N9K-C9364C-GX", border_version)]},
                    }
                },
                {
                    "node": {
                        "id": "dc12",
                        "name": _v("DC12"),
                        **flag(dc12_enforce),
                        "devices": {"edges": [_device("bl-12", "border-leaf", "N9K-C9336C-FX2", "10.3(3)", 24)]},
                    }
                },
            ]
        },
        "TopologyPod": {
            "edges": [
                {
                    "node": {
                        "parent": {"node": {"id": "dc10", "__typename": "TopologyDataCenter"}},
                        "devices": {"edges": pod_devices},
                    }
                }
            ]
        },
        "DcimGroupPolicyProfile": {"edges": [NXOS_PROFILE] if profiles is None else profiles},
        "ManagedNetworkSegment": {"edges": segments},
        "SecurityTag": {
            "edges": [_raw_component_tag("k-web", 50, "s-web"), _raw_component_tag("k-session", 51, "s-web")]
        },
    }


def _render(payload: dict[str, Any]) -> dict[str, Any]:
    """Run the transform on a raw response."""
    transform = SegmentationMatrix.__new__(SegmentationMatrix)
    return asyncio.run(transform.transform(payload))


class TestSegmentationMatrixSite:
    """The DC's reference verdict names every VTEP outside its profile row."""

    def test_site_names_the_ex_access_leaf(self) -> None:
        result = _render(_payload())
        assert result["deployment"] == "DC10"
        assert result["site"] == {
            "reference": False,
            "kind": "vxlan_gpo",
            "reason": "access-leaf al-01 N9K-C93180YC-EX: device type not in profile reference-cisco-nxos",
            "enforce": True,
        }

    def test_site_names_every_distinct_cause(self) -> None:
        result = _render(_payload(border_version="10.3(3)"))
        assert result["site"]["reason"] == (
            "access-leaf al-01 N9K-C93180YC-EX: device type not in profile reference-cisco-nxos; "
            "border-leaf bl-01 N9K-C9364C-GX: release 10.3(3) < 10.6(1)F"
        )
        assert [(vtep["name"], vtep["reason"]) for vtep in result["vteps"]] == [
            ("al-01", "device type not in profile reference-cisco-nxos"),
            ("bl-01", "release 10.3(3) < 10.6(1)F"),
            ("lf-01", None),
        ]

    def test_reference_site(self) -> None:
        result = _render(_payload(access_leaf=False))
        assert result["site"] == {"reference": True, "kind": "vxlan_gpo", "reason": None, "enforce": True}

    def test_site_enforce_is_the_staging_switch_of_this_data_center(self) -> None:
        """False for a staged DC10 even when another data center enforces; a missing key is False."""
        assert _render(_payload(enforce=False, dc12_enforce=True))["site"]["enforce"] is False
        assert _render(_payload(enforce=True, dc12_enforce=False))["site"]["enforce"] is True
        assert _render(_payload(enforce=None))["site"]["enforce"] is False

    def test_enforce_does_not_change_the_contracts_or_the_reference_verdict(self) -> None:
        enforcing, staged = _render(_payload(access_leaf=False)), _render(_payload(access_leaf=False, enforce=False))
        assert staged["site"] == {**enforcing["site"], "enforce": False}
        assert {key: value for key, value in staged.items() if key != "site"} == {
            key: value for key, value in enforcing.items() if key != "site"
        }

    def test_non_vtep_devices_are_not_listed(self) -> None:
        payload = _payload(access_leaf=False)
        payload["TopologyPod"]["edges"][0]["node"]["devices"]["edges"].append(
            _device("sp-01", "spine", "N9K-C9336C-FX2", "10.3(3)")
        )
        result = _render(payload)
        assert [vtep["name"] for vtep in result["vteps"]] == ["bl-01", "lf-01"]
        assert result["site"]["reference"] is True

    def test_without_a_profile_row_nothing_is_reference_and_no_kind_is_rendered(self) -> None:
        result = _render(_payload(access_leaf=False, profiles=[]))
        assert result["site"]["reference"] is False
        assert result["site"]["kind"] is None
        assert result["kinds"] == {}


class TestSegmentationMatrixGroups:
    """Segments active in the DC, their groups, classification and contracts."""

    def test_segments_of_the_dc_with_group_customer_and_vrf(self) -> None:
        """DC12-only segments are not listed; the MANAGEMENT and untagged segments are, without a group."""
        segments = {item["name"]: item for item in _render(_payload())["segments"]}
        assert sorted(segments) == [
            "c005-app-backend-stretch-p",
            "c005-web-frontend-local-dc10-p",
            "c007-untagged-p",
            "c008-trade-portal-local-dc10-p",
            "infra-core-dns-ntp-p",
        ]
        assert segments["c005-web-frontend-local-dc10-p"] == {
            "name": "c005-web-frontend-local-dc10-p",
            "group": "c005-web-frontend-local-dc10-p",
            "customer": "C005",
            "vrf": "PROD",
            "isolation_mode": "microsegmented",
            "gbp_required": True,
            "leaf_svi_acl": True,
            "inline_firewall": None,
        }
        assert segments["infra-core-dns-ntp-p"]["group"] is None

    def test_groups_are_the_dcs_segment_and_component_groups(self) -> None:
        groups = [(item["name"], item["kind"], item["intra_group"]) for item in _render(_payload())["groups"]]
        assert groups == [
            ("c005-app-backend-stretch-p", "segment", "permit"),
            ("c005-web-frontend-local-dc10-p", "segment", "deny"),
            ("k-session.example", "component", "permit"),
            ("k-web.example", "component", "permit"),
            ("c008-trade-portal-local-dc10-p", "segment", "permit"),
        ]

    def test_classification_selects_by_vni_or_vlan_and_lists_untagged_tenants(self) -> None:
        """The untagged C007 tenant segment is unclassified; the MANAGEMENT segment is never a tenant."""
        classification = _render(_payload())["classification"]
        portal = next(
            item for item in classification["selectors"] if item["segment"] == "c008-trade-portal-local-dc10-p"
        )
        assert portal == {
            "group": "c008-trade-portal-local-dc10-p",
            "segment": "c008-trade-portal-local-dc10-p",
            "vrf": "PROD",
            "vlan_id": None,
            "vni": 10100,
            "prefix": "10.9.30.0/24",
        }
        assert classification["unclassified"] == ["c007-untagged-p"]

    def test_contracts_are_local_and_deny_by_default(self) -> None:
        """The cross-customer rule names the web component: one cross-customer row, to that component,
        plus its service legs; a rule between two DC12 segments is not here."""
        contracts = _render(_payload())["contracts"]
        assert contracts["default_action"] == "deny"
        rows = contracts["rows"]
        assert [(row["src"], row["dst"]) for row in rows if row["cross_customer"]] == [
            ("c008-trade-portal-local-dc10-p", "k-web.example"),
        ]
        assert [(row["src"], row["dst"]) for row in rows if "portal-to-web" in row["rules"]] == [
            ("c008-trade-portal-local-dc10-p", "k-web.example"),
            ("c008-trade-portal-local-dc10-p", UNCLASSIFIED),
            (UNCLASSIFIED, "k-web.example"),
        ]
        assert not any("remote-to-remote" in row["rules"] for row in rows)
        assert ("k-web.example", "k-session.example") in {(row["src"], row["dst"]) for row in rows}

    def test_vxlan_gpo_view_treats_the_dc_as_reference_and_dc12_as_unaware(self) -> None:
        """DC10 is rendered as it would enforce once reference; DC12 (legacy) is the unaware group."""
        view = _render(_payload())["kinds"]["vxlan_gpo"]
        assert view["reserved"] == {"unaware": 15, "unclassified": 0}
        pairs = {(row["src"], row["dst"]): row for row in view["contracts"]}
        assert pairs[(UNAWARE, "c005-app-backend-stretch-p")]["src_group_id"] == 15
        assert pairs[("c008-trade-portal-local-dc10-p", "k-web.example")]["dst_group_id"] == 50
        assert ("c008-trade-portal-local-dc10-p", "c005-web-frontend-local-dc10-p") not in pairs
        assert (UNAWARE, "c008-trade-portal-local-dc10-p") not in pairs

    def test_one_section_per_kind_the_profile_names(self) -> None:
        result = _render(_payload(profiles=[NXOS_PROFILE, EOS_PROFILE]))
        assert sorted(result["kinds"]) == ["segment_security", "vxlan_gpo"]
        names = [item["name"] for item in result["kinds"]["segment_security"]["segments"]]
        assert "c010-remote-p" not in names
        assert "c008-trade-portal-local-dc10-p" in names

    def test_segment_security_kind_agrees_with_the_device_on_component_rows(self) -> None:
        """D4: a cross-segment row naming a component group is unsupported in the artifact, never the whole segment's."""
        view = _render(_payload(profiles=[NXOS_PROFILE, EOS_PROFILE]))["kinds"]["segment_security"]
        by_name = {item["name"]: item for item in view["segments"]}
        assert by_name["c005-web-frontend-local-dc10-p"]["policies"] == []
        assert by_name["c005-web-frontend-local-dc10-p"]["fallback"] is None
        assert {(item["src"], item["dst"]) for item in view["unsupported"]} >= {
            ("c008-trade-portal-local-dc10-p", "k-web.example"),
            (UNCLASSIFIED, "k-web.example"),
            ("k-web.example", "c005-app-backend-stretch-p"),
        }

    def test_re_render_is_byte_identical(self) -> None:
        assert json.dumps(_render(_payload()), sort_keys=True) == json.dumps(_render(_payload()), sort_keys=True)

    def test_missing_data_center_is_refused(self) -> None:
        with pytest.raises(ValueError, match="no TopologyDataCenter"):
            _render({"target": {"edges": []}})

    def test_registered_as_a_dc_artifact(self, root_dir: Path) -> None:
        """Artifact definition, python transform and query, like the cable matrix."""
        import yaml

        config = yaml.safe_load((root_dir / ".infrahub.yml").read_text())
        artifact = next(
            item for item in config["artifact_definitions"] if item["transformation"] == "segmentation_matrix"
        )
        assert (artifact["targets"], artifact["content_type"], artifact["parameters"]) == (
            "topologies_dc",
            "application/json",
            {"id": "id"},
        )
        assert any(item["name"] == "segmentation_matrix" for item in config["python_transforms"])
        assert any(item["name"] == SegmentationMatrix.query for item in config["queries"])

    def test_query_reads_the_fleet_through_the_builders_fragment(self, root_dir: Path) -> None:
        """Contracts are fabric-wide: every segment (no DC filter) and every DC's VTEPs, the pod parent typed."""
        text = (root_dir / "queries/topology/segmentation_matrix.gql").read_text()
        assert "...SegmentGroupPolicyFields" in text
        assert "target: TopologyDataCenter(ids: [$id])" in text
        assert "parent { node { id __typename } }" in text
