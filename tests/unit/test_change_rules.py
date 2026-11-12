"""utils/change_rules.py: every field of a change request, derived from the diffs and the device facts."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from utils.change_rules import (
    CUSTOMER_WEIGHT_CAP,
    MAINTENANCE_TZ,
    MIN_WINDOW_MINUTES,
    PROD_WEIGHT,
    REMOVED_WEIGHT,
    ROUTING_WEIGHT,
    STEP_MINUTES,
    ArtifactChange,
    ChangeInputs,
    DeviceFacts,
    ObjectDiff,
    affected_customers,
    artifact_fingerprint,
    change_type,
    conflicts,
    device_facts,
    effective_risk,
    impact,
    next_window,
    parse_artifact_diff,
    parse_datetime,
    parse_diff_summaries,
    parse_object_nodes,
    planned_window,
    prod_objects,
    risk_level,
    risk_rank,
    risk_score,
    role_weight,
    window_minutes,
)

NOW = datetime(2026, 11, 12, 9, 0, tzinfo=UTC)  # a Thursday, 10:00 in Warsaw


def _artifact(
    target: str,
    definition: str = "leaf",
    *,
    new: str | None = "n1",
    previous: str | None = "p1",
    action: str = "updated",
) -> ArtifactChange:
    return ArtifactChange(
        id=f"art-{target}-{definition}",
        action=action,
        target_id=f"id-{target}",
        target_kind="DcimPhysicalDevice",
        target=target,
        definition=definition,
        checksum_new=new,
        checksum_previous=previous,
        storage_previous=f"st-{previous}" if previous else None,
    )


def _inputs(**overrides: Any) -> ChangeInputs:
    base: dict[str, Any] = {
        "pc_name": "add segment",
        "pc_description": "Adds a web segment for C005.",
        "branch": "feat/segment",
        "now": NOW,
        "artifacts": [],
        "diff_counts": {"ADDED": 1, "UPDATED": 0, "REMOVED": 0},
    }
    base.update(overrides)
    return ChangeInputs(**base)


class TestParsing:
    """The REST artifact diff and the DiffTree nodes, as the server returns them (measured on 1.12)."""

    def test_an_artifact_entry_splits_its_label_into_target_and_definition(self) -> None:
        """display_label is "<device> - <definition>"; the checksums and the previous storage id are kept."""
        payload = {
            "a1": {
                "branch": "b",
                "id": "a1",
                "display_label": "dc1-leaf-01 - leaf_config",
                "action": "updated",
                "target": {"id": "d1", "kind": "DcimPhysicalDevice", "display_label": "dc1-leaf-01"},
                "item_new": {"storage_id": "s-new", "checksum": "new"},
                "item_previous": {"storage_id": "s-old", "checksum": "old"},
            },
            "a0": {
                "id": "a0",
                "display_label": "dc1-bl-01 - border_leaf",
                "action": "added",
                "target": {"id": "d0", "kind": "DcimPhysicalDevice", "display_label": "dc1-bl-01"},
                "item_new": {"storage_id": "s", "checksum": "c"},
                "item_previous": None,
            },
        }

        changes = parse_artifact_diff(payload)

        assert [(c.target, c.definition, c.checksum_previous) for c in changes] == [
            ("dc1-bl-01", "border_leaf", None),
            ("dc1-leaf-01", "leaf_config", "old"),
        ]
        assert changes[1].storage_previous == "s-old"
        assert changes[1].target_id == "d1"

    def test_an_empty_or_missing_payload_is_no_change(self) -> None:
        """No artifact diff (null or {}) is an empty list."""
        assert parse_artifact_diff(None) == []
        assert parse_artifact_diff({}) == []

    def test_an_object_node_has_its_values_peers_and_changes(self) -> None:
        """A value is the HAS_VALUE property (the previous one on a removed node, "NULL" is unset); a peer is a label and
        an id; an updated attribute keeps its previous value; a dropped peer of an update is listed apart."""
        has = "HAS_VALUE"
        nodes = parse_object_nodes(
            [
                {
                    "uuid": "u1",
                    "kind": "SecurityPolicyRule",
                    "label": "r",
                    "status": "UPDATED",
                    "attributes": [
                        {
                            "name": "action",
                            "properties": [{"property_type": has, "new_value": "permit", "previous_value": "deny"}],
                        },
                        {
                            "name": "port_end",
                            "properties": [{"property_type": has, "new_value": "NULL", "previous_value": None}],
                        },
                    ],
                    "relationships": [
                        {
                            "name": "destination_segment",
                            "elements": [
                                {"peer_label": "b", "peer_id": "2", "status": "ADDED"},
                                {"peer_label": "a", "peer_id": "1", "status": "REMOVED"},
                            ],
                        }
                    ],
                },
                {
                    "uuid": "u2",
                    "kind": "DcimCable",
                    "label": "gone",
                    "status": "REMOVED",
                    "attributes": [
                        {
                            "name": "type",
                            "properties": [{"property_type": has, "new_value": None, "previous_value": "smf"}],
                        }
                    ],
                    "relationships": [
                        {"name": "endpoints", "elements": [{"peer_label": "x", "peer_id": "3", "status": "REMOVED"}]}
                    ],
                },
                {"uuid": "u3", "kind": "DcimCable", "label": "same", "status": "UNCHANGED"},
            ]
        )

        rule, cable = nodes
        assert rule.values == {"action": "permit", "port_end": None}
        assert rule.changes == {"action": ("deny", "permit")}
        assert (rule.peers, rule.peer_ids, rule.dropped) == (
            {"destination_segment": ("b",)},
            {"destination_segment": ("2",)},
            {"destination_segment": ("a",)},
        )
        assert cable.values == {"type": "smf"}
        assert cable.peers == {"endpoints": ("x",)}
        assert len(nodes) == 2

    def test_the_summaries_give_the_counts_the_standard_flag_and_the_routing_flag(self) -> None:
        """Standard: something changed and nothing of another kind; routing: any routing object changed."""
        none = {"num_added": 0, "num_updated": 0, "num_removed": 0}
        total = {"num_added": 5, "num_updated": 2, "num_removed": 1}
        assert parse_diff_summaries({"total": total, "non_standard": none, "routing": none}) == (
            {"ADDED": 5, "UPDATED": 2, "REMOVED": 1},
            True,
            False,
        )
        assert parse_diff_summaries({"total": total, "non_standard": total, "routing": total})[1:] == (False, True)
        assert parse_diff_summaries({"total": none, "non_standard": none, "routing": none})[1] is False
        assert parse_diff_summaries({}) == ({"ADDED": 0, "UPDATED": 0, "REMOVED": 0}, False, False)

    def test_a_datetime_value_is_read_aware(self) -> None:
        """The server's ISO value (Z or an offset) is an aware datetime; unset or garbage is None."""
        assert parse_datetime("2026-11-12T21:00:00Z") == datetime(2026, 11, 12, 21, tzinfo=UTC)
        assert parse_datetime("2026-11-12T21:00:00") == datetime(2026, 11, 12, 21, tzinfo=UTC)
        assert parse_datetime(None) is None
        assert parse_datetime("soon") is None


