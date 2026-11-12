"""LISP rendering of the control-plane-neutral overlay model (transforms/helpers/overlay.py)."""

import ipaddress
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from transforms.helpers.addressing import host_ip
from transforms.helpers.overlay import _DEFAULT_ANYCAST_GATEWAY_MAC, build_overlay_node, select_locator
from transforms.helpers.segments import _get_segment_gateways
from transforms.helpers.vrf import device_vrfs
from utils.fabric_roles import LISP_BORDER_ROLES
from utils.limits import DEFAULT_NAMESPACE
from utils.segment_addressing import segment_namespace, segment_prefix


@dataclass(frozen=True)
class EidRecord:
    """One L3 EID prefix a border registers: ``instance_id`` is the VRF's L3 instance-id."""

    instance_id: int
    vrf: str
    prefix: str


@dataclass(frozen=True)
class LispInputs:
    """What the LISP renderer needs beyond the neutral overlay model: this fabric's LISP state."""

    map_servers: tuple[str, ...]
    site_name: str | None
    site_key: str | None
    l3_records: tuple[EidRecord, ...]
    l2_instance_ids: tuple[int, ...]
    wan_exit: bool = False


def _is_border(device_role: str) -> bool:
    """The role that is the co-located map-server/map-resolver of its site: the office's distribution-switch."""
    return device_role in LISP_BORDER_ROLES


def _map_server_addresses(servers: Iterable[dict[str, Any]]) -> tuple[str, ...]:
    """The loopback address of each map-server device, sorted by device name."""
    addresses: list[str] = []
    for server in sorted(servers, key=lambda s: s.get("name") or ""):
        address = next(
            (
                host_ip(iface["ip_address"]["address"])
                for iface in server.get("interfaces") or []
                if (iface.get("ip_address") or {}).get("address")
            ),
            None,
        )
        if address is None:
            raise ValueError(
                f"LISP map-server {server.get('name')} has no loopback address; refusing incomplete config"
            )
        addresses.append(address)
    return tuple(addresses)


def _border_records(segment_deployments: Iterable[dict[str, Any]]) -> tuple[tuple[EidRecord, ...], tuple[int, ...]]:
    """The L3 EID records and L2 instance-ids of the site's active VNI segments (what a border registers)."""
    l3_records: set[EidRecord] = set()
    l2_ids: set[int] = set()
    for dep in segment_deployments:
        segment = dep.get("segment") or {}
        vni = dep.get("vni")
        if not vni:
            continue
        l2_ids.add(vni)
        # IPv4 only by design: the LISP renderer has no `service ipv6` and refuses an IPv6 segment itself.
        if segment_prefix(segment, "ipv6"):
            raise ValueError(
                f"LISP segment {segment.get('customer_name') or segment.get('name')} has an IPv6 prefix "
                "(service ipv6 is not rendered); refusing incomplete config"
            )
        namespace = segment_namespace(segment)
        prefix = segment_prefix(segment, "ipv4")
        if prefix and namespace.get("name") not in (None, DEFAULT_NAMESPACE) and namespace.get("l3_vni"):
            l3_records.add(EidRecord(instance_id=namespace["l3_vni"], vrf=namespace["name"], prefix=prefix))
    return (
        tuple(sorted(l3_records, key=lambda r: (r.instance_id, r.prefix))),
        tuple(sorted(l2_ids)),
    )


def build_lisp_inputs(deployment: Any, device_role: str) -> LispInputs:
    """This device's LISP inputs, read off its own deployment: the office, which hosts its devices directly."""
    office = deployment if isinstance(deployment, dict) else {}
    site_key = (office.get("lisp_site_key") or {}).get("password")
    border = _is_border(device_role)
    l3_records, l2_ids = _border_records(office.get("segment_deployments") or []) if border else ((), ())
    return LispInputs(
        map_servers=_map_server_addresses(office.get("lisp_map_servers") or []),
        site_name=office.get("name") if border else None,
        site_key=site_key,
        l3_records=l3_records,
        l2_instance_ids=l2_ids,
        # The office's SD-WAN edge is the way out (add_office_fabric builds the BGP session to it).
        wan_exit=bool(office.get("lisp_wan_edges")),
    )


