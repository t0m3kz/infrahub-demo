"""The static routes of a device, read from its ``ManagedStaticRoute`` capabilities.

The generator (generators/exchange_routes.py) writes them from the exchange of each firewall and load-balancer context;
a person may add more (``origin: manual``). The templates take one platform-neutral list, so the transform renders
exactly the objects, with no second derivation.

A route the templates cannot render (the default table, a distance other than 1, IPv6) raises ``ValueError`` and the artifact
is refused, so a person's route is never dropped without a word.
"""

from __future__ import annotations

import ipaddress
from typing import Any

STATIC_ROUTE_KIND = "ManagedStaticRoute"


def get_static_routes(device_capabilities: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """[{vrf, l3_vni, prefix, nexthop}] sorted, ``local_only: True`` added when the route is not exported into EVPN."""
    routes: dict[tuple[str, str, str], dict[str, Any]] = {}
    for cap in device_capabilities or []:
        if cap.get("typename") != STATIC_ROUTE_KIND:
            continue
        namespace = ((cap.get("vrf") or {}).get("namespace")) or {}
        vrf = namespace.get("name")
        label = f"static route '{cap.get('name')}' ({cap['prefix']} via {cap['next_hop']})"
        # The templates render a static in a VRF with the default distance; refuse the rest rather than drop it.
        if not vrf:
            raise ValueError(
                f"{label} is in the default table: only VRF statics are rendered; refusing incomplete config"
            )
        if ipaddress.ip_network(cap["prefix"], strict=False).version != 4:
            raise ValueError(f"{label} is not IPv4: only IPv4 statics are rendered")
        if (cap.get("distance") or 1) != 1:
            raise ValueError(f"{label} has distance {cap['distance']}: only the default distance is rendered")
        route = {
            "vrf": vrf,
            "l3_vni": namespace.get("l3_vni"),
            "prefix": cap["prefix"],
            "nexthop": cap["next_hop"],
            **({} if cap.get("exported", True) else {"local_only": True}),
        }
        routes[(vrf, route["prefix"], route["nexthop"])] = route
    return sorted(routes.values(), key=lambda r: (r["vrf"], r["prefix"], r["nexthop"]))
