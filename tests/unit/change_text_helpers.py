"""Builders of the change request tests."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from utils.change_rules import (
    ArtifactChange,
    ChangeInputs,
    ObjectDiff,
    affected_customers,
    device_facts,
    parse_object_nodes,
    prod_objects,
)
from utils.data_cleaning import clean_data

NOW = datetime(2026, 11, 12, 9, 0, tzinfo=UTC)


def v(value: Any) -> dict[str, Any]:
    """An attribute as GraphQL answers it."""
    return {"value": value}


def node(value: Any) -> dict[str, Any]:
    """A cardinality-one relationship as GraphQL answers it."""
    return {"node": value}


def edges(values: list[Any]) -> dict[str, Any]:
    """A cardinality-many relationship as GraphQL answers it."""
    return {"edges": [{"node": value} for value in values]}


def raw_device(name: str, role: str, site: str, *, mlag_peer: str | None = None) -> dict[str, Any]:
    """A ``DcimDeviceManaged`` node: a physical device in a site, optionally in an MLAG pair."""
    capabilities = []
    if mlag_peer:
        capabilities.append(
            {
                "__typename": "ManagedMLAG",
                "members": edges(
                    [{"id": f"id-{name}", "name": v(name)}, {"id": f"id-{mlag_peer}", "name": v(mlag_peer)}]
                ),
            }
        )
    return {
        "id": f"id-{name}",
        "__typename": "DcimPhysicalDevice",
        "name": v(name),
        "role": v(role),
        "owner": node(None),
        "site": node({"name": v(site)}),
        "capabilities": edges(capabilities),
        "shared_capabilities": edges([]),
    }


def raw_node(
    kind: str,
    label: str,
    status: str,
    uuid: str,
    values: dict[str, str | None] | None = None,
    peers: dict[str, list[tuple[str, str]]] | None = None,
    previous: dict[str, str | None] | None = None,
) -> dict[str, Any]:
    """A node of ``DiffTree`` as the server answers it: an attribute's value is its HAS_VALUE property."""
    removed = status == "REMOVED"
    attributes = []
    for name, value in (values or {}).items():
        text = "NULL" if value is None else value
        attributes.append(
            {
                "name": name,
                "properties": [
                    {"property_type": "IS_PROTECTED", "new_value": "False", "previous_value": None},
                    {
                        "property_type": "HAS_VALUE",
                        "new_value": None if removed else text,
                        "previous_value": text if removed else (previous or {}).get(name),
                    },
                ],
            }
        )
    relationships = [
        {
            "name": name,
            "elements": [{"peer_label": label_, "peer_id": peer_id, "status": status} for label_, peer_id in elements],
        }
        for name, elements in (peers or {}).items()
    ]
    return {
        "uuid": uuid,
        "kind": kind,
        "label": label,
        "status": status,
        "attributes": attributes,
        "relationships": relationships,
    }


def raw_segment(
    uuid: str, name: str, gateway: str, *, status: str = "ADDED", firewall_gateway: bool = False
) -> dict[str, Any]:
    """A VXLAN segment of C005 in the footprint C005-P-DC-DC10."""
    return raw_node(
        "ManagedVxlanSegment",
        name,
        status,
        uuid,
        {
            "name": name,
            "environment": "p",
            "ip_version": "ipv4",
            "vlan_scope": "local",
            "stretch_scope": "local",
            "firewall_gateway": str(firewall_gateway),
        },
        {
            "owner": [("C005", "cust-c005")],
            "gateway": [(f"{gateway} - default", f"gw-{uuid}")],
            "customer_deployments": [("C005-P-DC-DC10", "fp-c005")],
        },
    )


