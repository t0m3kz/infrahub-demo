"""Offline guards for the office fabric (add_office_fabric): schema, bootstrap data and the C005 office.

The generator can only be exercised against a live stack, so everything it assumes about the data is checked
here from the YAML alone:

- the schema lets an office opt in (fabric_templates), host segments (SegmentHosting) and be named by a VXLAN segment;
- every fabric_templates entry of a 30_all office resolves to a design element and a device template whose ports
  satisfy the cabling rules the generator relies on (uplink and downlink speeds match, one uplink per distribution
  switch, an interlink for the pair);
- the bootstrap pools and namespace the generator slices from exist, with room for every office;
- only the offices meant to have a fabric have one.
"""

from __future__ import annotations

import ipaddress
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.integration.demo_data import (
    ROOT,
    attribute,
    attribute_names,
    declared,
    distinct,
    name,
    relationship,
    relationship_names,
    schema,
)

ALL_DEMO = ROOT / "data" / "demos" / "30_all"
BOOTSTRAP = ROOT / "data" / "bootstrap"

# Offices of 30_all that opt into a fabric. Every other office stays a single SD-WAN edge.
FABRIC_OFFICES = {"C005-OFFICE"}


def _spec_data(path: Path) -> list[dict[str, Any]]:
    return [
        entry
        for document in yaml.safe_load_all(path.read_text())
        if document
        for entry in document["spec"].get("data") or []
    ]


def _count(name_pattern: str) -> int:
    """Interfaces one template entry creates (``[a-b]`` ranges expanded)."""
    match = re.search(r"\[(\d+)-(\d+)\]", name_pattern)
    return int(match.group(2)) - int(match.group(1)) + 1 if match else 1


def _ports(template: dict[str, Any], role: str) -> list[dict[str, Any]]:
    return [i for i in template["interfaces"]["data"] if i["role"] == role]


def _port_count(template: dict[str, Any], role: str) -> int:
    return sum(_count(i["name"]) for i in _ports(template, role))


@pytest.fixture(scope="module")
def templates() -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for path in sorted(BOOTSTRAP.glob("10_physical_devices_templates_*.y*ml")):
        found.update({entry["template_name"]: entry for entry in _spec_data(path)})
    return found


@pytest.fixture(scope="module")
def elements() -> set[tuple[str, str, str]]:
    return {(str(e["quantity"]), e["role"], e["template"]) for e in _spec_data(BOOTSTRAP / "14_design_elements.yml")}


@pytest.fixture(scope="module")
def fabric_offices() -> dict[str, list[list[str]]]:
    """Office name -> its fabric_templates references, for the 30_all offices that declare any."""
    return {
        name(office): office.item["fabric_templates"]
        for office in distinct(declared("TopologyCustomerOffice", ALL_DEMO))
        if office.item.get("fabric_templates")
    }


class TestSchema:
    def test_office_opts_in_with_fabric_templates_of_design_elements(self) -> None:
        rel = relationship("TopologyCustomerOffice", "fabric_templates")
        assert rel is not None
        assert rel["peer"] == "TopologyElement"
        assert rel["cardinality"] == "many"
        assert rel["optional"] is True

    def test_office_hosts_segments_itself(self) -> None:
        """SegmentHosting brings the VNI pool the segment generator reads and the LISP site key."""
        assert "TopologySegmentHosting" in schema()["TopologyCustomerOffice"].inherit_from
        names = relationship_names("TopologyCustomerOffice")
        assert {"vni_pool", "lisp_site_key"} <= names

    def test_element_roles_include_the_campus_roles(self) -> None:
        choices = {choice["name"] for choice in (attribute("TopologyElement", "role") or {})["choices"]}
        assert {"access-switch", "distribution-switch"} <= choices

    def test_a_vxlan_segment_can_name_an_office_and_the_office_has_the_reverse_side(self) -> None:
        """The office is not a TopologyCustomer, so the segment's peer is the generic it shares with the
        customer footprints (TopologyDeployment) and the office carries the reverse relationship itself."""
        forward = relationship("ManagedVxlanSegment", "customer_deployments")
        reverse = relationship("TopologyCustomerOffice", "network_segments")
        assert forward is not None and reverse is not None
        assert forward["peer"] == "TopologyDeployment"
        assert "TopologyDeployment" in schema()["TopologyCustomerOffice"].inherit_from
        assert forward["identifier"] == reverse["identifier"]
        assert reverse["peer"] == "ManagedNetworkSegment"

    def test_office_is_always_prod_and_owned_directly(self) -> None:
        """No environment, status or wrapper: the owner sits on the office, its parent is the campus label."""
        assert not {"environment", "status", "namespace"} & attribute_names("TopologyCustomerOffice")
        assert "TopologyCustomer" not in schema()["TopologyCustomerOffice"].inherit_from
        assert "TopologyEnvironmentScoped" not in schema()["TopologyCustomerOffice"].inherit_from
        owner = relationship("TopologyCustomerOffice", "owner")
        assert owner is not None
        assert owner["peer"] == "OrganizationEntity"
        assert owner.get("optional") is False
        assert "TopologyOfficeCustomer" not in schema()

    def test_relationship_identifiers_are_unique_per_kind(self) -> None:
        identifiers = [rel.get("identifier") for rel in schema()["TopologyCustomerOffice"].relationships.values()]
        named = [i for i in identifiers if i]
        assert len(named) == len(set(named))


