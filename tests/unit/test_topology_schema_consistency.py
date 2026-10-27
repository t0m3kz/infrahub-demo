"""Consistency of the topology schema files that the colocation redesign touched.

The schema is the single source of truth, and several generators, queries and checks rely on facts the YAML alone
states and Infrahub only rejects at load time (or never):

- every touched schema file parses as a SchemaRoot;
- ``infrastructure_orchestrator`` is defined once, on the generic TopologyOrchestratorScoped, and every kind
  an orchestrator-routing generator routes by it inherits that generic (a generic cannot inherit a generic, so each
  node lists it);
- the colocation metro lost the kit (``deployment_type``, ``fabric_templates``); the zone and the virtual colocation
  get it from the one generic TopologyColocationFabric;
- relationship identifiers are unique within topology_colocation.yml (two relationships sharing one are the two ends
  of a single relationship, which is never what a copy-pasted block means), and a colocation footprint has no
  ``serving_zone`` (it is served by every zone of its metro with firewalls).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from functools import cache
from pathlib import Path
from typing import Any

import pytest
import yaml
from infrahub_sdk.schema import SchemaRoot

from generators.topology.orchestrator_routing import BaseOrchestratorRoutingGenerator
from tests.integration.demo_data import ROOT

_SCHEMAS = ROOT / "schemas"
_TOPOLOGY = _SCHEMAS / "extensions" / "topology"
_TOUCHED = sorted([*_TOPOLOGY.glob("*.yml"), _SCHEMAS / "base" / "dcim.yml"])
_COLOCATION_FILE = _TOPOLOGY / "topology_colocation.yml"

_ORCHESTRATED = "TopologyOrchestratorScoped"
_ORCHESTRATOR_ATTRIBUTE = "infrastructure_orchestrator"
_FABRIC = "TopologyColocationFabric"


@cache
def _document(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text())


def _entries(path: Path) -> list[dict[str, Any]]:
    """Generics and nodes of one schema file, each with its kind (namespace + name)."""
    document = _document(path)
    return [
        {**entry, "kind": entry["namespace"] + entry["name"]}
        for entry in [*(document.get("generics") or []), *(document.get("nodes") or [])]
    ]


@cache
def _all_entries() -> dict[str, tuple[Path, dict[str, Any]]]:
    """Kind -> (file, definition) over every schema file under schemas/."""
    found: dict[str, tuple[Path, dict[str, Any]]] = {}
    for path in sorted(_SCHEMAS.rglob("*.yml")):
        for entry in _entries(path):
            found[entry["kind"]] = (path, entry)
    return found


def _kind(kind: str) -> dict[str, Any]:
    return _all_entries()[kind][1]


def _relationship_names(kind: str) -> set[str]:
    return {relationship["name"] for relationship in _kind(kind).get("relationships") or []}


def _attribute_names(kind: str) -> set[str]:
    return {attribute["name"] for attribute in _kind(kind).get("attributes") or []}


@pytest.mark.parametrize("path", _TOUCHED, ids=lambda p: p.relative_to(ROOT).as_posix())
def test_touched_schema_file_parses_as_a_schema_root(path: Path) -> None:
    """SchemaRoot validates the structure (attribute kinds, cardinality, ...) without a running Infrahub."""
    root = SchemaRoot(**_document(path))

    document = _document(path)
    assert len(root.nodes) == len(document.get("nodes") or [])
    assert len(root.generics) == len(document.get("generics") or [])


def test_colocation_file_declares_the_expected_kinds() -> None:
    """Guards the walker, and the names every other test below refers to."""
    assert {entry["kind"] for entry in _entries(_COLOCATION_FILE)} == {
        _FABRIC,
        "TopologyColocation",
        "TopologyColocationMetro",
        "TopologyColocationZone",
        "TopologyVirtualColocation",
        "TopologyTemplateColocationMetro",
        "TopologyTemplateColocationZone",
    }


class TestInfrastructureOrchestrator:
    """One definition of the attribute for everything routed by it."""

    def test_attribute_is_defined_exactly_once_across_all_schema_files(self) -> None:
        """Generics, nodes and extensions alike: a second definition would drift from the choices of the first."""
        definitions: list[str] = []
        for path in sorted(_SCHEMAS.rglob("*.yml")):
            document = _document(path)
            for section in ("generics", "nodes"):
                for entry in document.get(section) or []:
                    if any(a["name"] == _ORCHESTRATOR_ATTRIBUTE for a in entry.get("attributes") or []):
                        definitions.append(f"{entry['namespace']}{entry['name']} ({path.relative_to(ROOT).as_posix()})")
            for extension in (document.get("extensions") or {}).get("nodes") or []:
                if any(a["name"] == _ORCHESTRATOR_ATTRIBUTE for a in extension.get("attributes") or []):
                    definitions.append(f"extension of {extension['kind']} ({path.relative_to(ROOT).as_posix()})")

        assert definitions == [f"{_ORCHESTRATED} (schemas/extensions/topology/topology_customer.yml)"]

    def test_no_other_kind_defines_the_attribute(self) -> None:
        """Equivalent view by kind, so a failure names the offender."""
        owners = sorted(kind for kind in _all_entries() if _ORCHESTRATOR_ATTRIBUTE in _attribute_names(kind))
        assert owners == [_ORCHESTRATED]

    @pytest.mark.parametrize(
        "kind",
        [
            "TopologyCustomerDC",
            "TopologyCustomerColocation",
            "TopologyCustomerCloud",
            "TopologyCustomerOffice",
            "TopologyVirtualColocation",
        ],
    )
    def test_kind_inherits_the_generic(self, kind: str) -> None:
        assert _ORCHESTRATED in _kind(kind)["inherit_from"]

    def test_every_kind_an_orchestrator_routing_generator_routes_inherits_the_generic(self) -> None:
        """The routing generators read the attribute from their query roots: each root must have it."""
        routed: dict[str, list[str]] = defaultdict(list)
        for generator in BaseOrchestratorRoutingGenerator.__subclasses__():
            if generator.orchestrator_field == _ORCHESTRATOR_ATTRIBUTE:
                for root in generator.query_roots:
                    routed[root].append(generator.__name__)

        assert set(routed) >= {
            "TopologyCustomerDC",
            "TopologyCustomerColocation",
            "TopologyCustomerOffice",
            "TopologyCustomerCloud",
            "TopologyVirtualColocation",
        }
        assert {root: generators for root, generators in routed.items() if len(generators) > 1} == {}
        assert [root for root in routed if _ORCHESTRATED not in _kind(root)["inherit_from"]] == []

    def test_the_inheriting_kinds_list_exactly_what_the_routing_generators_route(self) -> None:
        """No kind inherits the generic without a routing generator reading it (dead schema), or the reverse."""
        inheriting = {
            kind for kind, (_, entry) in _all_entries().items() if _ORCHESTRATED in (entry.get("inherit_from") or [])
        }
        routed = {
            root
            for generator in BaseOrchestratorRoutingGenerator.__subclasses__()
            if generator.orchestrator_field == _ORCHESTRATOR_ATTRIBUTE
            for root in generator.query_roots
        }
        assert inheriting == routed

    def test_the_choices_include_terraform_and_manual(self) -> None:
        """A virtual colocation routes only terraform; manual is the default, i.e. no routing group."""
        attribute = next(a for a in _kind(_ORCHESTRATED)["attributes"] if a["name"] == _ORCHESTRATOR_ATTRIBUTE)
        assert {choice["name"] for choice in attribute["choices"]} >= {"terraform", "manual"}
        assert attribute["default_value"] == "manual"


class TestColocationKit:
    """The metro lost the kit; the zone and the virtual colocation share it through one generic."""

    @pytest.mark.parametrize("name", ["deployment_type", "fabric_templates"])
    def test_metro_no_longer_defines_the_kit(self, name: str) -> None:
        metro = "TopologyColocationMetro"
        assert name not in _attribute_names(metro) | _relationship_names(metro)

    @pytest.mark.parametrize("kind", ["TopologyColocationMetro", "TopologyColocationZone", "TopologyVirtualColocation"])
    def test_no_colocation_node_defines_deployment_type(self, kind: str) -> None:
        """The node kind says what the kit is: a zone is physical, a virtual colocation virtual."""
        assert "deployment_type" not in _attribute_names(kind)

    def test_metro_does_not_inherit_the_fabric_generic(self) -> None:
        assert _FABRIC not in _kind("TopologyColocationMetro")["inherit_from"]

    @pytest.mark.parametrize("kind", ["TopologyColocationZone", "TopologyVirtualColocation"])
    def test_zone_and_virtual_colocation_inherit_the_fabric_generic(self, kind: str) -> None:
        assert _FABRIC in _kind(kind)["inherit_from"]

    @pytest.mark.parametrize("kind", ["TopologyColocationZone", "TopologyVirtualColocation"])
    def test_the_fabric_relationships_are_not_redefined_on_the_node(self, kind: str) -> None:
        """A node must not redefine what its generic provides (there is no allow_override)."""
        assert _relationship_names(kind) & _relationship_names(_FABRIC) == set()

    def test_the_fabric_generic_carries_design_templates_and_the_three_pools(self) -> None:
        assert _relationship_names(_FABRIC) == {
            "design",
            "fabric_templates",
            "loopback_pool",
            "technical_pool",
            "management_pool",
        }

    def test_a_virtual_colocation_is_no_physical_deployment_and_hosts_no_racks(self) -> None:
        """Provider-hosted: nothing of ours to rack or cable."""
        inherited = set(_kind("TopologyVirtualColocation")["inherit_from"])
        assert inherited.isdisjoint({"TopologyPhysicalDeployment", "TopologyRackHosting", "TopologyDeviceHosting"})
        assert {"TopologyDeployment", "TopologyProviderHosting", _FABRIC} <= inherited

    def test_the_metro_hosts_no_device_and_carries_no_cable(self) -> None:
        """The kit is the zones' and the virtual colocations': the metro keeps the Multi-Site site, the pools and the
        naming, nothing is deployed to it."""
        inherited = set(_kind("TopologyColocationMetro")["inherit_from"])
        assert inherited.isdisjoint({"TopologyDeviceHosting", "TopologyPhysicalDeployment", "TopologyRackHosting"})

    def test_the_zone_is_physical_and_hosts_racks(self) -> None:
        assert {"TopologyPhysicalDeployment", "TopologyRackHosting", _FABRIC} <= set(
            _kind("TopologyColocationZone")["inherit_from"]
        )

    def test_designs_point_at_their_own_tier(self) -> None:
        """A zone (or virtual colocation) design is a zone template; the metro's is a metro template."""
        zone_design = next(r for r in _kind(_FABRIC)["relationships"] if r["name"] == "design")
        metro_design = next(r for r in _kind("TopologyColocationMetro")["relationships"] if r["name"] == "design")

        assert zone_design["peer"] == "TopologyTemplateColocationZone"
        assert metro_design["peer"] == "TopologyTemplateColocationMetro"
        assert zone_design["optional"] is True
        assert metro_design["optional"] is True

    def test_metro_supernets_are_prefix_pools_and_the_zones_loopback_and_management_pools_address_pools(self) -> None:
        """A zone slices an address pool out of the metro's prefix pool, as a pod does out of its DC's."""
        metro = {r["name"]: r["peer"] for r in _kind("TopologyColocationMetro")["relationships"]}
        fabric = {r["name"]: r["peer"] for r in _kind(_FABRIC)["relationships"]}

        assert {metro[n] for n in ("loopback_pool", "management_pool", "technical_pool")} == {"CoreIPPrefixPool"}
        assert (fabric["loopback_pool"], fabric["management_pool"]) == ("CoreIPAddressPool", "CoreIPAddressPool")
        assert fabric["technical_pool"] == "CoreIPPrefixPool"

    def test_zone_templates_are_required_on_the_metro_template(self) -> None:
        """A metro design that lists no zone design has nothing to size from (the generator errors)."""
        relationship = next(
            r for r in _kind("TopologyTemplateColocationMetro")["relationships"] if r["name"] == "zone_templates"
        )
        assert relationship["optional"] is False
        assert relationship["peer"] == "TopologyTemplateColocationZone"
        assert relationship["cardinality"] == "many"


