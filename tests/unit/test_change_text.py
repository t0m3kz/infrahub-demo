"""utils/change_text.py and templates/change/: the text fields of a change request, rendered from the facts."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from tests.unit.change_text_helpers import NOW, artifact, raw_node, realistic_inputs
from utils.change_rules import (
    DESCRIPTION_MAX_CHARS,
    DEVICE_MAX_ITEMS,
    SECTION_MAX_ITEMS,
    ChangeInputs,
    DeviceFacts,
    ObjectDiff,
    parse_object_nodes,
)
from utils.change_text import TEXT_FIELDS, derive_change_request, limit_chars, render_field

GOLDEN_DESCRIPTION = """\
Adds 2 network segments, 2 firewall rules, 1 cable. Updates 1 firewall rule, 1 carrier circuit. Removes 1 firewall rule. Device configuration: 1 new, 2 updated.

Data diff: 10 added, 3 updated, 1 removed objects.
Customers: C005
Sites: DC10

DEVICES (3)
DC10
  leaf
    - dc10-leaf-01: config updated; leaf_config p1 -> n1; pair dc10-leaf-02
    - dc10-leaf-02: new config; leaf_config new config n1; pair dc10-leaf-01
  spine
    - dc10-spine-01: config updated; leaf_config p3 -> n3

LINKS (2)
Added:
  - cable CBL-1 [smf, id C-100]: dc10-leaf-01-Ethernet1/49 <-> dc10-spine-01-Ethernet1/1; DC10
Updated:
  - carrier circuit DF10001 [dark_fiber, Equinix (P016)]: dc10-spine-01-Ethernet1/30 <-> dc20-spine-01-Ethernet1/30; DC10 <-> DC20