class TestOfficeData:
    def test_only_the_fabric_offices_declare_fabric_templates(self, fabric_offices: dict[str, Any]) -> None:
        assert set(fabric_offices) == FABRIC_OFFICES

    def test_every_office_row_is_single_level_with_its_owner_and_the_campus_label(self) -> None:
        """The office is declared directly (no OfficeCustomer wrapper row): owner and label parent on the row."""
        offices = distinct(declared("TopologyCustomerOffice", ALL_DEMO))
        assert offices
        for office in offices:
            assert office.item.get("owner"), f"{name(office)}: no owner"
            assert office.item.get("parent") == "Campus and Office"
            assert "environment" not in office.item
            assert name(office).endswith("-OFFICE")

    def test_every_fabric_office_is_in_the_generator_target_group(self) -> None:
        for office in distinct(declared("TopologyCustomerOffice", ALL_DEMO)):
            if name(office) in FABRIC_OFFICES:
                assert "customer_deployments_office" in office.groups()

    def test_references_resolve_to_elements_and_templates_of_the_right_role(
        self,
        fabric_offices: dict[str, list[list[str]]],
        elements: set[tuple[str, str, str]],
        templates: dict[str, dict[str, Any]],
    ) -> None:
        for office, references in fabric_offices.items():
            roles = [reference[1] for reference in references]
            # one entry per role: device names derive from the role alone
            assert len(roles) == len(set(roles)), f"{office}: two entries for one role"
            assert set(roles) <= {"access-switch", "distribution-switch"}
            for quantity, role, template in map(tuple, references):
                assert (str(quantity), role, template) in elements, f"{office}: no design element {references}"
                assert templates[template]["role"] == role
                assert templates[template]["platform"] == "cisco_xe"

    def test_ports_satisfy_the_cabling_the_generator_plans(
        self, fabric_offices: dict[str, list[list[str]]], templates: dict[str, dict[str, Any]]
    ) -> None:
        for office, references in fabric_offices.items():
            by_role = {role: (int(quantity), templates[template]) for quantity, role, template in references}
            distribution_quantity, distribution = by_role["distribution-switch"]
            access_quantity, access = by_role["access-switch"]
            # rack strategy: every access-switch needs one uplink per distribution-switch ...
            assert _port_count(access, "uplink") >= distribution_quantity, office
            # ... and the i-th access-switch lands on the i-th downlink of each distribution-switch
            assert _port_count(distribution, "downlink") >= access_quantity, office
            # speed-aware cabling: the two ends must share a speed
            assert {p["interface_type"] for p in _ports(access, "uplink")} == {
                p["interface_type"] for p in _ports(distribution, "downlink")
            }, office
            if distribution_quantity == 2:
                assert _port_count(distribution, "interlink") >= 1, office
            for template in (access, distribution):
                assert _port_count(template, "management") == 1


class TestPools:
    """The parents add_office_fabric slices (generators/topology/office_fabric.py) exist and have room."""

    PARENTS = {
        "Loopback-IPv4": (28, 5),
        "Management-IPv4": (28, 5),
        "Technical-IPv4": (27, 5),
    }

    def test_parent_pools_exist_and_hold_a_slice_per_office(self) -> None:
        pools = {entry["name"]: entry for entry in _spec_data(BOOTSTRAP / "17_ip_prefix_pools.yml")}
        for pool, (length, offices) in self.PARENTS.items():
            supernet = ipaddress.ip_network(pools[pool]["resources"]["data"][0]["prefix"])
            slices = 2 ** (length - supernet.prefixlen)
            assert slices >= offices * 8, f"{pool} {supernet} cannot hold /{length} slices for {offices} offices"

    def test_slice_sizes_match_the_generator(self) -> None:
        from generators.topology.office_fabric import (
            _OFFICE_LOOPBACK_PREFIX_LENGTH,
            _OFFICE_MANAGEMENT_PREFIX_LENGTH,
            _OFFICE_TECHNICAL_PREFIX_LENGTH,
        )

        assert {pool: length for pool, (length, _) in self.PARENTS.items()} == {
            "Loopback-IPv4": _OFFICE_LOOPBACK_PREFIX_LENGTH,
            "Management-IPv4": _OFFICE_MANAGEMENT_PREFIX_LENGTH,
            "Technical-IPv4": _OFFICE_TECHNICAL_PREFIX_LENGTH,
        }

    def test_management_namespace_exists(self) -> None:
        assert "MANAGEMENT" in {entry["name"] for entry in _spec_data(BOOTSTRAP / "22_namespaces.yml")}

    def test_technical_slice_holds_the_links_of_the_demo_office(self) -> None:
        """2 distribution x 2 access uplinks + 1 interlink = 5 /31s; a /27 holds 16."""
        from generators.topology.office_fabric import _OFFICE_TECHNICAL_PREFIX_LENGTH

        assert 2 ** (31 - _OFFICE_TECHNICAL_PREFIX_LENGTH) >= 5


