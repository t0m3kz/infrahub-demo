"""Unit tests for CheckColocationPlacement (checks/colocation_placement.py).

The payloads are raw GraphQL responses shaped like queries/validation/colocation_placement.gql; ``validate`` runs
them through ``clean_data`` (which collapses a dict whose only key is ``id`` into the id string, so a serving zone's
``firewall_devices`` arrive as a list of ids). Each rule is also tested directly on the cleaned shape.

The design rules (kit needs a design, kit within the envelope, zone design in the metro envelope, zones within the
metro envelope) read the optional ``design`` of a metro, zone and virtual colocation; ``_hosting`` gives every zone
a roomy design (4 of each role) unless a test sets another or ``NO_DESIGN``.

The places mirror data/demos/30_all: the Equinix metros FR (zones FR2 with the edge pair and the firewalls, FR6
with nothing) and AM (virtual colocation AM-NE), the footprints C001-P-FR and C005-P-FR.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from checks.colocation_placement import (
    RULES,
    CheckColocationPlacement,
    Finding,
    kit_within_design_findings,
    kit_without_design_findings,
    metro_zone_count_findings,
    serving_zone_firewall_findings,
    serving_zone_metro_findings,
    template_kind_findings,
    terraform_order_data_findings,
    unique_name_findings,
    zone_design_in_metro_findings,
)
from utils.data_cleaning import clean_data

PHYSICAL = "TemplateDcimPhysicalDevice"
VIRTUAL = "TemplateDcimVirtualDevice"


def _edges(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    return {"edges": [{"node": node} for node in nodes]}


def _node(node_id: str, name: str) -> dict[str, Any]:
    """A raw GraphQL node with an id and a name attribute."""
    return {"id": node_id, "name": {"value": name}}


def _entry(role: str, quantity: int, template_kind: str, template: str) -> dict[str, Any]:
    """A raw fabric_templates entry."""
    return {
        "role": {"value": role},
        "quantity": {"value": quantity},
        "template": {"node": {"id": f"tpl-{template}", "template_kind": template_kind, "display_label": template}},
    }


NO_DESIGN: dict[str, Any] = {"node": None}
"""A raw ``design`` that is unset."""


def _zone_design(name: str = "ROOMY", edges: int = 4, firewalls: int = 4, load_balancers: int = 4) -> dict[str, Any]:
    """A raw zone ``design`` (TopologyTemplateColocationZone) with its envelope."""
    return {
        "node": {
            "id": f"zd-{name}",
            "name": {"value": name},
            "max_edges": {"value": edges},
            "max_firewalls": {"value": firewalls},
            "max_load_balancers": {"value": load_balancers},
        }
    }


def _metro_design(name: str, max_zones: int, zone_designs: list[dict[str, Any]]) -> dict[str, Any]:
    """A raw metro ``design`` (TopologyTemplateColocationMetro): max_zones and the zone designs it allows."""
    return {
        "node": {
            "id": f"md-{name}",
            "name": {"value": name},
            "max_zones": {"value": max_zones},
            "zone_templates": _edges(
                [{"id": zone["node"]["id"], "name": zone["node"]["name"]} for zone in zone_designs]
            ),
        }
    }


def _metro(node_id: str, name: str, design: dict[str, Any] | None = None) -> dict[str, Any]:
    """A raw metro row; the design is selected on the metro rows, so it is only added when given."""
    return {**_node(node_id, name), "design": design or NO_DESIGN}


def _hosting(
    node_id: str,
    name: str,
    metro: dict[str, Any],
    entries: list[dict[str, Any]],
    design: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A zone or virtual colocation with its parent metro, design (roomy by default) and fabric_templates."""
    return {
        **_node(node_id, name),
        "parent": {"node": {"id": metro["id"], "name": metro["name"]}},
        "design": design or _zone_design(),
        "fabric_templates": _edges(entries),
    }


