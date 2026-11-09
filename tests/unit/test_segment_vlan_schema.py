"""The schema of the VLAN rows: segment -> realization in the DC (VNI) -> realization on the switch (VLAN).

A row is a child of the segment deployment its switch sits in, points at its device (no VLAN domain object), and a port
is given a segment, never a VLAN (the row has no port).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml


def _load(root_dir: Path, relative: str) -> dict[str, Any]:
    return yaml.safe_load((root_dir / relative).read_text())


def _node(schema: dict[str, Any], name: str) -> dict[str, Any]:
    return next(n for n in schema["nodes"] if n["name"] == name)


def _relationship(node: dict[str, Any], name: str) -> dict[str, Any]:
    return next(r for r in node["relationships"] if r["name"] == name)


@pytest.fixture(scope="module")
def segment_schema(root_dir: Path) -> dict[str, Any]:
    return _load(root_dir, "schemas/extensions/service/segment.yml")


class TestRowIsAChildOfItsSwitchUnderTheSegmentDeployment:
    def test_the_row_has_a_mandatory_deployment_and_no_segment(self, segment_schema: dict[str, Any]) -> None:
        """The deployment is a plain mandatory edge: the Parent is the switch."""
        row = _node(segment_schema, "SegmentVlan")

        deployment = _relationship(row, "segment_deployment")
        assert (deployment["kind"], deployment["cardinality"], deployment["optional"]) == ("Attribute", "one", False)
        assert deployment["peer"] == "ManagedSegmentDeployment"
        assert "segment" not in {r["name"] for r in row["relationships"]}

    def test_the_row_is_a_child_of_its_switch_and_dies_with_it(self, segment_schema: dict[str, Any]) -> None:
        row = _node(segment_schema, "SegmentVlan")
        device = _relationship(row, "device")
        assert (device["kind"], device["cardinality"], device["optional"]) == ("Parent", "one", False)
        extension = next(n for n in segment_schema["extensions"]["nodes"] if n["kind"] == "DcimPhysicalDevice")
        vlans = _relationship(extension, "vlans")
        assert (vlans["kind"], vlans["on_delete"], vlans["identifier"]) == (
            "Component",
            "cascade",
            device["identifier"],
        )

    def test_the_row_lists_its_ports_of_its_own_switch_only(
        self, segment_schema: dict[str, Any], root_dir: Path
    ) -> None:
        """A port is a member of its switch's row: the row inherits the interface edge and re-declares it with
        ``common_parent: device``, so the server refuses another switch's port; nothing else names a port."""
        row = _node(segment_schema, "SegmentVlan")

        assert "ManagedGenericInterfaces" in row["inherit_from"]
        ports = _relationship(row, "capabilities")
        generic = _load(root_dir, "schemas/extensions/capabilities/capabilities.yml")
        edge = _relationship(next(g for g in generic["generics"] if g["name"] == "GenericInterfaces"), "capabilities")
        assert (ports["peer"], ports["identifier"], ports["common_parent"]) == (
            "DcimInterface",
            edge["identifier"],
            "device",
        )
        assert not {r["name"] for r in row["relationships"]} & {"interfaces", "interface", "ports"}
        # The deployment keeps the interface edge too: the ports a person asks for (the request, not the realization).
        assert "ManagedGenericInterfaces" in _node(segment_schema, "SegmentDeployment")["inherit_from"]

    def test_the_deployment_owns_its_rows_and_they_go_with_it(self, segment_schema: dict[str, Any]) -> None:
        deployment = _node(segment_schema, "SegmentDeployment")

        rows = _relationship(deployment, "vlans")
        assert (rows["kind"], rows["cardinality"], rows["on_delete"]) == ("Component", "many", "cascade")
        assert (
            rows["identifier"]
            == _relationship(_node(segment_schema, "SegmentVlan"), "segment_deployment")["identifier"]
        )

    def test_a_vlan_id_is_unique_per_device_and_one_row_per_deployment_and_device(
        self, segment_schema: dict[str, Any]
    ) -> None:
        row = _node(segment_schema, "SegmentVlan")

        assert ["segment_deployment", "device"] in row["uniqueness_constraints"]
        assert ["device", "vlan_id__value"] in row["uniqueness_constraints"]

    def test_the_segment_lists_its_rows_through_its_deployments_only_and_no_ports(
        self, segment_schema: dict[str, Any]
    ) -> None:
        extension = next(n for n in segment_schema["extensions"]["nodes"] if n["kind"] == "ManagedVxlanSegment")

        assert {r["name"] for r in extension["relationships"]} == {"segment_deployments"}

    def test_the_label_names_the_vlan_the_device_and_the_vni(self, segment_schema: dict[str, Any]) -> None:
        """A relationship path in a label is one hop: the deployment's VNI, not the segment two hops away."""
        label = _node(segment_schema, "SegmentVlan")["display_label"]

        assert "segment_deployment__vni__value" in label
        assert "device__name__value" in label


class TestTheRowPointsAtItsDevice:
    def test_the_row_has_a_mandatory_device_and_one_svi(self, segment_schema: dict[str, Any]) -> None:
        row = _node(segment_schema, "SegmentVlan")

        device = _relationship(row, "device")
        assert (device["peer"], device["optional"]) == ("DcimPhysicalDevice", False)
        assert _relationship(row, "svi")["cardinality"] == "one"
        assert "vlan_domain" not in {r["name"] for r in row["relationships"]}

    def test_the_vlan_domain_generic_is_gone(self, root_dir: Path) -> None:
        """No domain object and no marker generic: a device inherits nothing for it, an MLAG neither."""
        dcim = _load(root_dir, "schemas/base/dcim.yml")
        mlag = _load(root_dir, "schemas/extensions/capabilities/mlag.yml")

        assert "ManagedGenericVlanDomain" not in {g["name"] for g in dcim["generics"]}
        assert "ManagedGenericVlanDomain" not in _node(dcim, "PhysicalDevice")["inherit_from"]
        assert "ManagedGenericVlanDomain" not in _node(mlag, "MLAG")["inherit_from"]

    def test_no_standalone_domain_kind_is_left(self, root_dir: Path) -> None:
        offenders = [
            str(path.relative_to(root_dir))
            for directory in ("schemas", "generators", "transforms", "checks", "queries", "data")
            for path in (root_dir / directory).rglob("*")
            if path.is_file()
            and path.suffix in {".yml", ".py", ".gql", ".j2"}
            and "StandaloneVlanDomain" in path.read_text()
        ]

        assert offenders == []
