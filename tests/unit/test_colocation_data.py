"""Lint of the colocation object files in data/demos/10_colocation and data/demos/30_all.

A colocation metro owns its zones (cages, our own hardware) and virtual colocations (provider-hosted presences);
a zone and a virtual colocation declare their kit in ``fabric_templates``. The generators and the check
(checks/colocation_placement.py) rely on facts about the data that the schema cannot hold:

- every metro, zone and virtual colocation joins its generator group, even with no kit (a created-trigger for a
  non-member raises);
- metro, zone and virtual colocation names are unique across the three kinds, case-insensitive: they share the
  device-name space (zone FR2 names ``eg-fr201``) and the pool-name space;
- there is no ``deployment_type``: the node kind says what the kit is;
- a zone's templates are physical, a virtual colocation's virtual;
- a footprint's ``serving_zone`` is a zone of the footprint's own metro;
- the designs (data/bootstrap/14c_colocation_templates.yml): a zone or virtual colocation that declares kit has a
  ``design`` and so does a metro whose children do, every design name is a bootstrap row, a child's design is one of its
  metro design's ``zone_templates``, the kit stays within the design's ``max_*`` and a metro holds no more children
  than its ``max_zones`` (the generator warns and the check errors on each; the demo data must be clean).

The two demo sets are linted separately: each is loaded alone.
"""

from __future__ import annotations

from collections import defaultdict
from functools import cache
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.integration.demo_data import ROOT, Declared, attribute_names, declared_in, name, object_templates
from tests.unit.simulators.fabric_designs import (
    COLO_METRO_DESIGN_NAMES,
    COLO_ZONE_DESIGN_NAMES,
    colo_metro_design,
    colo_zone_design,
)

_DEMOS = ROOT / "data" / "demos"
_BOOTSTRAP = ROOT / "data" / "bootstrap"
_DEMO_SETS = {"10_colocation": _DEMOS / "10_colocation", "30_all": _DEMOS / "30_all"}

_METRO = "TopologyColocationMetro"
_ZONE = "TopologyColocationZone"
_VIRTUAL = "TopologyVirtualColocation"
_FOOTPRINT = "TopologyCustomerColocation"
_NAMESPACED = (_METRO, _ZONE, _VIRTUAL)
_GROUP = {_METRO: "colocation_metros", _ZONE: "colocation_zones", _VIRTUAL: "virtual_colocations"}
_TEMPLATE_KIND = {_ZONE: "TemplateDcimPhysicalDevice", _VIRTUAL: "TemplateCloudNetworkAppliance"}


@cache
def _objects(demo: str, *kinds: str) -> tuple[Declared, ...]:
    return tuple(obj for obj in declared_in(_DEMO_SETS[demo]) if obj.kind in kinds)


@cache
def _template_kinds() -> dict[str, str]:
    """Top-level object template name -> its kind, from data/bootstrap/09_*_templates_* and 10_physical_*."""
    kinds: dict[str, str] = {}
    for path in sorted(_BOOTSTRAP.glob("*_templates_*.y*ml")):
        for document in yaml.safe_load_all(path.read_text()):
            if isinstance(document, dict) and document.get("kind") == "Object":
                kind = document["spec"]["kind"]
                for entry in document["spec"].get("data") or []:
                    kinds[str(entry.get("template_name"))] = kind
    return kinds


def _label(obj: Declared) -> str:
    return f"{obj.kind} {name(obj)} ({obj.path.relative_to(ROOT).as_posix()})"


def _template_names(obj: Declared) -> list[str]:
    """The template names of a node's ``fabric_templates`` rows (``["2", "edge", "N9K-C9316D-GX_EDGE"]``)."""
    names: list[str] = []
    for row in obj.item.get("fabric_templates") or []:
        assert isinstance(row, list) and len(row) == 3, f"{_label(obj)}: fabric_templates row {row!r} is not an HFID"
        names.append(str(row[2]))
    return names


def _metro_of(obj: Declared) -> str | None:
    """The metro a zone or virtual colocation sits in: the object it is nested under, else its ``parent`` reference."""
    if obj.enclosing is not None and obj.enclosing.kind == _METRO:
        return name(obj.enclosing)
    parent = obj.item.get("parent")
    if isinstance(parent, list) and len(parent) == 1:
        parent = parent[0]
    return str(parent) if isinstance(parent, str) else None


