"""Builders for the routing-invariants check tests: canned `routing_invariants` query responses in the raw shape."""

from __future__ import annotations

from typing import Any, cast

from checks.routing_invariants import CheckRoutingInvariants


def _v(value: Any) -> dict[str, Any]:
    return {"value": value}


def _check() -> Any:
    """A check with log_error/log_info captured instead of sent to Infrahub."""
    check = cast(Any, CheckRoutingInvariants.__new__(CheckRoutingInvariants))
    errors: list[str] = []
    infos: list[str] = []
    check._captured_errors = errors
    check._captured_infos = infos
    check.log_error = lambda message: errors.append(message)
    check.log_info = lambda message: infos.append(message)
    return check


def _ns(name: str, ns_type: str | None, vni: int | None = None) -> dict[str, Any]:
    """An IpamNamespace in the raw query shape."""
    return {"id": f"ns-{name}", "name": _v(name), "l3_vni": _v(vni), "namespace_type": _v(ns_type)}


def _run(**sections: Any) -> Any:
    """Validate a payload made of the given query sections; return the check for assertions."""
    check = _check()
    check.validate({key: {"edges": [{"node": node} for node in nodes]} for key, nodes in sections.items()})
    return check


def _block(prefix: str, ns_name: str = "PROD", ns_type: str = "prod", block_id: str | None = None) -> dict[str, Any]:
    """A VPC cidr_blocks entry (an IpamPrefix) in the raw query shape."""
    return {
        "id": block_id or f"pfx-{ns_name}-{prefix}",
        "prefix": _v(prefix),
        "ip_namespace": {"node": {**_ns(ns_name, ns_type), "id": f"ns-{ns_name}"}},
    }


_CUSTOMER = ("OrganizationCustomer", "C003", "Vaultex Inc. (C003)")
_PROVIDER = ("OrganizationProvider", "P028", "Internal (P028)")


def _owner(who: tuple[str, str, str] | None) -> dict[str, Any]:
    """An `owner { node { __typename org_id display_label } }` selection (None: no owner)."""
    if who is None:
        return {"node": None}
    typename, org_id, label = who
    return {"node": {"__typename": typename, "org_id": _v(org_id), "display_label": label}}


def _vpc(
    name: str, env: str | None, *blocks: dict[str, Any], owner: tuple[str, str, str] | None = _CUSTOMER
) -> dict[str, Any]:
    """A CloudVirtualNetwork with its account environment and owner (a customer unless told otherwise) and blocks."""
    return {
        "id": f"vpc-{name}",
        "name": _v(name),
        "account": {"node": {"id": f"acc-{name}", "name": _v("acc"), "environment": _v(env), "owner": _owner(owner)}},
        "cidr_blocks": {"edges": [{"node": block} for block in blocks]},
    }


def _subnet(name: str, cidr: str | None, vpc: str) -> dict[str, Any]:
    """A CloudNetworkSegment (a subnet) of the named VPC."""
    return {
        "id": f"sub-{name}",
        "name": _v(name),
        "cidr_block": _v(cidr),
        "virtual_network": {"node": {"id": f"vpc-{vpc}", "name": _v(vpc)}},
    }


def _onprem(
    name: str, prefix: str, ns_name: str = "PROD", ns_type: str = "prod", member_type: str = "address", pid: str = "p"
) -> dict[str, Any]:
    """An on-prem segment whose gateway sits in the given prefix."""
    return {
        "__typename": "ManagedVxlanSegment",
        "id": f"seg-{name}",
        "name": _v(name),
        "gateway": {
            "node": {
                "ip_prefix": {
                    "node": {
                        "id": pid,
                        "prefix": _v(prefix),
                        "member_type": _v(member_type),
                        "ip_namespace": {"node": {**_ns(ns_name, ns_type), "id": f"ns-{ns_name}"}},
                    }
                }
            }
        },
    }


def _office(
    name: str,
    *prefixes: dict[str, Any],
    owner: tuple[str, str, str] | None = _CUSTOMER,
    segments: int = 0,
) -> dict[str, Any]:
    """A TopologyCustomerOffice footprint with its owner (a customer unless told otherwise), LAN prefixes and hosted
    segments (an office has no environment)."""
    return {
        "id": f"office-{name}",
        "name": _v(name),
        "owner": _owner(owner),
        "network_segments": {"edges": [{"node": {"id": f"seg-{name}-{n}"}} for n in range(segments)]},
        "lan_prefixes": {"edges": [{"node": prefix} for prefix in prefixes]},
    }


