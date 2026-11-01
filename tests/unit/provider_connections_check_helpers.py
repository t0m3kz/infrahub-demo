"""Payload builders and the runner for the CheckProviderConnections unit tests.

The payloads mirror queries/validation/provider_connections.gql (raw GraphQL, ``edges``/``node``/``value`` wrappers):
an AWS Direct Connect connection FR2 -> eu-central-1 on VLAN 3101 with one handoff session, as in data/demos/30_all.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

from checks.provider_connections import CheckProviderConnections
from checks.provider_decommission import CheckProviderDecommission


def edges(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    """A GraphQL ``edges`` list over the given nodes."""
    return {"edges": [{"node": node} for node in nodes]}


def node(value: dict[str, Any] | None) -> dict[str, Any]:
    """A GraphQL single-relationship wrapper."""
    return {"node": value}


def place(name: str, kind: str) -> dict[str, Any]:
    """A facility as the query selects it."""
    return {"id": f"id-{name}", "__typename": kind, "name": {"value": name}}


FR2 = place("FR2", "TopologyColocationZone")
EUC1 = place("eu-central-1", "TopologyCloudRegion")
DC10 = place("DC10", "TopologyDataCenter")
AM_NE = place("AM-NE", "TopologyVirtualColocation")
C001 = {"id": "id-C001", "__typename": "OrganizationCustomer", "org_id": {"value": "C001"}, "display_label": "C001"}
C002 = {"id": "id-C002", "__typename": "OrganizationCustomer", "org_id": {"value": "C002"}, "display_label": "C002"}


def namespace(name: str = "PROD", kind: str = "prod") -> dict[str, Any]:
    """An ip_namespace."""
    return {"id": f"id-ns-{name}", "name": {"value": name}, "namespace_type": {"value": kind}}


def prefix(value: str, ns: str = "PROD") -> dict[str, Any]:
    """An IpamPrefix with its namespace."""
    return {
        "id": f"id-{value}",
        "prefix": {"value": value},
        "ip_namespace": node({"id": f"id-ns-{ns}", "name": {"value": ns}}),
    }


def carrier_ref(name: str, status: str = "active") -> dict[str, Any]:
    """A carrier circuit as found on a port."""
    return {
        "__typename": "TopologyCarrierCircuit",
        "id": f"id-{name}",
        "name": {"value": name},
        "status": {"value": status},
    }


XC = carrier_ref("XC-FR2-EUC1")


def port(
    device: str,
    name: str,
    deployment: dict[str, Any],
    parent_carriers: tuple[dict[str, Any], ...] = (),
    asns: tuple[int, ...] = (),
    sub: bool = True,
) -> dict[str, Any]:
    """A session end port: its device with the deployment and BGP processes, the carriers at its parent."""
    result: dict[str, Any] = {
        "id": f"id-{device}:{name}",
        "name": {"value": name},
        "capabilities": edges([]),
        "device": node(
            {
                "id": f"id-{device}",
                "name": {"value": device},
                "deployment": node({k: deployment[k] for k in ("id", "__typename", "name")}),
                "capabilities": edges(
                    [{"__typename": "ManagedBGP", "local_as": node({"asn": {"value": asn}})} for asn in asns]
                    + [{"__typename": "ManagedOSPF"}]
                ),
            }
        ),
    }
    if sub:
        result["parent_interface"] = node({"capabilities": edges(list(parent_carriers))})
    return result


def session(
    name: str,
    circuit_type: str = "handoff",
    ends: list[dict[str, Any]] | None = None,
    connection: str | None = "PC-AWS-FR2",
    link_prefix: str | None = None,
    status: str = "active",
    locations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """A session circuit as the query selects it."""
    if ends is None:
        ends = [
            port("eg-fr201", "Ethernet1/13.3101", FR2, (XC,)),
            port("EXT-AWS-DX-FR2", "et-0/0/0.3101", EUC1, asns=(64512,)),
        ]
    return {
        "id": f"id-{name}",
        "name": {"value": name},
        "status": {"value": status},
        "circuit_type": {"value": circuit_type},
        "connection": node({"id": f"id-{connection}", "name": {"value": connection}} if connection else None),
        "ip_namespace": node({"id": "id-ns-PROD", "name": {"value": "PROD"}}),
        "link_prefix": node({"id": "id-lp", "prefix": {"value": link_prefix}} if link_prefix else None),
        "locations": edges(locations if locations is not None else [FR2, EUC1]),
        "capabilities": edges(ends),
    }


def connection(
    name: str = "PC-AWS-FR2",
    path_type: str = "dedicated",
    vlan_id: int | None = 3101,
    profile: str | None = "aws",
    near: dict[str, Any] = FR2,
    far: dict[str, Any] = EUC1,
    ns: dict[str, Any] | None = None,
    owner: dict[str, Any] | None = None,
    account: dict[str, Any] | None = None,
    provider_as: int | None = None,
    carriers: list[Any] | None = None,
    registered: list[dict[str, Any]] | None = None,
    link_prefixes: list[dict[str, Any]] | None = None,
    attached_gateway: bool = False,
    attached_network: bool = False,
    status: str = "active",
    near_port: str | None = None,
) -> dict[str, Any]:
    """A provider connection as the query selects it (the profile comes from a cloud account when there is one)."""
    if account is None and profile:
        account = None
    return {
        "id": f"id-{name}",
        "name": {"value": name},
        "status": {"value": status},
        "path_type": {"value": path_type},
        "vlan_id": {"value": vlan_id} if vlan_id is not None else {"value": None},
        "near_port": node({"id": near_port, "name": {"value": near_port}} if near_port else None),
        "near": node({k: near[k] for k in ("id", "__typename", "name")}),
        "far": node(
            {
                **{k: far[k] for k in ("id", "__typename", "name")},
                "parent": node(
                    {
                        "provider": node(
                            {"id": "id-prov", "name": {"value": "PROV"}, "connection_profile": {"value": profile}}
                        )
                    }
                ),
            }
        ),
        "ip_namespace": node(ns or namespace()),
        "owner": node(owner),
        "account": node(account),
        "provider_as": node({"id": "id-as", "asn": {"value": provider_as}} if provider_as is not None else None),
        "attached_network": node({"id": "id-vnet"} if attached_network else None),
        "attached_gateway": node({"id": "id-tgw"} if attached_gateway else None),
        "registered_prefixes": edges(registered or []),
        "link_prefixes": edges(link_prefixes or []),
        "carrier_circuits": edges(
            [
                {
                    "id": f"id-{c}",
                    "name": {"value": c},
                    "status": {"value": "active"},
                    "circuit_type": {"value": "internet" if c.startswith("INET") else "cross_connect"},
                    "locations": edges([{"id": FR2["id"]}, {"id": EUC1["id"]}]),
                }
                for c in (carriers if carriers is not None else ["XC-FR2-EUC1"])
            ]
        ),
    }


def account(env: str = "p", owner: dict[str, Any] | None = None, profile: str = "aws") -> dict[str, Any]:
    """A cloud account with its provider's profile and owner."""
    return {
        "id": "id-acct",
        "name": {"value": "acct-1"},
        "environment": {"value": env},
        "provider": node({"id": "id-prov", "name": {"value": "PROV"}, "connection_profile": {"value": profile}}),
        "owner": node(owner),
    }


