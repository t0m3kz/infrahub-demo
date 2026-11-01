"""The session add_virtual_circuit builds on one handoff or vpn circuit, judged against its flavour (test_62c, test_67).

One implementation for every routed link: the SD-WAN gateway's handoff and the circuits add_provider_connection
generates. The flavour (utils/provider_services.py, restated by all_demo_provider_connection_constants) decides where
the link prefix comes from, its length, which end takes the lower host address and the session's TTL.
"""

import ipaddress
from typing import Any, NamedTuple

from .interconnect_rules import (
    _address,
    _address_namespace,
    _interfaces,
    _p2p_errors,
    _process_nodes,
    _session_errors,
    _vlan,
)

LABEL_HANDOFF = "Handoff"
LABEL_VPN = "VPN"
END_US = "us"
END_PROVIDER = "provider"


class LinkFlavour(NamedTuple):
    """What a routed link's addressing and session look like."""

    prefix_length: int = 31
    #: The end that takes the lower host address: "us" (the overlay-process end) or "provider".
    lower_end: str = END_US
    ttl: int = 1
    bfd: bool = True
    #: The provider assigned the link range (the circuit's `link_prefix`): nothing is allocated from our pool.
    provider_assigned: bool = False


#: A handoff with no provider connection: a /31 from our pool, our end the lower address, directly connected.
DEFAULT_LINK_FLAVOUR = LinkFlavour()


def link_hosts(network: ipaddress.IPv4Network | ipaddress.IPv6Network) -> tuple[Any, Any]:
    """The (lower, upper) host addresses of a link prefix: a /31 has no network address (RFC 3021), a /30 or /29 does."""
    base = network.network_address
    first = base if network.prefixlen == 31 else base + 1
    return first, first + 1


