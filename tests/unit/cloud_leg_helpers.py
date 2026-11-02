"""Builders for the cloud leg tests: the ``CloudPeeringInterface`` and ``CloudGatewayAttachment`` roots in the raw
GraphQL shape the edge, routing invariants and segment security queries return (transforms/helpers/bgp_cloud.py)."""

from __future__ import annotations

from typing import Any

from utils.data_cleaning import clean_data


def v(value: Any) -> dict[str, Any]:
    """An attribute selection ``{ value }``."""
    return {"value": value}


def node(value: Any) -> dict[str, Any]:
    """A one-relationship selection ``{ node }``."""
    return {"node": value}


def edges(*nodes: dict[str, Any]) -> dict[str, Any]:
    """A many-relationship selection of the given nodes."""
    return {"edges": [{"node": n} for n in nodes]}


def cleaned(*nodes: dict[str, Any]) -> list[dict[str, Any]]:
    """The nodes of one query root, cleaned the way a transform or a check cleans its data."""
    return clean_data({"root": edges(*nodes)})["root"]


def owner(org_id: str | None, typename: str = "OrganizationCustomer") -> dict[str, Any]:
    """An ``owner { node { __typename org_id display_label } }`` selection (None: no owner)."""
    if org_id is None:
        return node(None)
    return node({"__typename": typename, "org_id": v(org_id), "display_label": v(f"{org_id} label")})


def prefix(cidr: str, ns_name: str = "PROD", ns_type: str = "prod") -> dict[str, Any]:
    """An IpamPrefix node with its namespace."""
    return {"prefix": v(cidr), "ip_namespace": node({"name": v(ns_name), "namespace_type": v(ns_type)})}


def gateway(name: str) -> dict[str, Any]:
    """A ``gateway { node { id name } }`` selection."""
    return node({"id": f"gw-{name}", "name": v(name)})


def attachment(
    name: str,
    on: str,
    *,
    network: str | None = None,
    blocks: tuple[dict[str, Any], ...] = (),
    peer: str | None = None,
    allowed: tuple[dict[str, Any], ...] = (),
    table: str | None = None,
    propagates: tuple[str, ...] = (),
) -> dict[str, Any]:
    """A CloudGatewayAttachment of gateway ``on``: to a virtual network (``blocks`` its CIDRs) or to ``peer``."""
    return {
        "name": v(name),
        "gateway": gateway(on),
        "peer_gateway": gateway(peer) if peer else node(None),
        "virtual_network": node({"id": f"vn-{network}", "name": v(network), "cidr_blocks": edges(*blocks)})
        if network
        else node(None),
        "allowed_prefixes": edges(*allowed),
        "associated_route_table": node({"id": f"rt-{table}", "name": v(table)}) if table else node(None),
        "propagates_to_route_tables": edges(*({"id": f"rt-{t}", "name": v(t)} for t in propagates)),
    }


def peering(
    name: str,
    transport: str,
    *,
    ends: tuple[tuple[str, str], ...] = (("eg-fr201", "Ethernet1/13.1000"),),
    on: str | None = "dxgw",
    scope: str = "private",
    routing: str = "bgp",
    peer_ip: str | None = "10.255.5.6/30",
    peer_asn: int | None = 64512,
    auth: str | None = "s3cret",
    account_owner: str | None = "C001",
    account_owner_type: str = "OrganizationCustomer",
    saas_owner: str | None = None,
    advertised: tuple[dict[str, Any], ...] = (),
    circuit_type: str = "handoff",
    kind: str = "CloudDirectConnect",
    transport_gateway: str | None = None,
) -> dict[str, Any]:
    """A CloudPeeringInterface of ``transport`` ending one session circuit whose other ends are ``ends`` (device, port).

    ``account_owner`` None: the transport has no account (a SaaS), its own ``saas_owner`` applies. ``on`` is the
    gateway it lands on; ``transport_gateway`` the one a CloudVPNConnection holds.
    """
    circuit_ends = [{"name": v(port), "device": node({"name": v(device)})} for device, port in ends]
    circuit_ends.append({"name": v(name), "device": node({"name": v(transport)})})
    transport_node: dict[str, Any] = {
        "__typename": kind,
        "name": v(transport),
        "account": node({"owner": owner(account_owner, account_owner_type)}) if account_owner else node(None),
        "owner": owner(saas_owner),
    }
    if transport_gateway:
        transport_node["gateway"] = gateway(transport_gateway)
    return {
        "name": v(name),
        "scope": v(scope),
        "routing": v(routing),
        "peer_ip": node({"address": v(peer_ip)}) if peer_ip else node(None),
        "peer_asn": node({"asn": v(peer_asn)}) if peer_asn is not None else node(None),
        "auth": node({"password": v(auth)}) if auth else node(None),
        "gateway": gateway(on) if on else node(None),
        "advertised_prefixes": edges(*advertised),
        "device": node(transport_node),
        "capabilities": edges(
            {"__typename": "ManagedBGPPeering"},
            {
                "__typename": "TopologySessionCircuit",
                "name": v(f"SC-{name}"),
                "circuit_type": v(circuit_type),
                "capabilities": edges(*circuit_ends),
            },
        ),
    }


def aws_chain(vpc_block: str = "10.10.0.0/16") -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The rule 10 chain of 30_all as (peerings, attachments), cleaned: C001's VIF on ``dxgw``, ``dxgw`` -> ``tgw`` ->
    the VPC ``nordix-vpc-prod`` holding ``vpc_block`` (PROD)."""
    peerings = cleaned(peering("vif-nordix", "dx-nordix", ends=(("eg-fr201", "Ethernet1/13.1000"),)))
    attachments = cleaned(
        attachment("tgwa-dxgw", "tgw", peer="dxgw", table="rt-shared"),
        attachment(
            "tgwa-vpc-nordix", "tgw", network="nordix-vpc-prod", blocks=(prefix(vpc_block),), propagates=("rt-shared",)
        ),
    )
    return peerings, attachments