def kit_end(device: str, port_name: str, deployment: dict[str, Any]) -> dict[str, Any]:
    """A carrier end port as ConnectionKitEnd selects it: the port id is ``<device>:<port>``."""
    return {
        "id": f"{device}:{port_name}",
        "device": node(
            {
                "id": f"id-{device}",
                "name": {"value": device},
                "owner": node(None),
                "deployment": node({"id": deployment["id"]}),
            }
        ),
    }


def carrier(
    name: str,
    locations: list[dict[str, Any]],
    connections: list[str],
    redundancy_group: str | None = None,
    ends: list[dict[str, Any]] | None = None,
    status: str = "active",
) -> dict[str, Any]:
    """A carrier circuit as the query selects it at the root."""
    return {
        "id": f"id-{name}",
        "name": {"value": name},
        "status": {"value": status},
        "redundancy_group": {"value": redundancy_group},
        "capabilities": edges(ends or []),
        "locations": edges(locations),
        "provider_connections": edges([{"id": f"id-{c}"} for c in connections]),
    }


def run(
    connections: list[dict[str, Any]] | None = None,
    sessions: list[dict[str, Any]] | None = None,
    carriers: list[dict[str, Any]] | None = None,
    segments: list[dict[str, Any]] | None = None,
    attachments: list[dict[str, Any]] | None = None,
) -> tuple[list[str], list[str]]:
    """Validate and return (error messages, info messages)."""
    check = cast(Any, CheckProviderConnections.__new__(CheckProviderConnections))
    errors: list[dict[str, Any]] = []
    infos: list[str] = []
    check.log_error = lambda **kwargs: errors.append(kwargs)
    check.log_info = lambda **kwargs: infos.append(kwargs["message"])
    check.validate(
        {
            "TopologyProviderConnection": edges(connections or []),
            "TopologySessionCircuit": edges(sessions or []),
            "TopologyCarrierCircuit": edges(carriers or []),
            "CloudNetworkSegment": edges(segments or []),
            "CloudGatewayAttachment": edges(attachments or []),
        }
    )
    for error in errors:
        assert error["object_id"] and error["object_type"]
    return [error["message"] for error in errors], infos


