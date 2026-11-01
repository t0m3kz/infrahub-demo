"""30_all provider connections (test_67, test_62): what add_provider_connection must build for each connection of
data/demos/30_all/08_interconnects/05_provider_connections.

Typed by hand on purpose, as an oracle independent of the generator: tests/unit/test_integration_provider_connections.py
holds this table against the data, the offline planner (generators/helpers/provider_connection.py) and the flavour table
(utils/provider_services.py). A new connection in the data fails that test until it is described here.

A connection yields one TopologySessionCircuit per link of its provider's flavour, named `<connection>-L<n>`:
`handoff` for a dedicated connection (the `<port>.<vlan>` sub-interfaces on the two ends of the carrier), `vpn` for an
internet_vpn one (a `Tunnel<n>` pair; the near tunnel rides the port of the internet carrier). The owner of such a
circuit is derived (utils.ownership.circuit_owner): the customer of the connection's account, else its own owner.
"""

from typing import TypedDict

from .all_demo_circuit_constants import ALL_DEMO_TUNNEL_OWNERS, ALL_DEMO_VIRTUAL_CIRCUITS, HANDOFF_TYPE

#: The `circuit_type` of a VPN session circuit (model only: no template renders a tunnel).
VPN_TYPE = "vpn"

ADDRESS_OUR_POOL = "our_pool"
ADDRESS_PROVIDER = "provider_assigned"


class End(TypedDict):
    """One end port of a link: where it is and the interface it rides."""

    device: str
    interface: str
    #: The parent interface (a sub-interface's port, a near tunnel's internet port); None for a far tunnel.
    parent: str | None


class LinkExpectation(TypedDict):
    """One session circuit of a connection."""

    name: str
    #: The two ends, sorted by (device, interface).
    ends: tuple[End, ...]
    #: The range the provider assigned (an authored prefix); None when the link prefix comes from our pool.
    link_prefix: str | None
    #: The party's device and its ASN (the `local_as` of the stub's one ManagedBGP process); None when both ends are
    #: devices whose process is generated (the Network Edge pair), so no ASN is declared.
    party: tuple[str, int] | None
    #: circuit_ids of the carrier circuits derived from the ends (on the port, or on its parent), sorted.
    carrier: tuple[str, ...]


class ConnectionExpectation(TypedDict):
    """One TopologyProviderConnection and the sessions it generates."""

    path_type: str
    service: str
    ip_namespace: str
    #: The VLAN of a dedicated connection (authored); None for an internet_vpn.
    vlan_id: int | None
    #: The org_id of the customer it is for (derived: the account's owner, else its own); None for shared kit.
    owner: str | None
    #: The two facilities, sorted: the connection's near and far.
    locations: tuple[str, ...]
    #: The authored `carrier_circuits`, sorted.
    carriers: tuple[str, ...]
    #: The name of the key put on every session, None when there is none.
    password: str | None
    #: The provider's flavour (utils/provider_services.py), restated: where the link addresses come from, their length,
    #: the end that takes the lower host address, the session TTL and BFD.
    address_source: str
    prefix_length: int
    lower_end: str
    ttl: int
    bfd: bool
    links: tuple[LinkExpectation, ...]


def _sub(device: str, port: str, vlan: int) -> End:
    """A `<port>.<vlan>` sub-interface end."""
    return {"device": device, "interface": f"{port}.{vlan}", "parent": port}


def _handoff_link(
    connection: str,
    number: int,
    near: tuple[str, str],
    far: tuple[str, str],
    vlan: int,
    party_asn: int,
    carrier: tuple[str, ...],
    link_prefix: str | None = None,
) -> LinkExpectation:
    """The link of a dedicated connection: `<port>.<vlan>` on both ports (near and far are (device, port))."""
    return {
        "name": f"{connection}-L{number}",
        "ends": tuple(sorted((_sub(*near, vlan), _sub(*far, vlan)), key=lambda e: (e["device"], e["interface"]))),
        "link_prefix": link_prefix,
        "party": (far[0], party_asn),
        "carrier": carrier,
    }