_MAX_BY_ROLE = {"edge": "max_edges", "firewall": "max_firewalls", "load-balancer": "max_load_balancers"}


def _design_name(obj: Declared) -> str | None:
    """The ``design`` an object declares (an HFID reference: a string, or a one-value list), else None."""
    design = obj.item.get("design")
    if isinstance(design, list) and len(design) == 1:
        design = design[0]
    return str(design) if design is not None else None


def _kit_by_role(obj: Declared) -> dict[str, int]:
    """Total quantity per role over the ``fabric_templates`` rows of one node (``["2", "edge", "C8000V_EDGE"]``)."""
    totals: dict[str, int] = defaultdict(int)
    for quantity, role, _template in obj.item.get("fabric_templates") or []:
        totals[str(role)] += int(quantity)
    return dict(totals)


def _children(demo: str) -> dict[str, list[Declared]]:
    """Distinct zones and virtual colocations per metro name (a re-declared child counts once)."""
    by_metro: dict[str, dict[str, Declared]] = defaultdict(dict)
    for obj in _objects(demo, _ZONE, _VIRTUAL):
        metro = _metro_of(obj)
        assert metro is not None, _label(obj)
        by_metro[metro].setdefault(name(obj), obj)
    return {metro: list(children.values()) for metro, children in by_metro.items()}


def _declarations(demo: str, kind: str) -> dict[str, list[Declared]]:
    """Every declaration of ``kind`` by name (a later stage may re-declare an object to update it)."""
    by_name: dict[str, list[Declared]] = defaultdict(list)
    for obj in _objects(demo, kind):
        by_name[name(obj)].append(obj)
    return by_name


def _design_of(declarations: list[Declared]) -> str | None:
    """The design any declaration of one object states."""
    return next((design for obj in declarations if (design := _design_name(obj))), None)


@pytest.mark.parametrize("demo", sorted(_DEMO_SETS))
class TestColocationDataset:
    def test_every_demo_declares_metros_zones_and_virtual_colocations(self, demo: str) -> None:
        """Guards the walker: an empty inventory would make every other test pass vacuously."""
        assert _objects(demo, _METRO)
        assert _objects(demo, _ZONE)
        assert _objects(demo, _VIRTUAL)

    def test_every_node_is_a_member_of_its_generator_group(self, demo: str) -> None:
        """Even a zone with no kit joins ``colocation_zones``: a created-trigger on a non-member raises."""
        missing = [
            f"{_label(obj)}: not in {_GROUP[obj.kind]}"
            for obj in _objects(demo, *_NAMESPACED)
            if _GROUP[obj.kind] not in obj.groups()
        ]
        assert missing == []

    def test_no_node_is_in_another_kinds_group(self, demo: str) -> None:
        """A zone in ``virtual_colocations`` would run the wrong generator."""
        wrong = [
            f"{_label(obj)}: also in {group}"
            for obj in _objects(demo, *_NAMESPACED)
            for kind, group in _GROUP.items()
            if kind != obj.kind and group in obj.groups()
        ]
        assert wrong == []

    def test_names_are_unique_across_the_three_kinds(self, demo: str) -> None:
        """Metro, zone and virtual colocation share the device-name and pool-name space (case-insensitive)."""
        kinds_by_name: dict[str, set[str]] = defaultdict(set)
        for obj in _objects(demo, *_NAMESPACED):
            kinds_by_name[name(obj).casefold()].add(obj.kind)
        assert {n: sorted(kinds) for n, kinds in kinds_by_name.items() if len(kinds) > 1} == {}

    def test_a_zone_or_virtual_colocation_is_declared_under_one_metro(self, demo: str) -> None:
        """The same zone name under two metros would be one object with two parents."""
        parents: dict[tuple[str, str], set[str | None]] = defaultdict(set)
        for obj in _objects(demo, _ZONE, _VIRTUAL):
            parents[(obj.kind, name(obj))].add(_metro_of(obj))
        assert {key: sorted(map(str, metros)) for key, metros in parents.items() if len(metros) > 1} == {}
        assert all(None not in metros for metros in parents.values())

    def test_no_deployment_type_on_any_colocation_node(self, demo: str) -> None:
        """The node kind says what the kit is: a zone is physical, a virtual colocation virtual."""
        offenders = [_label(obj) for obj in _objects(demo, *_NAMESPACED) if "deployment_type" in obj.item]
        assert offenders == []

    def test_a_metro_declares_no_kit(self, demo: str) -> None:
        """A metro lost fabric_templates and the kit pools: zones and virtual colocations carry them."""
        offenders = [
            f"{_label(obj)}: {key}"
            for obj in _objects(demo, _METRO)
            for key in ("fabric_templates", "loopback_pool", "technical_pool", "management_pool")
            if key in obj.item
        ]
        assert offenders == []

    @pytest.mark.parametrize("kind", sorted(_TEMPLATE_KIND))
    def test_fabric_templates_are_of_the_kind_the_node_can_host(self, demo: str, kind: str) -> None:
        """A zone hosts TemplateDcimPhysicalDevice, a virtual colocation TemplateCloudNetworkAppliance."""
        known = _template_kinds()
        offenders = []
        for obj in _objects(demo, kind):
            for template in _template_names(obj):
                if known.get(template) != _TEMPLATE_KIND[kind]:
                    offenders.append(f"{_label(obj)}: {template} is {known.get(template, 'not a known template')}")
        assert offenders == []

    def test_serving_zone_is_a_zone_of_the_footprints_own_metro(self, demo: str) -> None:
        """``serving_zone: FR2`` on a footprint of metro FR names a TopologyColocationZone nested under FR."""
        zones: dict[str, set[str | None]] = defaultdict(set)
        for obj in _objects(demo, _ZONE):
            zones[name(obj)].add(_metro_of(obj))
        offenders = []
        for footprint in _objects(demo, _FOOTPRINT):
            serving = footprint.item.get("serving_zone")
            if serving is None:
                continue
            metro = footprint.item.get("parent")
            if str(serving) not in zones:
                offenders.append(f"{_label(footprint)}: serving_zone {serving} is not a declared zone")
            elif metro not in zones[str(serving)]:
                offenders.append(
                    f"{_label(footprint)}: serving_zone {serving} is in {sorted(map(str, zones[str(serving)]))}, "
                    f"the footprint in {metro}"
                )
        assert offenders == []


