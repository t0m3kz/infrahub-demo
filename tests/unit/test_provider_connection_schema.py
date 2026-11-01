"""TopologyProviderConnection and the schema edits of the same stage (schema stage: no reader exists yet).

The connection groups what connects a facility of ours to a provider facility: its carriers (shared underlay, kept when
the connection goes) and its sessions (the routing it carries, deleted with it). It is not a ManagedGeneric, names no
provider (derived from the account or the far facility) and holds no redundancy or reach.
"""

from __future__ import annotations

from collections import Counter
from functools import cache
from pathlib import Path
from typing import Any

import pytest
import yaml
from infrahub_sdk.schema import SchemaRoot

from tests.integration.demo_data import ROOT

_SCHEMAS = ROOT / "schemas"
_TOPOLOGY = _SCHEMAS / "extensions" / "topology"
_CONNECTION_FILE = _TOPOLOGY / "topology_provider_connection.yml"
_CONNECTIVITY_FILE = _TOPOLOGY / "topology_connectivity.yml"


@cache
def _document(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text())


def _node(path: Path, name: str) -> dict[str, Any]:
    return next(item for item in _document(path)["nodes"] if item["name"] == name)


def _field(section: list[dict[str, Any]], name: str) -> dict[str, Any]:
    return next(item for item in section if item["name"] == name)


@cache
def _connection() -> dict[str, Any]:
    return _node(_CONNECTION_FILE, "ProviderConnection")


def _relationships() -> list[dict[str, Any]]:
    return _connection()["relationships"]


def test_the_connection_file_parses_as_a_schema_root() -> None:
    """The structure validates without a server."""
    SchemaRoot(**_document(_CONNECTION_FILE))


def test_the_connection_is_not_a_managed_generic_and_has_its_own_name() -> None:
    """Own name (unique, HFID name), no inheritance: ManagedGeneric would collide with the circuits it groups."""
    connection = _connection()

    assert connection["namespace"] == "Topology"
    assert not connection.get("inherit_from")
    name = _field(connection["attributes"], "name")
    assert name["unique"] is True
    assert connection["human_friendly_id"] == ["name__value"]


def test_the_connection_has_no_redundancy_reach_or_provider_field() -> None:
    """Redundancy is the grouping of its carriers and sessions, the provider is derived."""
    names = {item["name"] for item in _connection()["attributes"] + _relationships()}

    assert names.isdisjoint({"redundancy", "reach", "provider", "provider_type", "connection_profile"})


def test_the_path_type_and_service_are_required_dropdowns() -> None:
    """path_type is dedicated or internet_vpn; service is what the provider side attaches to."""
    attributes = _connection()["attributes"]

    path_type = _field(attributes, "path_type")
    assert path_type["kind"] == "Dropdown" and path_type["optional"] is False
    assert [c["name"] for c in path_type["choices"]] == ["dedicated", "internet_vpn"]
    service = _field(attributes, "service")
    assert service["kind"] == "Dropdown" and service["optional"] is False
    assert [c["name"] for c in service["choices"]] == ["private", "transit", "public"]


def test_the_vlan_is_a_bounded_optional_number() -> None:
    """vlan_id 1..4094 and bandwidth are optional numbers."""
    attributes = _connection()["attributes"]

    vlan = _field(attributes, "vlan_id")
    assert (vlan["kind"], vlan["optional"]) == ("Number", True)
    assert vlan["parameters"] == {"min_value": 1, "max_value": 4094}
    assert _field(attributes, "bandwidth")["kind"] == "Number"


