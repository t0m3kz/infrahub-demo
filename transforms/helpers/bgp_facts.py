"""The model facts the BGP route protection is derived from, read off a device's query data.

``transforms/common.py`` calls ``annotate_bgp_policy`` once per device after the template context is built: it turns the
cleaned device data and the extra query roots into the plain facts of ``utils/bgp_expectation.py``, derives each
session's expectation and composes it into ``session["policy"]`` (transforms/helpers/bgp_policy.py). The check
(checks/bgp_expectations.py) builds the same facts with the same functions, so the two cannot read the model
differently.

What the device query must carry (queries/config/*.gql): the hosting deployment's ``loopback_pool`` and
``routing_filter_enforce`` (queries/fragments/evpn_fabric.gql) and, as an extra root, the ``multisite-vip`` interfaces of
the model with their devices' loopbacks. A query without them yields no pool and no far end: the families that need
them are then not derivable and render unprotected, never as a deny-all list.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from ipaddress import IPv4Network, IPv6Network, collapse_addresses, ip_network
from typing import Any

from transforms.helpers.bgp_policy import compose_policy, name_filters
from utils.bgp_expectation import Device, DeviceAddrs, ExternalPolicy, Fabric, expect_session
from utils.exchange_transit import INTERNET_TYPE
from utils.limits import DEFAULT_NAMESPACE
from utils.segment_addressing import segment_hosting_candidates
from utils.vrf_demand import VRF_KIND, device_memberships

LOOPBACK_ROLES = frozenset({"loopback", "loopback-vtep"})
VIP_ROLE = "multisite-vip"
VIP_ROOT = "DcimVirtualInterface"
ENVIRONMENT_PREFIX_ROOT = "environment_prefixes_"
SEGMENT_ROOT = "ManagedVxlanSegment"


def _address(interface: Mapping[str, Any]) -> str | None:
    """The address of a cleaned interface node, with its mask."""
    address = (interface.get("ip_address") or {}).get("address")
    return address if isinstance(address, str) and address else None


def device_addrs(interfaces: Iterable[Mapping[str, Any]] | None) -> DeviceAddrs:
    """A device's own loopback and multisite-VIP addresses, read off its interfaces."""
    loopbacks: set[str] = set()
    vips: set[str] = set()
    for interface in interfaces or []:
        address = _address(interface)
        if address and interface.get("role") in LOOPBACK_ROLES:
            loopbacks.add(address)
        elif address and interface.get("role") == VIP_ROLE:
            vips.add(address)
    return DeviceAddrs(frozenset(loopbacks), frozenset(vips))


def _hosting(deployment: Any) -> dict[str, Any]:
    """The device's hosting deployment as the query returned it: the first of its deployment and that deployment's
    parent that carries ``routing_filter_enforce`` (only a TopologySegmentHosting does: a DC, a metro, an office).

    Found by the attribute and not by ``typename``, which the device queries do not all select. {} when the query
    selected none, so the device stages and has no pool.
    """
    return next((c for c in segment_hosting_candidates(deployment) if "routing_filter_enforce" in c), {})


def routing_filter_enforce(deployment: Any) -> bool:
    """Whether the hosting deployment enforces the route protection (``routing_filter_enforce``)."""
    return bool(_hosting(deployment).get("routing_filter_enforce"))


def loopback_ranges(deployment: Any) -> tuple[str, ...]:
    """The loopback pool prefixes of the hosting deployment, collapsed so overlapping ranges count once."""
    pool = _hosting(deployment).get("loopback_pool") or {}
    networks = [
        ip_network(resource["prefix"], strict=False)
        for resource in pool.get("resources") or []
        if isinstance(resource, dict) and resource.get("prefix")
    ]
    v4 = [n for n in networks if isinstance(n, IPv4Network)]
    v6 = [n for n in networks if isinstance(n, IPv6Network)]
    return tuple(str(n) for n in [*collapse_addresses(v4), *collapse_addresses(v6)])


def _vip_devices(vip_interfaces: Iterable[Mapping[str, Any]] | None) -> tuple[frozenset[str], dict[str, Device]]:
    """Every VIP address of the model and the border gateways that carry them, with their loopbacks.

    ``vip_interfaces`` is the cleaned ``DcimVirtualInterface(role: multisite-vip)`` root. A VIP on two devices (the
    edge pair of a metro) is one address and both devices carry it.
    """
    vips: set[str] = set()
    found: dict[str, tuple[str, set[str], set[str]]] = {}
    for interface in vip_interfaces or []:
        address = _address(interface)
        device = interface.get("device") or {}
        name = device.get("name")
        if not address or not name:
            continue
        vips.add(address)
        role, loopbacks, device_vips = found.setdefault(name, (device.get("role") or "", set(), set()))
        device_vips.add(address)
        loopbacks |= device_addrs(device.get("interfaces")).loopbacks
    devices = {
        name: Device(name, role, DeviceAddrs(frozenset(loopbacks), frozenset(device_vips)))
        for name, (role, loopbacks, device_vips) in found.items()
    }
    return frozenset(vips), devices


