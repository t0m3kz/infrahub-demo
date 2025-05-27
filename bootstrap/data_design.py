"""Data for Design demo"""

ASN_POOLS = [
    # name, description, node, node_attribute, start, end
    (
        "PRIVATE-ASN32",
        "Private 32 bit ASN Pool",
        "ServiceAutonomousSystem",
        "asn",
        4200000000,
        4294967294,
    ),
    (
        "PRIVATE-ASN4",
        "Private 4 bit ASN Pool",
        "ServiceAutonomousSystem",
        "asn",
        64512,
        65534,
    ),
]


# DC_DEPLOYMENT = {
#     "name": "DC-2",
#     "location": "Katowice",
#     "description": "Katowice Data Center",
#     "asn": 65005,
#     "management": "172.20.2.0/24",
#     "strategy": "ospf-ibgp",
#     "customer": "10.2.0.0/16",
#     "technical": "1.2.0.0/24",
#     "design": "SONIC S",
#     "emulation": True,
#     "provider": "Technology Partner",
# }


DC_DEPLOYMENT = {
    "name": "DC-3",
    "location": "Katowice",
    "description": "Katowice Data Center",
    "asn": 65005,
    "management": "172.20.3.0/24",
    "strategy": "ospf-ibgp",
    "customer": "10.3.0.0/16",
    "technical": "1.3.0.0/24",
    "design": "CISCO S",
    "emulation": True,
    "provider": "Technology Partner",
}
