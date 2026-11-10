"""Offline contract for VRF-owned routing, independent of IPAM identity."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
import yaml
from infrahub_sdk.schema import AttributeSchema, GenericSchema, NodeSchema, RelationshipSchema, SchemaRoot

from utils.limits import MAX_ENCODABLE_VNI, SITE_VNI_END

_SERVER_GENERICS = {"CoreArtifactTarget", "BuiltinIPAddress", "BuiltinIPPrefix"}


@pytest.fixture(scope="module")
def routing_schema_roots(root_dir: Path) -> list[SchemaRoot]:
    """Parse every schema with the SDK's offline structural validator."""
    paths = sorted((root_dir / "schemas").rglob("*.yml"))
    assert len(paths) > 20
    return [SchemaRoot(**yaml.safe_load(path.read_text())) for path in paths]


def _definition(roots: list[SchemaRoot], kind: str) -> NodeSchema | GenericSchema:
    """Find the original declaration rather than a cross-file extension."""
    return next(node for root in roots for node in [*root.generics, *root.nodes] if node.namespace + node.name == kind)


def _relationships(roots: list[SchemaRoot], kind: str) -> dict[str, RelationshipSchema]:
    """Merge inherited relationships and extensions as consumers see them."""
    node = _definition(roots, kind)
    fields: dict[str, RelationshipSchema] = {}
    if isinstance(node, NodeSchema):
        for generic in reversed(node.inherit_from):
            if generic not in _SERVER_GENERICS:
                fields.update(_relationships(roots, generic))
    fields.update((field.name, field) for field in node.relationships)
    for root in roots:
        if root.extensions:
            for extension in root.extensions.nodes:
                if extension.kind == kind:
                    for field in extension.relationships:
                        fields[field.name] = RelationshipSchema.model_validate(field.model_dump(exclude_unset=True))
    return fields


def _attributes(roots: list[SchemaRoot], kind: str) -> dict[str, AttributeSchema]:
    """Merge inherited attributes and extensions without losing their types."""
    node = _definition(roots, kind)
    fields: dict[str, AttributeSchema] = {}
    if isinstance(node, NodeSchema):
        for generic in reversed(node.inherit_from):
            if generic not in _SERVER_GENERICS:
                fields.update(_attributes(roots, generic))
    fields.update((field.name, field) for field in node.attributes)
    for root in roots:
        if root.extensions:
            for extension in root.extensions.nodes:
                if extension.kind == kind:
                    for field in extension.attributes:
                        fields[field.name] = AttributeSchema.model_validate(field.model_dump(exclude_unset=True))
    return fields


def test_security_zone_is_the_unique_global_routing_authority(routing_schema_roots: list[SchemaRoot]) -> None:
    """Name is the sole CLI identity; trust and routing purpose are independent."""
    zone = _definition(routing_schema_roots, "SecurityZone")
    fields = _attributes(routing_schema_roots, "SecurityZone")
    assert zone.human_friendly_id == ["name__value"]
    assert "vrf_name" not in fields
    for name in ("name", "vrf_type", "l3_vni"):
        assert fields[name].optional is False
        assert fields[name].unique is True
        assert fields[name].computed_attribute is None
    assert fields["vrf_type"].kind == "Dropdown"
    assert {choice["name"] for choice in fields["vrf_type"].choices or []} == {
        "prod",
        "non_prod",
        "internet",
        "management",
    }
    assert fields["trust_level"].kind == "Number"
    assert fields["trust_level"].computed_attribute is None


def test_fixed_l3_vni_band_is_reserved_and_encodable(routing_schema_roots: list[SchemaRoot]) -> None:
    """The four existing L3 VNIs stay outside site L2 and derived transit bands."""
    from utils.exchange_transit import TRANSIT_VNI_MAX, TRANSIT_VNI_MIN

    vni = _attributes(routing_schema_roots, "SecurityZone")["l3_vni"]
    assert vni.kind == "Number"
    assert vni.parameters == {"min_value": 60000, "max_value": 60003}
    assert SITE_VNI_END < 60000 <= 60003 <= MAX_ENCODABLE_VNI
    assert TRANSIT_VNI_MAX < 60000 or TRANSIT_VNI_MIN > 60003


