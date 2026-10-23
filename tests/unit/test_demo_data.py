"""Unit tests for tests/integration/demo_data.py, the YAML inventory the 30_all expectations are computed from."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.integration.demo_data import (
    attribute,
    attribute_default,
    created_dispatches,
    declared,
    declared_in,
    distinct,
    human_friendly_id,
    identity,
    name,
    relationship,
)

_SEGMENTS = """\
---
apiVersion: infrahub.app/v1
kind: Object
spec:
  kind: OrganizationCustomer
  data:
    - name: "Krafven GmbH"
      org_id: "C008"
      network_segments:
        - kind: ManagedVxlanSegment
          data:
            customer_name: "trade-portal"
            environment: "p"
            gateway:
              data:
                address: "10.8.10.1/24"
                ip_namespace: "PROD"
        - kind: ManagedVlanSegment
          data:
            customer_name: "Krafven Dev"
            environment: "d"
"""

_OFFICES = """\
---
apiVersion: infrahub.app/v1
kind: Object
spec:
  kind: TopologyCustomerOffice
  data:
    - owner: "C002"
      parent: "Campus and Office"
      member_of_groups:
        - customer_deployments_office
---
apiVersion: infrahub.app/v1
kind: Object
spec:
  kind: TopologyCustomerOffice
  data:
    - owner: "C002"
      parent: "Campus and Office"
      member_of_groups:
        - customer_deployments_office
      sdwan_gateway: "GW1"
"""

_APPLICATION = """\
---
apiVersion: infrahub.app/v1
kind: Object
spec:
  kind: AppApplication
  data:
    - label: Trade Portal
      environment: p
      owner: C008
      member_of_groups: [app_applications]
      children:
        kind: AppComponent
        data:
          - name: api
            fqdn: api.trade-portal.c008.demo.local
            member_of_groups: [app_components]
          - name: web
            fqdn: web.trade-portal.c008.demo.local
            member_of_groups: [app_components]
    - label: outsider
      environment: d
      owner: C009
"""


def _write(directory: Path, files: dict[str, str]) -> Path:
    """Write object files under a fresh directory and return it."""
    for relative, text in files.items():
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return directory


@pytest.fixture
def demo(tmp_path: Path) -> Path:
    """A miniature demo tree with the three nesting forms and one re-declaration."""
    return _write(
        tmp_path, {"07/00_segments.yml": _SEGMENTS, "05/02_offices.yml": _OFFICES, "07/01_app.yml": _APPLICATION}
    )


class TestWalk:
    """Every nesting form an object file uses is walked."""

    def test_kind_and_data_list_entries_are_walked(self, demo: Path) -> None:
        """network_segments: [{kind, data: {...}}] yields one object per entry, a single-object data included."""
        assert [obj.item["customer_name"] for obj in declared("ManagedVxlanSegment", demo)] == ["trade-portal"]
        assert [obj.item["customer_name"] for obj in declared("ManagedVlanSegment", demo)] == ["Krafven Dev"]

    def test_relationship_data_without_kind_takes_the_schema_peer(self, demo: Path) -> None:
        """gateway: {data: {...}} is an object of the gateway relationship's peer kind."""
        gateway = relationship("ManagedVxlanSegment", "gateway")
        assert gateway is not None
        gateways = declared(gateway["peer"], demo)
        assert [gateway.item["address"] for gateway in gateways] == ["10.8.10.1/24"]
        assert gateways[0].enclosing is not None and gateways[0].enclosing.kind == "ManagedVxlanSegment"

    def test_kind_and_data_mapping_with_a_list_is_walked(self, demo: Path) -> None:
        """children: {kind, data: [...]} yields each child, linked to its parent."""
        components = declared("AppComponent", demo)
        assert [component.item["name"] for component in components] == ["api", "web"]
        assert {component.enclosing.item["label"] for component in components if component.enclosing} == {
            "Trade Portal"
        }

    def test_files_are_read_in_load_order(self, demo: Path) -> None:
        """Sorted by path, like `infrahubctl object load` reads a directory."""
        paths = [obj.path.relative_to(demo).as_posix() for obj in declared_in(demo)]
        assert paths == sorted(paths)

    def test_non_object_documents_are_ignored(self, tmp_path: Path) -> None:
        """A menu or empty document contributes nothing."""
        directory = _write(tmp_path, {"menu.yml": "---\napiVersion: infrahub.app/v1\nkind: Menu\nspec:\n  data: []\n"})
        assert declared_in(directory) == ()