class TestRelationshipIdentifiers:
    """Identifiers pair relationship ends: a repeated one silently links two unrelated relationships."""

    def test_identifiers_are_unique_across_topology_colocation_yml(self) -> None:
        identifiers: Counter[str] = Counter()
        for entry in _entries(_COLOCATION_FILE):
            for relationship in entry.get("relationships") or []:
                if relationship.get("identifier"):
                    identifiers[relationship["identifier"]] += 1

        assert identifiers
        assert {identifier: count for identifier, count in identifiers.items() if count > 1} == {}

    def test_every_relationship_of_the_file_states_an_identifier(self) -> None:
        """Several relationships point at the same peer kinds (the pools): each must be told apart."""
        missing = [
            f"{entry['kind']}.{relationship['name']}"
            for entry in _entries(_COLOCATION_FILE)
            for relationship in entry.get("relationships") or []
            if not relationship.get("identifier")
        ]
        assert missing == []

    def test_metro_and_fabric_pool_identifiers_do_not_collide(self) -> None:
        """The metro and the zone both have a loopback_pool to a pool kind: distinct prefixes keep them apart."""
        metro = {r["identifier"] for r in _kind("TopologyColocationMetro")["relationships"]}
        fabric = {r["identifier"] for r in _kind(_FABRIC)["relationships"]}
        assert metro.isdisjoint(fabric)
        assert {i for i in metro if i.startswith("colocation_metro__")} >= {
            "colocation_metro__loopback_pool",
            "colocation_metro__management_pool",
            "colocation_metro__technical_pool",
        }

    def test_no_relationship_names_a_serving_zone(self) -> None:
        """A colocation footprint is served by every zone of its metro that has firewalls: no zone is chosen by data."""
        owners = [
            kind
            for kind, (_, entry) in _all_entries().items()
            for relationship in entry.get("relationships") or []
            if relationship["name"] == "serving_zone"
        ]

        assert owners == []
        assert "serving_zone" not in _relationship_names("TopologyCustomerColocation")

    def test_serving_firewall_context_is_one_on_a_dc_and_many_on_a_colocation_footprint(self) -> None:
        """Declared per kind, never on ManagedTenantScoped: a DC footprint has the one context serving it, a colocation
        footprint one per zone of its metro. Both share the identifier of the context's served_deployments."""
        ha = _document(_SCHEMAS / "extensions" / "capabilities" / "ha.yml")
        extensions = {e["kind"]: e for e in ha["extensions"]["nodes"]}
        served = next(r for r in _kind("ManagedFirewallContext")["relationships"] if r["name"] == "served_deployments")

        def relationship(kind: str, name: str) -> dict[str, Any]:
            return next(r for r in extensions[kind]["relationships"] if r["name"] == name)

        dc = relationship("TopologyCustomerDC", "serving_firewall_context")
        colocation = relationship("TopologyCustomerColocation", "serving_firewall_contexts")

        assert (dc["cardinality"], dc["optional"], dc["peer"]) == ("one", True, "ManagedFirewallContext")
        assert (colocation["cardinality"], colocation["optional"], colocation["peer"]) == (
            "many",
            True,
            "ManagedFirewallContext",
        )
        assert dc["identifier"] == colocation["identifier"] == served["identifier"]
        assert "ManagedTenantScoped" not in extensions
        assert {r["name"] for r in extensions["TopologyCustomerDC"]["relationships"]} >= {"serving_firewall_context"}
        assert "serving_firewall_context" not in {
            r["name"] for r in extensions["TopologyCustomerColocation"]["relationships"]
        }
        assert {"ManagedTenantScoped"} <= set(_kind("TopologyCustomerColocation")["inherit_from"])
        assert {"ManagedTenantScoped"} <= set(_kind("TopologyCustomerDC")["inherit_from"])

    def test_footprint_segments_are_declared_per_kind_with_a_concrete_peer(self) -> None:
        """The segments of a DC, colocation and office footprint are VXLAN only and a cloud footprint's are
        CloudNetworkSegment: one network_segments per kind, none on the TopologyCustomer generic (a narrowed
        peer next to a generic one would make `id` nullable in one kind and not in the other)."""
        segment = _document(_SCHEMAS / "extensions" / "service" / "segment.yml")
        declared: dict[str, list[dict[str, Any]]] = {}
        for extension in segment["extensions"]["nodes"]:
            for relationship in extension.get("relationships") or []:
                if relationship.get("identifier") == "segment__customer_deployments":
                    declared.setdefault(extension["kind"], []).append(relationship)

        assert {kind: [r["peer"] for r in rels] for kind, rels in declared.items()} == {
            "TopologyCustomerDC": ["ManagedVxlanSegment"],
            "TopologyCustomerColocation": ["ManagedVxlanSegment"],
            "TopologyCustomerOffice": ["ManagedVxlanSegment"],
            "TopologyCustomerCloud": ["CloudNetworkSegment"],
        }
        for rels in declared.values():
            (rel,) = rels
            assert (rel["cardinality"], rel["optional"], rel["identifier"], rel["order_weight"]) == (
                "many",
                True,
                "segment__customer_deployments",
                2100,
            )

    def test_every_topology_customer_kind_declares_its_own_segment_edge(self) -> None:
        """No kind inheriting TopologyCustomer is left without an edge now that the generic has none."""
        kinds = {
            kind for kind, (_, entry) in _all_entries().items() if "TopologyCustomer" in entry.get("inherit_from", [])
        }
        segment = _document(_SCHEMAS / "extensions" / "service" / "segment.yml")
        declared = {
            e["kind"]
            for e in segment["extensions"]["nodes"]
            if any(r.get("identifier") == "segment__customer_deployments" for r in e.get("relationships") or [])
        }

        assert kinds == {"TopologyCustomerDC", "TopologyCustomerColocation", "TopologyCustomerCloud"}
        assert kinds <= declared
        assert "TopologyCustomer" not in declared

    def test_the_field_conflict_rule_holds_for_the_footprint_segment_queries(self, root_dir: Path) -> None:
        """No query reaches network_segments through the TopologyCustomer generic any more: it would not parse
        against the schema, and two kinds with different peers conflict in one selection (id String! vs String)."""
        offenders = [
            path.relative_to(root_dir).as_posix()
            for path in sorted((root_dir / "queries").rglob("*.gql"))
            if "on TopologyCustomer {" in path.read_text() and "network_segments" in path.read_text()
        ]

        assert offenders == []