@pytest.mark.parametrize(
    ("name", "peer", "cardinality", "optional"),
    [
        ("near", "TopologyFacility", "one", False),
        ("far", "TopologyFacility", "one", False),
        ("ip_namespace", "IpamNamespace", "one", False),
        ("owner", "OrganizationCustomer", "one", True),
        ("account", "CloudAccount", "one", True),
        ("provider_as", "RoutingAutonomousSystem", "one", True),
        ("password", "RoutingPassword", "one", True),
        ("attached_network", "CloudVirtualNetwork", "one", True),
        ("attached_gateway", "CloudTransitGateway", "one", True),
        ("transit_attachment", "CloudTransitGatewayAttachment", "one", True),
        ("registered_prefixes", "IpamPrefix", "many", True),
        ("link_prefixes", "IpamPrefix", "many", True),
        ("carrier_circuits", "TopologyCarrierCircuit", "many", True),
        ("session_circuits", "TopologySessionCircuit", "many", True),
    ],
)
def test_the_connection_relationships(name: str, peer: str, cardinality: str, optional: bool) -> None:
    """Each relationship has its peer, cardinality and requiredness, and its own identifier."""
    relationship = _field(_relationships(), name)

    assert (relationship["peer"], relationship["cardinality"], relationship["optional"]) == (
        peer,
        cardinality,
        optional,
    )
    assert relationship["identifier"].startswith(("provider_connection__", "transit_gateway_attachment__"))


def test_relationships_to_the_same_peer_have_distinct_identifiers() -> None:
    """near/far (facility) and the two prefix lists would be one relationship if they shared an identifier."""
    identifiers = [relationship["identifier"] for relationship in _relationships()]

    assert len(identifiers) == len(set(identifiers))


def test_the_order_weights_of_the_connection_are_unique_and_explicit() -> None:
    """The kind inherits no generic, so its own fields are all there is; none is left to the server's numbering."""
    items = _connection()["attributes"] + _relationships()

    assert all("order_weight" in item for item in items)
    weights = [item["order_weight"] for item in items]
    assert [w for w, n in Counter(weights).items() if n > 1] == []
    assert [_field(_connection()["attributes"], n)["order_weight"] for n in ("name", "description", "status")] == [
        100,
        150,
        400,
    ]


def test_the_carriers_survive_the_connection_and_the_sessions_go_with_it() -> None:
    """The carrier is shared underlay (no-action); a session circuit is the routing the connection carries (cascade)."""
    carriers = _field(_relationships(), "carrier_circuits")
    sessions = _field(_relationships(), "session_circuits")

    assert (carriers["on_delete"], carriers["kind"]) == ("no-action", "Generic")
    assert (sessions["on_delete"], sessions["kind"]) == ("cascade", "Generic")
    assert "max_count" not in sessions and "max_count" not in carriers


def test_the_reverse_edges_pair_on_the_identifier() -> None:
    """provider_connections on the carrier and connection on the session circuit share the connection's identifiers."""
    carrier = _field(_node(_CONNECTIVITY_FILE, "CarrierCircuit")["relationships"], "provider_connections")
    session = _field(_node(_CONNECTIVITY_FILE, "SessionCircuit")["relationships"], "connection")

    assert (carrier["peer"], carrier["cardinality"], carrier["optional"]) == (
        "TopologyProviderConnection",
        "many",
        True,
    )
    assert carrier["identifier"] == _field(_relationships(), "carrier_circuits")["identifier"]
    assert carrier["on_delete"] == "no-action"
    assert (session["peer"], session["cardinality"], session["optional"]) == ("TopologyProviderConnection", "one", True)
    assert session["identifier"] == _field(_relationships(), "session_circuits")["identifier"]


def test_the_session_circuit_has_a_vpn_type_and_a_provider_link_prefix() -> None:
    """vpn joins the types; link_prefix is the provider-handed range, one, optional, on its own identifier."""
    session = _node(_CONNECTIVITY_FILE, "SessionCircuit")

    circuit_type = _field(session["attributes"], "circuit_type")
    vpn = next(choice for choice in circuit_type["choices"] if choice["name"] == "vpn")
    assert "tunnel interface" in vpn["description"]
    link_prefix = _field(session["relationships"], "link_prefix")
    assert (link_prefix["peer"], link_prefix["cardinality"], link_prefix["optional"]) == ("IpamPrefix", "one", True)
    assert link_prefix["identifier"] == "session_circuit__link_prefix"


