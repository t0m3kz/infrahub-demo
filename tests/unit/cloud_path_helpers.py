"""Raw GraphQL shapes of a cloud connection's path (queries/validation/cloud_connection.gql): footprints, hubs,
attachments, route tables and peerings down to the provider port, its cable or carrier and our port. Tests clean them
with ``clean_data`` like the check does.
"""

from __future__ import annotations

from typing import Any


def v(value: Any) -> dict[str, Any]:
    return {"value": value}


def one(node: dict[str, Any] | None) -> dict[str, Any]:
    return {"node": node}


def many(*nodes: dict[str, Any]) -> dict[str, Any]:
    return {"edges": [{"node": node} for node in nodes]}


def device(name: str, typename: str = "DcimPhysicalDevice", **fields: Any) -> dict[str, Any]:
    return one({"id": name, "__typename": typename, "name": v(name), **fields})


def end(dev: str, name: str, *, subs: tuple[str, ...] = ()) -> dict[str, Any]:
    """A cable or carrier end: an interface of ``dev`` with the ids of the sub-interfaces that ride on it."""
    return {
        "__typename": "DcimPhysicalInterface",
        "id": f"{dev}:{name}",
        "name": v(name),
        "device": device(dev),
        "sub_interfaces": many(*({"id": f"{dev}:{sub}"} for sub in subs)),
    }


def cable(name: str, *ends: dict[str, Any]) -> dict[str, Any]:
    return one({"id": name, "name": v(name), "endpoints": many(*ends)})


def carrier(name: str, *ends: dict[str, Any], status: str = "active", circuit_type: str = "cross_connect") -> dict:
    return {
        "__typename": "TopologyCarrierCircuit",
        "id": name,
        "name": v(name),
        "circuit_type": v(circuit_type),
        "status": v(status),
        "capabilities": many(*ends),
    }


def port(
    dev: str,
    name: str,
    *,
    cable_to: dict[str, Any] | None = None,
    carriers: tuple[dict[str, Any], ...] = (),
    members: tuple[dict[str, Any], ...] | None = None,
) -> dict[str, Any]:
    """A provider port (or a LAG, with ``members``) with its cable to ``cable_to`` and its carriers."""
    node: dict[str, Any] = {
        "id": f"{dev}:{name}",
        "__typename": "DcimLAGInterface" if members is not None else "DcimPhysicalInterface",
        "name": v(name),
        "device": device(dev, "TopologyProviderConnect"),
        "capabilities": many(*carriers),
    }
    if cable_to is not None:
        node["cable"] = cable(f"CBL-{dev}-{name}", end(dev, name), cable_to)
    if members is not None:
        node["member_interfaces"] = many(*members)
    return node


def session(name: str, *ends: tuple[str, str], circuit_type: str = "handoff", internet: tuple[str, ...] = ()) -> dict:
    """A session circuit whose ends are (device, interface); a device in ``internet`` has a live internet carrier."""

    def end_node(dev: str, ifname: str) -> dict[str, Any]:
        ports = (
            many({"id": f"{dev}:wan", "capabilities": many(carrier(f"INET-{dev}", circuit_type="internet"))})
            if dev in internet
            else many()
        )
        return {"id": f"{dev}:{ifname}", "name": v(ifname), "device": device(dev, interfaces=ports)}

    return {
        "__typename": "TopologySessionCircuit",
        "id": name,
        "name": v(name),
        "circuit_type": v(circuit_type),
        "status": v("active"),
        "capabilities": many(*(end_node(dev, ifname) for dev, ifname in ends)),
    }


def peering(
    name: str,
    transport: str,
    *,
    parent: dict[str, Any] | None = None,
    gateway: str | None = None,
    sessions: tuple[dict[str, Any], ...] = (),
    account: str = "acct-own",
    transport_kind: str = "CloudDirectConnect",
    scope: str = "private",
) -> dict[str, Any]:
    """A CloudPeeringInterface of ``transport`` riding on ``parent`` and landing on ``gateway``."""
    return {
        "id": f"{transport}:{name}",
        "name": v(name),
        "scope": v(scope),
        "tunnel_index": v(None),
        "endpoint_ip": v(None),
        "tunnel_mode": v(None),
        "crypto_profile": one(None),
        "tunnel_key": one(None),
        "device": one(
            {"__typename": transport_kind, "id": transport, "name": v(transport), "account": one({"id": account})}
        ),
        "gateway": one({"id": gateway, "name": v(gateway), "gateway_type": v("edge")}) if gateway else one(None),
        "capabilities": many(*sessions),
        "vlan_id": v(100),
        "parent": one(parent),
    }


def handoff_path(name: str = "vif", *, gateway: str | None = "dxgw", account: str = "acct-own") -> dict[str, Any]:
    """A complete authored leg: the peering on the provider port cabled to our edge, our sub-interface ending the
    handoff.
    """
    return peering(
        name,
        f"dx-{name}",
        gateway=gateway,
        account=account,
        parent=port("EXT-DX", f"port-{name}", cable_to=end("eg-01", f"Eth1/{name}", subs=(f"Eth1/{name}.100",))),
        sessions=(session(f"HANDOFF-{name}", ("eg-01", f"Eth1/{name}.100"), (f"dx-{name}", name)),),
    )


def network_attachment(
    name: str,
    gateway: str,
    network: str,
    owner: str | None,
    *,
    table: str | None = None,
    propagates: tuple[str, ...] = (),
) -> dict[str, Any]:
    account = {
        "id": f"acct-{network}",
        "owner": one({"__typename": "OrganizationCustomer", "org_id": v(owner)} if owner else None),
    }
    return {
        "id": name,
        "name": v(name),
        "gateway": one({"id": gateway, "name": v(gateway), "gateway_type": v("hub")}),
        "virtual_network": one({"id": network, "name": v(network), "account": one(account)}),
        "peer_gateway": one(None),
        "associated_route_table": one({"id": table, "name": v(table), "gateway": one({"id": gateway})})
        if table
        else one(None),
        "propagates_to_route_tables": many(
            *({"id": t, "name": v(t), "gateway": one({"id": gateway})} for t in propagates)
        ),
    }


def link_attachment(name: str, hub: str, peer: str, table: str | None = "rt-onprem") -> dict[str, Any]:
    """The hub's attachment to the edge gateway ``peer``: its link towards us."""
    return {
        "id": name,
        "name": v(name),
        "gateway": one({"id": hub, "name": v(hub), "gateway_type": v("hub")}),
        "virtual_network": one(None),
        "peer_gateway": one({"id": peer, "name": v(peer)}),
        "associated_route_table": one({"id": table, "name": v(table), "gateway": one({"id": hub})})
        if table
        else one(None),
        "propagates_to_route_tables": many(),
    }


def hub(name: str = "tgw", *, region: str = "region-1", environment: str = "p") -> dict[str, Any]:
    return {
        "id": name,
        "name": v(name),
        "gateway_type": v("hub"),
        "region": one({"id": region}),
        "account": one({"id": "acct-hub", "environment": v(environment)}),
    }


def footprint(name: str = "C003-P-X", *, connection: str = "transit", account: str = "acct-own") -> dict[str, Any]:
    return {
        "id": name,
        "name": v(name),
        "connection": v(connection),
        "environment": v("p"),
        "parent": one({"id": "region-1", "name": v("eu-central-1")}),
        "account": one({"id": account, "name": v(account)}),
        "owner": one({"__typename": "OrganizationCustomer", "org_id": v("C003")}),
    }
