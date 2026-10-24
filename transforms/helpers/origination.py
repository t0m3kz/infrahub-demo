"""Routing identity on the routes a VTEP originates: which segment prefixes carry which standard communities.

A tagged segment's SVI prefix is redistributed into EVPN from its tenant VRF; the platform templates add the
customer community (``64496:<customer tag>``) and the segment community (``64497:<segment tag>``) to that route
with a route-map sequence per entry returned here (``origination``). The community is a tag for visibility
(utils/routing_identity.py), never a filter: the model-derived prefix-lists stay the gate.
"""

from collections.abc import Iterable
from typing import Any

from transforms.helpers.segments import _get_segment_gateways, routed_activations
from utils.routing_identity import list_name_part, routing_communities, routing_tag


def get_origination(activations: list[dict[str, Any]] | None, vrfs: Iterable[str]) -> list[dict[str, Any]]:
    """The routed IPv4 segment prefixes of ``activations`` that originate with routing-identity communities.

    One entry per tagged segment, sorted by name: ``name`` (the segment name, safe in a prefix-list name),
    ``vrf``, ``prefix`` (the gateway's subnet, the route redistributed into EVPN) and ``communities`` (customer
    first, then segment; only the tags that exist). A segment is skipped when it has neither tag, when the
    fabric does not route it (terminate_inline, no gateway, IPv6) or when its VRF is not one of ``vrfs`` (the
    VRFs that redistribute into EVPN), so an untagged device renders exactly as before.

    ``activations`` are the cleaned segment activations of the device (``segment_deployments``); a segment's
    ``routing_tag`` and its owner's come from the query (queries/fragments/network_segment.gql).
    """
    evpn_vrfs = set(vrfs)
    entries: dict[str, dict[str, Any]] = {}
    for activation in routed_activations(activations):
        segment = activation.get("segment") or {}
        name = segment.get("name")
        gateway_ip, _, vrf, _ = _get_segment_gateways(segment)
        prefix = (((segment.get("gateway") or {}).get("ip_prefix")) or {}).get("prefix")
        communities = routing_communities(routing_tag(segment.get("owner")), routing_tag(segment))
        if name and gateway_ip and prefix and vrf in evpn_vrfs and communities:
            entries[name] = {
                "name": list_name_part(name),
                "vrf": vrf,
                "prefix": prefix,
                "communities": communities,
            }
    return [entries[name] for name in sorted(entries)]
