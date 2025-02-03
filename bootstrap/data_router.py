"""Router demo data."""

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


DESIGN_ELEMENTS = [
    (
        "CISCO S 2 EDGE N9K-C9316D-GX",
        "2 Cisco N9K-C9316D-GX edges",
        2,
        "edge",
        "N9K-C9316D-GX",
        [
            {"name": "Ethernet1/1", "type": "400gbase-x-qsfpdd", "role": "uplink"},
            {"name": "Ethernet1/2", "type": "400gbase-x-qsfpdd", "role": "uplink"},
            {"name": "Ethernet1/3", "type": "400gbase-x-qsfpdd", "role": "uplink"},
            {"name": "Ethernet1/4", "type": "400gbase-x-qsfpdd", "role": "uplink"},
            {"name": "Ethernet1/5", "type": "400gbase-x-qsfpdd", "role": "uplink"},
            {"name": "Ethernet1/6", "type": "400gbase-x-qsfpdd", "role": "uplink"},
            {"name": "Ethernet1/7", "type": "400gbase-x-qsfpdd", "role": "uplink"},
            {"name": "Ethernet1/8", "type": "400gbase-x-qsfpdd", "role": "uplink"},
            {"name": "Ethernet1/9", "type": "400gbase-x-qsfpdd", "role": "uplink"},
            {"name": "Ethernet1/10", "type": "400gbase-x-qsfpdd", "role": "uplink"},
            {"name": "Ethernet1/11", "type": "400gbase-x-qsfpdd", "role": "uplink"},
            {"name": "Ethernet1/12", "type": "400gbase-x-qsfpdd", "role": "uplink"},
            {"name": "Ethernet1/13", "type": "400gbase-x-qsfpdd", "role": "uplink"},
            {"name": "Ethernet1/14", "type": "400gbase-x-qsfpdd", "role": "uplink"},
            {"name": "Ethernet1/15", "type": "400gbase-x-qsfpdd", "role": "peering"},
            {"name": "Ethernet1/16", "type": "400gbase-x-qsfpdd", "role": "peering"},
            {"name": "mgmt0", "type": "1000base-t", "role": "management"},
            {"name": "con", "type": "1000base-t", "role": "console"},
        ],
    ),
]

DESIGN = [
    (
        "POP S",
        "POP 2 routers",
        "POP",
        [
            "CISCO S 2 EDGE N9K-C9316D-GX",
        ],
    ),
]


POP_DEPLOYMENT = {
    "name": "EQX-1",
    "location": "Frankfurt",
    "description": "Frankfurt POP",
    "asn": 65005,
    "customer": "10.11.0.0/24",
    "management": "10.112.0.0/24",
    "design": "POP S",
    "emulation": False,
    "provider": "Technology Partner",
}

ROUTERS = (
    # name, device_type, platform, status, role, location, management_ip,
    (
        "eqx1-edge-01",
        "N9K-C9316D-GX",
        "Cisco NX-OS",
        "active",
        "edge",
        "EQX-1",
        "10.100.7.120",
    ),
    (
        "eqx1-edge-02",
        "N9K-C9316D-GX",
        "Cisco NX-OS",
        "active",
        "edge",
        "EQX-1",
        "10.100.7.121",
    ),
)

INTERFACES = (
    # device, interface,speed, zone, ip, description, role
    (
        "eqx1-edge-01",
        "mgmt0",
        1_000,
        None,
        "10.100.7.120",
        "eqx1-edge-01 management interface",
        "management",
    ),
    (
        "eqx1-edge-01",
        "eth0/0",
        1_000_000,
        None,
        None,
        "eqx1-edge-01 outside interface",
        "peering",
    ),
    (
        "eqx1-edge-01",
        "eth0/1",
        1_000_000,
        None,
        "10.7.2.1",
        "eqx1-edge-01 inside interface",
        "peering",
    ),
    (
        "eqx1-edge-01",
        "eth0/2",
        1_000_000,
        "DMZ",
        "10.0.3.1",
        "eqx1-edge-01 dmz interface",
        "peering",
    ),
    (
        "eqx1-edge-02",
        "mgmt0",
        1_000_000,
        None,
        "10.100.7.121",
        "eqx1-edge-01 management interface",
        "management",
    ),
    (
        "eqx1-edge-02",
        "eth0/0",
        1_000_000,
        None,
        "10.1.1.1",
        "eqx1-edge-02 outside interface",
        "peering",
    ),
    (
        "eqx1-edge-02",
        "eth0/1",
        1_000_000,
        None,
        None,
        "eqx1-edge-02 inside interface",
        "peering",
    ),
    (
        "eqx1-edge-02",
        "eth0/2",
        1_000_000,
        None,
        "10.1.2.1",
        "eqx1-edge-02 inside interface",
        "peering",
    ),
)
