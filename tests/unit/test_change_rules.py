"""utils/change_rules.py: every field of a change request, derived from the diffs and the device facts."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from utils.change_rules import (
    CUSTOMER_WEIGHT_CAP,
    DESCRIPTION_MAX_CHARS,
    LABELS_PER_KIND,
    MAINTENANCE_TZ,
    MIN_WINDOW_MINUTES,
    PROD_WEIGHT,
    REMOVED_WEIGHT,
    ROUTING_WEIGHT,
    STEP_MINUTES,
    ArtifactChange,
    ChangeInputs,
    DeviceFacts,
    DiffNode,
    affected_customers,
    artifact_fingerprint,
    backout_plan,
    change_type,
    communication_plan,
    conflicts,
    derive_change_request,
    description,
    device_facts,
    effective_risk,
    impact,
    implementation_plan,
    next_window,
    parse_artifact_diff,
    parse_datetime,
    parse_diff_nodes,
    planned_window,
    prod_objects,
    risk_level,
    risk_rank,
    risk_score,
    role_weight,
    short_description,
    verification_plan,
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
        "diff_nodes": [DiffNode("ManagedVxlanSegment", "c005-web", "ADDED", "s1")],
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

    def test_unchanged_and_bookkeeping_nodes_are_dropped(self) -> None:
        """UNCHANGED parents, the request itself and the group memberships are never counted."""
        nodes = parse_diff_nodes(
            [
                {"kind": "ManagedVxlanSegment", "label": "a", "status": "ADDED", "uuid": "1"},
                {"kind": "TopologyDataCenter", "label": "dc1", "status": "UNCHANGED", "uuid": "2"},
                {"kind": "ChangeRequest", "label": "CHG-x", "status": "ADDED", "uuid": "3"},
                {"kind": "CoreStandardGroup", "label": "vxlan_segments", "status": "UPDATED", "uuid": "4"},
            ]
        )

        assert nodes == [DiffNode("ManagedVxlanSegment", "a", "ADDED", "1")]

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

    def test_customers_are_the_customer_owners_of_devices_footprints_and_objects(self) -> None:
        """A provider owner is not a customer; a segment owner (peer OrganizationCustomer) carries no typename."""
        cleaned = {
            "DcimDeviceManaged": [
                {"owner": {"typename": "OrganizationProvider"}},
                {
                    "owner": None,
                    "deployment": {"owner": {"typename": "OrganizationCustomer", "id": "c5", "name": "C005"}},
                },
            ],
            "ManagedNetworkSegment": [{"owner": {"id": "c1", "name": "C001"}}],
            "AppApplication": [{"owner": {"id": "c1", "name": "C001"}}],
        }

        assert affected_customers(cleaned) == {"c1": "C001", "c5": "C005"}

    def test_a_prod_footprint_or_an_office_is_a_prod_object(self) -> None:
        """The office is always PROD; a NONPROD footprint alone is not."""
        assert prod_objects({"TopologyCustomer": [{"environment": "p"}]})
        assert prod_objects({"TopologyCustomerOffice": [{"id": "o"}]})
        assert not prod_objects({"TopologyCustomer": [{"environment": "n"}]})


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
            diff_nodes=[DiffNode("ManagedBGPPeering", "p", "REMOVED"), DiffNode("RoutingPolicyTerm", "t", "UPDATED")],
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
        nodes = [DiffNode("ManagedVxlanSegment", "s", "ADDED"), DiffNode("IpamIPAddress", "a", "ADDED")]
        assert change_type(_inputs(diff_nodes=nodes), "low") == "standard"

    @pytest.mark.parametrize(
        ("nodes", "risk", "truncated"),
        [
            ([DiffNode("ManagedVxlanSegment", "s", "ADDED")], "moderate", False),
            ([DiffNode("ManagedVxlanSegment", "s", "REMOVED")], "low", False),
            ([DiffNode("ManagedBGP", "b", "UPDATED")], "low", False),
            ([DiffNode("ManagedVxlanSegment", "s", "ADDED")], "low", True),
            ([], "low", False),
        ],
        ids=["risk", "removal", "other-kind", "truncated", "empty"],
    )
    def test_anything_else_is_normal(self, nodes: list[DiffNode], risk: str, truncated: bool) -> None:
        """A higher risk, a removal, a non-standard kind, a diff read in part or no change at all."""
        assert change_type(_inputs(diff_nodes=nodes, diff_truncated=truncated), risk) == "normal"


class TestTexts:
    """short_description, description and the plans."""

    def test_short_description_counts_objects_and_configs(self) -> None:
        """Counted from the nodes read; the configs are the artifact entries."""
        inputs = _inputs(artifacts=[_artifact("a"), _artifact("b")])
        assert short_description(inputs) == "add segment: 1 added, 0 updated, 0 removed objects; 2 device configs"

    def test_a_truncated_diff_takes_the_server_counts(self) -> None:
        """The part read is not the whole: the summary counts."""
        inputs = _inputs(diff_truncated=True, diff_counts={"ADDED": 900, "UPDATED": 5, "REMOVED": 1})
        assert short_description(inputs).startswith("add segment: 900 added, 5 updated, 1 removed objects")

    def test_description_groups_by_status_then_kind_caps_labels_and_ends_with_the_pc_text(self) -> None:
        """Added before Updated, kinds sorted, at most LABELS_PER_KIND labels and the rest counted."""
        nodes = [DiffNode("IpamIPAddress", f"10.0.0.{i}/32", "ADDED") for i in range(LABELS_PER_KIND + 2)]
        nodes.append(DiffNode("DcimPhysicalDevice", "leaf-01", "UPDATED"))

        text = description(_inputs(diff_nodes=nodes))

        assert text.index("Added:") < text.index("Updated:")
        assert f"IpamIPAddress ({LABELS_PER_KIND + 2}):" in text
        assert "(+2 more)" in text
        assert text.endswith("Adds a web segment for C005.")

    def test_description_is_capped_and_keeps_the_pc_text(self) -> None:
        """A huge listing is cut at DESCRIPTION_MAX_CHARS; the PC description still follows."""
        nodes = [DiffNode(f"Kind{i:04d}", "x" * 50, "ADDED") for i in range(400)]
        text = description(_inputs(diff_nodes=nodes))
        listing = text.rsplit("\n\n", 1)[0]
        assert len(listing) <= DESCRIPTION_MAX_CHARS
        assert text.endswith("Adds a web segment for C005.")

    def test_implementation_plan_least_critical_first_and_a_pair_member_before_its_peer(self) -> None:
        """Leaves before the spine; of the MLAG pair one leaf, verify, then the other; checksums old -> new."""
        devices = [
            DeviceFacts("s", "spine-01", "spine"),
            DeviceFacts("b", "leaf-02", "leaf", peer="leaf-01"),
            DeviceFacts("a", "leaf-01", "leaf", peer="leaf-02"),
            DeviceFacts("c", "host-01", "endpoint"),
        ]
        artifacts = [
            _artifact("spine-01"),
            _artifact("leaf-02"),
            _artifact("leaf-01", previous=None),
            _artifact("host-01"),
        ]

        plan = implementation_plan(_inputs(devices=devices, artifacts=artifacts)).splitlines()

        assert [line.split(" ")[2].rstrip(":") for line in plan] == [
            "host-01",
            "host-01",
            "leaf-01",
            "leaf-01",
            "leaf-02",
            "leaf-02",
            "spine-01",
            "spine-01",
        ]
        assert plan[1] == "2. Verify host-01 before the next step"
        assert "leaf: new config n1" in plan[2]
        assert "leaf: p1 -> n1" in plan[4]

    def test_a_pair_is_rolled_out_at_the_weight_of_its_more_critical_member(self) -> None:
        """An HA pair whose peer is a firewall goes after a plain leaf, its two members back to back."""
        devices = [
            DeviceFacts("f1", "fw-01", "internal-firewall", peer="fw-02"),
            DeviceFacts("f2", "fw-02", "internal-firewall", peer="fw-01"),
            DeviceFacts("l", "leaf-09", "leaf"),
        ]
        artifacts = [_artifact("fw-02"), _artifact("fw-01"), _artifact("leaf-09")]
        plan = implementation_plan(_inputs(devices=devices, artifacts=artifacts)).splitlines()
        assert [line.split(" ")[2].rstrip(":") for line in plan[::2]] == ["leaf-09", "fw-01", "fw-02"]

    def test_no_artifact_is_a_data_only_plan(self) -> None:
        """Nothing to deploy."""
        assert implementation_plan(_inputs()).startswith("No device configuration changes")

    def test_backout_returns_to_the_previous_config_and_warns_on_removals(self) -> None:
        """The previous checksum and storage id, a new device's config removed, the auto-rollback line and the
        allocation warning when an object is removed."""
        inputs = _inputs(
            artifacts=[_artifact("leaf-01"), _artifact("leaf-03", previous=None)],
            diff_nodes=[DiffNode("IpamPrefix", "10.0.0.0/24", "REMOVED")],
        )
        plan = backout_plan(inputs)
        assert "Revoking the approval rolls every device back automatically." in plan
        assert "leaf-01 leaf: back to p1 (storage st-p1)" in plan
        assert "leaf-03 leaf: new device: config removed" in plan
        assert "WARNING" in plan
        assert "WARNING" not in backout_plan(_inputs(artifacts=[_artifact("leaf-01")]))

    def test_test_plan_names_the_expected_sessions_and_the_checksum_verification(self) -> None:
        """Per device its role and BGP sessions."""
        plan = verification_plan(
            _inputs(devices=[DeviceFacts("a", "leaf-01", "leaf", bgp_peerings=4), DeviceFacts("b", "h", None)])
        )
        assert "running config checksum equals the artifact checksum" in plan
        assert "leaf-01 (leaf): 4 BGP peering(s) established" in plan
        assert "h (no role): no BGP peering" in plan

    def test_communication_plan_lists_the_customers(self) -> None:
        """Sorted names, or the operations team alone."""
        assert communication_plan(_inputs(customers=["C005", "C001"])) == "Notify the affected customers: C001, C005"
        assert "No customer" in communication_plan(_inputs())

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

    def test_derive_returns_every_derived_field_and_no_author_field(self) -> None:
        """The generator writes exactly these keys."""
        derived = derive_change_request(
            _inputs(artifacts=[_artifact("leaf-01")], devices=[DeviceFacts("a", "leaf-01", "leaf")])
        )
        assert set(derived) == {
            "short_description",
            "description",
            "change_type",
            "risk",
            "risk_score",
            "impact",
            "implementation_plan",
            "backout_plan",
            "test_plan",
            "communication_plan",
            "conflicts",
            "planned_start",
            "planned_end",
            "artifact_fingerprint",
        }
        assert derived["conflicts"] is None
        assert derived["change_type"] == "standard"
        assert derived["planned_start"] == NOW