class TestWanExitData:
    """What the WAN exit (add_office_fabric._ensure_wan_exit) assumes of the data, read from the YAML alone."""

    EDGE_TEMPLATE = "VCE-610_EDGE"

    @staticmethod
    def _sdwan_fabric_offices() -> list[Any]:
        return [
            office
            for office in distinct(declared("TopologyCustomerOffice", ALL_DEMO))
            if office.item.get("fabric_templates") and office.item.get("sdwan_gateway")
        ]

    def test_the_demo_office_has_both_a_fabric_and_an_sdwan_edge(self) -> None:
        """No new operator input: the exit exists because the office declares both, nothing else."""
        assert {name(office) for office in self._sdwan_fabric_offices()} == FABRIC_OFFICES

    def test_each_distribution_switch_has_one_wan_port_facing_an_edge_lan_port(
        self, fabric_offices: dict[str, list[list[str]]], templates: dict[str, dict[str, Any]]
    ) -> None:
        """rack cabling: edge (bottom) ports to one `uplink` port per distribution-switch, speed-matched."""
        edge = templates[self.EDGE_TEMPLATE]
        for office, references in fabric_offices.items():
            quantity, _, template = next(ref for ref in references if ref[1] == "distribution-switch")
            distribution = templates[template]
            # the WAN port is the only uplink of a distribution-switch (an access-switch's uplinks face it)
            assert _port_count(distribution, "uplink") == 1, office
            assert _port_count(edge, "downlink") >= int(quantity), f"{office}: fewer edge LAN ports than switches"
            assert {p["interface_type"] for p in _ports(distribution, "uplink")} <= {
                p["interface_type"] for p in _ports(edge, "downlink")
            }, office

    def test_the_tenant_namespace_transit_pool_and_asn_pool_exist(self) -> None:
        from generators.topology.office_fabric import _WAN_EXIT_ASN_POOL
        from utils.exchange_transit import transit_pool_name

        namespaces = {entry["name"] for entry in _spec_data(BOOTSTRAP / "22_namespaces.yml")}
        pools = {entry["name"] for entry in _spec_data(BOOTSTRAP / "23_transit_pools.yml") if "resources" in entry}
        # an office is always PROD: its exit lives in the PROD tenant VRF
        assert self._sdwan_fabric_offices()
        assert "PROD" in namespaces
        assert transit_pool_name("PROD") in pools
        assert _WAN_EXIT_ASN_POOL in {entry["name"] for entry in _spec_data(BOOTSTRAP / "16_asn_pools.yml")}

    def test_the_demo_office_hosts_one_derived_vxlan_segment_in_its_own_environment(self) -> None:
        """The border needs a VRF with an L3 instance-id to exit from: the office hosts a segment there."""
        segments = [
            segment
            for segment in distinct(declared("ManagedVxlanSegment", ALL_DEMO))
            if "C005-OFFICE" in (segment.item.get("customer_deployments") or [])
        ]
        assert [segment.item["customer_name"] for segment in segments] == ["office-lan"]
        assert segments[0].item["environment"] == "p"
        assert "gateway" not in segments[0].item, "gateway and prefix are derived (tenant pool)"
        assert not segments[0].item.get("stretch_scope"), "an office never stretches a segment"

    def test_the_hosted_segment_is_the_offices_only_lan_there_is_no_typed_lan_prefix(self) -> None:
        """The office-lan segment's /24 is derived from the tenant pool and is what the SD-WAN handoff registers
        (get_external_peer_prefixes): a typed lan_prefixes entry would be a second, unreachable LAN."""
        (office,) = [office for office in declared("TopologyCustomerOffice", ALL_DEMO) if name(office) == "C005-OFFICE"]

        assert not office.item.get("lan_prefixes")
        assert office.item.get("sdwan_gateway")

    def test_the_test_constants_derive_the_offices_switches_from_the_fabric_templates(self) -> None:
        from tests.integration.test_constants import ALL_DEMO_OFFICE_FABRICS, ALL_DEMO_OFFICE_WAN_EXITS

        assert ALL_DEMO_OFFICE_FABRICS == {
            "C005-OFFICE": {
                "distribution-switch": ("ds-c005-office01", "ds-c005-office02"),
                "access-switch": ("as-c005-office01", "as-c005-office02"),
            }
        }
        assert ALL_DEMO_OFFICE_WAN_EXITS == {"C005-OFFICE": ("ds-c005-office01", "ds-c005-office02")}