class TestNames:
    """Computed names render like the server's Jinja2 templates."""

    def test_segment_name_takes_its_owner_from_the_enclosing_organization(self, demo: Path) -> None:
        """{{owner__org_id|lower}}-{{customer_name|lower|replace(' ', '-')}}-{{environment}}."""
        assert name(declared("ManagedVxlanSegment", demo)[0]) == "c008-trade-portal-p"
        assert name(declared("ManagedVlanSegment", demo)[0]) == "c008-krafven-dev-d"

    def test_office_name_is_computed_from_its_owner_alone(self, demo: Path) -> None:
        """An office has no environment and no wrapper: `{OWNER}-OFFICE`, whatever declaration it is."""
        first, second = declared("TopologyCustomerOffice", demo)
        assert name(first) == "C002-OFFICE"
        assert name(second) == "C002-OFFICE"

    def test_application_name_is_owner_label_environment(self, demo: Path) -> None:
        """A space in the label becomes a dash."""
        assert [name(app) for app in declared("AppApplication", demo)] == ["c008-trade-portal-p", "c009-outsider-d"]

    def test_computed_name_wins_over_a_generics_plain_one(self) -> None:
        """ManagedGeneric's plain `name` must not hide ManagedNetworkSegment's computed one."""
        assert (attribute("ManagedVxlanSegment", "name") or {}).get("computed_attribute")

    def test_unresolvable_reference_raises(self, tmp_path: Path) -> None:
        """A top-level segment with no owner cannot be named."""
        directory = _write(
            tmp_path,
            {
                "s.yml": (
                    "---\napiVersion: infrahub.app/v1\nkind: Object\nspec:\n  kind: ManagedVlanSegment\n"
                    "  data:\n    - customer_name: x\n      environment: p\n"
                )
            },
        )
        with pytest.raises(LookupError, match="owner__org_id"):
            name(declared("ManagedVlanSegment", directory)[0])


class TestDistinct:
    """A re-declaration that updates an object is the same object."""

    def test_office_declared_twice_is_one_office(self, demo: Path) -> None:
        """The stage-4 SD-WAN opt-in re-declares an office by its owner-derived name."""
        offices = declared("TopologyCustomerOffice", demo)
        assert len(offices) == 2
        assert len(distinct(offices)) == 1
        assert distinct(offices)[0] is offices[0]

    def test_distinct_objects_are_all_kept(self, demo: Path) -> None:
        """Different human-friendly ids are different objects."""
        assert len(distinct(declared("AppApplication", demo))) == 2

    def test_kind_without_an_identity_counts_every_declaration(self, tmp_path: Path) -> None:
        """No hfid resolvable from the file: each declaration is its own object."""
        text = (
            "---\napiVersion: infrahub.app/v1\nkind: Object\nspec:\n  kind: ManagedVlanSegment\n  data:\n"
            "    - customer_name: x\n    - customer_name: x\n"
        )
        objects = declared("ManagedVlanSegment", _write(tmp_path, {"s.yml": text}))
        assert identity(objects[0]) != identity(objects[1])
        assert len(distinct(objects)) == 2

    def test_rack_identity_falls_back_to_its_uniqueness_constraint(self) -> None:
        """LocationRack declares no human_friendly_id; its constraint is the computed name."""
        assert human_friendly_id("LocationRack") == ("name__value",)


class TestDefaults:
    """Values a file leaves out are the schema defaults."""

    def test_omitted_attribute_reads_the_schema_default(self, demo: Path) -> None:
        """AppApplication.criticality defaults to medium."""
        app = declared("AppApplication", demo)[0]
        assert app.value("criticality") == attribute_default("AppApplication", "criticality") == "medium"

    def test_attribute_without_a_default_is_none(self) -> None:
        """environment is required on an application: no default."""
        assert attribute_default("AppApplication", "environment") is None


class TestCreatedDispatches:
    """A load dispatches one created-trigger run per distinct target-group member."""

    def test_counts_members_of_the_target_group_once(self, demo: Path) -> None:
        """Two declarations of one office are one add_sdwan_edge run."""
        assert created_dispatches("add_sdwan_edge", demo) == 1

    def test_non_members_are_not_counted(self, demo: Path) -> None:
        """The outsider application is not in app_applications, so its trigger would raise, not run."""
        assert created_dispatches("add_app_application", demo) == 1

    def test_nested_objects_are_counted(self, demo: Path) -> None:
        """Components declared under their application dispatch add_app_component_segment."""
        assert created_dispatches("add_app_component_segment", demo) == 2

    def test_unknown_generator_raises(self, demo: Path) -> None:
        """A generator with no definition is a KeyError, not zero."""
        with pytest.raises(KeyError):
            created_dispatches("add_nothing", demo)