@pytest.mark.parametrize("demo", sorted(_DEMO_SETS))
class TestColocationDesigns:
    """The designs the demo data names are bootstrap rows, and the kit it declares fits them."""

    def test_every_node_that_declares_kit_has_a_design(self, demo: str) -> None:
        """Kit without a design is sized with the fixed fallback and nothing caps it (the check errors)."""
        offenders = []
        for kind in (_ZONE, _VIRTUAL):
            for node_name, declarations in _declarations(demo, kind).items():
                if any(obj.item.get("fabric_templates") for obj in declarations) and _design_of(declarations) is None:
                    offenders.append(f"{kind} {node_name}")
        assert offenders == []

    def test_every_metro_whose_children_declare_kit_has_a_design(self, demo: str) -> None:
        """A metro without a design creates no supernets; its kit would slice the bootstrap pools unbounded."""
        metros = _declarations(demo, _METRO)
        offenders = []
        for metro, children in _children(demo).items():
            if any(child.item.get("fabric_templates") for child in children) and _design_of(metros[metro]) is None:
                offenders.append(metro)
        assert offenders == []

    def test_design_names_are_bootstrap_rows(self, demo: str) -> None:
        """A typo in ``design:`` is a dangling HFID at load time; catch it here."""
        offenders = []
        for kind, known in (
            (_METRO, COLO_METRO_DESIGN_NAMES),
            (_ZONE, COLO_ZONE_DESIGN_NAMES),
            (_VIRTUAL, COLO_ZONE_DESIGN_NAMES),
        ):
            for obj in _objects(demo, kind):
                design = _design_name(obj)
                if design is not None and design not in known:
                    offenders.append(f"{_label(obj)}: design {design!r} is not one of {list(known)}")
        assert offenders == []

    def test_a_childs_design_is_one_of_its_metro_designs_zone_templates(self, demo: str) -> None:
        """The metro's supernets are sized for the zone designs it lists, no others."""
        metros = _declarations(demo, _METRO)
        offenders = []
        for metro, children in _children(demo).items():
            metro_design = _design_of(metros[metro])
            if metro_design is None:
                continue
            allowed = {zone["name"] for zone in colo_metro_design(metro_design)["zone_templates"]}
            for child in children:
                design = _design_of(_declarations(demo, child.kind)[name(child)])
                if design is not None and design not in allowed:
                    offenders.append(
                        f"{_label(child)}: design {design} is not in metro {metro} design {metro_design} {sorted(allowed)}"
                    )
        assert offenders == []

    def test_kit_is_within_the_design_maximum_of_each_role(self, demo: str) -> None:
        """Edge, firewall and load-balancer totals of a node never pass max_edges / max_firewalls / max_load_balancers."""
        offenders = []
        for kind in (_ZONE, _VIRTUAL):
            for node_name, declarations in _declarations(demo, kind).items():
                design = _design_of(declarations)
                if design is None:
                    continue
                maxima = colo_zone_design(design)
                for obj in declarations:
                    for role, total in _kit_by_role(obj).items():
                        if total > maxima[_MAX_BY_ROLE[role]]:
                            offenders.append(
                                f"{_label(obj)}: {total} {role} > {_MAX_BY_ROLE[role]}={maxima[_MAX_BY_ROLE[role]]} of {design}"
                            )
        assert offenders == []

    def test_a_metro_holds_no_more_zones_and_virtual_colocations_than_its_max_zones(self, demo: str) -> None:
        """max_zones counts zones AND virtual colocations together: they share the metro's supernets and ASN block."""
        metros = _declarations(demo, _METRO)
        offenders = []
        for metro, children in _children(demo).items():
            metro_design = _design_of(metros[metro])
            if metro_design is not None and len(children) > colo_metro_design(metro_design)["max_zones"]:
                offenders.append(f"{metro}: {sorted(name(c) for c in children)} > max_zones of {metro_design}")
        assert offenders == []

    def test_the_designs_are_actually_used(self, demo: str) -> None:
        """Guards the walker: if no design were found every test above would pass vacuously."""
        assert any(_design_name(obj) for obj in _objects(demo, _METRO))
        assert any(_design_name(obj) for obj in _objects(demo, _ZONE, _VIRTUAL))


