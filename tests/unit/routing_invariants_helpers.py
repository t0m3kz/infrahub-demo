"""Builders for the routing-invariants check tests: canned `routing_invariants` query responses in the raw shape."""

from __future__ import annotations

from typing import Any, cast

from checks.routing_invariants import CheckRoutingInvariants
from tests.unit.external_vrf_peers_helpers import _raw_gateway, _raw_vrf


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
    return {"id": f"zone-{name}", "name": _v(name), "l3_vni": _v(vni), "vrf_type": _v(ns_type)}


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
        "ip_namespace": {"node": {"name": {"value": "default"}}},
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
        "security_zone": {"node": _ns(ns_name, ns_type)},
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
                        "ip_namespace": {"node": {"name": {"value": "default"}}},
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
        "sdwan_gateway": {"node": _raw_gateway(gateway)} if gateway else {"node": None},
        "vrfs": {"edges": [{"node": _raw_vrf("PROD", name)}]},
        "segment_deployments": {
            "edges": [
                {
                    "node": {
                        "segment": {
                            "node": {
                                "security_zone": {"node": {"id": "zone-PROD", "name": _v("PROD")}},
                                "owner": _owner(segment_owner),
                                "gateway": {
                                    "node": {
                                        "ip_prefix": {
                                            "node": {
                                                "prefix": _v(prefix),
                                                "ip_namespace": {"node": {"name": {"value": "default"}}},
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
