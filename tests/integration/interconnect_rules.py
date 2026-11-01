"""The rules the 30_all interconnect steps assert on the live data (test_62, test_62b, test_62c): session, prefix and
circuit placement errors, the policy rules of a result."""

import ipaddress
from typing import Any

from utils.exchange_transit import PROD_TYPE, TENANT_NAMESPACE_TYPES
from utils.limits import CIRCUIT_ENDS

from .all_demo_circuit_constants import (
    ALL_DEMO_FACILITY_KINDS,
    ALL_DEMO_SDWAN_GATEWAY_PUBLIC_PORT,
    ALL_DEMO_VIRTUAL_CIRCUITS,
    HANDOFF_TYPE,
    INTERCONNECT_TYPE,
    TUNNEL_TYPE,
)
from .all_demo_firewall_constants import ALL_DEMO_TRANSIT_RESOURCES, transit_namespace_type
from .all_demo_provider_connection_constants import ALL_DEMO_CIRCUIT_OWNERS, ALL_DEMO_CONNECTION_CIRCUITS, VPN_TYPE


def _rules(result: dict) -> list[dict]:
    """The SecurityPolicyRule nodes of a RULES_BY_* result, flattened."""

    def peer_name(node: dict, relationship: str) -> str | None:
        return (((node.get(relationship) or {}).get("node") or {}).get("name") or {}).get("value")

    return [
        {
            "name": node["name"]["value"],
            "action": node["action"]["value"],
            "protocol": node["protocol"]["value"],
            "port": node["port_start"]["value"],
            "disabled": node["disabled"]["value"],
            "policy": peer_name(node, "policy"),
            "source_segment": peer_name(node, "source_segment"),
            "destination_segment": peer_name(node, "destination_segment"),
            "destination_ips": {
                ipaddress.IPv4Interface(e["node"]["address"]["value"])
                for e in node["destination_ip_addresses"]["edges"]
            },
            "source_prefixes": {
                ipaddress.IPv4Network(e["node"]["prefix"]["value"]) for e in node["source_prefixes"]["edges"]
            },
        }
        for node in (edge["node"] for edge in result["SecurityPolicyRule"]["edges"])
    ]


# DCI-Technical-IPv6 (data/bootstrap/20_dci_pools.yml) — the DC fabrics run an IPv6 underlay.
DCI_POOL_NETWORK = ipaddress.IPv6Network("fd00:2200::/40")


_TRANSIT_PEER_NAMES = {"internet": "INTERNET", "management": "MANAGEMENT"}


_ZONE_KINDS = frozenset({"TopologyColocationZone", "TopologyVirtualColocation"})

_OFFICE_KIND = "TopologyCustomerOffice"

_ISP_KIND = "TopologyExternalProviders"

#: The deployments a provider hosts (a cloud region, a SaaS region, a virtual colocation, an external provider): a
#: session circuit with an end device deployed in one belongs to a provider connection (PC1 of
#: checks/provider_connections.py).
_PROVIDER_HOSTED_KINDS = frozenset(
    {"TopologyCloudRegion", "TopologySaasRegion", "TopologyVirtualColocation", _ISP_KIND}
)

_TUNNEL_INTERFACE_PREFIX = "Tunnel"

_DECOMMISSIONED = "decommissioned"


def _label(location: dict[str, Any]) -> str:
    """`FR2 (TopologyColocationZone)`."""
    return f"{location['name']} ({location['kind']})"


