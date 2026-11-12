"""External BGP session policy helpers: what an external (tenant VRF, handoff) session accepts and is announced."""

import logging
from collections.abc import Iterable, Mapping
from typing import Any

from transforms.helpers.bgp import _peering_ends
from transforms.helpers.bgp_cloud import CloudLeg
from utils.exchange_transit import EXCHANGE_PEERS
from utils.internet_prefixes import sorted_prefixes
from utils.ownership import (
    CircuitOwnerError,
    circuit_owner,
    customer_owner,
    exchange_policies,
    ownership_conflicts,
)
from utils.routed_prefix import Registered, registrations
from utils.routing_domain import RoutingDomainError, interface_security_zone, resolve_vrf
from utils.routing_identity import customer_community, list_name_part
from utils.segment_addressing import segment_namespace

_log = logging.getLogger(__name__)


def _routable_ipv4(prefix: str | None) -> bool:
    """An IPv4 prefix other than the default route: what an external tenant-VRF filter may list."""
    return bool(prefix) and ":" not in str(prefix) and str(prefix).split("/")[0] != "0.0.0.0"


def get_onprem_prefixes(segments: list[dict[str, Any]] | None) -> dict[str, dict[str, list[str]]]:
    """Tenant VRF name -> owner -> the IPv4 prefixes behind that owner's on-prem segment gateways, sorted."""
    by_vrf: dict[str, dict[str, set[str]]] = {}
    for segment in segments or []:
        owner = customer_owner(segment.get("owner"))
        prefix = ((segment.get("gateway") or {}).get("ip_prefix")) or {}
        if prefix.get("member_type") == "prefix" or not _routable_ipv4(prefix.get("prefix")):
            continue
        namespace = segment_namespace(segment)
        if (
            owner
            and namespace.get("vrf_type") in EXCHANGE_PEERS
            and namespace.get("name")
            and prefix.get("member_type") != "prefix"
            and _routable_ipv4(prefix.get("prefix"))
        ):
            by_vrf.setdefault(namespace["name"], {}).setdefault(owner, set()).add(prefix["prefix"])
    return {
        vrf: {owner: sorted_prefixes(found) for owner, found in sorted(owners.items())}
        for vrf, owners in sorted(by_vrf.items())
    }


def session_circuit_owner(circuit: dict[str, Any]) -> str | None:
    """The customer key of a cleaned session circuit, derived (the circuit stores no owner)."""
    return circuit_owner(customer_owner(location.get("owner")) for location in circuit.get("locations") or [])


def _far_end_devices(device_capabilities: list[dict[str, Any]] | None, device_name: str) -> dict[str, set[str]]:
    """Local interface name -> the devices at the far end of this device's BGP peerings on it."""
    far: dict[str, set[str]] = {}
    for service in device_capabilities or []:
        if service.get("typename") != "ManagedBGP":
            continue
        for peering in service.get("peerings") or []:
            try:
                local_iface, remote_iface = _peering_ends(peering, device_name)
            except ValueError:
                # Not two ends, one of them ours: the session builder refuses it; it has no far end to name here.
                continue
            remote = (remote_iface.get("device") or {}).get("name")
            if local_iface.get("name") and remote:
                far.setdefault(local_iface["name"], set()).add(remote)
    return far


def office_registered(office: dict[str, Any]) -> list[Registered]:
    """The (owner, prefix) pairs one office footprint registers."""
    office_owner = customer_owner(office.get("owner"))
    gateway = office.get("sdwan_gateway") or {}
    instances = [
        resolve_vrf([cap, iface], required=True, label=f"office {office.get('name')} gateway handoff")
        for iface in gateway.get("interfaces") or []
        for cap in iface.get("capabilities") or []
        if cap.get("typename") == "TopologySessionCircuit" and cap.get("circuit_type") == "handoff"
    ]
    instances = [instance for instance in instances if instance["security_zone"].get("vrf_type") == "prod"]
    if office.get("lan_prefixes") and not instances:
        raise RoutingDomainError(f"office {office.get('name')}: missing PROD gateway handoff VRF")
    registered = [
        registration
        for vrf in {instance["id"]: instance for instance in instances}.values()
        for registration in registrations(office.get("lan_prefixes"), {"vrf": vrf}, customer=office_owner)
    ]
    for deployment in office.get("segment_deployments") or []:
        segment = deployment.get("segment") or {}
        hosted = (segment.get("gateway") or {}).get("ip_prefix")
        if hosted:
            matching = [
                instance
                for instance in office.get("vrfs") or []
                if (instance.get("security_zone") or {}).get("id") == (segment.get("security_zone") or {}).get("id")
            ]
            if len(matching) != 1:
                raise RoutingDomainError(f"office {office.get('name')}: missing or ambiguous hosted segment VRF")
            registered.extend(
                registrations([hosted], {"vrf": matching[0]}, customer=customer_owner(segment.get("owner")))
            )
    return registered