def test_the_session_circuit_order_weights_are_unique_with_the_new_edges() -> None:
    """The session kind and the circuit generic do not collide after the two new relationships."""
    session = _node(_CONNECTIVITY_FILE, "SessionCircuit")
    generic = next(g for g in _document(_CONNECTIVITY_FILE)["generics"] if g["name"] == "Circuit")
    items = session["attributes"] + session["relationships"]
    items += (generic.get("attributes") or []) + (generic.get("relationships") or [])
    weights = [item["order_weight"] for item in items]

    assert [w for w, n in Counter(weights).items() if n > 1] == []


def test_the_carrier_weights_are_unique_with_the_reverse_edge() -> None:
    """provider_connections sits beside the ends and the generic's relationships without a tie."""
    carrier = _node(_CONNECTIVITY_FILE, "CarrierCircuit")
    generic = next(g for g in _document(_CONNECTIVITY_FILE)["generics"] if g["name"] == "Circuit")
    items = carrier["attributes"] + carrier["relationships"]
    items += (generic.get("attributes") or []) + (generic.get("relationships") or [])
    weights = [item["order_weight"] for item in items]

    assert [w for w, n in Counter(weights).items() if n > 1] == []


def test_the_saas_provider_is_a_required_relationship_to_the_organization_provider() -> None:
    """The dropdown (cisco, zscaler, other) is a relationship like CloudAccount.provider."""
    saas = _node(_TOPOLOGY / "topology_saas.yml", "Saas")

    assert "provider" not in {a["name"] for a in saas["attributes"]}
    provider = _field(saas["relationships"], "provider")
    assert (provider["peer"], provider["cardinality"], provider["optional"]) == ("OrganizationProvider", "one", False)


def test_the_provider_has_an_optional_connection_profile() -> None:
    """The vendor flavour is the provider's: aws, azure, gcp, oci, equinix, megaport, zscaler, generic."""
    provider = _node(_SCHEMAS / "base" / "organization.yml", "Provider")
    profile = _field(provider["attributes"], "connection_profile")

    assert (profile["kind"], profile["optional"]) == ("Dropdown", True)
    assert [c["name"] for c in profile["choices"]] == [
        "aws",
        "azure",
        "gcp",
        "oci",
        "equinix",
        "megaport",
        "zscaler",
        "generic",
    ]


def test_the_provider_profiles_in_the_bootstrap_data_are_choices() -> None:
    """A connection_profile written in data/bootstrap/02_providers.yml is one the schema accepts."""
    provider = _node(_SCHEMAS / "base" / "organization.yml", "Provider")
    choices = {c["name"] for c in _field(provider["attributes"], "connection_profile")["choices"]}
    data = list(yaml.safe_load_all((ROOT / "data" / "bootstrap" / "02_providers.yml").read_text()))
    used = {p["connection_profile"] for doc in data for p in doc["spec"]["data"] if "connection_profile" in p}

    assert used and used <= choices


def test_the_removed_kinds_are_gone_from_every_schema() -> None:
    """ManagedCloudFirewallHA and ProxyURLCategory were dead; the proxy rule matches an explicit destination."""
    text = "\n".join(path.read_text() for path in _SCHEMAS.rglob("*.yml"))

    assert "name: CloudFirewallHA" not in text
    assert "name: URLCategory" not in text
    rule = _node(_SCHEMAS / "extensions" / "security" / "proxy_policy.yml", "PolicyRule")
    destination_type = _field(rule["attributes"], "destination_type")
    assert [c["name"] for c in destination_type["choices"]] == ["fqdn", "cidr", "regex"]
    assert destination_type["default_value"] == "fqdn"
    assert "categories" not in {r["name"] for r in rule["relationships"]}