class TestCloudNetworkAppliance:
    """A provider-hosted network function (Equinix Network Edge) is a device with a cloud-like lifecycle."""

    _KIND = "CloudNetworkAppliance"

    def test_it_inherits_the_device_generics_and_not_cloud_resource_nor_the_hosted_instance_ones(self) -> None:
        """DcimDevice gives interfaces/owner/platform, DcimCapabilities the BGP and HA edges; CloudResource would clash on
        name/description/status (no allow_override), AppInstance belongs to a machine of ours."""
        inherited = _kind(self._KIND)["inherit_from"]

        assert {"CoreArtifactTarget", "DcimDevice", "DcimCapabilities"} <= set(inherited)
        assert "CloudResource" not in inherited
        assert "AppInstance" not in inherited
        assert _kind(self._KIND)["generate_template"] is True

    def test_it_declares_cloud_id_itself_and_the_provider_offer(self) -> None:
        assert {"cloud_id", "service_code", "package_code", "software_version", "term_length"} <= (
            _attribute_names(self._KIND)
        )
        assert "cpu_count" not in _attribute_names(self._KIND)  # the vCPU count is DcimDeviceType.cpu_count

    def test_it_has_no_hypervisor_and_no_redundancy_fields_of_its_own(self) -> None:
        """No hosting_device (nothing of ours runs it); redundancy is an HA domain, not a field or a self-relationship."""
        assert "hosting_device" not in _relationship_names(self._KIND)
        assert {"redundancy", "cluster_name"}.isdisjoint(_attribute_names(self._KIND))
        assert {"secondary_device", "primary_device"}.isdisjoint(_relationship_names(self._KIND))

    def test_its_account_is_an_optional_cloud_account(self) -> None:
        account = next(r for r in _kind(self._KIND)["relationships"] if r["name"] == "account")

        assert (account["peer"], account["cardinality"], account["optional"]) == ("CloudAccount", "one", True)

    def test_its_deployment_peers_the_provider_hosting_generic_under_the_identifier_of_the_other_device_kinds(
        self,
    ) -> None:
        """TopologyProviderHosting.appliances lists the appliances through the identifier of their own. The peer is the
        generic TopologyProviderHosting (the virtual colocation or the customer cloud a provider function is rented
        in), so the schema refuses any other deployment and the picker offers nothing else. A query that selects
        `deployment` across the device kinds spreads fragments per kind, and a fragment for a kind that peer cannot
        be (a zone) is a schema error. A physical device (and a physical controller) is deployed to a
        TopologyDeviceHosting; a virtual one runs for a footprint, so its peer stays the TopologyDeployment generic."""
        extensions = _document(_TOPOLOGY / "topology.yml")["extensions"]["nodes"]
        device_kinds = {
            "DcimPhysicalDevice",
            "DcimVirtualDevice",
            "ManagedControllerPhysical",
            "ManagedControllerVirtual",
        }
        deployments = {
            extension["kind"]: next(r for r in extension["relationships"] if r["name"] == "deployment")
            for extension in extensions
            if any(r["name"] == "deployment" for r in extension["relationships"])
        }

        assert device_kinds | {self._KIND} <= set(deployments)
        assert deployments[self._KIND]["peer"] == "TopologyProviderHosting"
        assert {k: deployments[k]["peer"] for k in device_kinds} == {
            "DcimPhysicalDevice": "TopologyDeviceHosting",
            "ManagedControllerPhysical": "TopologyDeviceHosting",
            "DcimVirtualDevice": "TopologyDeployment",
            "ManagedControllerVirtual": "TopologyDeployment",
        }
        assert {deployments[k]["identifier"] for k in device_kinds} == {"topology__devices"}
        assert deployments[self._KIND]["identifier"] == "topology__appliances"

    def test_the_provider_hosting_generic_is_a_valid_peer_and_only_provider_hosted_deployments_inherit_it(self) -> None:
        """A generic used as a relationship peer needs a name and an hfid, or an object file cannot reference it."""
        generic = next(g for g in _document(_TOPOLOGY / "topology.yml")["generics"] if g["name"] == "ProviderHosting")
        assert generic["human_friendly_id"] == ["name__value"]
        assert [a["name"] for a in generic["attributes"]] == ["name"]
        inheritors = [
            node["name"]
            for path in sorted(_TOPOLOGY.glob("*.yml"))
            for node in _document(path).get("nodes") or []
            if "TopologyProviderHosting" in (node.get("inherit_from") or [])
        ]
        assert sorted(inheritors) == ["CustomerCloud", "VirtualColocation"]
        appliances = next(r for r in generic["relationships"] if r["name"] == "appliances")
        assert (appliances["peer"], appliances["cardinality"], appliances["identifier"]) == (
            "CloudNetworkAppliance",
            "many",
            "topology__appliances",
        )

    def test_the_cloud_hosting_generic_links_a_customer_cloud_to_its_instances(self) -> None:
        """A cloud instance is a CloudResource, not a device: the footprint reads it through a generic of its own."""
        generic = next(g for g in _document(_TOPOLOGY / "topology.yml")["generics"] if g["name"] == "CloudHosting")
        assert generic["human_friendly_id"] == ["name__value"]
        instances = next(r for r in generic["relationships"] if r["name"] == "instances")
        assert (instances["peer"], instances["cardinality"], instances["identifier"]) == (
            "CloudInstance",
            "many",
            "topology__cloud_instances",
        )
        extensions = _document(_TOPOLOGY / "topology.yml")["extensions"]["nodes"]
        deployment = next(
            r
            for extension in extensions
            if extension["kind"] == "CloudInstance"
            for r in extension["relationships"]
            if r["name"] == "deployment"
        )
        assert (deployment["peer"], deployment["identifier"]) == ("TopologyCloudHosting", "topology__cloud_instances")
        assert "TopologyCloudHosting" in _kind("TopologyCustomerCloud")["inherit_from"]
        assert {"TopologyCloudHosting", "TopologyDeviceHosting"}.isdisjoint(
            set(_kind("TopologyVirtualColocation")["inherit_from"])
        )

    def test_a_rack_names_its_hosting_deployment_like_a_device_does(self) -> None:
        """LocationRack.deployment (peer TopologyRackHosting: a pod or a colocation zone) pairs with
        TopologyRackHosting.racks under one identifier."""
        rack = next(
            n
            for path in sorted((_TOPOLOGY.parent / "location").glob("*.yml"))
            for n in _document(path).get("nodes") or []
            if n["name"] == "Rack"
        )
        deployment = next(r for r in rack["relationships"] if r["name"] == "deployment")
        racks = next(
            r
            for g in _document(_TOPOLOGY / "topology.yml")["generics"]
            if g["name"] == "RackHosting"
            for r in g["relationships"]
            if r["name"] == "racks"
        )
        assert deployment["peer"] == "TopologyRackHosting"
        assert deployment["identifier"] == racks["identifier"] == "rack__deployment"
        assert "pod" not in {r["name"] for r in rack["relationships"]}

    def test_the_virtual_colocation_orders_under_an_account_and_has_no_order_fields_of_its_own(self) -> None:
        kind = "TopologyVirtualColocation"
        account = next(r for r in _kind(kind)["relationships"] if r["name"] == "account")

        assert (account["peer"], account["cardinality"], account["optional"]) == ("CloudAccount", "one", True)
        assert {"provider_account_number"}.isdisjoint(_attribute_names(kind))
        assert "owner" not in _relationship_names(kind)

    def test_the_virtual_device_lost_the_provider_ordering_fields(self) -> None:
        assert [name for name in _attribute_names("DcimVirtualDevice") if name.startswith("provider_")] == []

    def test_routers_have_no_ha_domain_kind_and_firewall_pairs_use_the_existing_one(self) -> None:
        """A provider-hosted router is an independent appliance; a firewall pair is a plain ManagedFirewallHA, whose
        members attach through `capabilities` and whose `mode` active-passive is the Equinix cluster."""
        ha_kinds = {kind for kind in _all_entries() if kind.endswith("HA")}

        assert "ManagedRouterHA" not in ha_kinds
        assert {"ManagedHA", "ManagedGenericDevice"} <= set(_kind("ManagedFirewallHA")["inherit_from"])
        mode = next(a for a in _kind("ManagedHA")["attributes"] if a["name"] == "mode")
        assert "active-passive" in {choice["name"] for choice in mode["choices"]}
        assert mode["default_value"] == "active-passive"

    def test_the_cloud_native_firewall_ha_is_left_alone(self) -> None:
        """A vendor VM cluster is not a cloud-native firewall service: ManagedCloudFirewallHA keeps its members-less shape."""
        ha = _kind("ManagedCloudFirewallHA")

        assert "ManagedGenericDevice" not in ha["inherit_from"]
        provider = next(a for a in ha["attributes"] if a["name"] == "provider")
        assert {choice["name"] for choice in provider["choices"]} == {"aws_nfw", "azure_firewall", "gcp_ngfw", "other"}