def _tunnel_link(
    connection: str,
    number: int,
    near: tuple[str, str],
    stub: str,
    party_asn: int,
    carrier: tuple[str, ...],
    link_prefix: str | None = None,
) -> LinkExpectation:
    """The link of an internet VPN: `Tunnel<n>` on the near device (on its internet port) and on the stub."""
    tunnel = f"Tunnel{number}"
    ends: list[End] = [
        {"device": near[0], "interface": tunnel, "parent": near[1]},
        {"device": stub, "interface": tunnel, "parent": None},
    ]
    return {
        "name": f"{connection}-L{number}",
        "ends": tuple(sorted(ends, key=lambda e: (e["device"], e["interface"]))),
        "link_prefix": link_prefix,
        "party": (stub, party_asn),
        "carrier": carrier,
    }


_AWS_ASN = 64512
_AWS_VPN_ASN = 64513
_AZURE_ASN = 12076
_MEGAPORT_ASN = 133937
_ZSCALER_ASN = 22616

#: connection name -> its expectation.
ALL_DEMO_CONNECTIONS: dict[str, ConnectionExpectation] = {
    # Direct Connect 10G through the virtual connection onto the service edge NIC vnic-fr2: the customer specifies the peer addresses, so they come from OUR
    # pool (a /31). The account net-hub-aws-prod belongs to the operator ("Internal"), not to a customer: unowned.
    "PC-FR2-AWS-EUC1-PROD": {
        "path_type": "dedicated",
        "service": "transit",
        "ip_namespace": "PROD",
        "vlan_id": 1000,
        "owner": None,
        "locations": ("FR2", "eu-central-1"),
        "carriers": ("XC-EQXFR2-AWS-EUC1",),
        "password": "eqx-fr2-aws-vif-key",
        "address_source": ADDRESS_OUR_POOL,
        "prefix_length": 31,
        "lower_end": "us",
        "ttl": 1,
        "bfd": True,
        "links": (
            _handoff_link(
                "PC-FR2-AWS-EUC1-PROD",
                1,
                ("eg-fr201", "Ethernet1/13"),
                ("EXT-AWS-DX-FR2", "vnic-fr2"),
                1000,
                _AWS_ASN,
                ("XC-EQXFR2-AWS-EUC1",),
            ),
        ),
    },
    # The Equinix Fabric connection cage -> Network Edge router eg-am-ne01: both ends are ours (a layer 2 service).
    "PC-FR2-AMNE-PROD": {
        "path_type": "dedicated",
        "service": "private",
        "ip_namespace": "PROD",
        "vlan_id": 1200,
        "owner": None,
        "locations": ("AM-NE", "FR2"),
        "carriers": ("FABRIC-FR2-AMNE01",),
        "password": None,
        "address_source": ADDRESS_OUR_POOL,
        "prefix_length": 31,
        "lower_end": "us",
        "ttl": 1,
        "bfd": True,
        "links": (
            {
                "name": "PC-FR2-AMNE-PROD-L1",
                "ends": (_sub("eg-am-ne01", "Ethernet1/1", 1200), _sub("eg-fr201", "Ethernet1/1", 1200)),
                "link_prefix": None,
                "party": None,
                "carrier": ("FABRIC-FR2-AMNE01",),
            },
        ),
    },
    # The hosted Direct Connect of the second Network Edge router, through the Fabric connection.
    "PC-AMNE-AWS-EUC1-PROD": {
        "path_type": "dedicated",
        "service": "transit",
        "ip_namespace": "PROD",
        "vlan_id": 1100,
        "owner": None,
        "locations": ("AM-NE", "eu-central-1"),
        "carriers": ("FABRIC-AMNE02-AWS-EUC1",),
        "password": "eqx-am-ne-aws-vif-key",
        "address_source": ADDRESS_OUR_POOL,
        "prefix_length": 31,
        "lower_end": "us",
        "ttl": 1,
        "bfd": True,
        "links": (
            _handoff_link(
                "PC-AMNE-AWS-EUC1-PROD",
                1,
                ("eg-am-ne02", "Ethernet1/1"),
                ("EXT-AWS-DX-FR2", "vnic-amne"),
                1100,
                _AWS_ASN,
                ("FABRIC-AMNE02-AWS-EUC1",),
            ),
        ),
    },
    # Site-to-Site VPN: AWS assigns a 169.254/30 per tunnel, two tunnels, the provider end the lower address; the
    # account nordix-aws-prod belongs to C001. Model only: nothing renders the tunnel.
    "PC-FR2-AWS-EUC1-VPN-PROD": {
        "path_type": "internet_vpn",
        "service": "private",
        "ip_namespace": "PROD",
        "vlan_id": None,
        "owner": "C001",
        "locations": ("FR2", "eu-central-1"),
        "carriers": ("INET-LUMEN-EQXFR2",),
        "password": None,
        "address_source": ADDRESS_PROVIDER,
        "prefix_length": 30,
        "lower_end": "provider",
        "ttl": 1,
        "bfd": False,
        "links": (
            _tunnel_link(
                "PC-FR2-AWS-EUC1-VPN-PROD",
                1,
                ("eg-fr201", "Ethernet1/14"),
                "EXT-AWS-VPN-EUC1",
                _AWS_VPN_ASN,
                ("INET-LUMEN-EQXFR2",),
                "169.254.10.0/30",
            ),
            _tunnel_link(
                "PC-FR2-AWS-EUC1-VPN-PROD",
                2,
                ("eg-fr201", "Ethernet1/14"),
                "EXT-AWS-VPN-EUC1",
                _AWS_VPN_ASN,
                ("INET-LUMEN-EQXFR2",),
                "169.254.10.4/30",
            ),
        ),
    },
    # Megaport Cloud Router, the cloud-neutral example: a /31 from our pool.
    "PC-PA4-MEGAPORT-PROD": {
        "path_type": "dedicated",
        "service": "private",
        "ip_namespace": "PROD",
        "vlan_id": 1300,
        "owner": None,
        "locations": ("MCR-PAR1", "PA4"),
        "carriers": ("XC-EQXPA4-MEGAPORT",),
        "password": "eqx-pa4-megaport-key",
        "address_source": ADDRESS_OUR_POOL,
        "prefix_length": 31,
        "lower_end": "us",
        "ttl": 1,
        "bfd": True,
        "links": (
            _handoff_link(
                "PC-PA4-MEGAPORT-PROD",
                1,
                ("eg-pa401", "Ethernet1/1"),
                ("EXT-MEGAPORT-MCR1", "et-0/0/0"),
                1300,
                _MEGAPORT_ASN,
                ("XC-EQXPA4-MEGAPORT",),
            ),
        ),
    },
    # ExpressRoute private peering: Microsoft takes a /30 per link and the customer end is the lower address; one
    # connection per leg, the two legs on two cage edges and two NICs of the one service edge. C004's account.
    "PC-PA4-AZURE-WEU-PRI-PROD": {
        "path_type": "dedicated",
        "service": "private",
        "ip_namespace": "PROD",
        "vlan_id": 1400,
        "owner": "C004",
        "locations": ("PA4", "westeurope"),
        "carriers": ("XC-EQXPA4-AZURE-WEU-PRI",),
        "password": "eqx-pa4-azure-er-key",
        "address_source": ADDRESS_PROVIDER,
        "prefix_length": 30,
        "lower_end": "us",
        "ttl": 1,
        "bfd": True,
        "links": (
            _handoff_link(
                "PC-PA4-AZURE-WEU-PRI-PROD",
                1,
                ("eg-pa401", "Ethernet1/13"),
                ("EXT-AZURE-ER-PA4", "vnic-pri"),
                1400,
                _AZURE_ASN,
                ("XC-EQXPA4-AZURE-WEU-PRI",),
                "10.255.72.0/30",
            ),
        ),
    },
    "PC-PA4-AZURE-WEU-SEC-PROD": {
        "path_type": "dedicated",
        "service": "private",
        "ip_namespace": "PROD",
        "vlan_id": 1400,
        "owner": "C004",
        "locations": ("PA4", "westeurope"),
        "carriers": ("XC-EQXPA4-AZURE-WEU-SEC",),
        "password": "eqx-pa4-azure-er-key",
        "address_source": ADDRESS_PROVIDER,
        "prefix_length": 30,
        "lower_end": "us",
        "ttl": 1,
        "bfd": True,
        "links": (
            _handoff_link(
                "PC-PA4-AZURE-WEU-SEC-PROD",
                1,
                ("eg-pa402", "Ethernet1/13"),
                ("EXT-AZURE-ER-PA4", "vnic-sec"),
                1400,
                _AZURE_ASN,
                ("XC-EQXPA4-AZURE-WEU-SEC",),
                "10.255.72.4/30",
            ),
        ),
    },
    # Zscaler private peering: two tunnels to the nearest node, a /30 from OUR pool, a multihop session (TTL 2). The
    # connection is C001's (no account) and registers its private application range.
    "PC-FR2-ZSCALER-MUC-PROD": {
        "path_type": "internet_vpn",
        "service": "private",
        "ip_namespace": "PROD",
        "vlan_id": None,
        "owner": "C001",
        "locations": ("FR2", "ZSCALER-MUNICH I"),
        "carriers": ("INET-LUMEN-EQXFR2",),
        "password": None,
        "address_source": ADDRESS_OUR_POOL,
        "prefix_length": 30,
        "lower_end": "us",
        "ttl": 2,
        "bfd": False,
        "links": (
            _tunnel_link(
                "PC-FR2-ZSCALER-MUC-PROD",
                1,
                ("eg-fr201", "Ethernet1/14"),
                "EXT-ZSCALER-MUC1",
                _ZSCALER_ASN,
                ("INET-LUMEN-EQXFR2",),
            ),
            _tunnel_link(
                "PC-FR2-ZSCALER-MUC-PROD",
                2,
                ("eg-fr201", "Ethernet1/14"),
                "EXT-ZSCALER-MUC1",
                _ZSCALER_ASN,
                ("INET-LUMEN-EQXFR2",),
            ),
        ),
    },
}