def registered_by_gateway(
    offices: list[dict[str, Any]] | None, tunnels: list[dict[str, Any]] | None
) -> dict[str, list[Registered]]:
    """SD-WAN gateway name -> the (owner, prefix) pairs registered behind it, by offices and by tunnels."""
    registered_by: dict[str, list[Registered]] = {}
    for office in offices or []:
        gateway = (office.get("sdwan_gateway") or {}).get("name")
        if gateway:
            registered_by.setdefault(gateway, []).extend(office_registered(office))
    for tunnel in tunnels or []:
        try:
            tunnel_owner = session_circuit_owner(tunnel)
        except CircuitOwnerError as exc:
            _log.warning("tunnel %s: %s; its registered prefixes are accepted from nobody", tunnel.get("name"), exc)
            tunnel_owner = None
        ends = {(port.get("device") or {}).get("name") for port in tunnel.get("capabilities") or []}
        for gateway in sorted(name for name in ends if name in registered_by):
            registered_by[gateway].extend(
                registrations(tunnel.get("registered_prefixes"), tunnel, customer=tunnel_owner)
            )
    return registered_by


def registered_by_attachment(
    offices: list[dict[str, Any]] | None,
    tunnels: list[dict[str, Any]] | None,
    legs: Iterable[CloudLeg] | None,
) -> dict[str, list[Registered]]:
    """Attachment point -> the (owner, prefix) pairs registered behind it, as the edge reads them."""
    points = dict(registered_by_gateway(offices, tunnels))
    for leg in legs or []:
        points.setdefault(leg.point, []).extend(leg.registered)
    return points


def registered_by_namespace(registered: list[Registered]) -> dict[str, dict[str, set[str]]]:
    """Namespace name -> owner -> the routable IPv4 prefixes of the (owner, IpamPrefix) pairs registered there."""
    found: dict[str, dict[str, set[str]]] = {}
    for registration in registered:
        owner, prefix = registration.customer, registration.prefix
        namespace = registration.zone.get("name")
        if owner and namespace and _routable_ipv4(prefix.get("prefix")):
            found.setdefault(namespace, {}).setdefault(owner, set()).add(prefix["prefix"])
    return found


def get_external_peer_prefixes(
    interfaces: list[dict[str, Any]] | None,
    device_name: str,
    *,
    device_capabilities: list[dict[str, Any]] | None,
    offices: list[dict[str, Any]] | None,
    tunnels: list[dict[str, Any]] | None,
    cloud_legs: Mapping[str, CloudLeg] | None = None,
) -> dict[str, dict[str, list[str]]]:
    """Handoff sub-interface name -> owner -> the far end's registered IPv4 prefixes of that owner, sorted."""
    registered_behind = registered_by_gateway(offices, tunnels)
    far_ends = _far_end_devices(device_capabilities, device_name)

    found: dict[str, dict[str, list[str]]] = {}
    for iface in interfaces or []:
        name = iface.get("name") or ""
        far = far_ends.get(name) or set()
        if len(far) == 1 and next(iter(far)) in registered_behind:
            registered = registered_behind[next(iter(far))]
        elif name in (cloud_legs or {}):
            registered = list((cloud_legs or {})[name].registered)
        else:
            continue
        namespace = interface_security_zone(iface).get("name")
        by_owner = registered_by_namespace(registered).get(namespace or "", {})
        found[name] = {owner: sorted_prefixes(prefixes) for owner, prefixes in sorted(by_owner.items())}
    return found


def get_external_session_policies(
    interfaces: list[dict[str, Any]] | None,
    peer_prefixes: dict[str, dict[str, list[str]]],
    onprem_prefixes: dict[str, dict[str, list[str]]],
    *,
    device_name: str = "",
    customer_tags: Mapping[str, int] | None = None,
    routing_identity: bool = False,
    cloud_legs: Mapping[str, CloudLeg] | None = None,
) -> dict[str, dict[str, Any]]:
    """Handoff sub-interface name -> the prefix policy of its external session."""
    namespaces = {iface.get("name"): interface_security_zone(iface).get("name") for iface in interfaces or []}
    # One group per namespace; a session without one stands alone, it has no on-prem space to exchange.
    groups: dict[tuple[str | None, str | None], dict[str, dict[str, list[str]]]] = {}
    for name, by_owner in peer_prefixes.items():
        namespace = namespaces.get(name)
        groups.setdefault((namespace, None) if namespace else (None, name), {})[name] = by_owner

    legs = cloud_legs or {}
    session_groups = {name: leg.point for name, leg in legs.items()}
    announced = {name: [p for p in leg.advertised if _routable_ipv4(p)] for name, leg in legs.items()}
    policies: dict[str, dict[str, Any]] = {}
    for (namespace, _), learned in groups.items():
        segments = onprem_prefixes.get(namespace or "", {})
        for conflict in ownership_conflicts(segments, learned, session_groups=session_groups):
            _log.warning("%s: namespace %s: %s", device_name, namespace or "<none>", conflict.message)
        for name, policy in exchange_policies(segments, learned, announced=announced).items():
            policies[name] = {
                "owners": sorted(policy["owners"]),
                "in": sorted_prefixes(policy["in"]),
                "out": sorted_prefixes(policy["out"]),
            }
            if routing_identity:
                policies[name].update(identity=True, in_owners=_in_owners(learned[name], customer_tags or {}))
    return {name: policies[name] for name in peer_prefixes}


def _in_owners(by_owner: Mapping[str, list[str]], customer_tags: Mapping[str, int]) -> list[dict[str, Any]]:
    """The inbound policy entries of one session: its owners with a prefix, each with its customer community."""
    return [
        {
            "owner": owner,
            "name": list_name_part(owner),
            "prefixes": sorted_prefixes(prefixes),
            "community": customer_community(customer_tags[owner]) if owner in customer_tags else None,
        }
        for owner, prefixes in sorted(by_owner.items())
        if prefixes
    ]
