"""Static data"""

REGIONS = (
    # name, shortname
    ("EMEA", "emea"),
    ("APAC", "apac"),
    ("AMERICAS", "americas"),
)

COUNTRIES = (
    # name, shortname, parent(REGION)
    # Europe
    ("France", "FR", "EMEA"),
    ("Germany", "DE", "EMEA"),
    ("Netherlands", "NL", "EMEA"),
    # Americas
    ("United States of America", "USA", "AMERICAS"),
    ("Canada", "CA", "AMERICAS"),
    # Asia
    ("China", "CN", "APAC"),
    ("Japan", "JP", "APAC"),
    ("Korea", "KR", "APAC"),
    ("India", "IN", "APAC"),
    # Africa
    ("Egypt", "EG", "EMEA"),
    ("South Africa", "ZA", "EMEA"),
)

CITIES = (
    # name, shortname, parent (COUNTRY)
    ("Paris", "PAR", "France"),
    ("Frankfurt", "FRA", "Germany"),
    ("Amsterdam", "AMS", "Netherlands"),
    ("New York", "NYC", "United States of America"),
    ("Toronto", "TOR", "Canada"),
    ("Beijing", "BJ", "China"),
    ("Tokyo", "TYO", "Japan"),
    ("Seoul", "SEL", "Korea"),
    ("Delhi", "DEL", "India"),
    ("Cairo", "CAI", "Egypt"),
    ("Johannesburg", "JNB", "South Africa"),
)

SITES = (
    # name, shortname, status, site_type, parent
    ("DC-1", "DC1", "active", "dc", "Frankfurt"),
    ("EQX-1", "EQX1", "active", "pop", "Frankfurt"),
    ("FRA-1", "FRA1", "active", "campus", "Frankfurt"),
    ("FRA-2", "FRA2", "active", "office", "Frankfurt"),
)

ACCOUNTS = (
    # name, password, type, role
    ("pop-builder", "Script", "Password123", "read-write"),
    ("generator", "Script", "Password123", "read-write"),
    ("CRM Synchronization", "Script", "Password123", "read-write"),
    ("Tomek Zajac", "User", "Password123", "read-write"),
    ("Some User1", "User", "Password123", "read-only"),
    ("Some User2", "User", "Password123", "read-write"),
    ("Some User3", "User", "Password123", "read-write"),
    ("Engineering Team", "User", "Password123", "read-write"),
    ("Architecture Team", "User", "Password123", "read-only"),
    ("Operation Team", "User", "Password123", "read-only"),
)

TAGS = ["blue", "green", "red"]


PROVIDERS = [
    "Arelion",
    "Colt Technology Services",
    "Verizon Business",
    "GTT Communications",
    "Hurricane Electric",
    "Lumen",
    "Zayo",
    "Equinix",
    "Interxion",
    "PCCW Global",
    "Orange S.A",
    "Tata Communications",
    "Sprint",
    "NTT America",
    "Cogent Communications",
    "Comcast Cable Communication",
    "Telecom Italia Sparkle",
    "AT&T Services",
    "Technology Partner",
]

CUSTOMERS = ["OE1", "ABC", "CDE"]

MANUFACTURERS = ["Juniper", "Cisco", "Arista", "Perle", "Citrix", "Sonic", "Linux"]

SUBNETS_1918 = {
    # prefix, state, owner
    ("10.0.0.0/8", "active"),
    ("172.16.0.0/16", "active"),
    ("192.168.0.0/16", "active"),
}


ASNS = (
    # asn, organization
    (1299, "Arelion"),
    (8220, "Colt Technology Services"),
    (701, "Verizon Business"),
    (3257, "GTT Communications"),
    (6939, "Hurricane Electric"),
    (3356, "Lumen"),
    (6461, "Zayo"),
    (24115, "Equinix"),
    (20710, "Interxion"),
    (3491, "PCCW Global"),
    (5511, "Orange S.A"),
    (6453, "Tata Communications"),
    (1239, "Sprint"),
    (2914, "NTT America"),
    (174, "Cogent Communications"),
    (7922, "Comcast Cable Communication"),
    (6762, "Telecom Italia Sparkle"),
    (7018, "AT&T Services"),
)

VRFS = {
    # Name, Description, RD, RT-import, RT-export
    ("Internet", "Internet VRF", "100", "100:100", "100:100"),
    ("Backbone", "Backbone VRF", "101", "101:101", "101:101"),
    ("Management", "OOBA Management VRF", "199", "199:199", "199:199"),
    ("Production", "Production VRF", "200", "200:200", "200:200"),
    ("Test", "Staging VRF", "201", "201:201", "201:201"),
    ("Development", "Development VRF", "202", "202:202", "202:202"),
    ("DMZ", "DMZ VRF", "666", "666:666", "666:666"),
    ("Test-1", "Test VRF", "199", None, "199:199"),
    ("Test-2", "Test VRF", "199", "199:199", None),
}