@pytest.mark.parametrize(
    ("kind", "field", "identifier", "reverse", "optional"),
    [
        ("ManagedVRF", "security_zone", "vrf__security_zone", "vrfs", False),
        ("TopologySessionCircuit", "vrf", "session_circuit__vrf", "session_circuits", True),
        ("ManagedNetworkSegment", "security_zone", "network_segment__security_zone", "network_segments", False),
    ],
)
def test_routing_authority_edges_have_matching_typed_inverses(
    routing_schema_roots: list[SchemaRoot],
    kind: str,
    field: str,
    identifier: str,
    reverse: str,
    optional: bool,
) -> None:
    """Local routing owners link to a VRF; only VRFs and policy segments link to zones."""
    peer = "SecurityZone" if field == "security_zone" else "ManagedVRF"
    forward = _relationships(routing_schema_roots, kind)[field]
    backward = _relationships(routing_schema_roots, peer)[reverse]
    assert (forward.peer, forward.cardinality, forward.kind, forward.optional) == (
        peer,
        "one",
        "Attribute",
        optional,
    )
    assert (backward.peer, backward.cardinality, backward.kind) == (kind, "many", "Generic")
    assert forward.identifier == backward.identifier == identifier
    assert forward.order_weight is not None and backward.order_weight is not None
    if kind != "ManagedNetworkSegment":
        assert forward.on_delete == backward.on_delete == "no-action"


def test_vrf_instances_keep_parent_lifecycle_and_shared_membership(routing_schema_roots: list[SchemaRoot]) -> None:
    """Changing routing ownership must not change instance deletion or device membership."""
    vrf = _definition(routing_schema_roots, "ManagedVRF")
    assert isinstance(vrf, NodeSchema)
    # Its interfaces are its interface capabilities (ManagedGenericInterfaces), its members its shared devices.
    assert vrf.inherit_from == ["ManagedGeneric", "ManagedGenericSharedDevice", "ManagedGenericInterfaces"]
    assert vrf.human_friendly_id == ["deployment__name__value", "security_zone__name__value"]
    assert vrf.uniqueness_constraints == [["deployment", "security_zone"]]
    assert "l3_vni" not in _attributes(routing_schema_roots, "ManagedVRF")
    assert "namespace" not in _relationships(routing_schema_roots, "ManagedVRF")
    parent = _relationships(routing_schema_roots, "ManagedVRF")["deployment"]
    children = _relationships(routing_schema_roots, "TopologySegmentHosting")["vrfs"]
    assert (parent.peer, parent.kind, parent.optional) == ("TopologySegmentHosting", "Parent", False)
    assert parent.identifier == children.identifier == "vrf__deployment"
    assert (children.peer, children.kind, children.on_delete) == ("ManagedVRF", "Component", "cascade")


def test_exchange_gateways_and_session_circuits_no_longer_use_ipam_routing_metadata(
    routing_schema_roots: list[SchemaRoot],
) -> None:
    """Neither a circuit nor an exchange has a namespace-based routing authority."""
    assert "ip_namespace" not in _relationships(routing_schema_roots, "TopologySessionCircuit")
    exchange = _definition(routing_schema_roots, "TopologyRoutedExchange")
    assert exchange.uniqueness_constraints == [["vrf_a", "vrf_z", "gateway"]]
    assert (
        not {"namespace_a", "namespace_z", "security_zone_a", "security_zone_z"}
        & _relationships(routing_schema_roots, "TopologyRoutedExchange").keys()
    )
    # Its legs are derived (the context's legs in its two VRFs): it is not on the interface edge.
    assert isinstance(exchange, NodeSchema)
    assert "ManagedGenericInterfaces" not in exchange.inherit_from
    for root in routing_schema_roots:
        if root.extensions:
            for extension in root.extensions.nodes:
                if extension.kind in {"BuiltinIPNamespace", "IpamNamespace"}:
                    assert not {"l3_vni", "namespace_type"} & {field.name for field in extension.attributes}
                    assert "vrfs" not in {field.name for field in extension.relationships}


@pytest.mark.parametrize("kind", ["IpamPrefix", "IpamIPAddress"])
def test_ipam_identity_stays_namespace_scoped(routing_schema_roots: list[SchemaRoot], kind: str) -> None:
    """Routing-zone ownership never becomes an IPAM HFID or uniqueness component."""
    node = _definition(routing_schema_roots, kind)
    address = "prefix__value" if kind == "IpamPrefix" else "address__value"
    assert node.human_friendly_id == [address, "ip_namespace__name__value"]
    assert node.uniqueness_constraints == [[address, "ip_namespace"]]
    assert not {"security_zone", "vrf"} & _relationships(routing_schema_roots, kind).keys()