FIREWALL RULES (4)
Added:
  - seg-c005-web-p-egress / web-to-app (#10): permit the policy's segment -> segment c005-app-p tcp/443
  - seg-c005-web-p-egress / web-to-internet (#20): permit the policy's segment -> zone INTERNET any
Updated:
  - seg-c005-web-p-egress / mgmt-to-web (#30): permit segment c005-mgmt-p -> segment c005-app-p tcp/8000-8010 (changed: action deny -> permit; port_start 443 -> 8000)
Removed:
  - seg-c005-web-p-egress / old-rule (#40): permit the policy's segment -> segment c005-app-p tcp/443

NETWORK SEGMENTS (2)
Added:
  - c005-app-p: C005, p, ipv4, 10.5.2.0/24, gateway 10.5.2.1, vlan local, stretch local, firewall terminated; footprints C005-P-DC-DC10
  - c005-web-p: C005, p, ipv4, 10.5.1.0/24, gateway 10.5.1.1, vlan local, stretch local; footprints C005-P-DC-DC10

OTHER OBJECTS (not listed): 4 added, 1 updated

Adds the web and app tiers of C005 in DC10."""


def _bare(**overrides: Any) -> ChangeInputs:
    """Inputs with no object, no device row and no customer."""
    base: dict[str, Any] = {
        "rows": {},
        "customers": [],
        "devices": [],
        "artifacts": [],
        "objects": [],
        "diff_counts": {"ADDED": 1, "UPDATED": 0, "REMOVED": 0},
        "prod_objects": False,
    }
    base.update(overrides)
    return replace(realistic_inputs(), **base)


def _removed_rules(count: int) -> list[ObjectDiff]:
    return parse_object_nodes(
        [
            raw_node("SecurityPolicyRule", f"gone-{i:03d}", "REMOVED", f"g{i}", {"name": f"gone-{i:03d}"})
            for i in range(count)
        ]
    )


class TestDescription:
    """Summary, the four sections, the other kinds, then the PC text."""

    def test_the_realistic_change_renders_the_golden_description(self) -> None:
        """2 segments, 3 rules (one to INTERNET), 1 cable, 1 carrier circuit, 3 devices with one MLAG pair."""
        assert render_field(realistic_inputs(), "description") == GOLDEN_DESCRIPTION

    def test_no_object_changes_and_the_pc_text_alone(self) -> None:
        """An empty diff says so; the PC description follows verbatim."""
        text = render_field(_bare(diff_counts={}), "description")
        assert text.startswith("No object changes.")
        assert text.endswith("Adds the web and app tiers of C005 in DC10.")

    def test_without_a_pc_text_the_listing_stands_alone(self) -> None:
        """No trailing blank lines."""
        text = render_field(realistic_inputs(pc_description=None), "description")
        assert text.endswith("OTHER OBJECTS (not listed): 4 added, 1 updated")

    def test_the_other_objects_are_the_counts_the_text_does_not_describe(self) -> None:
        """Totals minus the objects read; none when the read was cut, which carries its note instead."""
        text = render_field(realistic_inputs(objects_truncated=True), "description")
        assert "OTHER OBJECTS" not in text
        assert "(more objects changed than the part read" in text
        assert "OTHER OBJECTS" not in render_field(
            realistic_inputs(diff_counts={"ADDED": 6, "UPDATED": 2, "REMOVED": 1}), "description"
        )

    def test_sections_are_capped_with_a_more_line(self) -> None:
        """More than SECTION_MAX_ITEMS rules: the first ones, then ``(+N more)``."""
        extra = SECTION_MAX_ITEMS + 3
        text = render_field(_bare(objects=_removed_rules(extra)), "description")
        assert f"FIREWALL RULES ({extra})" in text
        assert "(+3 more)" in text
        assert text.count("  - gone-") == SECTION_MAX_ITEMS

    def test_devices_are_capped(self) -> None:
        """DEVICE_MAX_ITEMS devices are listed; the rest is counted."""
        artifacts = [artifact(f"sw-{i:03d}") for i in range(DEVICE_MAX_ITEMS + 2)]
        text = render_field(_bare(artifacts=artifacts), "description")
        assert f"DEVICES ({DEVICE_MAX_ITEMS + 2})" in text
        assert "(+2 more)" in text

    def test_the_listing_is_cut_at_the_limit_and_the_pc_text_follows(self) -> None:
        """A huge listing is cut at DESCRIPTION_MAX_CHARS; the PC description still follows in full."""
        rules = parse_object_nodes(
            [
                raw_node("SecurityPolicyRule", f"{'r' * 300}-{i}", "REMOVED", f"g{i}", {"name": f"{'r' * 300}-{i}"})
                for i in range(SECTION_MAX_ITEMS)
            ]
        )
        artifacts = [artifact(f"{'x' * 60}-{i:03d}") for i in range(DEVICE_MAX_ITEMS)]
        text = render_field(_bare(artifacts=artifacts, objects=rules, pc_description="x" * 100 + " END"), "description")
        listing = text.rsplit("\n\n", 1)[0]
        assert len(listing) <= DESCRIPTION_MAX_CHARS
        assert listing.endswith("\n...")
        assert text.endswith("x" * 100 + " END")

    def test_limit_chars_marks_the_cut(self) -> None:
        """Short text is untouched."""
        assert limit_chars("abc", 10) == "abc"
        assert limit_chars("a" * 20, 10).endswith("\n...")
        assert len(limit_chars("a" * 20, 10)) <= 10 + 1


class TestShortDescription:
    """One line: the counts."""

    def test_short_description_counts_objects_and_configs(self) -> None:
        """The server's counts; the configs are the artifact entries."""
        assert render_field(_bare(artifacts=[artifact("a"), artifact("b")]), "short_description") == (
            "web tier for C005: 1 added, 0 updated, 0 removed objects; 2 device configs"
        )

    def test_one_config_is_singular_and_the_counts_are_the_servers(self) -> None:
        """900 objects added are 900, whatever part of them the text describes."""
        inputs = _bare(artifacts=[artifact("a")], diff_counts={"ADDED": 900, "UPDATED": 5, "REMOVED": 1})
        assert render_field(inputs, "short_description") == (
            "web tier for C005: 900 added, 5 updated, 1 removed objects; 1 device config"
        )


class TestPlans:
    """Implementation, backout, test and communication plans."""

    def test_implementation_plan_least_critical_first_and_a_pair_member_before_its_peer(self) -> None:
        """Leaves before the spine; one pair member, verified, then the other; the role and the site are named."""
        plan = render_field(realistic_inputs(), "implementation_plan").splitlines()
        assert plan == [
            "1. Deploy dc10-leaf-01 (leaf, DC10): leaf_config: p1 -> n1",
            "2. Verify dc10-leaf-01 before the next step",
            "3. Deploy dc10-leaf-02 (leaf, DC10): leaf_config: new config n1",
            "4. Verify dc10-leaf-02 before the next step",
            "5. Deploy dc10-spine-01 (spine, DC10): leaf_config: p3 -> n3",
            "6. Verify dc10-spine-01 before the next step",
        ]

    def test_no_artifact_is_a_data_only_plan(self) -> None:
        """Nothing to deploy."""
        assert render_field(_bare(), "implementation_plan").startswith("No device configuration changes")

    def test_backout_returns_to_the_previous_config_and_warns_on_removals(self) -> None:
        """The previous checksum and storage id, a new device's config removed, the allocation warning and the
        removed objects by name; no warning when nothing is removed."""
        plan = render_field(realistic_inputs(), "backout_plan")
        assert "Revoking the approval rolls every device back automatically." in plan
        assert "dc10-leaf-01 leaf_config: back to p1 (storage st-p1)" in plan
        assert "dc10-leaf-02 leaf_config: new device: config removed" in plan
        assert "WARNING" in plan
        assert "Removed: old-rule" in plan
        assert "WARNING" not in render_field(realistic_inputs(diff_counts={"ADDED": 3}), "backout_plan")

    def test_test_plan_names_the_sessions_the_links_and_the_gateways(self) -> None:
        """Per device its role and BGP sessions; then the new links and segment gateways to check."""
        devices = [DeviceFacts("a", "dc10-leaf-01", "leaf", bgp_peerings=4), DeviceFacts("b", "h", None)]
        plan = render_field(realistic_inputs(devices=devices), "test_plan")
        assert "running config checksum equals the artifact checksum" in plan
        assert "dc10-leaf-01 (leaf): 4 BGP peering(s) established" in plan
        assert "h (no role): no BGP peering" in plan
        assert "  - cable CBL-1: dc10-leaf-01-Ethernet1/49 <-> dc10-spine-01-Ethernet1/1" in plan
        assert "  - c005-web-p: 10.5.1.1" in plan

    def test_communication_plan_lists_the_customers_and_sites(self) -> None:
        """Sorted names, or the operations team alone."""
        assert render_field(_bare(customers=["C005", "C001"]), "communication_plan") == (
            "Notify the affected customers: C001, C005"
        )
        assert render_field(realistic_inputs(), "communication_plan").endswith("Sites in scope: DC10")
        assert "No customer" in render_field(_bare(), "communication_plan")


class TestDerive:
    """The single entry."""

    def test_derive_returns_every_derived_field_and_no_author_field(self) -> None:
        """The generator writes exactly these keys, the text fields from the templates."""
        derived = derive_change_request(realistic_inputs())
        assert set(derived) == {
            *TEXT_FIELDS,
            "change_type",
            "risk",
            "risk_score",
            "impact",
            "conflicts",
            "planned_start",
            "planned_end",
            "artifact_fingerprint",
        }
        assert derived["description"] == GOLDEN_DESCRIPTION
        assert derived["conflicts"] is None

    def test_a_rerun_renders_the_same_text(self) -> None:
        """No clock, no ordering by chance: the generator writes only a field that changed."""
        first = derive_change_request(realistic_inputs(now=NOW))
        again = derive_change_request(realistic_inputs(now=NOW))
        assert {k: v for k, v in first.items() if k in TEXT_FIELDS} == {
            k: v for k, v in again.items() if k in TEXT_FIELDS
        }

    @pytest.mark.parametrize("field_name", sorted(TEXT_FIELDS))
    def test_every_template_renders_on_an_empty_change(self, field_name: str) -> None:
        """StrictUndefined: a template that reads a name the context lacks fails here."""
        assert render_field(_bare(objects=[], diff_counts={}), field_name) is not None