ROUTE_TARGETS = {
    # Name, Description
    ("100:100", "Internet VRF Route Target"),
    ("101:101", "Backbone VRF Route Target"),
    ("199:199", "OOBA Management VRF Route Target"),
    ("200:200", "Production Environment VRF Route Target"),
    ("201:201", "Staging Environment VRF Route Target"),
    ("202:202", "Development Environment VRF Route Target"),
    ("666:666", "DMZ VRF Route Target"),
}

PLATFORMS = (
    # name, nornir_platform, napalm_driver, netmiko_device_type, ansible_network_os, containerlab_os
    ("Cisco IOS-XE", "ios", "ios", "cisco_ios", "ios", "ios"),
    ("Cisco IOS-XR", "iosxr", "iosxr", "cisco_xr", "cisco.iosxr.iosxr", "cisco_xrv"),
    ("Cisco NX-OS", "nxos_ssh", "nxos_ssh", "cisco_nxos", "nxos", "cisco_n9kv"),
    (
        "Juniper JunOS",
        "junos",
        "junos",
        "juniper_junos",
        "junos",
        "juniper_vjunosswitch",
    ),
    ("Arista EOS", "eos", "eos", "arista_eos", "eos", "ceos"),
    ("Sonic OS", "dell_sonic", "dell_sonic", "dell_sonic", "dell_sonic", "dell_sonic"),
    ("Linux", "linux", "linux", "linux", "linux", "linux"),
    ("Perle IOLAN", "linux", "linux", "linux", "linux", "linux"),
)

DEVICE_TYPES = (
    # name, part_number, height (U), full_depth, platform
    ("MX204", "MX204-HWBASE-AC-FS", 1, False, "Juniper JunOS"),
    ("CCS-720DP-48S-2F", None, 1, False, "Arista EOS"),
    ("DCS-7280DR3-24-F", None, 1, False, "Arista EOS"),
    ("NCS-5501-SE", None, 1, False, "Cisco IOS-XR"),
    ("ASR1002-HX", None, 2, True, "Cisco IOS-XR"),
    ("N9K-C9000V", "N9K-C9000V", 1, False, "Cisco NX-OS"),
    ("N9K-C93108TC-FX", "N9K-C93108TC-FX", 1, False, "Cisco NX-OS"),
    ("N9K-C93108TC-FX3", "N9K-C93108TC-FX3", 1, False, "Cisco NX-OS"),
    ("N9K-C93108TC-FX3P", None, 1, False, "Cisco NX-OS"),
    ("N9K-C93120TX", None, 1, False, "Cisco NX-OS"),
    ("N9K-C93128TX", None, 1, False, "Cisco NX-OS"),
    ("N9K-C9316D-GX", None, 1, False, "Cisco NX-OS"),
    ("N9K-C93180YC-EX", None, 1, False, "Cisco NX-OS"),
    ("N9K-C93180YC-FX", None, 1, False, "Cisco NX-OS"),
    ("N9K-C93180YC-FX3", None, 1, False, "Cisco NX-OS"),
    ("N9K-C93180YC-FX3S", None, 1, False, "Cisco NX-OS"),
    ("N9K-C93216TC-FX2", None, 1, False, "Cisco NX-OS"),
    ("N9K-C93240YC-FX2", None, 1, False, "Cisco NX-OS"),
    ("N9K-C9332C", None, 1, False, "Cisco NX-OS"),
    ("N9K-C9332D-GX2B", None, 1, False, "Cisco NX-OS"),
    ("N9K-C9332PQ", None, 1, False, "Cisco NX-OS"),
    ("N9K-C9336C-FX2", None, 1, False, "Cisco NX-OS"),
    ("N9K-C9336PQ", None, 1, False, "Cisco NX-OS"),
    ("N9K-C9348D-GX2A", None, 1, False, "Cisco NX-OS"),
    ("N9K-C9348GC-FX3", None, 1, False, "Cisco NX-OS"),
    ("N9K-C9348GC-FX3PH", None, 1, False, "Cisco NX-OS"),
    ("N9K-C9348GC-FXP", None, 1, False, "Cisco NX-OS"),
    ("N9K-C93600CD-GX", None, 1, False, "Cisco NX-OS"),
    ("N9K-C9364C-GX", None, 1, False, "Cisco NX-OS"),
    ("N9K-C9364C", None, 1, False, "Cisco NX-OS"),
    ("N9K-C9364D-GX2A", None, 1, False, "Cisco NX-OS"),
    ("N9K-C9372PX-E", None, 1, False, "Cisco NX-OS"),
    ("N9K-C9372PX", None, 1, False, "Cisco NX-OS"),
    ("N9K-C9372TX-E", None, 1, False, "Cisco NX-OS"),
    ("N9K-C9372TX", None, 1, False, "Cisco NX-OS"),
    ("N9K-C9396PX", None, 1, False, "Cisco NX-OS"),
    ("SRX-1500", None, 1, False, "Juniper JunOS"),
    ("C9200L-48P-4G", None, 1, False, "Cisco IOS-XE"),
    ("C9200L-24P-4G", None, 1, False, "Cisco IOS-XE"),
    ("SCG50R", None, 1, False, "Perle IOLAN"),
)