def raw_rule(
    uuid: str,
    name: str,
    index: int,
    *,
    status: str = "ADDED",
    previous: dict[str, str | None] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """A rule of the policy of c005-web-p: a permit to c005-app-p, tcp/443 (``overrides`` replace values or peers)."""
    values: dict[str, str | None] = {
        "name": name,
        "index": str(index),
        "action": "permit",
        "protocol": "tcp",
        "port_start": "443",
        "port_end": None,
        "disabled": "False",
    }
    peers: dict[str, list[tuple[str, str]]] = {
        "policy": [("seg-c005-web-p-egress", "pol-1")],
        "destination_segment": [("c005-app-p", "seg-app")],
    }
    for key, value in overrides.items():
        if key.endswith("_peers"):
            peers[key.removesuffix("_peers")] = value
        else:
            values[key] = value
    return raw_node("SecurityPolicyRule", name, status, uuid, values, peers, previous)


RAW_OBJECTS: list[dict[str, Any]] = [
    raw_segment("seg-web", "c005-web-p", "10.5.1.1/24"),
    raw_segment("seg-app", "c005-app-p", "10.5.2.1/24", firewall_gateway=True),
    raw_node(
        "TopologyCustomerDC",
        "C005-P-DC-DC10",
        "ADDED",
        "fp-c005",
        {"name": "C005-P-DC-DC10", "environment": "p"},
        {"owner": [("C005", "cust-c005")]},
    ),
    raw_rule("rule-1", "web-to-app", 10),
    raw_rule(
        "rule-2",
        "web-to-internet",
        20,
        protocol="any",
        port_start=None,
        destination_segment_peers=[],
        destination_zone_peers=[("INTERNET", "zone-inet")],
    ),
    raw_rule(
        "rule-3",
        "mgmt-to-web",
        30,
        status="UPDATED",
        previous={"port_start": "443", "action": "deny"},
        port_start="8000",
        port_end="8010",
        source_segment_peers=[("c005-mgmt-p", "seg-mgmt")],
    ),
    raw_rule("gone-rule", "old-rule", 40, status="REMOVED"),
    raw_node(
        "DcimCable",
        "CBL-1",
        "ADDED",
        "cable-1",
        {"name": "CBL-1", "type": "smf", "cable_id": "C-100"},
        {
            "endpoints": [("dc10-leaf-01-Ethernet1/49", "if-1"), ("dc10-spine-01-Ethernet1/1", "if-2")],
            "deployment": [("DC10", "dep-dc10")],
        },
    ),
    raw_node(
        "TopologyCarrierCircuit",
        "df10001-name",
        "UPDATED",
        "carrier-1",
        {"name": "df10001-name", "circuit_id": "DF10001", "circuit_type": "dark_fiber"},
        {
            "provider": [("Equinix (P016)", "prov-1")],
            "locations": [("DC10", "dep-dc10"), ("DC20", "dep-dc20")],
            "capabilities": [("dc10-spine-01-Ethernet1/30", "if-3"), ("dc20-spine-01-Ethernet1/30", "if-4")],
        },
    ),
]
"""2 segments, a footprint, 3 rules (one to INTERNET) and a removed one, 1 cable, 1 carrier circuit."""

RAW_FACTS: dict[str, Any] = {
    "DcimDeviceManaged": edges(
        [
            raw_device("dc10-leaf-01", "leaf", "DC10", mlag_peer="dc10-leaf-02"),
            raw_device("dc10-leaf-02", "leaf", "DC10", mlag_peer="dc10-leaf-01"),
            raw_device("dc10-spine-01", "spine", "DC10"),
        ]
    )
}


def raw_summaries(
    total: tuple[int, int, int], non_standard: tuple[int, int, int], routing: tuple[int, int, int]
) -> dict:
    """The aliased ``DiffTreeSummary`` roots of change_request_diff.gql (added, updated, removed)."""

    def summary(counts: tuple[int, int, int]) -> dict[str, int]:
        return dict(zip(("num_added", "num_updated", "num_removed"), counts, strict=True))

    return {"total": summary(total), "non_standard": summary(non_standard), "routing": summary(routing)}


def artifact(target: str, *, new: str = "n1", previous: str | None = "p1", action: str = "updated") -> ArtifactChange:
    """The artifact diff entry of a device's ``leaf_config``."""
    return ArtifactChange(
        id=f"art-{target}",
        action=action,
        target_id=f"id-{target}",
        target_kind="DcimPhysicalDevice",
        target=target,
        definition="leaf_config",
        checksum_new=new,
        checksum_previous=previous,
        storage_previous=f"st-{previous}" if previous else None,
    )


def objects_of(raw: list[dict[str, Any]] | None = None) -> list[ObjectDiff]:
    """The objects as the generator parses them from the raw nodes."""
    return parse_object_nodes(RAW_OBJECTS if raw is None else raw)


def realistic_inputs(**overrides: Any) -> ChangeInputs:
    """2 segments, 3 rules (one to INTERNET), 1 cable, 1 carrier circuit, 3 devices with one MLAG pair."""
    rows = clean_data(RAW_FACTS)
    objects = objects_of()
    base: dict[str, Any] = {
        "pc_name": "web tier for C005",
        "pc_description": "Adds the web and app tiers of C005 in DC10.",
        "branch": "feat/c005-web",
        "now": NOW,
        "artifacts": [
            artifact("dc10-leaf-01"),
            artifact("dc10-leaf-02", previous=None, action="added"),
            artifact("dc10-spine-01", new="n3", previous="p3"),
        ],
        "diff_counts": {"ADDED": 10, "UPDATED": 3, "REMOVED": 1},
        "routing_changed": True,
        "objects": objects,
        "devices": [device_facts(row) for row in rows["DcimDeviceManaged"]],
        "customers": sorted(affected_customers({}, objects).values()),
        "prod_objects": prod_objects(objects),
        "rows": rows,
    }
    base.update(overrides)
    return ChangeInputs(**base)