# ----------------------------------------------------------------------
# queries/validation/provider_decommission.gql (CheckProviderDecommission, rules PC14 and PC15)
# ----------------------------------------------------------------------


def dc_connection(
    name: str = "PC-AWS-FR2", status: str = "decommissioned", carriers: Sequence[tuple[str, str]] = ()
) -> dict:
    """A connection as provider_decommission.gql selects it; ``carriers`` are (name, status) pairs."""
    return {
        "id": f"id-{name}",
        "name": {"value": name},
        "status": {"value": status},
        "carrier_circuits": edges(
            [{"id": f"id-{c}", "name": {"value": c}, "status": {"value": s}} for c, s in carriers]
        ),
    }


def dc_session(name: str, connection_name: str | None = "PC-AWS-FR2", ends: tuple[str, ...] = ()) -> dict:
    """A session circuit as provider_decommission.gql selects it."""
    return {
        "id": f"id-{name}",
        "name": {"value": name},
        "status": {"value": "active"},
        "connection": node(
            {"id": f"id-{connection_name}", "name": {"value": connection_name}} if connection_name else None
        ),
        "capabilities": edges([{"id": end} for end in ends]),
    }


def dc_interface(
    iface_id: str,
    name: str,
    device: str = "eg-fr201",
    device_kind: str = "DcimPhysicalDevice",
    device_role: str = "edge",
    parent: tuple[str, str | None] | None = ("Ethernet1/13", "cloud"),
    address: str | None = None,
    capabilities: tuple[str, ...] = (),
) -> dict:
    """A virtual interface as the DcimHasIPAddress root selects it (``parent`` is (name, role))."""
    return {
        "id": iface_id,
        "__typename": "DcimVirtualInterface",
        "ip_address": node({"id": address} if address else None),
        "name": {"value": name},
        "parent_interface": node(
            {"id": f"id-{parent[0]}", "name": {"value": parent[0]}, "role": {"value": parent[1]}} if parent else None
        ),
        "device": node(
            {
                "id": f"id-{device}",
                "__typename": device_kind,
                "name": {"value": device},
                "role": {"value": device_role},
            }
        ),
        "capabilities": edges([{"id": f"id-{c}", "__typename": c} for c in capabilities]),
    }


def dc_physical(iface_id: str, address: str | None = None) -> dict:
    """A physical interface as the DcimHasIPAddress root selects it (only its address)."""
    return {
        "id": iface_id,
        "__typename": "DcimPhysicalInterface",
        "ip_address": node({"id": address} if address else None),
    }


def dc_pool(children: list[dict], name: str = "Handoff-PROD-IPv4") -> dict:
    """A link pool with one resource prefix and the given child prefixes."""
    return {
        "id": f"id-{name}",
        "name": {"value": name},
        "resources": edges([{"id": "id-res", "prefix": {"value": "10.255.128.0/20"}, "children": edges(children)}]),
    }


def dc_child(prefix: str, addresses: tuple[str, ...] = ()) -> dict:
    """A child prefix of a link pool with its addresses (ids are the address strings)."""
    return {
        "id": f"id-{prefix}",
        "prefix": {"value": prefix},
        "description": {"value": "Handoff"},
        "ip_addresses": edges([{"id": a, "address": {"value": a}} for a in addresses]),
    }


def dc_peering(name: str, ends: tuple[str, ...] = (), role: str = "regular") -> dict:
    """A BGP peering as provider_decommission.gql selects it."""
    return {
        "id": f"id-{name}",
        "name": {"value": name},
        "peering_role": {"value": role},
        "capabilities": edges([{"id": e} for e in ends]),
    }


def run_decommission(
    connections: list[dict] | None = None,
    sessions: list[dict] | None = None,
    interfaces: list[dict] | None = None,
    pools: list[dict] | None = None,
    peerings: list[dict] | None = None,
) -> tuple[list[str], list[str]]:
    """Validate and return (error messages, warning messages without the prefix handling)."""
    check = cast(Any, CheckProviderDecommission.__new__(CheckProviderDecommission))
    errors: list[dict[str, Any]] = []
    infos: list[str] = []
    check.log_error = lambda **kwargs: errors.append(kwargs)
    check.log_info = lambda **kwargs: infos.append(kwargs["message"])
    check.validate(
        {
            "TopologyProviderConnection": edges(connections or []),
            "TopologySessionCircuit": edges(sessions or []),
            "DcimHasIPAddress": edges(interfaces or []),
            "CoreIPPrefixPool": edges(pools or []),
            "ManagedBGPPeering": edges(peerings or []),
        }
    )
    for error in errors:
        assert error["object_id"] and error["object_type"]
    return [error["message"] for error in errors], infos