def routed_session_errors(
    name: str,
    circuit: dict[str, Any],
    session: dict[str, Any] | None,
    *,
    label: str,
    flavour: LinkFlavour,
    pools: list[Any],
    prefixes: list[dict[str, Any]] | None,
    party: tuple[str, int] | None,
) -> list[str]:
    """Why the `<name>-bgp` session of one handoff/vpn circuit is not what its flavour plans.

    ``circuit`` is the inventory entry (tests/integration/circuit_helpers.py), ``session`` the ManagedBGPPeering node of
    SESSIONS_QUERY (None: there is none), ``pools`` the networks of the circuit's namespace pool, ``prefixes`` the
    IpamPrefix nodes described `<label> <name>` (checked for an our-pool flavour only) and ``party`` the (device, ASN)
    declared for the far stub, None when both processes are generated.

    One eBGP session (multihop when the TTL is above 1) on the circuit's two ends, `regular`, IPv4 unicast, the
    circuit's key; both ends in the circuit's namespace on one link prefix, whose hosts are (lower, upper): the
    flavour's lower end takes the lower one; processes on the two end devices, in two ASNs, the party's the data's.
    """
    session_label = f"{name}-bgp"
    if session is None:
        return [f"{name}: no {session_label} session"]
    errors = _session_errors(
        session,
        {
            "status": "active",
            "peering_role": "regular",
            "session_type": "EBGP" if flavour.ttl == 1 else "EBGP_MULTIHOP",
            "ttl": flavour.ttl,
            "bfd_enabled": flavour.bfd,
            "send_extended_community": False,
            "key": circuit["password"],
            "families": [("ipv4", "unicast")],
        },
    )

    # the two end interfaces: the circuit's ends, one namespace, one link prefix
    ports = _interfaces(session)
    if sorted(ports) != circuit["ends"]:
        return [*errors, f"{session_label}: on interfaces {sorted(ports)}, expected {circuit['ends']}"]
    by_end = {(end["device"], end["interface"]): end for end in circuit["end_nodes"]}
    for end, node in sorted(ports.items()):
        namespace = _address_namespace(node)
        if namespace != circuit["ip_namespace"]:
            errors.append(
                f"{session_label}: {end[0]}:{end[1]} is addressed in '{namespace}', expected '{circuit['ip_namespace']}'"
            )
        if _address(node) != by_end[end]["address"]:
            errors.append(
                f"{session_label}: {end[0]}:{end[1]} address {_address(node)} differs from the circuit's end "
                f"{by_end[end]['address']}"
            )
        if node.get("status", {}).get("value") != "active":
            errors.append(
                f"{session_label}: {end[0]}:{end[1]} status {node.get('status', {}).get('value')}, expected active"
            )
        if not str(node.get("description", {}).get("value") or "").startswith(f"{label} {name} to "):
            errors.append(
                f"{session_label}: {end[0]}:{end[1]} description '{node.get('description', {}).get('value')}'"
            )
        if circuit["circuit_type"] == "handoff":
            parent = ((node.get("parent_interface") or {}).get("node") or {}).get("name", {}).get("value")
            if parent != by_end[end]["parent_name"] or end[1] != f"{parent}.{_vlan(end[1])}":
                errors.append(f"{session_label}: {end[0]}:{end[1]} is not <parent>.<vlan> of its parent ({parent})")
    addresses = {end: _address(node) for end, node in ports.items()}
    if flavour.provider_assigned:
        errors.extend(_assigned_errors(session_label, circuit, flavour, list(addresses.values())))
    else:
        errors.extend(
            _p2p_errors(
                session_label,
                list(addresses.values()),
                flavour.prefix_length,
                pools,
                prefixes,
            )
        )

    # processes: one per device; the remote ASN is the party stub's local_as, ours differs (eBGP)
    processes = _process_nodes(session)
    devices = sorted(device for device, _ in circuit["ends"])
    if sorted(processes) != devices:
        return [*errors, f"{session_label}: runs on processes of {sorted(processes)}, expected {devices}"]
    asns = {device: node["local_as"]["node"]["asn"]["value"] for device, node in processes.items()}
    if len(set(asns.values())) != len(asns):
        errors.append(
            f"{session_label}: both processes are in AS {sorted(set(asns.values()))}, an eBGP session needs two"
        )
    if party is not None:
        device, asn = party
        if asns.get(device) != asn:
            errors.append(f"{session_label}: party {device} is in AS {asns.get(device)}, expected the data's AS{asn}")
    # our end (the overlay process), else the first device by name, takes the flavour's end of the link prefix
    ours = min(devices, key=lambda device: (processes[device]["process_role"]["value"] != "overlay", device))
    our_end = next(end for end in addresses if end[0] == ours)
    if all(addresses.values()):
        ordered = sorted(addresses, key=lambda end: ipaddress.IPv4Interface(str(addresses[end])))
        lowest = ordered[0]
        wanted = our_end if flavour.lower_end == END_US else next(end for end in addresses if end != our_end)
        if lowest != wanted:
            side = "the lower end" if flavour.lower_end == END_US else "the provider's end, the lower one"
            errors.append(
                f"{session_label}: {wanted[0]} ({side}) has {addresses[wanted]}, not the lower address of the "
                f"/{flavour.prefix_length}"
            )
    return errors


def _assigned_errors(
    session_label: str, circuit: dict[str, Any], flavour: LinkFlavour, addresses: list[str | None]
) -> list[str]:
    """Why the two addresses are not the two hosts of the circuit's `link_prefix`, a /N the provider assigned."""
    link_prefix = circuit["link_prefix"]
    if not link_prefix:
        return [f"{session_label}: a provider-assigned link has no link_prefix on its circuit"]
    network = ipaddress.ip_network(link_prefix)
    errors: list[str] = []
    if network.prefixlen != flavour.prefix_length:
        errors.append(f"{session_label}: link_prefix {link_prefix} is not a /{flavour.prefix_length}")
    if any(address is None for address in addresses) or len(addresses) != len(set(addresses)):
        return [*errors, f"{session_label}: interface addresses {addresses} are not two"]
    hosts = set(link_hosts(network))
    found = {ipaddress.ip_interface(str(address)) for address in addresses}
    if {interface.ip for interface in found} != hosts or any(interface.network != network for interface in found):
        errors.append(
            f"{session_label}: addresses {sorted(str(i) for i in found)} are not the hosts {sorted(map(str, hosts))} of "
            f"{link_prefix}"
        )
    return errors
