"""The edge config query delivers the owner and process_role fields the external BGP filter reads.

transforms/helpers/bgp_external.py filters the external sessions of the NX-OS colocation edge per owner. It reads the cleaned
response (utils/data_cleaning.clean_data: ``__typename`` -> ``typename``, ``{node}``/``{edges}`` flattened,
``{value: x}`` -> x), so these tests pin, offline, that

* the changed .gql files parse,
* every new field exists on the schema kind it is selected on (schemas/ read through tests/integration/demo_data.py),
* a raw response shaped like the selection cleans into the shape the transform expects.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from graphql import FieldNode, FragmentDefinitionNode, OperationDefinitionNode, parse

from tests.integration import demo_data
from utils.data_cleaning import clean_data

_QUERIES = Path(__file__).parents[2] / "queries"
_EDGE = _QUERIES / "config" / "edge.gql"
_FRAGMENT = _QUERIES / "fragments" / "device_capabilities.gql"

_OWNER_FIELDS = ("__typename", "org_id", "display_label")


def _children(field: FieldNode) -> list[FieldNode]:
    """The directly selected fields of ``field`` (the wrappers ``edges``/``node`` are fields too)."""
    if field.selection_set is None:
        return []
    return [s for s in field.selection_set.selections if isinstance(s, FieldNode)]


def _descendants(field: FieldNode) -> Iterator[FieldNode]:
    """Every field selected anywhere below ``field`` (inline fragments are not entered)."""
    for child in _children(field):
        yield child
        yield from _descendants(child)


def _root(root: str) -> FieldNode:
    """The top-level ``root`` field of the edge_config operation."""
    document = parse(_EDGE.read_text())
    operation = next(d for d in document.definitions if isinstance(d, OperationDefinitionNode))
    return next(s for s in operation.selection_set.selections if isinstance(s, FieldNode) and s.name.value == root)


def _node(field: FieldNode) -> FieldNode:
    """The ``edges { node { ... } }`` or ``{ node { ... } }`` node selection of a relationship field."""
    inner = field
    for name in ("edges", "node"):
        found = next((c for c in _children(inner) if c.name.value == name), None)
        if found is not None:
            inner = found
    return inner


def _child(field: FieldNode, name: str) -> FieldNode:
    """The direct child ``name`` of ``field``."""
    return next(c for c in _children(field) if c.name.value == name)


def _owner_selection_names(owner: FieldNode) -> list[str]:
    """The fields selected under ``owner { node { ... } }``."""
    return [c.name.value for c in _children(_node(owner))]


def _select_owner(selection: FieldNode) -> FieldNode:
    """``owner`` below a node selection, after asserting it is the expected ``{ node { ... } }``."""
    owner = _child(selection, "owner")
    assert _owner_selection_names(owner) == ["__typename", "org_id", "display_label"]
    return owner


def _raw_owner(org_id: str = "C003") -> dict[str, Any]:
    """A raw ``owner`` relationship as the server returns the owner selection."""
    return {"node": {"__typename": "OrganizationCustomer", "org_id": {"value": org_id}, "display_label": "Acme (C003)"}}


def _cleaned_owner(org_id: str = "C003") -> dict[str, str]:
    """The cleaned owner the transform reads."""
    return {"typename": "OrganizationCustomer", "org_id": org_id, "display_label": "Acme (C003)"}


def _peer(kind: str, relationship: str) -> str:
    """The peer kind of ``relationship`` on ``kind`` in the schema."""
    definition = demo_data.relationship(kind, relationship)
    assert definition is not None, f"{kind}.{relationship} is not in the schema"
    return definition["peer"]


class TestChangedFilesParse:
    """Every changed .gql file is valid GraphQL syntax."""

    @pytest.mark.parametrize("path", [_EDGE, _FRAGMENT], ids=lambda p: p.name)
    def test_the_gql_file_parses(self, path: Path) -> None:
        """graphql-core parses the file (fragment spreads are validated by invoke validate-queries)."""
        assert parse(path.read_text()).definitions

    def test_the_edge_operation_keeps_its_four_extra_roots(self) -> None:
        """The roots the owner filter reads are all still selected."""
        for root in ("IpamPrefix", "ManagedNetworkSegment", "CloudVirtualInterface", "TopologyCustomerOffice"):
            assert _root(root) is not None


class TestSegmentRoot:
    """ManagedNetworkSegment delivers its owner."""

    def test_the_root_selects_the_owner(self) -> None:
        """owner { node { __typename org_id display_label } } sits next to the gateway."""
        node = _node(_root("ManagedNetworkSegment"))
        names = [c.name.value for c in _children(node)]

        assert {"owner", "gateway"} <= set(names)
        assert _owner_selection_names(_select_owner(node)) == list(_OWNER_FIELDS)

    def test_the_fields_exist_on_the_schema(self) -> None:
        """owner is a relationship to OrganizationCustomer, which carries org_id."""
        assert _peer("ManagedNetworkSegment", "owner") == "OrganizationCustomer"
        assert "org_id" in demo_data.attribute_names("OrganizationCustomer")

    def test_a_raw_response_cleans_to_the_transform_shape(self) -> None:
        """segment["owner"] is the typename/org_id/display_label dict."""
        raw = {"ManagedNetworkSegment": {"edges": [{"node": {"owner": _raw_owner(), "gateway": {"node": None}}}]}}

        segment = clean_data(raw)["ManagedNetworkSegment"][0]

        assert segment["owner"] == _cleaned_owner()


class TestCloudVirtualInterfaceRoot:
    """Both virtual_network selections of the VIF root carry the account owner."""

    def _virtual_networks(self) -> list[FieldNode]:
        """Every virtual_network selection below the CloudVirtualInterface root."""
        return [d for d in _descendants(_root("CloudVirtualInterface")) if d.name.value == "virtual_network"]

    def test_the_vif_and_every_attachment_select_the_account_owner(self) -> None:
        """Two virtual_network selections (the VIF's own, the transit gateway attachments'), both with account.owner."""
        networks = self._virtual_networks()

        assert len(networks) == 2
        for network in networks:
            account = _child(_node(network), "account")
            owner = _select_owner(_node(account))
            assert _owner_selection_names(owner) == list(_OWNER_FIELDS)
            assert "cidr_blocks" in [c.name.value for c in _children(_node(network))]

    def test_the_fields_exist_on_the_schema(self) -> None:
        """CloudVirtualNetwork.account is a CloudAccount, whose owner is an OrganizationEntity with org_id."""
        assert _peer("CloudVirtualNetwork", "account") == "CloudAccount"
        assert _peer("CloudAccount", "owner") == "OrganizationEntity"
        assert "org_id" in demo_data.attribute_names("OrganizationEntity")

    def test_a_raw_response_cleans_to_the_transform_shape(self) -> None:
        """vif and attachments[j] both expose virtual_network.account.owner as the cleaned dict."""
        network = {"node": {"cidr_blocks": {"edges": []}, "account": {"node": {"owner": _raw_owner()}}}}
        raw = {
            "CloudVirtualInterface": {
                "edges": [
                    {
                        "node": {
                            "name": {"value": "vif-1"},
                            "virtual_network": network,
                            "transit_gateway_attachment": {
                                "node": {
                                    "transit_gateway": {
                                        "node": {
                                            "attachments": {
                                                "edges": [
                                                    {
                                                        "node": {
                                                            "attachment_type": {"value": "vpc"},
                                                            "virtual_network": network,
                                                        }
                                                    }
                                                ]
                                            }
                                        }
                                    }
                                }
                            },
                        }
                    }
                ]
            }
        }

        vif = clean_data(raw)["CloudVirtualInterface"][0]
        attachment = vif["transit_gateway_attachment"]["transit_gateway"]["attachments"][0]

        assert vif["virtual_network"]["account"]["owner"] == _cleaned_owner()
        assert attachment["virtual_network"]["account"]["owner"] == _cleaned_owner()


class TestOfficeRoot:
    """TopologyCustomerOffice delivers its owner and each hosted segment's owner."""

    def test_the_office_selects_its_owner(self) -> None:
        """owner sits on the office node next to sdwan_gateway and lan_prefixes."""
        node = _node(_root("TopologyCustomerOffice"))

        assert {"owner", "sdwan_gateway", "lan_prefixes", "segment_deployments"} <= {
            c.name.value for c in _children(node)
        }
        assert _owner_selection_names(_select_owner(node)) == list(_OWNER_FIELDS)

    def test_the_hosted_segment_selects_the_owner(self) -> None:
        """segment_deployments.segment carries the owner and the gateway prefix."""
        deployments = _child(_node(_root("TopologyCustomerOffice")), "segment_deployments")
        segment = _node(_child(_node(deployments), "segment"))

        assert {"owner", "gateway"} <= {c.name.value for c in _children(segment)}
        assert _owner_selection_names(_select_owner(segment)) == list(_OWNER_FIELDS)

    def test_the_fields_exist_on_the_schema(self) -> None:
        """The office owner is an OrganizationEntity; the hosted segment is a ManagedNetworkSegment."""
        assert _peer("TopologyCustomerOffice", "owner") == "OrganizationEntity"
        assert _peer("TopologySegmentHosting", "segment_deployments") in demo_data.schema()
        assert "owner" in demo_data.relationship_names("ManagedNetworkSegment")

    def test_a_raw_response_cleans_to_the_transform_shape(self) -> None:
        """office["owner"] and segment_deployments[i]["segment"]["owner"] are cleaned."""
        raw = {
            "TopologyCustomerOffice": {
                "edges": [
                    {
                        "node": {
                            "owner": _raw_owner("C005"),
                            "sdwan_gateway": {"node": {"name": {"value": "gw1"}}},
                            "lan_prefixes": {"edges": []},
                            "segment_deployments": {
                                "edges": [
                                    {
                                        "node": {
                                            "segment": {
                                                "node": {
                                                    "owner": _raw_owner("C005"),
                                                    "gateway": {"node": None},
                                                }
                                            }
                                        }
                                    }
                                ]
                            },
                        }
                    }
                ]
            }
        }

        office = clean_data(raw)["TopologyCustomerOffice"][0]
        segment = office["segment_deployments"][0]["segment"]

        assert office["owner"] == _cleaned_owner("C005")
        assert segment["owner"] == _cleaned_owner("C005")