def _eid_prefix(gateway_ip: str) -> str:
    """The subnet of a gateway address ("10.1.0.1/24" -> "10.1.0.0/24")."""
    return str(ipaddress.ip_interface(gateway_ip).network)


def _refuse(role: str, why: str) -> ValueError:
    return ValueError(f"cisco_xe {role} cannot render the LISP overlay: {why}; refusing incomplete config")


def _refuse_unroutable_gateways(device_role: str, activations: list[dict[str, Any]]) -> None:
    """Raise for a segment whose SVI the LISP overlay cannot carry, VNI-bearing or not."""
    for act in activations:
        gateway_ip, gateway_ipv6, vrf, l3_vni = _get_segment_gateways(act.get("segment") or {})
        if gateway_ipv6:
            raise _refuse(device_role, f"VLAN {act.get('vlan_id')} has an IPv6 gateway (service ipv6 is not rendered)")
        if gateway_ip and not (vrf and l3_vni):
            raise _refuse(device_role, f"the gateway of VLAN {act.get('vlan_id')} is in no VRF with an L3 instance-id")


def get_lisp_config(
    data: dict[str, Any],
    device_role: str,
    activations: list[dict[str, Any]] | None,
    lisp_inputs: LispInputs,
    fabric_anycast_mac: str | None = None,
) -> dict[str, Any]:
    """The ``lisp`` template context of one switch of an office's LISP site (access-switch or distribution-switch)."""
    border = _is_border(device_role)
    vrfs = device_vrfs(data)
    node = build_overlay_node(
        data.get("interfaces", []),
        device_role,
        activations or [],
        vrfs,
        # The way out of the site: a border with a WAN exit is the proxy-ETR/ITR of every VRF it has.
        frozenset(ns["name"] for ns in vrfs) if lisp_inputs.wan_exit and border else frozenset(),
        fabric_anycast_mac or _DEFAULT_ANYCAST_GATEWAY_MAC,
    )
    if not lisp_inputs.map_servers:
        raise _refuse(device_role, "the fabric has no map-server (distribution-switch)")
    if not lisp_inputs.site_key:
        raise _refuse(device_role, "the fabric has no LISP site key")
    if border and not lisp_inputs.site_name:
        raise _refuse(device_role, "the fabric has no name for the LISP site")

    locator = select_locator(data.get("interfaces", []))
    if lisp_inputs.wan_exit and border and not locator.address:
        raise _refuse(device_role, "the WAN exit needs an address on the locator loopback (proxy-ITR)")

    _refuse_unroutable_gateways(device_role, activations or [])
    l2s = node.l2 if node else ()
    l3s = node.l3 if node else ()
    prefixes: dict[str, set[str]] = {l3.vrf: set() for l3 in l3s}
    for l2 in l2s:
        if l2.gateway_ip and l2.vrf in prefixes:
            prefixes[l2.vrf].add(_eid_prefix(l2.gateway_ip))

    l3_instances = [{"instance_id": l3.instance_id, "vrf": l3.vrf, "prefixes": sorted(prefixes[l3.vrf])} for l3 in l3s]
    if lisp_inputs.wan_exit and border:
        l3_instances.sort(key=lambda instance: instance["instance_id"])

    gateway = node.gateway if node else None
    return {
        "enabled": True,
        "role": "border" if border else "edge",
        "rloc_interface": locator.interface or "Loopback0",
        "rloc_address": locator.address,
        "wan_exit": lisp_inputs.wan_exit,
        "map_servers": list(lisp_inputs.map_servers),
        "site_key": lisp_inputs.site_key,
        "l2_instances": [{"instance_id": l2.instance_id, "vlan_id": l2.vlan_id, "name": l2.name} for l2 in l2s],
        "l3_instances": l3_instances,
        "anycast_gateway": {
            "enabled": bool(gateway and gateway.enabled),
            "mac": gateway.mac if gateway else fabric_anycast_mac or _DEFAULT_ANYCAST_GATEWAY_MAC,
        },
        "site": (
            {
                "name": lisp_inputs.site_name,
                "l3_records": [
                    {"instance_id": r.instance_id, "vrf": r.vrf, "prefix": r.prefix} for r in lisp_inputs.l3_records
                ],
                "l2_instance_ids": list(lisp_inputs.l2_instance_ids),
            }
            if border
            else None
        ),
    }