def _footprint(
    node_id: str, name: str, metro: dict[str, Any], serving_zone: dict[str, Any] | None = None
) -> dict[str, Any]:
    """A colocation footprint; serving_zone is a raw zone node (with parent and firewall_devices) or unset."""
    return {
        **_node(node_id, name),
        "parent": {"node": {"id": metro["id"], "name": metro["name"]}},
        "serving_zone": {"node": serving_zone},
    }


def _serving(node_id: str, name: str, metro: dict[str, Any], firewalls: list[str]) -> dict[str, Any]:
    """The serving_zone selection: the zone, its parent metro, and its firewall device ids."""
    return {
        **_node(node_id, name),
        "parent": {"node": {"id": metro["id"], "name": metro["name"]}},
        "firewall_devices": _edges([{"id": firewall} for firewall in firewalls]),
    }


FR = _metro("m-fr", "FR")
AM = _metro("m-am", "AM")
PA = _metro("m-pa", "PA")

EDGE = _entry("edge", 2, PHYSICAL, "N9K-C9316D-GX_EDGE")
FIREWALL = _entry("firewall", 2, PHYSICAL, "CP-26000_FIREWALL")
VEDGE = _entry("edge", 2, VIRTUAL, "C8000V_EDGE")

FR2 = _hosting("z-fr2", "FR2", FR, [EDGE, FIREWALL])
FR6 = _hosting("z-fr6", "FR6", FR, [])
AM_NE = _hosting("vc-am-ne", "AM-NE", AM, [VEDGE])

FR2_SERVING = _serving("z-fr2", "FR2", FR, ["fw-1", "fw-2"])
FR6_SERVING = _serving("z-fr6", "FR6", FR, [])
PA4_SERVING = _serving("z-pa4", "PA4", PA, ["fw-3"])


def _payload(
    metros: list[dict[str, Any]] | None = None,
    zones: list[dict[str, Any]] | None = None,
    virtual: list[dict[str, Any]] | None = None,
    footprints: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "TopologyColocationMetro": _edges([FR, AM] if metros is None else metros),
        "TopologyColocationZone": _edges([FR2, FR6] if zones is None else zones),
        "TopologyVirtualColocation": _edges([AM_NE] if virtual is None else virtual),
        "TopologyCustomerColocation": _edges(footprints or []),
    }