class TestAllDemoServingZones:
    """The 30_all customers that share the Frankfurt firewall pair."""

    def test_c001_and_c005_are_served_by_fr2(self) -> None:
        """FR2 carries the edge pair and the firewalls; both footprints name it, so they share one context."""
        served = {
            str(footprint.item.get("owner")): footprint.item.get("serving_zone")
            for footprint in _objects("30_all", _FOOTPRINT)
            if footprint.item.get("parent") == "FR"
        }
        assert served == {"C001": "FR2", "C005": "FR2"}

    def test_the_serving_zone_declares_a_firewall(self) -> None:
        """A serving zone without a firewall gets its footprints no FirewallContext (checks/colocation_placement.py)."""
        zones = {name(zone): zone for zone in _objects("30_all", _ZONE)}
        for footprint in _objects("30_all", _FOOTPRINT):
            serving = footprint.item.get("serving_zone")
            if serving is None:
                continue
            roles = [row[1] for row in zones[str(serving)].item.get("fabric_templates") or []]
            assert "firewall" in roles, f"{_label(footprint)}: zone {serving} declares no firewall"

    def test_equinix_amsterdam_is_one_virtual_colocation(self) -> None:
        """Decision 6: the Amsterdam presence is AM-NE under metro AM; the old zones AM1/AM4 are gone."""
        virtual = {name(obj): obj for obj in _objects("30_all", _VIRTUAL)}
        assert "AM-NE" in virtual
        assert _metro_of(virtual["AM-NE"]) == "AM"
        assert {"AM1", "AM4"}.isdisjoint({name(obj) for obj in _objects("30_all", _ZONE)})

    def test_30_all_virtual_colocation_carries_no_kit(self) -> None:
        """AM-NE has no fabric_templates in 30_all (the edge pair of the Amsterdam on-ramp is declared elsewhere)."""
        assert all(not obj.item.get("fabric_templates") for obj in _objects("30_all", _VIRTUAL))

    def test_10_colocation_virtual_colocation_carries_the_edge_pair(self) -> None:
        """The colocation demo alone has AM-NE host two C8000V edges."""
        am_ne = next(obj for obj in _objects("10_colocation", _VIRTUAL) if name(obj) == "AM-NE")
        assert am_ne.item["fabric_templates"] == [["2", "edge", "C8000V_EDGE"]]