def environment_prefixes(extra_roots: Mapping[str, Any]) -> tuple[dict[str, frozenset[str]], frozenset[str]]:
    """The routed prefixes per environment namespace and the namespaces matched as ranges.

    Read off the ``environment_prefixes_*`` roots (queries/config/*.gql): every ``IpamPrefix`` of a PROD, NON-PROD,
    INTERNET or MANAGEMENT namespace is a prefix a VRF of that namespace may carry as an EVPN type-5 route. The
    internet namespace is matched as ranges: the SNAT /32s ride inside the public aggregate.
    """
    found: dict[str, set[str]] = {}
    ranges: set[str] = set()
    for key, prefixes in extra_roots.items():
        if not key.startswith(ENVIRONMENT_PREFIX_ROOT):
            continue
        for prefix in prefixes or []:
            namespace = prefix.get("ip_namespace") or {}
            name = namespace.get("name")
            if not name or name == DEFAULT_NAMESPACE or not prefix.get("prefix"):
                continue
            found.setdefault(name, set()).add(prefix["prefix"])
            if namespace.get("namespace_type") == INTERNET_TYPE:
                ranges.add(name)
    return {name: frozenset(prefixes) for name, prefixes in found.items()}, frozenset(ranges)


def default_exits(extra_roots: Mapping[str, Any], gateways: Iterable[str]) -> dict[str, frozenset[str]]:
    """Namespace -> the devices that originate its default route: border gateways in an internet VRF instance.

    ``gateways`` are the devices that carry a multisite VIP (utils/vrf_demand.py ``internet_exits``: only a border
    gateway exits to the internet). The instances are the ``ManagedVRF`` root.
    """
    border_gateways = set(gateways)
    exits: dict[str, set[str]] = {}
    for instance in extra_roots.get(VRF_KIND) or []:
        namespace = instance.get("namespace") or {}
        if namespace.get("namespace_type") != INTERNET_TYPE or not namespace.get("name"):
            continue
        members = {device.get("name") for device in instance.get("shared_devices") or []}
        exits.setdefault(namespace["name"], set()).update(members & border_gateways)
    return {name: frozenset(devices) for name, devices in exits.items()}


def build_fabric(
    device_data: Mapping[str, Any],
    extra_roots: Mapping[str, Any],
    *,
    external_policies: Mapping[str, Mapping[str, Any]] | None = None,
    internet_prefixes: Iterable[str] | None = None,
    unmanaged: frozenset[str] = frozenset(),
) -> Fabric:
    """The fabric facts of one device's query data."""
    vips, devices = _vip_devices(extra_roots.get(VIP_ROOT))
    namespace_prefixes, range_namespaces = environment_prefixes(extra_roots)
    return Fabric(
        loopback_ranges=loopback_ranges(device_data.get("deployment")),
        vips=vips,
        devices=devices,
        namespace_prefixes=namespace_prefixes,
        range_namespaces=range_namespaces,
        default_exits=default_exits(extra_roots, devices),
        l2vni_count=len(extra_roots.get(SEGMENT_ROOT) or []),
        external={
            name: ExternalPolicy(frozenset(policy.get("in") or []), frozenset(policy.get("out") or []))
            for name, policy in (external_policies or {}).items()
        },
        internet_prefixes=frozenset(internet_prefixes or []),
        unmanaged=unmanaged,
    )


def build_device(device_data: Mapping[str, Any], local_only: Iterable[str] = ()) -> Device:
    """The device whose sessions are derived: name, role, own loopbacks and VIPs, VRF memberships.

    ``local_only``: prefixes it holds for itself and must not originate into EVPN (a leaf's transit /29s).
    """
    namespaces = {
        (membership.get("namespace") or {}).get("name") for membership in device_memberships(dict(device_data))
    }
    return Device(
        str(device_data.get("name") or ""),
        str(device_data.get("role") or ""),
        device_addrs(device_data.get("interfaces")),
        namespaces=frozenset(name for name in namespaces if name and name != DEFAULT_NAMESPACE),
        local_only=frozenset(local_only),
    )


def annotate_bgp_policy(
    config: dict[str, Any],
    device_data: Mapping[str, Any],
    extra_roots: Mapping[str, Any],
    *,
    unmanaged: frozenset[str] = frozenset(),
) -> None:
    """Derive every session's expectation and compose it into ``session["policy"]`` of ``config["bgp"]``."""
    bgp = config.get("bgp") or []
    if not any(bgp_config.get("sessions") for bgp_config in bgp):
        return
    fabric = build_fabric(
        device_data,
        extra_roots,
        external_policies=config.get("external_policies"),
        internet_prefixes=config.get("internet_prefixes"),
        unmanaged=unmanaged,
    )
    device = build_device(device_data, local_only=config.get("leaf_transit_prefixes") or [])
    enforce = routing_filter_enforce(device_data.get("deployment"))
    for bgp_config in bgp:
        sessions = bgp_config.get("sessions") or []
        for session in sessions:
            session["policy"] = compose_policy(session, expect_session(session, device, fabric), enforce=enforce)
        name_filters(sessions)