class TestFacts:
    """A cleaned row of change_request_facts.gql."""

    def test_a_device_row_gives_its_peer_bgp_sessions_and_prod(self) -> None:
        """The other member of its MLAG or HA domain is the peer; the sessions of its BGP processes add up; a PROD
        VRF membership is PROD."""
        row = {
            "id": "d1",
            "typename": "DcimPhysicalDevice",
            "name": "dc1-leaf-01",
            "role": "leaf",
            "capabilities": [
                {
                    "typename": "ManagedMLAG",
                    "members": [{"id": "d1", "name": "dc1-leaf-01"}, {"id": "d2", "name": "dc1-leaf-02"}],
                },
                {"typename": "ManagedBGP", "peerings": 3},
                {"typename": "ManagedBGP", "peerings": 2},
            ],
            "shared_capabilities": [{"typename": "ManagedVRF", "security_zone": {"name": "PROD"}}],
        }

        facts = device_facts(row)

        assert facts == DeviceFacts(
            id="d1",
            name="dc1-leaf-01",
            role="leaf",
            kind="DcimPhysicalDevice",
            prod=True,
            peer="dc1-leaf-02",
            bgp_peerings=5,
        )

    def test_a_virtual_device_in_a_prod_footprint_is_prod_and_a_nonprod_vrf_is_not(self) -> None:
        """The footprint's environment p is PROD; a device only in NONPROD is not."""
        assert device_facts({"id": "v", "name": "fw", "deployment": {"environment": "p"}}).prod
        assert not device_facts(
            {
                "id": "x",
                "name": "l",
                "shared_capabilities": [{"typename": "ManagedVRF", "security_zone": {"name": "NONPROD"}}],
            }
        ).prod

    def test_customers_are_the_customer_owners_of_devices_footprints_and_changed_objects(self) -> None:
        """A provider owner is not a customer; the owner of a changed segment or application is its ``owner`` peer."""
        cleaned = {
            "DcimDeviceManaged": [
                {"owner": {"typename": "OrganizationProvider"}},
                {
                    "owner": None,
                    "deployment": {"owner": {"typename": "OrganizationCustomer", "id": "c5", "name": "C005"}},
                },
            ]
        }
        objects = [
            ObjectDiff(
                "s", "ManagedVxlanSegment", "seg", "ADDED", peers={"owner": ("C001",)}, peer_ids={"owner": ("c1",)}
            ),
            ObjectDiff("a", "AppApplication", "app", "ADDED", peers={"owner": ("C001",)}, peer_ids={"owner": ("c1",)}),
            ObjectDiff("r", "SecurityPolicyRule", "rule", "ADDED", peers={"owner": ("X",)}, peer_ids={"owner": ("x",)}),
        ]

        assert affected_customers(cleaned, objects) == {"c1": "C001", "c5": "C005"}

    def test_a_prod_footprint_or_an_office_is_a_prod_object(self) -> None:
        """The office is always PROD; a NONPROD footprint alone is not; a removed footprint says so by its old value."""
        footprint = ObjectDiff("f", "TopologyCustomerDC", "fp", "ADDED", values={"environment": "p"})
        assert prod_objects([footprint])
        assert prod_objects([ObjectDiff("o", "TopologyCustomerOffice", "office", "UPDATED")])
        assert not prod_objects([ObjectDiff("f", "TopologyCustomerDC", "fp", "ADDED", values={"environment": "n"})])