class TestVirtualColocationTerraform:
    """AM-NE is the one Terraform-orchestrated presence: routed by infrastructure_orchestrator, ordered from its template."""

    def test_am_ne_is_orchestrated_by_terraform_in_the_colocation_demo(self) -> None:
        """The virtual_colocation_terraform routing group is picked from this value."""
        am_ne = next(obj for obj in _objects("10_colocation", _VIRTUAL) if name(obj) == "AM-NE")

        assert am_ne.item["infrastructure_orchestrator"] == "terraform"
        assert am_ne.kind == _VIRTUAL

    def test_the_orchestrator_values_are_schema_choices(self) -> None:
        """A value outside the dropdown would fail the load; the object files only ever name a real choice."""
        schema = yaml.safe_load((ROOT / "schemas" / "extensions" / "topology" / "topology_customer.yml").read_text())
        generic = next(g for g in schema["generics"] if g["name"] == "OrchestratorScoped")
        choices = {choice["name"] for choice in generic["attributes"][0]["choices"]}
        used = {
            obj.item["infrastructure_orchestrator"]
            for demo in _DEMO_SETS
            for obj in _objects(demo, _VIRTUAL, _FOOTPRINT)
            if "infrastructure_orchestrator" in obj.item
        }
        assert "terraform" in used
        assert used <= choices

    def test_the_template_it_uses_carries_the_provider_order_fields(self) -> None:
        """The Terraform artifact reads type code, package, version, term and cores off the appliance (from its template)."""
        am_ne = next(obj for obj in _objects("10_colocation", _VIRTUAL) if name(obj) == "AM-NE")
        template_names = {row[2] for row in am_ne.item["fabric_templates"]}
        assert template_names == {"C8000V_EDGE"}

        for template_name in template_names:
            template = object_templates()[template_name]
            for field in ("service_code", "package_code", "software_version", "term_length", "cpu_count"):
                assert template.get(field) not in (None, ""), f"{template_name}: {field} is not set"
            assert isinstance(template["term_length"], int)
            assert template["term_length"] in (1, 12, 24, 36)

    def test_the_provider_fields_are_declared_on_the_appliance_schema_and_gone_from_the_virtual_device(self) -> None:
        """Template values only load if CloudNetworkAppliance (and so its templates) has the attributes; DcimVirtualDevice
        no longer carries any provider ordering field."""
        assert {"service_code", "package_code", "software_version", "term_length", "cpu_count", "cloud_id"} <= (
            attribute_names("CloudNetworkAppliance")
        )
        assert not [name_ for name_ in attribute_names("DcimVirtualDevice") if name_.startswith("provider_")]

    def test_the_presence_is_ordered_under_an_equinix_account_of_the_internal_owner(self) -> None:
        """AM-NE names its CloudAccount; the account is Equinix's, owned by Internal (P028), which carries the e-mails."""
        am_ne = next(obj for obj in _objects("10_colocation", _VIRTUAL) if name(obj) == "AM-NE")
        assert "provider_account_number" not in am_ne.item and "owner" not in am_ne.item
        accounts = {name(obj): obj for obj in _objects("10_colocation", "CloudAccount")}
        account = accounts[str(am_ne.item["account"])].item

        assert account["provider"] == "P016"  # Equinix
        assert account["owner"] == "P028"  # Internal
        assert str(account["account_id"]).strip()
        providers = {
            str(entry["org_id"]): entry
            for document in yaml.safe_load_all((_BOOTSTRAP / "02_providers.yml").read_text())
            if isinstance(document, dict) and document["spec"]["kind"] == "OrganizationProvider"
            for entry in document["spec"]["data"]
        }
        assert providers["P016"]["name"] == "Equinix"
        assert providers["P028"]["notification_emails"]

    def test_the_account_is_declared_before_the_presence_that_names_it(self) -> None:
        """Object files load in name order: 02_account.yml sorts before 02_topology.yml."""
        am = _DEMOS / "10_colocation" / "equinix" / "am"
        assert sorted(path.name for path in am.glob("0[0-9]_*.yml")).index("02_account.yml") < sorted(
            path.name for path in am.glob("0[0-9]_*.yml")
        ).index("02_topology.yml")


def test_template_kind_lookup_knows_both_template_families() -> None:
    """The lookup used above resolves a physical and a virtual template, so a typo cannot hide as 'unknown'."""
    known: dict[str, Any] = _template_kinds()
    assert known["N9K-C9316D-GX_EDGE"] == "TemplateDcimPhysicalDevice"
    assert known["C8000V_EDGE"] == "TemplateCloudNetworkAppliance"
    assert known["PA-VM_EDGE_CUSTOMER_S"] == "TemplateDcimVirtualDevice"
    assert Path(_BOOTSTRAP).is_dir()