GROUPS = (
    # name, description
    ("edge_routers", "Edge Routers"),
    ("core_routers", "Core Routers"),
    ("cisco_devices", "Cisco Devices"),
    ("arista_devices", "Arista Devices"),
    ("juniper_devices", "Juniper Devices"),
    ("perle_devices", "Perle Devices"),
    ("upstream_interfaces", "Upstream Interface"),
    ("core_interfaces", "Core Interface"),
    ("all_topologies", "All Topologies"),
    ("provisioning_circuits", "Provisioning Circuits"),
)

BGP_PEER_GROUPS = (
    #     # name, import policy, export policy, local AS, remote AS
    #     ("POP_INTERNAL", "IMPORT_INTRA_POP", "EXPORT_INTRA_POP", "AS65000", "AS65000"),
    #     ("POP_GLOBAL", "IMPORT_POP_GLOBAL", "EXPORT_POP_GLOBLA", "AS65000", None),
    #     ("UPSTREAM_DEFAULT", "IMPORT_UPSTREAM", "EXPORT_PUBLIC_PREFIX", "AS65000", None),
    #     (
    #         "UPSTREAM_ARELION",
    #         "IMPORT_UPSTREAM",
    #         "EXPORT_PUBLIC_PREFIX",
    #         "AS65000",
    #         "AS1299",
    #     ),
    #     ("IX_DEFAULT", "IMPORT_IX", "EXPORT_PUBLIC_PREFIX", "AS65000", None),
)

TOPOLOGY = (
    # name, description, strategy,country, management, internal, data , external
    (
        "eqx-fra",
        "Medium Fabric in Equinix Frankfurt",
        65100,
        "ebgp-ebgp",
        "Frankfurt",
        "medium",
        "cisco",
        "172.16.0.0/23",
        "192.168.0.0/22",
        "10.1.0.0/16",
        "203.0.113.0/28",
    ),
    (
        "eqx-chi",
        "Small Fabric in Equinix Chicago",
        65200,
        "ospf-ibgp",
        "Chicago",
        "virtual",
        "cisco",
        "172.16.4.0/23",
        "192.168.4.0/22",
        "10.2.0.0/16",
    ),
)

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

FIREWALLS = (
    # name, device_type, platform, status, role, location, management_ip, policy
    (
        "dc1-fra-fw1",
        "SRX-1500",
        "Juniper JunOS",
        "active",
        "firewall",
        "DC-1",
        "10.100.5.10",
        "FRA_POLICY",
    ),
    (
        "eqx1-fra-fw1",
        "SRX-1500",
        "Juniper JunOS",
        "active",
        "firewall",
        "EQX-1",
        "10.200.100.20",
        None,
    ),
)

L3_INTERFACES = (
    # device, interface, ,speed, zone, ip, description
    (
        "dc1-fra-fw1",
        "ge-0/0/0",
        1_000_000,
        None,
        "10.100.5.10",
        "dc1-fra-fw1 management interface",
    ),
    (
        "dc1-fra-fw1",
        "ge-0/0/1",
        1_000_000,
        "OUTSIDE",
        "10.0.1.1",
        "dc1-fra-fw1 outside interface",
    ),
    (
        "dc1-fra-fw1",
        "ge-0/0/2",
        1_000_000,
        "INSIDE",
        "10.0.2.1",
        "dc1-fra-fw1 inside interface",
    ),
    (
        "dc1-fra-fw1",
        "ge-0/0/3",
        1_000_000,
        "DMZ",
        "10.0.3.1",
        "dc1-fra-fw1 dmz interface",
    ),
    (
        "eqx1-fra-fw1",
        "ge-0/0/0",
        1_000_000,
        None,
        "10.200.100.20",
        "dc1-fra-fw1 management interface",
    ),
    (
        "eqx1-fra-fw1",
        "ge-0/0/1",
        1_000_000,
        None,
        "10.1.1.1",
        "eqx1-fra-fw1 outside interface",
    ),
    (
        "eqx1-fra-fw1",
        "ge-0/0/2",
        1_000_000,
        None,
        "10.1.2.1",
        "eqx1-fra-fw1 inside interface",
    ),
)
