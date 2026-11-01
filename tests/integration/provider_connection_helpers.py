"""Readers and judges of test_67 (provider connections): the connection rows, their generated circuits and the
interfaces they end on, judged against all_demo_provider_connection_constants.

Pure functions over cleaned GraphQL payloads (no stack needed): tests/unit/test_integration_provider_connections.py runs
them on canned data. The fetchers are the only code that talks to a client.
"""

from __future__ import annotations

from typing import Any

from infrahub_sdk import InfrahubClient

from utils.data_cleaning import clean_data
from utils.ownership import customer_owner

from .all_demo_provider_connection_constants import ADDRESS_PROVIDER, ConnectionExpectation, LinkExpectation

# Every connection with what the generator reads and writes of it: the key, the owner and the account's owner (the
# owner of its circuits is derived from them), the carriers it lists and the circuits and ranges it holds.
CONNECTIONS_QUERY = """
query {
  TopologyProviderConnection {
    edges {
      node {
        id
        name { value }
        path_type { value }
        service { value }
        status { value }
        vlan_id { value }
        ip_namespace { node { name { value } } }
        password { node { name { value } } }
        owner { node { id __typename org_id { value } } }
        account { node { name { value } owner { node { id __typename org_id { value } } } } }
        carrier_circuits { edges { node { id circuit_id { value } } } }
        session_circuits { edges { node { id name { value } } } }
        link_prefixes { edges { node { prefix { value } } } }
      }
    }
  }
}
"""

# The interfaces the generator creates on a device, by name: sub-interfaces `<port>.<vlan>` and `Tunnel<n>`.
INTERFACES_QUERY = """
query ($devices: [String], $names: [String]) {
  DcimVirtualInterface(device__name__values: $devices, name__values: $names) {
    edges { node { id name { value } device { node { name { value } } } parent_interface { node { name { value } } } } }
  }
}
"""

PEERINGS_QUERY = """
query ($names: [String]) {
  ManagedBGPPeering(name__values: $names) { edges { node { id name { value } } } }
}
"""


async def fetch_connections(client: InfrahubClient, branch: str) -> dict[str, dict[str, Any]]:
    """name -> the cleaned TopologyProviderConnection (see CONNECTIONS_QUERY), with `owner_org`, `account_owner_org`
    (the org_id of a customer, else None: a provider is no owner), `carriers` (circuit_ids), `sessions` (names) and
    `link_prefix_list` (sorted prefixes) added."""
    result = clean_data(await client.execute_graphql(query=CONNECTIONS_QUERY, branch_name=branch))
    return {
        str(row["name"]): {
            **row,
            "owner_org": customer_owner(row.get("owner")),
            "account_owner_org": customer_owner((row.get("account") or {}).get("owner")),
            "carriers": sorted(str(c["circuit_id"]) for c in row.get("carrier_circuits") or []),
            "sessions": sorted(str(c["name"]) for c in row.get("session_circuits") or []),
            "link_prefix_list": sorted(str(p["prefix"]) for p in row.get("link_prefixes") or []),
        }
        for row in result.get("TopologyProviderConnection") or []
    }


async def fetch_interface_ids(client: InfrahubClient, branch: str, devices: list[str], names: list[str]) -> list[str]:
    """`device/name` of the virtual interfaces of ``devices`` named ``names`` that exist on the branch."""
    result = clean_data(
        await client.execute_graphql(
            query=INTERFACES_QUERY, variables={"devices": devices, "names": names}, branch_name=branch
        )
    )
    return sorted(
        f"{row['device']['name']}/{row['name']}"
        for row in result.get("DcimVirtualInterface") or []
        if row.get("device")
    )


def connection_errors(connection: dict[str, Any] | None, name: str, expected: ConnectionExpectation) -> list[str]:
    """Why a connection row is not what the data and the flavour say: path, service, namespace, VLAN, key, derived
    owner, carriers listed, session count and names, link ranges."""
    if connection is None:
        return [f"{name}: no such connection on the branch"]
    errors: list[str] = []
    links = [link["name"] for link in expected["links"]]
    namespace = (connection.get("ip_namespace") or {}).get("name")
    password = (connection.get("password") or {}).get("name")
    checks: list[tuple[str, Any, Any]] = [
        ("path_type", connection["path_type"], expected["path_type"]),
        ("service", connection["service"], expected["service"]),
        ("status", connection["status"], "active"),
        ("ip_namespace", namespace, expected["ip_namespace"]),
        ("vlan_id", connection.get("vlan_id"), expected["vlan_id"]),
        ("password", password, expected["password"]),
        ("carrier_circuits", connection["carriers"], sorted(expected["carriers"])),
        ("session_circuits", connection["sessions"], sorted(links)),
        (
            "link_prefixes",
            connection["link_prefix_list"],
            sorted(link["link_prefix"] for link in expected["links"] if link["link_prefix"]),
        ),
    ]
    errors += [f"{name}: {field} {actual!r}, expected {want!r}" for field, actual, want in checks if actual != want]
    if len(connection["sessions"]) != len(links):
        errors.append(f"{name}: {len(connection['sessions'])} session circuit(s), the flavour has {len(links)} link(s)")
    # PC5: with an account the owner is empty and derived from the account; a SaaS or partner connection names its own
    if connection.get("account") and connection.get("owner"):
        errors.append(f"{name}: names an account and an owner, the owner is derived from the account")
    owner = connection["account_owner_org"] or connection["owner_org"]
    if owner != expected["owner"]:
        errors.append(f"{name}: derived owner {owner!r}, expected {expected['owner']!r}")
    return errors