_C002 = ("OrganizationCustomer", "C002", "Solvex (C002)")
_C005 = ("OrganizationCustomer", "C005", "Brightwave (C005)")
_C015 = ("OrganizationCustomer", "C015", "Novantis (C015)")


def _segment(
    name: str,
    prefix: str,
    owner: tuple[str, str, str] | None,
    *,
    ns_name: str = "PROD",
    ns_type: str = "prod",
) -> dict[str, Any]:
    """An on-prem segment with its owner (what rule 8 reads next to the gateway prefix)."""
    return {**_onprem(name, prefix, ns_name, ns_type), "owner": _owner(owner)}


def _gateway_office(
    name: str,
    gateway: str | None,
    owner: tuple[str, str, str] | None,
    *lans: dict[str, Any],
    hosted: tuple[tuple[str, tuple[str, str, str] | None], ...] = (),
) -> dict[str, Any]:
    """An office behind an SD-WAN gateway with its LANs and hosted segments, (prefix, owner) each (PROD)."""
    return {
        **_office(name, *lans, owner=owner),
        "sdwan_gateway": {"node": {"name": _v(gateway)}} if gateway else {"node": None},
        "segment_deployments": {
            "edges": [
                {
                    "node": {
                        "segment": {
                            "node": {
                                "owner": _owner(segment_owner),
                                "gateway": {
                                    "node": {
                                        "ip_prefix": {
                                            "node": {
                                                "prefix": _v(prefix),
                                                "ip_namespace": {"node": {"name": _v("PROD")}},
                                            }
                                        }
                                    }
                                },
                            }
                        }
                    }
                }
                for prefix, segment_owner in hosted
            ]
        },
    }


def _rule_eight(check: Any) -> list[str]:
    """The errors rule 8 raised (they start with the namespace)."""
    return [message for message in check._captured_errors if message.startswith("Namespace '")]


def _connection(
    conn_id: str,
    ns_type: str | None = "prod",
    *,
    service: str = "private",
    network: dict[str, Any] | None = None,
    sessions: tuple[str, ...] = ("HO",),
    owner: tuple[str, str, str] | None = None,
    registered: tuple[dict[str, Any], ...] = (),
    table: str | None = None,
    attachments: list[tuple[str, dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """A TopologyProviderConnection in the raw query shape.

    ``ns_type`` is the type of its ip_namespace (None: untyped), ``network`` the attached virtual network (a
    ``_vpc``), ``sessions`` the names of its session circuits (none: the connection has no session), ``table`` the
    route table its transit attachment is associated with and ``attachments`` the (propagated table, VPC) pairs of
    its gateway.
    """
    gateway = {
        "node": {
            "attachments": {
                "edges": [
                    {
                        "node": {
                            "attachment_type": _v("vpc"),
                            "propagates_to_route_tables": {"edges": [{"node": {"id": propagated}}]},
                            "virtual_network": {"node": vpc},
                        }
                    }
                    for propagated, vpc in attachments or []
                ]
            }
        }
    }
    return {
        "id": conn_id,
        "name": _v(conn_id),
        "service": _v(service),
        "ip_namespace": {"node": {"name": _v("NS"), "namespace_type": _v(ns_type)}} if ns_type else {"node": None},
        "owner": _owner(owner),
        "account": {"node": None},
        "session_circuits": {"edges": [{"node": {"name": _v(name)}} for name in sessions]},
        "registered_prefixes": {"edges": [{"node": prefix} for prefix in registered]},
        "attached_network": {"node": network} if network else {"node": None},
        "transit_attachment": {"node": {"associated_route_table": {"node": {"id": table}}}}
        if table
        else {"node": None},
        "attached_gateway": gateway if attachments is not None else {"node": None},
    }


def _private_connection(
    conn_id: str, vpc: dict[str, Any], sessions: tuple[str, ...] = ("HO",), ns_type: str | None = "prod"
) -> dict[str, Any]:
    """A private connection onto one VPC (its blocks and account owner), with the session circuits ``sessions``."""
    return _connection(conn_id, ns_type, network=vpc, sessions=sessions)


def _transit_connection(
    conn_id: str, table: str, attachments: list[tuple[str, dict[str, Any]]], sessions: tuple[str, ...] = ("HO-HUB",)
) -> dict[str, Any]:
    """A transit connection whose hybrid attachment sits on ``table``; ``attachments`` are (propagated table, VPC)."""
    return _connection(conn_id, service="transit", sessions=sessions, table=table, attachments=attachments)