@pytest.mark.parametrize("kind", ["TopologyCustomerOffice", "CloudVirtualNetwork", "CloudGatewayAttachment"])
def test_standalone_registries_do_not_store_routing_authority(
    routing_schema_roots: list[SchemaRoot], kind: str
) -> None:
    """LANs, CIDRs and allowed prefixes are inventory, not duplicate routing owners."""
    assert "security_zone" not in _relationships(routing_schema_roots, kind)


def test_policy_zones_remain_explicit_selector_contexts(routing_schema_roots: list[SchemaRoot]) -> None:
    """Policy selectors retain zone contexts without adding routing fields to IPAM."""
    rule = _relationships(routing_schema_roots, "SecurityPolicyRule")
    for direction in ("source", "destination"):
        context = rule[f"{direction}_zone"]
        assert (context.peer, context.kind, context.cardinality, context.optional) == (
            "SecurityZone",
            "Attribute",
            "one",
            True,
        )
        assert context.identifier == f"policyrule_{direction}_zone"


def test_direct_zone_routing_edges_and_inverse_registries_are_removed(routing_schema_roots: list[SchemaRoot]) -> None:
    """No interface, circuit or exchange bypasses the deployment-scoped VRF owner."""
    for kind in ("DcimInterface", "TopologySessionCircuit", "TopologyExchangeGateway"):
        assert (
            not {"security_zone", "security_zone_a", "security_zone_z"}
            & _relationships(routing_schema_roots, kind).keys()
        )
    assert set(_relationships(routing_schema_roots, "SecurityZone")) == {"network_segments", "vrfs"}


def test_routing_zone_fields_have_unique_explicit_display_weights(routing_schema_roots: list[SchemaRoot]) -> None:
    """The inherited interface extensions and zone fields do not acquire weight ties."""
    for kind in ("SecurityZone", "ManagedVRF", "DcimPhysicalInterface", "DcimLAGInterface", "DcimVirtualInterface"):
        fields = [
            *_attributes(routing_schema_roots, kind).values(),
            *_relationships(routing_schema_roots, kind).values(),
        ]
        weighted = {field.name: field.order_weight for field in fields if field.order_weight is not None}
        assert len(weighted.values()) == len(set(weighted.values())), (kind, weighted)


def test_hand_curated_protocols_match_routing_authority_types(root_dir: Path) -> None:
    """Protocols expose concrete SDK attributes/managers, not namespace remnants or Any."""
    module = ast.parse((root_dir / "generators" / "protocols.py").read_text())
    fields = {
        node.name: {
            statement.target.id: ast.unparse(statement.annotation)
            for statement in node.body
            if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name)
        }
        for node in module.body
        if isinstance(node, ast.ClassDef)
    }
    for kind in ("ManagedVRF", "ManagedNetworkSegment"):
        assert fields[kind]["security_zone"] == "RelationshipAttribute[SecurityZone]"
    assert fields["TopologySessionCircuit"]["vrf"] == "RelationshipAttribute[ManagedVRF]"
    assert "security_zone" not in fields["TopologySessionCircuit"]
    # An interface's VRF is the ManagedVRF among its capabilities: no relationship of the interface.
    assert not {"vrf", "security_zone"} & fields["DcimInterface"].keys()
    for kind in ("IpamPrefix", "IpamIPAddress"):
        assert not {"security_zone", "vrf"} & fields[kind].keys()
    assert "namespace" not in fields["ManagedVRF"]
    assert "l3_vni" not in fields["ManagedVRF"]
    assert "ip_namespace" not in fields["TopologySessionCircuit"]
    assert fields["TopologyExchangeGateway"] == {
        "vrf_a": "RelationshipAttribute[ManagedVRF]",
        "vrf_z": "RelationshipAttribute[ManagedVRF]",
    }
    assert fields["SecurityZone"]["l3_vni"] == "Integer"
    assert fields["SecurityZone"]["vrf_type"] == "Dropdown"
    assert fields["ManagedVRF"]["session_circuits"] == "RelationshipManager[TopologySessionCircuit]"
    # The exchanges name the VRF one-sidedly; their legs are derived (the context legs in the two VRFs).
    assert set(fields["ManagedVRF"]) == {"deployment", "security_zone", "session_circuits"}
    assert fields["SecurityZone"]["vrfs"] == "RelationshipManager[ManagedVRF]"
    assert fields["SecurityZone"]["network_segments"] == "RelationshipManager[ManagedNetworkSegment]"
    assert (
        not {
            "session_circuits",
            "routed_interfaces",
            "registered_prefixes",
            "virtual_networks",
            "gateway_attachments",
            "offices",
        }
        & fields["SecurityZone"].keys()
    )
    assert not any("TopologyExchangeGateway" in annotation for annotation in fields["SecurityZone"].values())
