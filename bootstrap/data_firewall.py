"""Data for the firewall demo."""

ZONES = ["OUTSIDE", "INSIDE", "DMZ", "EXTRANET"]

IP_PROTOCOLS = [
    # protol, name, description
    (None, "IP", "IP"),
    (1, "ICMP", "ICMP"),
    (6, "TCP", "TCP"),
    (17, "UDP", "UDP"),
    (21, "FTP", "FTP"),
    (22, "SSH", "SSH"),
    (23, "Telnet", "Telnet"),
    (25, "SMTP", "SMTP"),
    (42, "VRRP", "VRRP"),
    (53, "DNS", "DNS"),
    (67, "DHCP", "DHCP"),
    (69, "TFTP", "TFTP"),
    (80, "HTTP", "HTTP"),
    (443, "HTTPS", "HTTPS"),
    (3389, "RDP", "RDP"),
    (5060, "SIP", "SIP"),
    (8080, "HTTP", "HTTP"),
    (8443, "HTTPS", "HTTPS"),
]

SERVICES = [
    # name, ip_protocol, port
    ("DNS_UDP", "UDP", 53),
    ("DNS_TCP", "TCP", 53),
    ("HTTP", "TCP", 80),
    ("HTTPS", "TCP", 443),
    ("SSH", "TCP", 22),
    ("TELNET", "TCP", 23),
    ("RDP", "TCP", 3389),
    ("SIP", "UDP", 5060),
    ("TFTP", "UDP", 69),
    ("VRRP_UDP", "UDP", 42),
    ("VRRP_TCP", "TCP", 42),
    ("DHCP_UDP", "UDP", 67),
    ("FTP", "TCP", 21),
    ("SMTP", "TCP", 25),
    ("SNMP_UDP", "UDP", 161),
    ("NTP_UDP", "UDP", 123),
    ("SNMP_TCP", "TCP", 161),
]


SERVICE_GROUPS = [
    # name, services
    ("HTTP_HTTPS", ["HTTP", "HTTPS"]),
    ("ACCESS", ["SSH", "TELNET"]),
    ("DNS", ["DNS_UDP", "DNS_TCP"]),
]

PREFIXES = [
    # name, prefix
    ("IANA_PRIVATE_PREFIX_1", "10.0.0.0/8"),
    ("IANA_PRIVATE_PREFIX_2", "172.16.0.0/12"),
    ("IANA_PRIVATE_PREFIX_3", "192.168.0.0/16"),
    ("DATACENTER-EU-FR", "89.207.132.0/24"),
    ("DATACENTER-EU-DE", "44.178.0.0/16"),
    ("DATACENTER-US-NY", "244.178.0.0/16"),
]

ADDRESSES = [
    # name, address
    ("ANY", "0.0.0.0/0"),
    ("SMTP_SERVER_1", "192.168.0.10/32"),
    ("SMTP_SERVER_2", "10.0.12.3/32"),
    ("EUR_WEB_PROXY_1", "89.0.142.86/32"),
    ("EUR_WEB_PROXY_2", "10.7.4.3/32"),
]

ADDRESS_GROUPS = [
    # name, addresses
    ("SMTP_SERVERS", ["SMTP_SERVER_1", "SMTP_SERVER_2"]),
    ("EUR_WEB_PROXY", ["EUR_WEB_PROXY_1", "EUR_WEB_PROXY_2"]),
    (
        "IANA_PRIVATE_PREFIXES",
        ["IANA_PRIVATE_PREFIX_1", "IANA_PRIVATE_PREFIX_2", "IANA_PRIVATE_PREFIX_3"],
    ),
    ("DATACENTERS", ["DATACENTER-EU-FR", "DATACENTER-EU-DE", "DATACENTER-US-NY"]),
    (
        "BLOCK_INTERNET",
        [
            "ANY",
            "IANA_PRIVATE_PREFIX_1",
            "IANA_PRIVATE_PREFIX_2",
            "IANA_PRIVATE_PREFIX_3",
            "DATACENTER-US-NY",
        ],
    ),
]

POLICIES = [
    "GLOBAL_POLICY",
    "NORTH_AMERICA_POLICY",
    "EUROPE_POLICY",
    "FRA_POLICY",
    "FRA_FW1_POLICY",
]