#: circuit name -> (connection name, its link) for every session circuit of every connection.
ALL_DEMO_CONNECTION_LINKS: dict[str, tuple[str, LinkExpectation]] = {
    link["name"]: (connection, link)
    for connection, expected in ALL_DEMO_CONNECTIONS.items()
    for link in expected["links"]
}

#: The links of the dedicated connections (circuit_type handoff); the others are vpn.
ALL_DEMO_CONNECTION_HANDOFFS: frozenset[str] = frozenset(
    circuit
    for circuit, (connection, _) in ALL_DEMO_CONNECTION_LINKS.items()
    if ALL_DEMO_CONNECTIONS[connection]["path_type"] == "dedicated"
)

#: The connection circuits test_62 does not know (ALL_DEMO_VIRTUAL_CIRCUITS holds the four one-link handoffs on our
#: pool): the provider-assigned and multi-link ones. test_67 asserts all of them.
ALL_DEMO_CONNECTION_CIRCUITS: frozenset[str] = frozenset(ALL_DEMO_CONNECTION_LINKS) - frozenset(
    name for name, expected in ALL_DEMO_VIRTUAL_CIRCUITS.items() if expected["circuit_type"] == HANDOFF_TYPE
)

#: circuit name -> the org_id of its derived owner, None for shared platform (the dci, the gateway handoff, the
#: hub connections of the operator's own account). Every circuit of test_62 and every connection circuit.
ALL_DEMO_CIRCUIT_OWNERS: dict[str, str | None] = {
    **{name: None for name in ALL_DEMO_VIRTUAL_CIRCUITS},
    **ALL_DEMO_TUNNEL_OWNERS,
    **{
        circuit: ALL_DEMO_CONNECTIONS[connection]["owner"]
        for circuit, (connection, _) in ALL_DEMO_CONNECTION_LINKS.items()
    },
}