def _run(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Validate and return every logged finding (message, object_id, object_type)."""
    check = cast(Any, CheckColocationPlacement.__new__(CheckColocationPlacement))
    findings: list[dict[str, Any]] = []
    check.log_error = lambda **kwargs: findings.append(kwargs)
    check.validate(payload)
    for finding in findings:
        assert finding["object_id"] and finding["object_type"]
    return findings


class TestTemplateKind:
    """Rule 1: a zone declares physical templates, a virtual colocation virtual ones."""

    def test_zone_with_physical_and_virtual_colocation_with_virtual_pass(self) -> None:
        """The demo shape: no finding."""
        assert _run(_payload()) == []

    def test_zone_and_virtual_colocation_without_kit_pass(self) -> None:
        """Most zones declare nothing (FR6): no entries, no finding."""
        assert _run(_payload(zones=[FR6], virtual=[_hosting("vc-x", "AM-X", AM, [])])) == []

    def test_zone_declaring_a_virtual_template_fails(self) -> None:
        zone = _hosting("z-fr2", "FR2", FR, [_entry("edge", 2, VIRTUAL, "C8000V_EDGE")])

        findings = _run(_payload(zones=[zone]))

        assert len(findings) == 1
        assert findings[0]["object_id"] == "z-fr2"
        assert findings[0]["object_type"] == "TopologyColocationZone"
        message = findings[0]["message"]
        assert "TopologyColocationZone 'FR2' declares 2 x edge on 'C8000V_EDGE'" in message
        assert "a TemplateDcimVirtualDevice" in message
        assert "fabric_templates are TemplateDcimPhysicalDevice only" in message
        assert "our own hardware in our own cage" in message

    def test_virtual_colocation_declaring_a_physical_template_fails(self) -> None:
        virtual = _hosting("vc-am-ne", "AM-NE", AM, [_entry("edge", 2, PHYSICAL, "N9K-C9316D-GX_EDGE")])

        findings = _run(_payload(virtual=[virtual]))

        assert len(findings) == 1
        assert findings[0]["object_id"] == "vc-am-ne"
        assert findings[0]["object_type"] == "TopologyVirtualColocation"
        message = findings[0]["message"]
        assert "TopologyVirtualColocation 'AM-NE' declares 2 x edge on 'N9K-C9316D-GX_EDGE'" in message
        assert "fabric_templates are TemplateDcimVirtualDevice only" in message
        assert "provider-hosted instance" in message

    def test_one_finding_per_wrong_entry(self) -> None:
        """A zone with two wrong entries and one right one reports exactly the two wrong ones."""
        zone = _hosting(
            "z-fr2",
            "FR2",
            FR,
            [EDGE, _entry("firewall", 2, VIRTUAL, "PA-VM"), _entry("load-balancer", 2, VIRTUAL, "F5-VE")],
        )

        findings = _run(_payload(zones=[zone]))

        assert len(findings) == 2
        assert {"PA-VM" in f["message"] or "F5-VE" in f["message"] for f in findings} == {True}

    def test_template_of_unknown_kind_fails(self) -> None:
        """A template whose kind the query did not return cannot be called physical."""
        entry = _entry("edge", 2, PHYSICAL, "X")
        entry["template"]["node"].pop("template_kind")
        zone = _hosting("z-fr2", "FR2", FR, [entry])

        findings = _run(_payload(zones=[zone]))

        assert len(findings) == 1
        assert "a template of unknown kind" in findings[0]["message"]

    def test_entry_without_a_template_is_skipped(self) -> None:
        """No template peer at all is the schema's concern (it is required), not this rule's."""
        zone = _hosting(
            "z-fr2", "FR2", FR, [{"role": {"value": "edge"}, "quantity": {"value": 2}, "template": {"node": None}}]
        )

        assert _run(_payload(zones=[zone])) == []

    def test_rule_function_on_the_cleaned_shape(self) -> None:
        """The rule works on the cleaned dicts, which is also how a caller outside validate() uses it."""
        cleaned = clean_data(
            _payload(zones=[_hosting("z-fr2", "FR2", FR, [_entry("edge", 2, VIRTUAL, "C8000V_EDGE")])])
        )

        findings = template_kind_findings(cleaned)

        assert [type(f) for f in findings] == [Finding]
        assert findings[0].object_type == "TopologyColocationZone"


class TestServingZoneMetro:
    """Rule 2: the serving zone is a zone of the footprint's own metro."""

    def test_zone_of_the_footprints_metro_passes(self) -> None:
        footprint = _footprint("fp-1", "C001-P-FR", FR, FR2_SERVING)

        assert _run(_payload(footprints=[footprint])) == []

    def test_zone_of_another_metro_fails(self) -> None:
        footprint = _footprint("fp-1", "C001-P-FR", FR, PA4_SERVING)

        findings = _run(_payload(footprints=[footprint]))

        assert len(findings) == 1
        assert findings[0]["object_id"] == "fp-1"
        assert findings[0]["object_type"] == "TopologyCustomerColocation"
        message = findings[0]["message"]
        assert "TopologyCustomerColocation 'C001-P-FR' is served by zone 'PA4' of metro 'PA'" in message
        assert "is itself in metro 'FR'" in message

    def test_serving_zone_without_a_parent_fails_naming_no_metro(self) -> None:
        serving = {
            **_node("z-orphan", "ORPHAN"),
            "parent": {"node": None},
            "firewall_devices": _edges([{"id": "fw-9"}]),
        }
        footprint = _footprint("fp-1", "C001-P-FR", FR, serving)

        findings = _run(_payload(footprints=[footprint]))

        assert len(findings) == 1
        assert "of metro '<no metro>'" in findings[0]["message"]

    def test_unset_serving_zone_is_silent(self) -> None:
        """Unset serving_zone means no context by design: not an error."""
        footprint = _footprint("fp-1", "C001-P-FR", FR, None)

        assert _run(_payload(footprints=[footprint])) == []

    def test_footprint_without_a_parent_id_is_skipped(self) -> None:
        """No parent to compare against: the schema requires one, this rule does not guess."""
        footprint = {**_footprint("fp-1", "C001-P-FR", FR, FR2_SERVING), "parent": {"node": None}}

        assert serving_zone_metro_findings(clean_data(_payload(footprints=[footprint]))) == []


class TestServingZoneFirewalls:
    """Rule 3: a serving zone holds a firewall, else the footprint gets no FirewallContext."""

    def test_zone_with_firewalls_passes(self) -> None:
        footprint = _footprint("fp-1", "C001-P-FR", FR, FR2_SERVING)

        assert _run(_payload(footprints=[footprint])) == []

    def test_zone_without_firewalls_fails(self) -> None:
        footprint = _footprint("fp-1", "C001-P-FR", FR, FR6_SERVING)

        findings = _run(_payload(footprints=[footprint]))

        assert len(findings) == 1
        assert findings[0]["object_id"] == "fp-1"
        message = findings[0]["message"]
        assert "TopologyCustomerColocation 'C001-P-FR' is served by zone 'FR6', which has no firewall device" in message
        assert "FirewallContext" in message

    def test_unset_serving_zone_is_silent(self) -> None:
        footprint = _footprint("fp-1", "C001-P-FR", FR, None)

        assert serving_zone_firewall_findings(clean_data(_payload(footprints=[footprint]))) == []

    def test_two_footprints_report_independently(self) -> None:
        """C001 is served by a firewall zone, C005 by a bare one: only C005 is reported."""
        findings = _run(
            _payload(
                footprints=[
                    _footprint("fp-1", "C001-P-FR", FR, FR2_SERVING),
                    _footprint("fp-5", "C005-P-FR", FR, FR6_SERVING),
                ]
            )
        )

        assert [f["object_id"] for f in findings] == ["fp-5"]

    def test_wrong_metro_and_no_firewall_are_two_findings(self) -> None:
        """Rules are independent: a zone of another metro with no firewalls trips both rules."""
        serving = _serving("z-pa9", "PA9", PA, [])

        findings = _run(_payload(footprints=[_footprint("fp-1", "C001-P-FR", FR, serving)]))

        assert len(findings) == 2


class TestUniqueNames:
    """Rule 4: metro, zone and virtual colocation names are unique across the three kinds, case-insensitive."""

    def test_distinct_names_pass(self) -> None:
        assert _run(_payload()) == []

    def test_zone_named_like_its_metro_fails_on_both(self) -> None:
        findings = _run(_payload(zones=[_hosting("z-fr", "FR", FR, [])]))

        assert sorted((f["object_type"], f["object_id"]) for f in findings) == [
            ("TopologyColocationMetro", "m-fr"),
            ("TopologyColocationZone", "z-fr"),
        ]
        assert all("unique across the three kinds" in f["message"] for f in findings)

    def test_collision_is_case_insensitive(self) -> None:
        """Device and pool names are lower-cased, so FR2 and fr2 collide."""
        findings = _run(_payload(zones=[FR2], virtual=[_hosting("vc-fr2", "fr2", AM, [])]))

        assert len(findings) == 2
        messages = " ".join(f["message"] for f in findings)
        assert "TopologyColocationZone 'FR2'" in messages
        assert "TopologyVirtualColocation 'fr2'" in messages

    def test_zone_and_virtual_colocation_with_the_same_name_fail(self) -> None:
        findings = _run(_payload(zones=[FR2], virtual=[_hosting("vc-x", "FR2", AM, [])]))

        assert {f["object_type"] for f in findings} == {"TopologyColocationZone", "TopologyVirtualColocation"}

    def test_each_finding_names_the_other_owner(self) -> None:
        findings = _run(_payload(zones=[_hosting("z-fr", "FR", FR, [])]))

        by_type = {f["object_type"]: f["message"] for f in findings}
        assert "shares its name with TopologyColocationZone 'FR'" in by_type["TopologyColocationMetro"]
        assert "shares its name with TopologyColocationMetro 'FR'" in by_type["TopologyColocationZone"]

    def test_three_way_collision_reports_all_three(self) -> None:
        findings = unique_name_findings(
            clean_data(
                _payload(
                    metros=[FR],
                    zones=[_hosting("z-fr", "Fr", FR, [])],
                    virtual=[_hosting("vc-fr", "fR", FR, [])],
                )
            )
        )

        assert len(findings) == 3

    def test_footprint_names_are_not_part_of_the_namespace(self) -> None:
        """A footprint is named after its metro (C001-P-FR); it shares no device or pool name with a zone."""
        footprint = _footprint("fp-1", "FR2", FR, FR2_SERVING)

        assert _run(_payload(footprints=[footprint])) == []


class TestKitNeedsDesign:
    """Rule 5: a zone or virtual colocation that declares kit has a design."""

    def test_kit_with_a_design_passes(self) -> None:
        assert _run(_payload()) == []

    def test_no_kit_without_a_design_passes(self) -> None:
        """FR6 declares nothing: it needs no envelope."""
        assert _run(_payload(zones=[_hosting("z-fr6", "FR6", FR, [], design=NO_DESIGN)], virtual=[])) == []

    def test_zone_kit_without_a_design_fails(self) -> None:
        zone = _hosting("z-fr2", "FR2", FR, [EDGE, FIREWALL], design=NO_DESIGN)

        findings = _run(_payload(zones=[zone]))

        assert len(findings) == 1
        assert findings[0]["object_id"] == "z-fr2"
        assert findings[0]["object_type"] == "TopologyColocationZone"
        assert (
            "TopologyColocationZone 'FR2' declares 2 fabric_templates entries but has no design"
            in findings[0]["message"]
        )

    def test_virtual_colocation_kit_without_a_design_fails(self) -> None:
        virtual = _hosting("vc-am-ne", "AM-NE", AM, [VEDGE], design=NO_DESIGN)

        findings = _run(_payload(virtual=[virtual]))

        assert len(findings) == 1
        assert findings[0]["object_type"] == "TopologyVirtualColocation"
        assert (
            "TopologyVirtualColocation 'AM-NE' declares 1 fabric_templates entries but has no design"
            in (findings[0]["message"])
        )

    def test_rule_function_on_the_cleaned_shape(self) -> None:
        zone = _hosting("z-fr2", "FR2", FR, [EDGE], design=NO_DESIGN)

        findings = kit_without_design_findings(clean_data(_payload(zones=[zone])))

        assert [type(f) for f in findings] == [Finding]


class TestKitWithinDesign:
    """Rule 6: the quantity of each role, summed over its entries, stays within the design's max."""

    def test_kit_at_the_maximum_passes(self) -> None:
        design = _zone_design("EDGE_FW", edges=2, firewalls=2, load_balancers=0)
        zone = _hosting("z-fr2", "FR2", FR, [EDGE, FIREWALL], design=design)

        assert _run(_payload(zones=[zone])) == []

    @pytest.mark.parametrize(
        ("entry", "attribute", "role"),
        [
            (EDGE, "max_edges", "edge"),
            (FIREWALL, "max_firewalls", "firewall"),
            (_entry("load-balancer", 2, PHYSICAL, "F5_LB"), "max_load_balancers", "load-balancer"),
        ],
        ids=["edge", "firewall", "load-balancer"],
    )
    def test_role_beyond_its_max_fails(self, entry: dict[str, Any], attribute: str, role: str) -> None:
        design = _zone_design("TIGHT", edges=1, firewalls=1, load_balancers=1)
        zone = _hosting("z-fr2", "FR2", FR, [entry], design=design)

        findings = _run(_payload(zones=[zone]))

        assert len(findings) == 1
        assert findings[0]["object_id"] == "z-fr2"
        message = findings[0]["message"]
        assert f"declares 2 x {role} but its design 'TIGHT' allows {attribute} = 1" in message

    def test_quantities_of_one_role_are_summed_across_entries(self) -> None:
        """Two entries of 2 edges each are 4 edges, over a max of 3."""
        design = _zone_design("THREE", edges=3)
        zone = _hosting("z-fr2", "FR2", FR, [EDGE, _entry("edge", 2, PHYSICAL, "OTHER_EDGE")], design=design)

        findings = _run(_payload(zones=[zone]))

        assert len(findings) == 1
        assert "declares 4 x edge" in findings[0]["message"]

    def test_zero_max_with_no_entry_of_the_role_passes(self) -> None:
        design = _zone_design("EDGE_ONLY", edges=2, firewalls=0, load_balancers=0)
        zone = _hosting("z-pa4", "PA4", PA, [EDGE], design=design)

        assert _run(_payload(zones=[zone], virtual=[])) == []

    def test_virtual_colocation_beyond_its_max_fails(self) -> None:
        virtual = _hosting("vc-am-ne", "AM-NE", AM, [VEDGE], design=_zone_design("ONE", edges=1))

        findings = _run(_payload(virtual=[virtual]))

        assert len(findings) == 1
        assert findings[0]["object_type"] == "TopologyVirtualColocation"

    def test_each_role_over_its_max_reports_separately(self) -> None:
        design = _zone_design("NONE", edges=0, firewalls=0, load_balancers=0)
        zone = _hosting("z-fr2", "FR2", FR, [EDGE, FIREWALL], design=design)

        assert len(_run(_payload(zones=[zone]))) == 2

    def test_no_design_is_left_to_the_kit_without_design_rule(self) -> None:
        zone = _hosting("z-fr2", "FR2", FR, [EDGE], design=NO_DESIGN)

        assert kit_within_design_findings(clean_data(_payload(zones=[zone]))) == []


class TestZoneDesignInMetroDesign:
    """Rule 7: with a design on both, the zone's design is one of the metro design's zone_templates."""

    EDGE_FW = _zone_design("EDGE_FW")
    EDGE_ONLY = _zone_design("EDGE")

    def _metro(self, *allowed: dict[str, Any], max_zones: int = 4) -> dict[str, Any]:
        return _metro("m-fr", "FR", _metro_design("S", max_zones, list(allowed)))

    def test_zone_design_in_the_metro_templates_passes(self) -> None:
        zone = _hosting("z-fr2", "FR2", FR, [EDGE], design=self.EDGE_FW)

        assert _run(_payload(metros=[self._metro(self.EDGE_FW, self.EDGE_ONLY)], zones=[zone], virtual=[])) == []

    def test_zone_design_outside_the_metro_templates_fails(self) -> None:
        zone = _hosting("z-fr2", "FR2", FR, [EDGE], design=self.EDGE_FW)

        findings = _run(_payload(metros=[self._metro(self.EDGE_ONLY)], zones=[zone], virtual=[]))

        assert len(findings) == 1
        assert findings[0]["object_id"] == "z-fr2"
        assert findings[0]["object_type"] == "TopologyColocationZone"
        message = findings[0]["message"]
        assert "uses design 'EDGE_FW', which metro 'FR' does not allow" in message
        assert "its design 'S' has zone_templates EDGE" in message

    def test_metro_design_without_zone_templates_allows_none(self) -> None:
        zone = _hosting("z-fr2", "FR2", FR, [EDGE], design=self.EDGE_FW)

        findings = _run(_payload(metros=[self._metro()], zones=[zone], virtual=[]))

        assert len(findings) == 1
        assert "has zone_templates none" in findings[0]["message"]

    def test_virtual_colocation_design_outside_the_metro_templates_fails(self) -> None:
        am = _metro("m-am", "AM", _metro_design("S", 2, [self.EDGE_FW]))
        virtual = _hosting("vc-am-ne", "AM-NE", AM, [VEDGE], design=self.EDGE_ONLY)

        findings = _run(_payload(metros=[am], zones=[], virtual=[virtual]))

        assert len(findings) == 1
        assert findings[0]["object_type"] == "TopologyVirtualColocation"

    def test_metro_without_a_design_is_skipped(self) -> None:
        zone = _hosting("z-fr2", "FR2", FR, [EDGE], design=self.EDGE_FW)

        assert _run(_payload(metros=[FR], zones=[zone], virtual=[])) == []

    def test_zone_without_a_design_is_skipped(self) -> None:
        zone = _hosting("z-fr6", "FR6", FR, [], design=NO_DESIGN)

        assert _run(_payload(metros=[self._metro(self.EDGE_ONLY)], zones=[zone], virtual=[])) == []

    def test_rule_function_on_the_cleaned_shape(self) -> None:
        zone = _hosting("z-fr2", "FR2", FR, [EDGE], design=self.EDGE_FW)

        findings = zone_design_in_metro_findings(
            clean_data(_payload(metros=[self._metro(self.EDGE_ONLY)], zones=[zone], virtual=[]))
        )

        assert len(findings) == 1


class TestMetroZoneCount:
    """Rule 8: a metro with a design holds at most max_zones zones and virtual colocations together."""

    def _fr(self, max_zones: int) -> dict[str, Any]:
        return _metro("m-fr", "FR", _metro_design("S", max_zones, [_zone_design()]))

    def test_zones_at_the_maximum_pass(self) -> None:
        assert _run(_payload(metros=[self._fr(2)], zones=[FR2, FR6], virtual=[])) == []

    def test_zones_beyond_the_maximum_fail(self) -> None:
        extra = _hosting("z-fr9", "FR9", FR, [])

        findings = _run(_payload(metros=[self._fr(2)], zones=[FR2, FR6, extra], virtual=[]))

        assert len(findings) == 1
        assert findings[0]["object_id"] == "m-fr"
        assert findings[0]["object_type"] == "TopologyColocationMetro"
        message = findings[0]["message"]
        assert "TopologyColocationMetro 'FR' holds 3 zones and virtual colocations (FR2, FR6, FR9)" in message
        assert "allows max_zones = 2" in message

    def test_virtual_colocations_count_with_the_zones(self) -> None:
        virtual = _hosting("vc-fr", "FR-NE", FR, [])

        findings = _run(_payload(metros=[self._fr(2)], zones=[FR2, FR6], virtual=[virtual]))

        assert len(findings) == 1
        assert "FR-NE" in findings[0]["message"]

    def test_zones_of_another_metro_are_not_counted(self) -> None:
        pa4 = _hosting("z-pa4", "PA4", PA, [])

        assert _run(_payload(metros=[self._fr(2), PA], zones=[FR2, FR6, pa4], virtual=[])) == []

    def test_metro_without_a_design_is_unbounded(self) -> None:
        zones = [_hosting(f"z-fr{i}", f"FR{i}", FR, []) for i in range(10)]

        assert _run(_payload(metros=[FR], zones=zones, virtual=[])) == []

    def test_rule_function_on_the_cleaned_shape(self) -> None:
        findings = metro_zone_count_findings(clean_data(_payload(metros=[self._fr(1)], zones=[FR2, FR6], virtual=[])))

        assert [type(f) for f in findings] == [Finding]


class TestTerraformOrderData:
    """Rule 9: a virtual colocation routed to terraform has the account and an owner with notification e-mails."""

    OWNER = {
        "node": {"id": "org-p028", "display_label": "Internal", "notification_emails": {"value": ["noc@example.com"]}}
    }

    def _vc(
        self, *, orchestrator: str = "terraform", account: str | None = "1000001", owner: Any = None
    ) -> dict[str, Any]:
        return {
            **AM_NE,
            "infrastructure_orchestrator": {"value": orchestrator},
            "provider_account_number": {"value": account},
            "owner": self.OWNER if owner is None else owner,
        }

    def test_complete_order_data_passes(self) -> None:
        assert _run(_payload(virtual=[self._vc()])) == []

    def test_a_presence_not_routed_to_terraform_needs_none(self) -> None:
        vc = self._vc(orchestrator="manual", account=None, owner={"node": None})

        assert _run(_payload(virtual=[vc])) == []

    def test_missing_account_fails(self) -> None:
        findings = _run(_payload(virtual=[self._vc(account=None)]))

        assert len(findings) == 1
        assert findings[0]["object_id"] == "vc-am-ne"
        assert findings[0]["object_type"] == "TopologyVirtualColocation"
        assert "has no provider_account_number" in findings[0]["message"]

    def test_blank_account_fails(self) -> None:
        assert len(_run(_payload(virtual=[self._vc(account="  ")]))) == 1

    def test_missing_owner_fails(self) -> None:
        findings = _run(_payload(virtual=[self._vc(owner={"node": None})]))

        assert len(findings) == 1
        assert "no owner to notify" in findings[0]["message"]

    def test_owner_without_emails_fails(self) -> None:
        owner = {"node": {"id": "org-p028", "display_label": "Internal", "notification_emails": {"value": []}}}

        findings = _run(_payload(virtual=[self._vc(owner=owner)]))

        assert len(findings) == 1
        assert "owner 'Internal' has no notification_emails" in findings[0]["message"]

    def test_entry_that_is_no_email_address_fails(self) -> None:
        owner = {"node": {"id": "org-p028", "display_label": "Internal", "notification_emails": {"value": ["noc"]}}}

        findings = _run(_payload(virtual=[self._vc(owner=owner)]))

        assert len(findings) == 1
        assert "not all e-mail addresses" in findings[0]["message"]

    def test_account_and_owner_missing_are_one_finding_naming_both(self) -> None:
        findings = _run(_payload(virtual=[self._vc(account=None, owner={"node": None})]))

        assert len(findings) == 1
        assert "no provider_account_number and no owner to notify" in findings[0]["message"]

    def test_rule_function_on_the_cleaned_shape(self) -> None:
        findings = terraform_order_data_findings(clean_data(_payload(virtual=[self._vc(account=None)])))

        assert [type(f) for f in findings] == [Finding]


class TestRegistry:
    def test_every_rule_is_registered_once(self) -> None:
        assert RULES == (
            template_kind_findings,
            serving_zone_metro_findings,
            serving_zone_firewall_findings,
            unique_name_findings,
            kit_without_design_findings,
            kit_within_design_findings,
            zone_design_in_metro_findings,
            metro_zone_count_findings,
            terraform_order_data_findings,
        )

    def test_empty_response_has_no_findings(self) -> None:
        assert _run({}) == []

    def test_findings_from_different_rules_all_surface(self) -> None:
        """One run reports every violated rule: bad template, wrong metro and a name collision together."""
        zone = _hosting("z-fr2", "FR2", FR, [_entry("edge", 2, VIRTUAL, "C8000V_EDGE")])
        payload = _payload(
            zones=[zone],
            virtual=[_hosting("vc-fr2", "fr2", AM, [])],
            footprints=[_footprint("fp-1", "C001-P-FR", FR, PA4_SERVING)],
        )

        findings = _run(payload)

        # template kind, serving zone metro, and two name-collision findings
        assert len(findings) == 4

    @pytest.mark.parametrize("rule", RULES)
    def test_every_rule_tolerates_an_empty_cleaned_input(self, rule: Any) -> None:
        assert rule({}) == []