RULES = [
    # name, policy, index, action,
    # source_zone, destination_zone,
    # source_address, source_groups, source_services,source_service_groups,
    # destination_address, destination_groups, destination_services, destination_service_groups
    (
        "permit-inbound-smtp",
        "GLOBAL_POLICY",
        0,
        "permit",
        "OUTSIDE",
        "DMZ",
        ["ANY"],
        [],
        [],
        [],
        [],
        ["SMTP_SERVERS"],
        ["SMTP"],
        [],
    ),
    (
        "deny-smtp-servers-outbound",
        "GLOBAL_POLICY",
        1,
        "deny",
        "DMZ",
        "OUTSIDE",
        [],
        ["SMTP_SERVERS"],
        [],
        [],
        ["ANY"],
        [],
        ["IP"],
        [],
    ),
    (
        "permit-web-proxies-outbound",
        "EUROPE_POLICY",
        0,
        "permit",
        "DMZ",
        "OUTSIDE",
        [],
        ["EUR_WEB_PROXY"],
        [],
        [],
        ["ANY"],
        [],
        [],
        ["HTTP_HTTPS"],
    ),
    (
        "deny-web-proxies-ip-outbound",
        "EUROPE_POLICY",
        1,
        "deny",
        "DMZ",
        "OUTSIDE",
        [],
        ["EUR_WEB_PROXY"],
        [],
        [],
        ["ANY"],
        [],
        ["IP"],
        [],
    ),
    (
        "permit-internal-to-webproxies",
        "EUROPE_POLICY",
        2,
        "permit",
        "INSIDE",
        "DMZ",
        ["ANY"],
        [],
        [],
        [],
        [],
        ["EUR_WEB_PROXY"],
        ["HTTP"],
        [],
    ),
    (
        "permit-smpt-servers-icmp",
        "EUROPE_POLICY",
        3,
        "permit",
        "DMZ",
        "OUTSIDE",
        [],
        ["SMTP_SERVERS"],
        [],
        [],
        ["ANY"],
        [],
        ["ICMP"],
        [],
    ),
    (
        "permit-inbound-smtp",
        "EUROPE_POLICY",
        4,
        "permit",
        "OUTSIDE",
        "DMZ",
        ["ANY"],
        [],
        [],
        [],
        [],
        ["SMTP_SERVERS"],
        ["SMTP"],
        [],
    ),
    (
        "deny-block-internet-internal",
        "FRA_POLICY",
        0,
        "deny",
        "OUTSIDE",
        "INSIDE",
        [],
        ["BLOCK_INTERNET"],
        [],
        [],
        ["ANY"],
        [],
        ["IP"],
        [],
    ),
    (
        "deny-block-internet-dmz",
        "FRA_POLICY",
        1,
        "deny",
        "OUTSIDE",
        "DMZ",
        [],
        ["BLOCK_INTERNET"],
        [],
        [],
        ["ANY"],
        [],
        ["IP"],
        [],
    ),
    (
        "permit-internal-ssh-smtp-servers",
        "FRA_POLICY",
        2,
        "permit",
        "INSIDE",
        "DMZ",
        ["ANY"],
        [],
        [],
        [],
        [],
        ["SMTP_SERVERS"],
        ["SSH"],
        [],
    ),
    (
        "permit-extranet-ssh-internal",
        "FRA_POLICY",
        999,
        "permit",
        "EXTRANET",
        "INSIDE",
        ["ANY"],
        [],
        [],
        [],
        ["ANY"],
        [],
        ["SSH"],
        [],
    ),
    (
        "deny-internal-block-internet",
        "FRA_FW1_POLICY",
        0,
        "deny",
        "INSIDE",
        "OUTSIDE",
        ["ANY"],
        [],
        [],
        [],
        [],
        ["BLOCK_INTERNET"],
        ["IP"],
        [],
    ),
]

DESIGN_ELEMENTS = [
    (
        "JUNIPER 2 FIREWALLS SRX-1500",
        "2 Juniper SRX-1500 firewalls",
        2,
        "firewall",
        "SRX-1500",
        [
            {"name": "fxp0", "type": "1000base-t", "role": "management"},
            {"name": "ge-0/0/0", "type": "1000base-t", "role": "leaf"},
            {"name": "ge-0/0/1", "type": "1000base-t", "role": "leaf"},
            {"name": "ge-0/0/2", "type": "1000base-t", "role": "leaf"},
            {"name": "ge-0/0/3", "type": "1000base-t", "role": "leaf"},
        ],
    ),
]

DESIGN = [
    (
        "POP S",
        "POP 2 firewalls",
        "POP",
        [
            "JUNIPER 2 FIREWALLS SRX-1500",
        ],
    ),
]


POP_DEPLOYMENT = {
    "name": "EQX-1",
    "location": "Frankfurt",
    "description": "Frankfurt POP",
    "asn": 65005,
    "customer": "10.11.0.0/24",
    "management": "10.117.0.0/24",
    "design": "POP S",
    "provider": "Technology Partner",
}


FIREWALLS = (
    # name, device_type, platform, status, role, location, management_ip, policy
    (
        "eqx1-fra-fw1",
        "SRX-1500",
        "Juniper JunOS",
        "active",
        "customer_firewall",
        "DC-1",
        "10.117.0.10",
        "FRA_POLICY",
    ),
    (
        "eqx1-fra-fw2",
        "SRX-1500",
        "Juniper JunOS",
        "active",
        "edge_firewall",
        "EQX-1",
        "10.117.0.20",
        None,
    ),
)

L3_INTERFACES = (
    # device, interface,speed, zone, ip, description, role
    (
        "eqx1-fra-fw1",
        "ge-0/0/0",
        1_000_000,
        None,
        "10.117.0.10",
        "dc1-fra-fw1 management interface",
        "management",
    ),
    (
        "eqx1-fra-fw1",
        "ge-0/0/1",
        1_000_000,
        "OUTSIDE",
        "10.0.1.1",
        "dc1-fra-fw1 outside interface",
        "peer",
    ),
    (
        "eqx1-fra-fw1",
        "ge-0/0/2",
        1_000_000,
        "INSIDE",
        "10.0.2.1",
        "dc1-fra-fw1 inside interface",
        "peer",
    ),
    (
        "eqx1-fra-fw1",
        "ge-0/0/3",
        1_000_000,
        "DMZ",
        "10.0.3.1",
        "dc1-fra-fw1 dmz interface",
        "peer",
    ),
    (
        "eqx1-fra-fw2",
        "ge-0/0/0",
        1_000_000,
        None,
        "10.117.0.20",
        "dc1-fra-fw1 management interface",
        "management",
    ),
    (
        "eqx1-fra-fw2",
        "ge-0/0/1",
        1_000_000,
        None,
        "10.1.1.1",
        "eqx1-fra-fw1 outside interface",
        "peer",
    ),
    (
        "eqx1-fra-fw2",
        "ge-0/0/2",
        1_000_000,
        None,
        "10.1.2.1",
        "eqx1-fra-fw1 inside interface",
        "peer",
    ),
)