def link_errors(
    circuit: dict[str, Any] | None, expected: ConnectionExpectation, link: LinkExpectation, connection: str
) -> list[str]:
    """Why the generated session circuit of one link is not what the connection plans (the inventory entry of
    tests/integration/circuit_helpers.py): its kind, connection, ends and their parent interfaces, locations,
    namespace, key, link range, derived owner and derived carrier."""
    name = link["name"]
    if circuit is None:
        return [f"{name}: no session circuit"]
    errors: list[str] = []
    by_end = {(end["device"], end["interface"]): end for end in circuit["end_nodes"]}
    ends = sorted((end["device"], end["interface"]) for end in link["ends"])
    parents = {(end["device"], end["interface"]): end["parent"] for end in link["ends"]}
    checks: list[tuple[str, Any, Any]] = [
        ("circuit_type", circuit["circuit_type"], "handoff" if expected["path_type"] == "dedicated" else "vpn"),
        ("connection", circuit["connection"], connection),
        ("ends", circuit["ends"], ends),
        ("locations", circuit["locations"], sorted(expected["locations"])),
        ("ip_namespace", circuit["ip_namespace"], expected["ip_namespace"]),
        ("password", circuit["password"], expected["password"]),
        ("link_prefix", circuit["link_prefix"], link["link_prefix"]),
        ("owner", circuit["owner_org"], expected["owner"]),
        ("carrier", circuit["carrier"], sorted(link["carrier"])),
    ]
    errors += [f"{name}: {field} {actual!r}, expected {want!r}" for field, actual, want in checks if actual != want]
    errors += [
        f"{name}: end {end[0]}:{end[1]} rides {by_end[end]['parent_name']!r}, expected {parent!r}"
        for end, parent in sorted(parents.items(), key=str)
        if end in by_end and by_end[end]["parent_name"] != parent
    ]
    if expected["address_source"] == ADDRESS_PROVIDER and not link["link_prefix"]:
        errors.append(f"{name}: a provider-assigned link without an authored range")
    return errors


def unowned_circuit_errors(
    virtual: list[dict[str, Any]], connection_circuits: set[str], declared: set[str]
) -> list[str]:
    """Every handoff/vpn session circuit is a generated one (`connection` set and listed) or a declared one with no
    connection (the SD-WAN gateway handoff): nothing else, so a dropped link or a stray circuit is named."""
    errors: list[str] = []
    for circuit in sorted(virtual, key=lambda c: c["name"]):
        if circuit["circuit_type"] not in ("handoff", "vpn"):
            continue
        name = circuit["name"]
        if circuit["connection"] and name not in connection_circuits:
            errors.append(f"{name}: belongs to {circuit['connection']}, which plans no such link")
        elif not circuit["connection"] and name not in declared:
            errors.append(
                f"{name}: a {circuit['circuit_type']} circuit with no connection that the data does not declare"
            )
    return errors


def carrier_membership_errors(physical: list[dict[str, Any]], expected: dict[str, ConnectionExpectation]) -> list[str]:
    """PC2 from the carrier side: a carrier listed by a connection lists that connection back (`provider_connections`),
    and a carrier lists no connection that does not list it."""
    by_id = {circuit["circuit_id"]: circuit for circuit in physical}
    listed: dict[str, set[str]] = {}
    for connection, row in expected.items():
        for carrier in row["carriers"]:
            listed.setdefault(carrier, set()).add(connection)
    errors: list[str] = []
    for carrier, connections in sorted(listed.items()):
        circuit = by_id.get(carrier)
        if circuit is None:
            errors.append(f"{carrier}: listed by {sorted(connections)} but not on the branch")
        elif set(circuit["connections"]) != connections:
            errors.append(f"{carrier}: provider_connections {circuit['connections']}, expected {sorted(connections)}")
    stray = sorted(c["circuit_id"] for c in physical if c["connections"] and c["circuit_id"] not in listed)
    errors += [f"{carrier}: names a connection no connection lists it for" for carrier in stray]
    return errors