class TestBgpProcessRole:
    """The shared ManagedBGPServiceFields fragment delivers process_role on each peering's processes."""

    def _processes(self) -> FieldNode:
        """The bgp_processes selection of the peerings in ManagedBGPServiceFields."""
        document = parse(_FRAGMENT.read_text())
        fragment = next(
            d
            for d in document.definitions
            if isinstance(d, FragmentDefinitionNode) and d.name.value == "ManagedBGPServiceFields"
        )
        fields = [s for s in fragment.selection_set.selections if isinstance(s, FieldNode)]
        peerings = next(f for f in fields if f.name.value == "peerings")
        return next(d for d in _descendants(peerings) if d.name.value == "bgp_processes")

    def test_the_processes_select_process_role(self) -> None:
        """process_role { value } is selected under peerings.bgp_processes next to router_id and local_as."""
        names = {c.name.value for c in _children(_node(self._processes()))}

        assert {"process_role", "router_id", "local_as", "capabilities"} <= names

    def test_the_process_role_is_selected_as_a_value(self) -> None:
        """process_role { value } cleans to a plain string."""
        role = _child(_node(self._processes()), "process_role")

        assert [c.name.value for c in _children(role)] == ["value"]

    def test_the_field_exists_on_the_schema(self) -> None:
        """bgp_processes peers ManagedBGP, which declares process_role."""
        assert _peer("ManagedBGPPeering", "bgp_processes") == "ManagedBGP"
        assert "process_role" in demo_data.attribute_names("ManagedBGP")

    def test_a_raw_response_cleans_to_the_transform_shape(self) -> None:
        """peering["bgp_processes"][k]["process_role"] is the dropdown value."""
        raw = {
            "peerings": {
                "edges": [
                    {
                        "node": {
                            "bgp_processes": {
                                "edges": [
                                    {"node": {"process_role": {"value": "overlay"}, "local_as": {"node": None}}},
                                    {"node": {"process_role": {"value": "regular"}, "local_as": {"node": None}}},
                                ]
                            }
                        }
                    }
                ]
            }
        }

        peering = clean_data(raw)["peerings"][0]

        assert [p["process_role"] for p in peering["bgp_processes"]] == ["overlay", "regular"]


class TestOrganizationEntityOwner:
    """The owner peers (OrganizationCustomer, OrganizationEntity) share org_id and are never asked for name."""

    @pytest.mark.parametrize("kind", ["OrganizationCustomer", "OrganizationEntity"])
    def test_the_owner_peer_declares_org_id(self, kind: str) -> None:
        """org_id is selectable on both owner peers."""
        assert "org_id" in demo_data.attribute_names(kind)

    def test_the_entity_generic_has_no_name_so_the_query_does_not_select_it(self) -> None:
        """name lives on OrganizationGeneric, not on OrganizationEntity: display_label is selected instead."""
        assert "name" not in demo_data.attribute_names("OrganizationEntity")
        for root in ("ManagedNetworkSegment", "CloudVirtualInterface", "TopologyCustomerOffice"):
            for owner in (d for d in _descendants(_root(root)) if d.name.value == "owner"):
                assert "name" not in _owner_selection_names(owner)
