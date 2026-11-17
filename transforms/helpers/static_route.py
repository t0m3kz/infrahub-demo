"""The static routes of a device, read from its ``ManagedStaticRoute`` capabilities."""

from __future__ import annotations

import ipaddress
from typing import Any

STATIC_ROUTE_KIND = "ManagedStaticRoute"


def get_static_routes(device_capabilities: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """[{vrf, l3_vni, prefix, nexthop}] sorted, ``local_only."""
    routes: dict[tuple[str, str, str], dict[str, Any]] = {}
    for cap in device_capabilities or []:
        if cap.get("typename") != STATIC_ROUTE_KIND:
            continue
        namespace = ((cap.get("vrf") or {}).get("security_zone")) or {}
        vrf = namespace.get("name")
        label = f"static route '{cap.get('name')}' ({cap['prefix']} via {cap['next_hop']})"
        # The templates render a static in a VRF with the default distance; refuse the rest rather than drop it.
        if not vrf:
            raise ValueError(
                f"{label} is in the default table: only VRF statics are rendered; refusing incomplete config"
            )
        # IPv4 by design: these are the exchange statics through a firewall context, whose legs and next hops are IPv4.
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