def test_every_schema_kind_fits_the_infrahub_profile_name_limit() -> None:
    """Infrahub derives a ProfileSchema from every node and generic and caps its name at 32 characters.

    A longer kind passes SchemaRoot parsing and only fails at `infrahubctl schema load`, which is how
    TopologyInfrastructureOrchestrated (34) broke the integration setup.
    """
    too_long = sorted(
        entry["kind"] for path in _SCHEMAS.glob("**/*.y*ml") for entry in _entries(path) if len(entry["kind"]) > 32
    )

    assert too_long == []


class TestLoadBalancerVips:
    """Every load balancer lists the VIPs it hosts: the reverse of LoadbalancerVIP.load_balancer is defined once."""

    def test_the_reverse_of_the_vip_load_balancer_is_on_the_generic_only(self) -> None:
        owners = [
            kind
            for kind in _all_entries()
            if any(
                r["name"] == "vips" and r.get("peer") == "LoadbalancerVIP"
                for r in _kind(kind).get("relationships") or []
            )
        ]
        assert owners == ["ManagedLoadBalancer"]

    def test_the_implementations_inherit_it(self) -> None:
        for kind in ("ManagedLoadbalancerHA", "ManagedLoadbalancerContext", "CloudLoadBalancer"):
            assert "ManagedLoadBalancer" in _kind(kind)["inherit_from"], kind

    def test_it_pairs_with_the_vip_under_one_identifier(self) -> None:
        vips = next(r for r in _kind("ManagedLoadBalancer")["relationships"] if r["name"] == "vips")
        load_balancer = next(r for r in _kind("LoadbalancerVIP")["relationships"] if r["name"] == "load_balancer")

        assert vips["identifier"] == load_balancer["identifier"] == "load_balancer__vip_services"
        assert load_balancer["peer"] == "ManagedLoadBalancer"
