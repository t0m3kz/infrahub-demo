"""LB backend no-SNAT return-path PBR helpers for device transforms."""

from typing import Any


def _flatten_deployment_lb_vips(deployment: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Flatten the device-scoped LB-VIP traversal into a flat list of
    {"vip": LoadbalancerVIP dict, "ip_address": that sub-interface's own
    ip_address dict} pairs — mirrors
    transforms.helpers.firewall._flatten_deployment_firewall_contexts.

    `deployment` is this device's own `deployment` field (already cleaned),
    shaped by queries/fragments/loadbalancer_vips.gql's
    LoadbalancerVipsOnDeploymentFields fragment: deployment.lb_devices
    (aliased — a plain `devices` alongside FirewallContextsOnDeviceHostingFields's
    own differently-filtered `devices` would be a GraphQL field-merge
    conflict; role="load-balancer") -> each device's interfaces -> the
    sub-interface generators/topology/loadbalancer.py's
    LoadbalancerBackendNexthopGenerator created for a no-SNAT VIP's
    return path (ip_address = the LB's own nexthop IP on that backend
    VLAN) -> interface_capabilities -> [LoadbalancerVIP, ...]. Same
    DC-vs-pod parent nesting as the firewall fragment.
    """
    if not deployment:
        return []

    def _entries_from_device_hosting(hosting: dict[str, Any]) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        for device in hosting.get("lb_devices") or []:
            for iface in device.get("interfaces") or []:
                ip_address = iface.get("ip_address") or {}
                for cap in iface.get("interface_capabilities") or []:
                    if cap.get("typename") != "LoadbalancerVIP":
                        continue
                    found.append({"vip": cap, "ip_address": ip_address})
        return found

    entries = _entries_from_device_hosting(deployment)
    parent = deployment.get("parent")
    if parent:
        entries.extend(_entries_from_device_hosting(parent))

    deduped: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in entries:
        vip_id = entry["vip"].get("id")
        addr = entry["ip_address"].get("address")
        key = (vip_id or "", addr or "")
        if vip_id and key not in deduped:
            deduped[key] = entry
    return list(deduped.values())


def get_lb_backend_pbr_rules(
    activations: list[dict[str, Any]] | None,
    lb_vips: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Build PBR rules redirecting a no-SNAT VIP's backend pool member
    response traffic to the load balancer, on the VLAN their backend_segment
    is realized as.

    Unlike get_customer_pbr_rules (default-redirect-all + bypass list), this
    is default-pass + explicit-redirect: only traffic sourced from an active
    pool member of a snat_enabled=false VIP on this VLAN needs the LB as its
    return-path next-hop — everything else keeps using the anycast gateway
    normally. Absence of a match falls through to standard fabric routing.

    lb_nexthop is the LB's own IP on its backend_segment sub-interface (see
    generators/topology/loadbalancer.py's LoadbalancerBackendNexthopGenerator)
    — reachable from any leaf carrying that VLAN via the normal SVI/anycast
    gateway, same reasoning as get_customer_pbr_rules's fw_nexthop.
    """
    if not activations or not lb_vips:
        return []

    nexthop_by_segment: dict[str, str] = {}
    backend_ips_by_segment: dict[str, set[str]] = {}
    identity_by_segment: dict[str, tuple[str | None, str | None]] = {}

    for entry in lb_vips:
        vip = entry.get("vip") or {}
        if vip.get("snat_enabled", True):
            continue
        backend_segment = vip.get("backend_segment") or {}
        seg_id = backend_segment.get("id")
        if not seg_id:
            continue

        address = (entry.get("ip_address") or {}).get("address")
        if address and seg_id not in nexthop_by_segment:
            nexthop_by_segment[seg_id] = address.split("/")[0]

        identity_by_segment.setdefault(
            seg_id, (backend_segment.get("customer_name"), backend_segment.get("environment"))
        )

        member_ips = backend_ips_by_segment.setdefault(seg_id, set())
        for member in vip.get("members") or []:
            for pool_iface in member.get("pool_interfaces") or []:
                member_address = (pool_iface.get("ip_address") or {}).get("address")
                if member_address:
                    member_ips.add(member_address.split("/")[0])

    if not nexthop_by_segment:
        return []

    rules: list[dict[str, Any]] = []
    seen_vlans: set[int] = set()
    for act in activations:
        vlan_id = act.get("vlan_id")
        if not vlan_id or vlan_id in seen_vlans:
            continue
        seg = act.get("segment") or {}
        seg_id = seg.get("id")
        if not seg_id or seg_id not in nexthop_by_segment:
            continue
        backend_ips = sorted(backend_ips_by_segment.get(seg_id) or set())
        if not backend_ips:
            continue
        seen_vlans.add(vlan_id)

        customer_name, environment = identity_by_segment.get(seg_id, (None, None))
        rules.append(
            {
                "vlan_id": vlan_id,
                "backend_ips": backend_ips,
                "lb_nexthop": nexthop_by_segment[seg_id],
                "customer_name": customer_name or seg.get("customer_name"),
                "environment": environment or seg.get("environment"),
            }
        )

    rules.sort(key=lambda r: r["vlan_id"])
    return rules
