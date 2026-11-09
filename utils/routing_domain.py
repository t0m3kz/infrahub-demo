"""Resolve routing authority from owners, never from IPAM identity or address values."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

VRF_KIND = "ManagedVRF"
"""The kind of a VRF instance: a shared device capability, and an interface capability of the interfaces in it."""

INTERFACE_CAPABILITY_KEYS = ("zone_capabilities", "capabilities")
"""The keys of an interface's ``capabilities`` in cleaned query data: queries/fragments/routing_zone.gql selects the VRF
and the session circuits under the alias ``zone_capabilities``, so they never collide with a query's own
``capabilities`` selection (the server answers only the first selection of a field)."""


class RoutingDomainError(ValueError):
    """Missing or conflicting routing ownership."""


def interface_vrfs(interface: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The VRF instances of an interface: the ``ManagedVRF`` nodes among its capabilities, each once (by id).

    An interface is in at most one VRF (checks/vrf_instances.py); a reader takes ``interface_vrf``. A query that selects
    the interface's own ``capabilities`` next to ``zone_capabilities`` sees the instance there too, often with nothing
    but its typename: an entry with no id counts only when no entry has one."""
    entries = [
        cap
        for key in INTERFACE_CAPABILITY_KEYS
        for cap in interface.get(key) or []
        if isinstance(cap, dict) and cap.get("typename") == VRF_KIND
    ]
    with_id = {str(cap["id"]): cap for cap in reversed(entries) if cap.get("id")}
    if with_id:
        return [with_id[key] for key in dict.fromkeys(str(cap["id"]) for cap in entries if cap.get("id"))]
    return entries[:1]


def interface_vrf(interface: Mapping[str, Any] | None) -> dict[str, Any]:
    """The VRF instance of an interface (the ``ManagedVRF`` among its capabilities), ``{}`` when it has none.

    Two instances on one interface raise: the routing table of the interface would be ambiguous."""
    vrfs = interface_vrfs(interface or {})
    if len(vrfs) > 1:
        raise RoutingDomainError(f"{(interface or {}).get('name') or 'interface'}: in more than one VRF instance")
    return vrfs[0] if vrfs else {}


def resolve_vrf(
    owners: Iterable[Mapping[str, Any] | None], *, required: bool = False, label: str = "routed object"
) -> dict[str, Any]:
    """Resolve an explicit scoped instance; IPAM and parent interfaces supply no authority.

    An owner's instance is its ``vrf`` (a session circuit, a static route, a registration) or, for an interface, the
    ``ManagedVRF`` among its capabilities (``interface_vrf``)."""
    instances = [vrf for owner in owners if owner if (vrf := owner.get("vrf") or interface_vrf(owner))]
    identities = {vrf.get("id") for vrf in instances if vrf.get("id")}
    if len(identities) > 1:
        raise RoutingDomainError(f"{label}: conflicting VRF instances")
    merged: dict[str, Any] = {}
    for vrf in instances:
        for key, value in vrf.items():
            if value is not None:
                if key in merged and merged[key] != value:
                    raise RoutingDomainError(f"{label}: conflicting VRF {key}")
                merged[key] = value
    if required and not merged.get("id"):
        raise RoutingDomainError(f"{label}: missing VRF instance")
    if merged:
        resolve_security_zone([merged], required=True, label=label)
    return merged


def resolve_security_zone(
    owners: Iterable[Mapping[str, Any] | None], *, required: bool = False, label: str = "routed object"
) -> dict[str, Any]:
    """Merge explicit owner zones; reject disagreement and incomplete tenant authority.

    Owners are VRF instances or policy segments with a native ``security_zone`` relationship.
    Underlay objects have no zone and resolve to an empty mapping. IPAM namespace,
    pool names and CIDRs are deliberately not consulted.
    """
    zones = [zone for owner in owners if owner if (zone := (owner.get("vrf") or owner).get("security_zone"))]
    identities = {(zone.get("id"), zone.get("name")) for zone in zones}
    ids = {identity[0] for identity in identities if identity[0]}
    names = {identity[1] for identity in identities if identity[1]}
    if len(ids) > 1 or len(names) > 1:
        raise RoutingDomainError(f"{label}: conflicting security zones")
    merged: dict[str, Any] = {}
    for zone in zones:
        for key, value in zone.items():
            if value is not None:
                if key in merged and merged[key] != value:
                    raise RoutingDomainError(f"{label}: conflicting security zone {key}")
                merged[key] = value
    if required and not merged.get("name"):
        raise RoutingDomainError(f"{label}: missing security zone")
    return merged


def interface_security_zone(interface: Mapping[str, Any], *, required: bool = False) -> dict[str, Any]:
    """Read an interface's local VRF; a circuit validates its zone, not its scoped identity.

    A parent port is not an owner: different sub-interfaces legitimately carry
    different VRFs. A remote deployment realizes the same zone in a different instance.
    """
    vrf = interface_vrf(interface)
    capabilities = [cap for key in INTERFACE_CAPABILITY_KEYS for cap in interface.get(key) or []]
    circuits = [
        cap
        for cap in capabilities
        if cap.get("typename") == "TopologySessionCircuit" and cap.get("circuit_type") in {"handoff", "vpn"}
    ]
    owners = [vrf, *((circuit.get("vrf") or {}) for circuit in circuits)]
    tenant = required or bool(circuits) or interface.get("role") in {"svi", "l3vni"}
    if tenant and not vrf:
        raise RoutingDomainError(f"{interface.get('name') or 'interface'}: missing VRF instance")
    return resolve_security_zone(owners, required=tenant, label=str(interface.get("name") or "interface"))


def prefix_security_zone(prefix: Mapping[str, Any], *owners: Mapping[str, Any] | None) -> dict[str, Any]:
    """A neutral prefix's routing context comes exclusively from its non-IPAM owners."""
    vrf = resolve_vrf(owners, required=True, label=str(prefix.get("prefix") or "prefix"))
    return resolve_security_zone([vrf], required=True, label=str(prefix.get("prefix") or "prefix"))
