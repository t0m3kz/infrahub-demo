"""The 30_all virtual circuits the data declares, read from its YAML, for the circuit placement data test."""

from typing import TypedDict

from generators.helpers.naming import sdwan_edge_name
from tests.integration.demo_data import ROOT, Declared, declared, distinct, name

ALL_DEMO_DATA = "data/demos/30_all"

TUNNEL_TYPE = "tunnel"
INTERCONNECT_TYPE = "dci"
HANDOFF_TYPE = "handoff"

SDWAN_EDGE_UPLINK = "GE1"


def all_demo(kind: str) -> list[Declared]:
    """The distinct objects of ``kind`` 30_all declares (a re-declaration that updates one counts once)."""
    return distinct(declared(kind, ROOT / ALL_DEMO_DATA))


# The offices that opt into add_sdwan_edge (generators/fabric/sdwan_edge.py) by naming a gateway in sdwan_gateway.
_SDWAN_OFFICES: list[Declared] = [
    office for office in all_demo("TopologyCustomerOffice") if office.item.get("sdwan_gateway")
]


class VirtualCircuitExpectation(TypedDict):
    """What one session circuit (a tunnel, dci or handoff `circuit_type`) in 30_all must look like."""

    #: The `circuit_type` of the TopologySessionCircuit: tunnel, dci or handoff.
    circuit_type: str
    #: The circuit's locations, sorted.
    locations: tuple[str, ...]
    #: The (device, interface) ends in `capabilities`, sorted.
    ends: tuple[tuple[str, str], ...]
    #: circuit_ids of the carrier circuits derived from the ends (on the end port, or on its parent interface when it is
    #: a sub-interface), in end order, each once; never declared.
    carrier: tuple[str, ...]
    #: The routing zone of the circuit's `vrf`: a Handoff's
    security_zone: str | None


_DEVICE_DEPLOYMENTS: dict[str, str] = {
    name(device): str(device.value("deployment")) for device in all_demo("DcimPhysicalDevice")
}


#: The public port of the shared SD-WAN gateway every tunnel ends on (its one `customer` physical port).
ALL_DEMO_SDWAN_GATEWAY_PUBLIC_PORT = "eth1"

#: The circuit that carries the gateway's public port.
ALL_DEMO_SDWAN_GATEWAY_ACCESS = "INET-LUMEN-EQXFR2-GW"


def _sdwan_tunnel(office: Declared) -> tuple[str, VirtualCircuitExpectation]:
    """The tunnel add_sdwan_edge builds for one opted-in office, named as the generator names it."""
    office_name = name(office)
    gateway = str(office.item["sdwan_gateway"])
    zone = _DEVICE_DEPLOYMENTS[gateway]
    return f"{office_name}-SDWAN-{zone}", {
        "circuit_type": TUNNEL_TYPE,
        "locations": tuple(sorted((office_name, zone))),
        "ends": tuple(
            sorted(
                (
                    (gateway, ALL_DEMO_SDWAN_GATEWAY_PUBLIC_PORT),
                    (sdwan_edge_name(office_name), SDWAN_EDGE_UPLINK),
                )
            )
        ),
        "carrier": (f"INET-{office_name}", ALL_DEMO_SDWAN_GATEWAY_ACCESS),
        "security_zone": None,
    }


# The virtual circuits the placement check must see
ALL_DEMO_VIRTUAL_CIRCUITS: dict[str, VirtualCircuitExpectation] = {
    # One generated tunnel per SD-WAN office (_sdwan_tunnel).
    **dict(_sdwan_tunnel(office) for office in _SDWAN_OFFICES),
    # C001's partner extranet (08_interconnects/07_zone_policies/01_partner_tunnel.yml)
    "C001-PARTNER-ACME-FR2": {
        "circuit_type": TUNNEL_TYPE,
        "locations": ("C001-OFFICE", "FR2"),
        "ends": (("EQX-FR2-SDWAN-GW1", "eth1"), ("EXT-ACME-EDGE1", "et-0/0/0")),
        "carrier": ("INET-LUMEN-EQXFR2-GW", "INET-ACME-COGENT"),
        "security_zone": "PROD",
    },
    # The DCI: ends = the fibre's two ports, locations = the fibre's two facilities.
    **{
        circuit_name: {
            "circuit_type": INTERCONNECT_TYPE,
            "locations": tuple(sorted(locations)),
            "ends": ends,
            "carrier": (fibre,),
            "security_zone": None,
        }
        for circuit_name, locations, ends, fibre in (
            (
                "ICX-DC10-EQXFR2",
                ("DC10", "FR2"),
                (("bl-dc101101", "Ethernet1/35"), ("eg-fr201", "Ethernet1/10")),
                "DF-DC10-EQXFR2",
            ),
            (
                "ICX-DC11-EQXFR2",
                ("DC11", "FR2"),
                (("bl-dc1111101", "Ethernet1/35"), ("eg-fr201", "Ethernet1/11")),
                "DF-DC11-EQXFR2",
            ),
            (
                "ICX-DC12-EQXPA4",
                ("DC12", "PA4"),
                (("bl-dc1212101", "Ethernet1/35"), ("eg-pa401", "Ethernet1/10")),
                "DF-DC12-EQXPA4",
            ),
            (
                "ICX-DC10-EQXFR2-B",
                ("DC10", "FR2"),
                (("bl-dc101101", "Ethernet1/36"), ("eg-fr202", "Ethernet1/10")),
                "DF-DC10-EQXFR2-B",
            ),
            (
                "ICX-DC11-EQXFR2-B",
                ("DC11", "FR2"),
                (("bl-dc1111101", "Ethernet1/36"), ("eg-fr202", "Ethernet1/11")),
                "DF-DC11-EQXFR2-B",
            ),
            (
                "ICX-DC12-EQXPA4-B",
                ("DC12", "PA4"),
                (("bl-dc1212101", "Ethernet1/36"), ("eg-pa402", "Ethernet1/10")),
                "DF-DC12-EQXPA4-B",
            ),
        )
    },
    # The SD-WAN gateway's LAN handoff (04_sdwan/06_vrf_handoff.yml): both ends in one cage
    "HANDOFF-FR2-SDWAN-GW1-PROD": {
        "circuit_type": HANDOFF_TYPE,
        "locations": ("FR2",),
        "ends": (("EQX-FR2-SDWAN-GW1", "eth0.1900"), ("eg-fr201", "Ethernet1/12.1900")),
        "carrier": (),
        "security_zone": "PROD",
    },
    # C001's AWS handoff (08_interconnects/03_virtual_circuits/02_aws_prod_handoff.yml)
    "HANDOFF-FR2-AWS-EUC1-PROD": {
        "circuit_type": HANDOFF_TYPE,
        "locations": ("FR2", "eu-central-1"),
        "ends": (("dx-30all-euc1-fr2", "dxcon-fr2.1000"), ("eg-fr201", "Ethernet1/13.1000")),
        "carrier": (),
        "security_zone": "PROD",
    },
}


ALL_DEMO_CUSTOMER_ORG_IDS: tuple[str, ...] = tuple(
    sorted(str(customer.item["org_id"]) for customer in all_demo("OrganizationCustomer"))
)
