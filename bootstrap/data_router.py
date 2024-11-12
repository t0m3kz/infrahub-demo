"""Router demo data."""

# VRFS = {
#     # Name, Description, RD, RT-import, RT-export
#     ("Internet", "Internet VRF", "100", "100:100", "100:100"),
#     ("Backbone", "Backbone VRF", "101", "101:101", "101:101"),
#     ("Management", "OOBA Management VRF", "199", "199:199", "199:199"),
#     ("Production", "Production VRF", "200", "200:200", "200:200"),
#     ("Test", "Staging VRF", "201", "201:201", "201:201"),
#     ("Development", "Development VRF", "202", "202:202", "202:202"),
#     ("DMZ", "DMZ VRF", "666", "666:666", "666:666"),
#     ("Test-1", "Test VRF", "199", None, "199:199"),
#     ("Test-2", "Test VRF", "199", "199:199", None),
# }

# ROUTE_TARGETS = {
#     # Name, Description
#     ("100:100", "Internet VRF Route Target"),
#     ("101:101", "Backbone VRF Route Target"),
#     ("199:199", "OOBA Management VRF Route Target"),
#     ("200:200", "Production Environment VRF Route Target"),
#     ("201:201", "Staging Environment VRF Route Target"),
#     ("202:202", "Development Environment VRF Route Target"),
#     ("666:666", "DMZ VRF Route Target"),
# }

ROUTERS = (
    # name, device_type, platform, status, role, location, management_ip,
    (
        "dc2-ktw-core1",
        "N9K-C9316D-GX",
        "Cisco NX-OS",
        "active",
        "core",
        "DC-2",
        "10.100.7.120",
    ),
    (
        "dc2-ktw-core2",
        "N9K-C9316D-GX",
        "Cisco NX-OS",
        "active",
        "core",
        "DC-2",
        "10.100.7.121",
    ),
)

INTERFACES = (
    # device, interface,speed, zone, ip, description, role
    (
        "dc2-ktw-core1",
        "mgmt0",
        1_000,
        None,
        "10.100.7.120",
        "dc2-ktw-core1 management interface",
        "management",
    ),
    (
        "dc2-ktw-core1",
        "eth0/0",
        1_000_000,
        None,
        None,
        "dc2-ktw-core1 outside interface",
        "peering",
    ),
    (
        "dc2-ktw-core1",
        "eth0/1",
        1_000_000,
        None,
        "10.7.2.1",
        "dc2-ktw-core1 inside interface",
        "peering",
    ),
    (
        "dc2-ktw-core1",
        "eth0/2",
        1_000_000,
        "DMZ",
        "10.0.3.1",
        "dc2-ktw-core1 dmz interface",
        "peering",
    ),
    (
        "dc2-ktw-core2",
        "mgmt0",
        1_000_000,
        None,
        "10.100.7.121",
        "dc2-ktw-core1 management interface",
        "management",
    ),
    (
        "dc2-ktw-core2",
        "eth0/0",
        1_000_000,
        None,
        "10.1.1.1",
        "dc2-ktw-core2 outside interface",
        "peering",
    ),
    (
        "dc2-ktw-core2",
        "eth0/2",
        1_000_000,
        None,
        "10.1.2.1",
        "dc2-ktw-core2 inside interface",
        "peering",
    ),
)
