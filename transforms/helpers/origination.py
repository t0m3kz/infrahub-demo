"""Routing identity on the routes a VTEP originates: which segment prefixes carry which standard communities."""

from collections.abc import Iterable
from typing import Any

from transforms.helpers.segments import _get_segment_gateways, routed_activations
from utils.routing_identity import list_name_part, routing_communities, routing_tag
from utils.segment_addressing import segment_family


def get_origination(activations: list[dict[str, Any]] | None, vrfs: Iterable[str]) -> list[dict[str, Any]]:
    """The routed segment prefixes (IPv4 or IPv6) of ``activations`` that originate with routing-identity
    communities."""
    evpn_vrfs = set(vrfs)
    entries: dict[str, dict[str, Any]] = {}
    for activation in routed_activations(activations):
        segment = activation.get("segment") or {}
        name = segment.get("name")
        gateway_ip, gateway_ipv6, vrf, _ = _get_segment_gateways(segment)
        prefix = (((segment.get("gateway") or {}).get("ip_prefix")) or {}).get("prefix")
        communities = routing_communities(routing_tag(segment.get("owner")), routing_tag(segment))
        if name and (gateway_ip or gateway_ipv6) and prefix and vrf in evpn_vrfs and communities:
            entries[name] = {
                "name": list_name_part(name),
                "vrf": vrf,
                "family": segment_family(segment),
                "prefix": prefix,
                "communities": communities,
            }
    return [entries[name] for name in sorted(entries)]