class TestRisk:
    """The one weight table: role, removal, routing, PROD, customers, device count."""

    @pytest.mark.parametrize(
        ("role", "weight"),
        [
            ("spine", 30),
            ("border-leaf", 30),
            ("edge", 30),
            ("external-firewall", 30),
            ("load-balancer", 20),
            ("leaf", 10),
            ("tor", 10),
            ("endpoint", 3),
            (None, 3),
        ],
    )
    def test_role_weights(self, role: str | None, weight: int) -> None:
        """Large blast radius first, the load balancer, the access tier, the rest."""
        assert role_weight(role) == weight

    def test_one_leaf_outside_prod_is_low(self) -> None:
        """A leaf (10) plus one device (2) is 12: low."""
        inputs = _inputs(devices=[DeviceFacts("d1", "leaf-01", "leaf")], artifacts=[_artifact("leaf-01")])
        assert risk_score(inputs) == 12
        assert risk_level(12) == "low"

    def test_the_most_critical_role_counts_once_not_per_device(self) -> None:
        """Two spines score one spine plus the device count."""
        devices = [DeviceFacts("a", "s1", "spine"), DeviceFacts("b", "s2", "spine")]
        assert risk_score(_inputs(devices=devices)) == 30 + 4

    def test_removal_routing_prod_and_customers_add_up(self) -> None:
        """Each factor adds its weight; customers are capped."""
        inputs = _inputs(
            diff_counts={"ADDED": 0, "UPDATED": 1, "REMOVED": 1},
            routing_changed=True,
            customers=[f"C{i}" for i in range(10)],
            prod_objects=True,
        )
        assert risk_score(inputs) == REMOVED_WEIGHT + ROUTING_WEIGHT + PROD_WEIGHT + CUSTOMER_WEIGHT_CAP

    @pytest.mark.parametrize(
        ("score", "level"), [(0, "low"), (19, "low"), (20, "moderate"), (49, "moderate"), (50, "high")]
    )
    def test_thresholds(self, score: int, level: str) -> None:
        """< 20 low, < 50 moderate, else high."""
        assert risk_level(score) == level

    def test_the_override_is_the_effective_risk(self) -> None:
        """An author's override wins; none keeps the derived risk; levels rank low < moderate < high."""
        assert effective_risk("high", "low") == "low"
        assert effective_risk("moderate", None) == "moderate"
        assert risk_rank("low") < risk_rank("moderate") < risk_rank("high")

    @pytest.mark.parametrize(
        ("prod", "customers", "expected"), [(True, ["C1"], 1), (True, [], 2), (False, ["C1"], 2), (False, [], 3)]
    )
    def test_impact(self, prod: bool, customers: list[str], expected: int) -> None:
        """1 when PROD and customers, 2 when one of them, else 3."""
        assert impact(_inputs(prod_objects=prod, customers=customers)) == expected