def test_the_renamed_hosting_generics_are_the_only_names() -> None:
    """TopologyApplianceHosting and TopologyBilledAccount replace the provider-named generics."""
    topology = _document(_TOPOLOGY / "topology.yml")
    names = {generic["name"] for generic in topology["generics"]}

    assert {"ApplianceHosting", "BilledAccount"} <= names
    assert {"ProviderHosting", "ProviderAccount"}.isdisjoint(names)


def test_the_transit_attachment_pairs_with_the_hybrid_endpoint_on_one_identifier() -> None:
    """Both sides of the connection to attachment edge share an identifier and the endpoint peers the connection."""
    attachment = _node(_TOPOLOGY / "topology_cloud_network.yml", "TransitGatewayAttachment")
    endpoint = _field(attachment["relationships"], "hybrid_endpoint")
    reverse = _field(_relationships(), "transit_attachment")

    assert endpoint["peer"] == "TopologyProviderConnection"
    assert endpoint["identifier"] == reverse["identifier"]


def test_the_cloud_direct_connect_and_virtual_interface_kinds_are_gone() -> None:
    """The cloud config is generated from the connection: no DX, VIF or hybrid-attachment objects remain."""
    text = "\n".join(path.read_text() for path in _SCHEMAS.rglob("*.yml"))

    for removed in ("name: DirectConnect", "name: HybridAttachment", "kind: CloudHybridAttachment", "cloud_endpoint:"):
        assert removed not in text, removed
    assert "name: VirtualInterface\n    namespace: Cloud" not in text


@pytest.mark.parametrize(
    ("path", "kind"),
    [
        (_SCHEMAS / "extensions" / "location" / "location.yml", "Facility"),
        (_SCHEMAS / "extensions" / "location" / "location.yml", "Suite"),
        (_SCHEMAS / "extensions" / "location" / "location.yml", "Rack"),
        (_SCHEMAS / "extensions" / "capabilities" / "ha.yml", "CloudProxy"),
    ],
)
def test_the_unread_owner_relationships_are_gone(path: Path, kind: str) -> None:
    """Nobody reads these owners; a facility, suite, rack or cloud proxy has no owner relationship."""
    node = _node(path, kind)

    assert "owner" not in [r["name"] for r in node.get("relationships") or []]


def test_a_facility_lists_the_connections_that_start_or_land_in_it() -> None:
    """The two ends of a connection are two edges; the Facility generic lists each back on the same identifier, so a
    cloud region, a SaaS region and a colocation zone show their connections (all of them inherit the generic)."""
    facility = next(g for g in _document(_CONNECTIVITY_FILE)["generics"] if g["name"] == "Facility")
    connection = {r["name"]: r for r in _relationships()}

    for end, back in (("near", "near_connections"), ("far", "far_connections")):
        listed = _field(facility["relationships"], back)

        assert (listed["peer"], listed["cardinality"], listed["kind"]) == (
            "TopologyProviderConnection",
            "many",
            "Generic",
        )
        assert listed["identifier"] == connection[end]["identifier"], back


def test_a_customer_lists_the_cloud_accounts_it_owns_on_the_owner_edge() -> None:
    """The customer's list is the other end of CloudAccount.owner (the default identifier of that edge), so no second
    owner field exists and the list cannot disagree with the account."""
    cloud = _TOPOLOGY / "topology_cloud.yml"
    account = _field(_node(cloud, "Account")["relationships"], "owner")
    customer = next(e for e in _document(cloud)["extensions"]["nodes"] if e["kind"] == "OrganizationCustomer")
    listed = _field(customer["relationships"], "cloud_accounts")

    assert (listed["peer"], listed["cardinality"]) == ("CloudAccount", "many")
    assert account["peer"] == "OrganizationEntity" and "identifier" not in account
    assert listed["identifier"] == "cloudaccount__organizationentity"
