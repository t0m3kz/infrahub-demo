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
    ("firewall_devices", "Firewall Devices"),
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