def _sessions_by_name(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The ManagedBGPPeering nodes of a SESSIONS_QUERY result, by name."""
    return {edge["node"]["name"]["value"]: edge["node"] for edge in result["ManagedBGPPeering"]["edges"]}


def _interfaces(session: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    """The interfaces a session runs on (its `capabilities`), by (device, interface)."""
    return {
        (edge["node"]["device"]["node"]["name"]["value"], edge["node"]["name"]["value"]): edge["node"]
        for edge in session["capabilities"]["edges"]
    }


def _address(interface: dict[str, Any]) -> str | None:
    """The address on an interface node of SESSIONS_QUERY, None when unaddressed."""
    return (((interface.get("ip_address") or {}).get("node")) or {}).get("address", {}).get("value")


def _address_namespace(interface: dict[str, Any]) -> str | None:
    """The namespace of the address on an interface node of SESSIONS_QUERY, None when unaddressed."""
    address = ((interface.get("ip_address") or {}).get("node")) or {}
    return (((address.get("ip_namespace") or {}).get("node")) or {}).get("name", {}).get("value")


def _process_nodes(session: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The BGP processes a session runs on, by the one device each runs on."""
    processes = {}
    for edge in session["bgp_processes"]["edges"]:
        devices = [d["node"]["name"]["value"] for d in edge["node"]["capabilities"]["edges"]]
        if len(devices) == 1:
            processes[devices[0]] = edge["node"]
    return processes


def _process_roles(session: dict[str, Any]) -> dict[str, str]:
    """device -> process_role of the processes a session runs on."""
    return {device: node["process_role"]["value"] for device, node in _process_nodes(session).items()}


def _session_errors(session: dict[str, Any], expected: dict[str, Any]) -> list[str]:
    """Why a session is not what ``expected`` says: status, peering_role, session_type, ttl, bfd_enabled,
    send_extended_community, key (the password's name) and address families."""
    name = session["name"]["value"]
    password = (session.get("password") or {}).get("node") or {}
    actual = {
        "status": session["status"]["value"],
        "peering_role": session["peering_role"]["value"],
        "session_type": session["session_type"]["value"],
        "ttl": session["ttl"]["value"],
        "bfd_enabled": session["bfd_enabled"]["value"],
        "send_extended_community": session["send_extended_community"]["value"],
        "key": password.get("name", {}).get("value"),
        "families": sorted(
            (edge["node"]["afi"]["value"], edge["node"]["safi"]["value"])
            for edge in session["address_families"]["edges"]
        ),
    }
    return [
        f"{name}: {field} {actual[field]!r}, expected {want!r}"
        for field, want in expected.items()
        if actual[field] != want
    ]


def _prefixes_by_description(result: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """The IpamPrefix nodes of a CIRCUIT_PREFIXES_QUERY result, by description."""
    prefixes: dict[str, list[dict[str, Any]]] = {}
    for edge in result["IpamPrefix"]["edges"]:
        prefixes.setdefault(edge["node"]["description"]["value"], []).append(edge["node"])
    return prefixes


def _p2p_errors(
    label: str, addresses: list[str | None], length: int, pools: list[Any], prefixes: list[dict[str, Any]] | None
) -> list[str]:
    """Why two interface addresses are not one point-to-point /``length`` inside one of ``pools``, or why the circuit's
    own prefix (``prefixes``, found by description: exactly one) is not that network, a technical one."""
    if len(addresses) != CIRCUIT_ENDS or any(address is None for address in addresses):
        return [f"{label}: interface addresses {addresses} are not two"]
    first, second = (ipaddress.ip_interface(str(address)) for address in addresses)
    network: Any = first.network
    errors: list[str] = []
    if second.network != network or first == second:
        return [f"{label}: interface addresses {first}, {second} are not one P2P"]
    if network.prefixlen != length or not any(
        network.version == pool.version and network.subnet_of(pool) for pool in pools
    ):
        errors.append(f"{label}: P2P {network} is not a /{length} from {[str(pool) for pool in pools]}")
    if not prefixes or len(prefixes) != 1:
        errors.append(f"{label}: {len(prefixes or [])} prefix object(s) for the circuit, expected exactly 1")
    else:
        prefix = prefixes[0]
        if ipaddress.ip_network(prefix["prefix"]["value"]) != network:
            errors.append(f"{label}: its prefix is {prefix['prefix']['value']}, the addresses are on {network}")
        if prefix["role"]["value"] != "technical":
            errors.append(
                f"{label}: prefix {prefix['prefix']['value']} has role {prefix['role']['value']}, expected technical"
            )
    return errors


def _live(carriers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The carriers (``{"id", "circuit_id", "status"}``) of a port that are not decommissioned."""
    return [carrier for carrier in carriers if carrier["status"] != _DECOMMISSIONED]


def _vlan(interface: str) -> int | None:
    """The VLAN of a sub-interface named ``<port>.<vlan>``, None for any other name."""
    _, dot, suffix = interface.rpartition(".")
    return int(suffix) if dot and suffix.isdigit() else None


def _contiguous(hops: list[list[str]], start: str, finish: str) -> bool:
    """Whether some order of ALL hops (each the ids of a circuit's two locations) is one path from ``start`` to
    ``finish``: each hop begins where the previous one ended, in either direction."""

    def walk(place: str, remaining: list[list[str]]) -> bool:
        if not remaining:
            return place == finish
        for index, hop in enumerate(remaining):
            rest = remaining[:index] + remaining[index + 1 :]
            for near, far in (hop, hop[::-1]):
                if near == place and walk(far, rest):
                    return True
        return False

    return walk(start, hops)


def _tunnel_errors(circuit: dict[str, Any], physical_by_id: dict[str, dict[str, Any]]) -> list[str]:
    """A Tunnel: owned by its office's owner, one end on the gateway's public port in the gateway's zone, and every
    end on a live internet carrier circuit of its own (one with an end on an ISP): no shared path to be contiguous."""
    name = circuit["name"]
    errors: list[str] = []
    locations = circuit["location_nodes"]
    offices = [location for location in locations if location["kind"] == _OFFICE_KIND]
    if not circuit["owner_id"] or not any(office["owner_id"] == circuit["owner_id"] for office in offices):
        errors.append(
            f"{name}: owner {circuit['owner']} (derived from its office), but none of its locations "
            f"{[_label(loc) for loc in locations]} is an office with a customer owner"
        )
    gateway = [
        device
        for end, device in zip(circuit["end_nodes"], circuit["end_devices"], strict=True)
        if end["interface"] == ALL_DEMO_SDWAN_GATEWAY_PUBLIC_PORT
    ]
    if len(gateway) != 1:
        errors.append(
            f"{name}: {len(gateway)} end(s) on '{ALL_DEMO_SDWAN_GATEWAY_PUBLIC_PORT}', expected the gateway's one"
        )
    elif gateway[0]["deployment_id"] not in {location["id"] for location in locations}:
        errors.append(
            f"{name}: gateway {gateway[0]['device']} is deployed in {gateway[0]['deployment_name']}, which is none of "
            f"its locations {circuit['locations']}"
        )
    for end in circuit["end_nodes"]:
        live = _live(end["carriers"])
        internet = any(
            location["kind"] == _ISP_KIND
            for carrier in live
            for location in physical_by_id.get(carrier["circuit_id"], {}).get("location_nodes", [])
        )
        if not internet:
            errors.append(
                f"{name}: end {end['device']}:{end['interface']} rides no live internet carrier circuit "
                f"(carriers {[carrier['circuit_id'] for carrier in end['carriers']]}): every end of a tunnel has its own"
            )
    return errors


def _interconnect_errors(circuit: dict[str, Any], physical_by_id: dict[str, dict[str, Any]]) -> list[str]:
    """An Interconnect (a DCI): unowned, two facilities, every end on a live carrier circuit, and those circuits one
    contiguous path between the two facilities. When one circuit carries it, it is pure fibre: its two ends are
    exactly the DCI's two ports."""
    name = circuit["name"]
    errors: list[str] = []
    locations = circuit["location_nodes"]
    if circuit["owner_id"] or circuit["connection"]:
        errors.append(
            f"{name}: owned by {circuit['owner']} / connection {circuit['connection']}, "
            "a DCI between border gateways is shared platform"
        )
    strays = [_label(location) for location in locations if location["kind"] not in ALL_DEMO_FACILITY_KINDS]
    if strays:
        errors.append(f"{name}: a DCI ends on facilities, but {strays} is not one")
    uncarried = [f"{end['device']}:{end['interface']}" for end in circuit["end_nodes"] if not _live(end["carriers"])]
    if uncarried:
        errors.append(f"{name}: end(s) {uncarried} are on no live carrier circuit")
        return errors
    carriers: dict[str, dict[str, Any]] = {}
    for end in circuit["end_nodes"]:
        for carrier in _live(end["carriers"]):
            carriers.setdefault(carrier["circuit_id"], physical_by_id.get(carrier["circuit_id"], {}))
    missing = sorted(circuit_id for circuit_id, node in carriers.items() if not node)
    if missing:
        errors.append(f"{name}: carrier(s) {missing} are not on the branch")
        return errors
    if len(carriers) == 1:
        (fibre,) = carriers.values()
        if set(fibre["end_ids"]) != {end["id"] for end in circuit["end_nodes"]}:
            errors.append(
                f"{name}: its ports {circuit['ends']} are not the ends {fibre['ends']} of its carrier "
                f"{fibre['circuit_id']}: a DCI is pure fibre"
            )
    hops = [[location["id"] for location in node["location_nodes"]] for node in carriers.values()]
    if any(len(hop) != CIRCUIT_ENDS for hop in hops):
        errors.append(f"{name}: a carrier does not terminate on exactly {CIRCUIT_ENDS} locations")
    elif len(locations) == CIRCUIT_ENDS:
        first, second = (location["id"] for location in locations)
        if not (_contiguous(hops, first, second) or _contiguous(hops, second, first)):
            errors.append(
                f"{name}: carrier {sorted(carriers)} is not one contiguous path between "
                f"{[_label(location) for location in locations]}"
            )
    return errors


def _routed_link_errors(circuit: dict[str, Any], kind: str) -> list[str]:
    """What a handoff and a vpn share: one or two facilities that are where its two ends are deployed, its namespace
    stated, at least one end carried by a live carrier circuit or a cable, and a provider connection when an end
    device is deployed in a provider-hosted facility (the connection generates the circuit, the owner derives from it)."""
    name = circuit["name"]
    errors: list[str] = []
    locations = circuit["location_nodes"]
    strays = [_label(location) for location in locations if location["kind"] not in ALL_DEMO_FACILITY_KINDS]
    if strays:
        errors.append(f"{name}: a {kind} ends on facilities, but {strays} is not one")
    if any(location["kind"] == _OFFICE_KIND and location["owner_id"] for location in locations):
        errors.append(f"{name}: a {kind} names an office with an owner among {circuit['locations']}")
    deployed = {device["deployment_id"] for device in circuit["end_devices"]}
    if {location["id"] for location in locations} != deployed:
        errors.append(
            f"{name}: locations {circuit['locations']} are not where its ends are deployed "
            f"{sorted(str(device['deployment_name']) for device in circuit['end_devices'])}"
        )
    if not circuit["ip_namespace"]:
        errors.append(f"{name}: no ip_namespace")
    if not any(_live(end["carriers"]) or end["cabled"] for end in circuit["end_nodes"]):
        errors.append(f"{name}: no end is on a live carrier circuit or a cable (at the port or its parent)")
    hosted = sorted(
        str(device["device"])
        for device in circuit["end_devices"]
        if device["deployment_kind"] in _PROVIDER_HOSTED_KINDS
    )
    if hosted and not circuit["connection"]:
        errors.append(f"{name}: ends on provider-hosted {hosted} but belongs to no provider connection")
    return errors


def _handoff_errors(circuit: dict[str, Any]) -> list[str]:
    """A Handoff: the shared rules (_routed_link_errors), both ends VLAN sub-interfaces `<port>.<vlan>` on one VLAN
    (the connection's, when it has one), and a dedicated connection when it has one."""
    name = circuit["name"]
    errors = _routed_link_errors(circuit, "handoff")
    vlans = {_vlan(end["interface"]) for end in circuit["end_nodes"]}
    for end in circuit["end_nodes"]:
        if end["kind"] != "DcimVirtualInterface" or _vlan(end["interface"]) is None:
            errors.append(f"{name}: end {end['device']}:{end['interface']} is not a VLAN sub-interface <port>.<vlan>")
        elif end["parent_name"] is None or end["interface"] != f"{end['parent_name']}.{_vlan(end['interface'])}":
            errors.append(
                f"{name}: end {end['device']}:{end['interface']} has parent '{end['parent_name']}', "
                "expected <parent>.<vlan>"
            )
    connection = circuit["connection_node"]
    if connection and connection["path_type"] != "dedicated":
        errors.append(f"{name}: a handoff belongs to a {connection['path_type']} connection {connection['name']}")
    if len(vlans) > 1:
        errors.append(f"{name}: the ends are on different VLANs {sorted(v for v in vlans if v is not None)}")
    elif connection and connection["vlan_id"] is not None and connection["vlan_id"] not in vlans:
        errors.append(
            f"{name}: its ends are on VLAN {sorted(v for v in vlans if v is not None)} but connection "
            f"{connection['name']} states vlan_id {connection['vlan_id']}"
        )
    return errors


def _vpn_errors(circuit: dict[str, Any]) -> list[str]:
    """A VPN: the shared rules (_routed_link_errors), both ends `Tunnel<n>` virtual interfaces, and an internet_vpn
    provider connection (a vpn is generated by one; it states no VLAN)."""
    name = circuit["name"]
    errors = _routed_link_errors(circuit, "vpn")
    for end in circuit["end_nodes"]:
        if end["kind"] != "DcimVirtualInterface" or not end["interface"].startswith(_TUNNEL_INTERFACE_PREFIX):
            errors.append(
                f"{name}: end {end['device']}:{end['interface']} is not a {_TUNNEL_INTERFACE_PREFIX}<n> interface"
            )
    connection = circuit["connection_node"]
    if not connection:
        errors.append(f"{name}: a vpn belongs to a provider connection, it has none")
    elif connection["path_type"] != "internet_vpn":
        errors.append(f"{name}: a vpn belongs to a {connection['path_type']} connection {connection['name']}")
    return errors


def _session_port_errors(virtual: list[dict[str, Any]]) -> list[str]:
    """One port, one session: no port is an end of two Interconnect or Handoff circuits (a Tunnel is exempt: one
    gateway port serves many tunnels)."""
    circuits_of_port: dict[str, list[str]] = {}
    for circuit in virtual:
        if circuit["circuit_type"] in (INTERCONNECT_TYPE, HANDOFF_TYPE):
            for end in circuit["end_nodes"]:
                circuits_of_port.setdefault(end["id"], []).append(circuit["name"])
    return [
        f"port {port_id} is an end of {sorted(names)}: one port carries one interconnect or handoff session"
        for port_id, names in sorted(circuits_of_port.items())
        if len(names) > 1
    ]


def _environment_errors(virtual: list[dict[str, Any]]) -> list[str]:
    """Prod and non-prod never connect: circuits meeting at a device share an environment class. A Tunnel is PROD (its
    office is always production), a Handoff or a VPN takes the type of its namespace (INTERNET and MANAGEMENT state none), an
    Interconnect runs in the underlay and states none."""
    by_device: dict[str, tuple[str, list[tuple[str, str]]]] = {}
    for circuit in virtual:
        env_class: str | None = None
        if circuit["circuit_type"] == TUNNEL_TYPE:
            env_class = PROD_TYPE
        elif circuit["circuit_type"] in (HANDOFF_TYPE, VPN_TYPE) and circuit["ip_namespace"]:
            namespace_type = transit_namespace_type(circuit["ip_namespace"])
            env_class = namespace_type if namespace_type in TENANT_NAMESPACE_TYPES else None
        if env_class is None:
            continue
        for end in circuit["end_nodes"]:
            by_device.setdefault(str(end["device_id"]), (end["device"], []))[1].append((circuit["name"], env_class))
    return [
        f"device {device} carries circuits of different environments: {sorted(set(members))}"
        for device, members in by_device.values()
        if len({env_class for _, env_class in members}) > 1
    ]


def _expectation_errors(virtual: list[dict[str, Any]]) -> list[str]:
    """Where the virtual circuits of the branch differ from ALL_DEMO_VIRTUAL_CIRCUITS: a missing or an unexpected
    one, and per circuit its kind, derived owner, locations, ends, derived carrier and namespace. The session circuits
    of the other provider connections (ALL_DEMO_CONNECTION_CIRCUITS) are allowed here and asserted by test_67."""
    by_name = {circuit["name"]: circuit for circuit in virtual}
    errors: list[str] = []
    missing = sorted(set(ALL_DEMO_VIRTUAL_CIRCUITS) - set(by_name))
    if missing:
        errors.append(f"virtual circuit(s) missing: {missing}")
    unexpected = sorted(set(by_name) - set(ALL_DEMO_VIRTUAL_CIRCUITS) - ALL_DEMO_CONNECTION_CIRCUITS)
    if unexpected:
        errors.append(f"unexpected virtual circuit(s): {unexpected}")

    for name, expected in sorted(ALL_DEMO_VIRTUAL_CIRCUITS.items()):
        circuit = by_name.get(name)
        if circuit is None:
            continue
        if circuit["circuit_type"] != expected["circuit_type"]:
            errors.append(f"{name}: circuit_type '{circuit['circuit_type']}', expected '{expected['circuit_type']}'")
        if circuit["owner_org"] != ALL_DEMO_CIRCUIT_OWNERS[name]:
            errors.append(f"{name}: derived owner '{circuit['owner_org']}', expected '{ALL_DEMO_CIRCUIT_OWNERS[name]}'")
        if circuit["locations"] != sorted(expected["locations"]):
            errors.append(f"{name}: locations {circuit['locations']}, expected {sorted(expected['locations'])}")
        if circuit["ends"] != sorted(expected["ends"]):
            errors.append(
                f"{name}: ends {circuit['ends']}, expected {sorted(expected['ends'])} "
                "(capabilities: the circuit's interfaces)"
            )
        if circuit["carrier"] != sorted(expected["carrier"]):
            errors.append(
                f"{name}: carrier {circuit['carrier']}, expected {sorted(expected['carrier'])} — "
                "the carrier is derived: the carrier circuits on the circuit's own ports (or their parents)"
            )
        if circuit["ip_namespace"] != expected["ip_namespace"]:
            errors.append(f"{name}: ip_namespace '{circuit['ip_namespace']}', expected '{expected['ip_namespace']}'")
    return errors


def _deployments(circuit: dict[str, Any]) -> list[str]:
    """`bl-dc101101 in DC10 (TopologyDataCenter)` for each end device of a circuit."""
    return [
        f"{device['device']} in {device['deployment_name']} ({device['deployment_kind']})"
        for device in circuit["end_devices"]
    ]


def _exchange_ends(exchange_name: str, context_name: str) -> tuple[str, ...]:
    """The namespaces `{context}-{A}-{Z}` joins; namespace names may contain '-' (NON-PROD), so match known ones."""
    rest = exchange_name.removeprefix(f"{context_name}-")
    return tuple(
        ns for ns in ALL_DEMO_TRANSIT_RESOURCES if rest == ns or rest.startswith(f"{ns}-") or rest.endswith(f"-{ns}")
    )
