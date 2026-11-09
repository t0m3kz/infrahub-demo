"""The session add_virtual_circuit builds on one handoff circuit (test_62c): a /31 from the namespace's pool, our end
the lower host address, a directly connected eBGP session with BFD."""

import ipaddress
from typing import Any

from utils.routing_domain import RoutingDomainError

from .interconnect_rules import (
    _address,
    _interface_zone,
    _interfaces,
    _p2p_errors,
    _process_nodes,
    _session_errors,
    _vlan,
)

LABEL_HANDOFF = "Handoff"
LINK_PREFIX_LENGTH = 31


def routed_session_errors(
    name: str,
    circuit: dict[str, Any],
    session: dict[str, Any] | None,
    *,
    pools: list[Any],
    prefixes: list[dict[str, Any]] | None,
    party: tuple[str, int] | None,
) -> list[str]:
    """Why the `<name>-bgp` session of one handoff circuit is not what add_virtual_circuit plans.

    ``circuit`` is the inventory entry (tests/integration/circuit_helpers.py), ``session`` the ManagedBGPPeering node of
    SESSIONS_QUERY (None: there is none), ``pools`` the networks of the circuit's namespace pool, ``prefixes`` the
    IpamPrefix nodes described `Handoff <name>` and ``party`` the (device, ASN)
    declared for the far stub, None when both processes are generated.

    One eBGP session (TTL 1, BFD) on the circuit's two ends, `regular`, IPv4 unicast, the circuit's key; both ends in
    the circuit's namespace on one /31, our end taking the lower host address; processes on the two end devices, in two ASNs, the party's the data's.
    """
    session_label = f"{name}-bgp"
    if session is None:
        return [f"{name}: no {session_label} session"]
    errors = _session_errors(
        session,
        {
            "status": "active",
            "peering_role": "regular",
            "session_type": "EBGP",
            "ttl": 1,
            "bfd_enabled": True,
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
        try:
            namespace = _interface_zone(node)
        except RoutingDomainError as exc:
            errors.append(f"{session_label}: {end[0]}:{end[1]} {exc}")
            continue
        if namespace != circuit["security_zone"]:
            errors.append(
                f"{session_label}: {end[0]}:{end[1]} is addressed in '{namespace}', expected '{circuit['security_zone']}'"
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
        if not str(node.get("description", {}).get("value") or "").startswith(f"{LABEL_HANDOFF} {name} to "):
            errors.append(
                f"{session_label}: {end[0]}:{end[1]} description '{node.get('description', {}).get('value')}'"
            )
        if circuit["circuit_type"] == "handoff":
            parent = ((node.get("parent_interface") or {}).get("node") or {}).get("name", {}).get("value")
            if parent != by_end[end]["parent_name"] or end[1] != f"{parent}.{_vlan(end[1])}":
                errors.append(f"{session_label}: {end[0]}:{end[1]} is not <parent>.<vlan> of its parent ({parent})")
    addresses = {end: _address(node) for end, node in ports.items()}
    errors.extend(_p2p_errors(session_label, list(addresses.values()), LINK_PREFIX_LENGTH, pools, prefixes))

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
    # our end (the overlay process), else the first device by name, takes the lower address of the link prefix
    ours = min(devices, key=lambda device: (processes[device]["process_role"]["value"] != "overlay", device))
    our_end = next(end for end in addresses if end[0] == ours)
    if all(addresses.values()):
        lowest = min(addresses, key=lambda end: ipaddress.IPv4Interface(str(addresses[end])))
        if lowest != our_end:
            errors.append(
                f"{session_label}: {our_end[0]} (the lower end) has {addresses[our_end]}, not the lower address of "
                f"the /{LINK_PREFIX_LENGTH}"
            )
    return errors