class TestChangeType:
    """emergency, standard, normal."""

    def test_a_hotfix_branch_is_an_emergency(self) -> None:
        """Whatever it changes."""
        assert change_type(_inputs(branch="hotfix/bgp"), "high") == "emergency"

    def test_standard_kinds_only_at_low_risk_is_standard(self) -> None:
        """A new segment with its realization and an address, nothing removed."""
        assert change_type(_inputs(only_standard=True), "low") == "standard"

    @pytest.mark.parametrize(
        ("overrides", "risk"),
        [
            ({"only_standard": True}, "moderate"),
            ({"only_standard": True, "diff_counts": {"ADDED": 0, "UPDATED": 0, "REMOVED": 1}}, "low"),
            ({"only_standard": False}, "low"),
        ],
        ids=["risk", "removal", "other-kind-or-nothing"],
    )
    def test_anything_else_is_normal(self, overrides: dict[str, Any], risk: str) -> None:
        """A higher risk, a removal, a non-standard kind or no change at all."""
        assert change_type(_inputs(**overrides), risk) == "normal"


class TestConflicts:
    """The other open PCs whose request lists a device of this one."""

    def test_conflicts_are_the_other_pcs_sharing_a_device(self) -> None:
        """Only the shared devices are named; a PC with none is left out."""
        inputs = _inputs(
            devices=[DeviceFacts("a", "leaf-01", "leaf"), DeviceFacts("b", "leaf-02", "leaf")],
            conflicts={"other (feat/x)": ["leaf-02", "spine-01"], "far (feat/y)": ["spine-09"]},
        )
        assert conflicts(inputs) == "other (feat/x): leaf-02"
        assert conflicts(_inputs()) == ""


class TestWindow:
    """planned_start and planned_end."""

    def test_the_next_window_is_thursday_22_warsaw_after_now(self) -> None:
        """On Thursday morning it is the same evening; after it the next Thursday; across DST it stays 22:00 local."""
        assert next_window(NOW) == datetime(2026, 11, 12, 22, tzinfo=MAINTENANCE_TZ)
        assert next_window(datetime(2026, 11, 12, 21, 30, tzinfo=UTC)) == datetime(
            2026, 11, 19, 22, tzinfo=MAINTENANCE_TZ
        )
        summer = next_window(datetime(2026, 10, 20, 12, tzinfo=UTC))
        assert summer == datetime(2026, 10, 22, 22, tzinfo=MAINTENANCE_TZ)
        assert summer.utcoffset() == timedelta(hours=2)
        assert next_window(datetime(2026, 10, 30, 12, tzinfo=UTC)).utcoffset() == timedelta(hours=1)

    def test_the_duration_is_a_step_per_device_with_a_floor(self) -> None:
        """STEP_MINUTES per device, at least MIN_WINDOW_MINUTES."""
        assert window_minutes(1) == MIN_WINDOW_MINUTES
        assert window_minutes(10) == 10 * STEP_MINUTES

    def test_a_normal_change_waits_for_the_window(self) -> None:
        """Start in the window, end after the steps."""
        start, end = planned_window(_inputs(artifacts=[_artifact(f"d{i}") for i in range(6)]), "normal")
        assert start == datetime(2026, 11, 12, 22, tzinfo=MAINTENANCE_TZ)
        assert end - start == timedelta(minutes=60)

    def test_a_standard_change_starts_now_and_a_rerun_keeps_its_start(self) -> None:
        """A start a previous run set (already due) is kept; a future one (it was normal) moves to now."""
        assert planned_window(_inputs(), "standard")[0] == NOW
        earlier = NOW - timedelta(hours=1)
        assert planned_window(_inputs(current_planned_start=earlier), "emergency")[0] == earlier
        assert planned_window(_inputs(current_planned_start=NOW + timedelta(days=3)), "standard")[0] == NOW


class TestFingerprintAndDerive:
    """The fingerprint and the whole derivation."""

    def test_the_fingerprint_ignores_order_and_follows_the_new_checksums(self) -> None:
        """Same artifacts in another order: same hash; a new checksum: another."""
        a, b = _artifact("a"), _artifact("b")
        assert artifact_fingerprint([a, b]) == artifact_fingerprint([b, a])
        assert artifact_fingerprint([a, b]) != artifact_fingerprint([a, replace(b, checksum_new="n2")])
        assert len(artifact_fingerprint([])) == 64
