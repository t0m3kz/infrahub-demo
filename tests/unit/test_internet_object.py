"""THE INTERNET (TopologyInternet): a device that is a facility, with one shared interface (rules I1 to I3).

Every internet attachment (an ISP router's uplink, the public side of a SaaS or cloud region) is a carrier circuit of its
own with two ends: that port and THE INTERNET's one interface, a ``TopologyInternetInterface``. The kind of the
interface is what lets many carriers end on it (checks/carrier_circuit_ports.py), add_circuit places its end by the
device itself (it has no ``deployment``), checks/circuit_placement.py keeps the two-locations rule, and
utils/internet_object.py holds I1 to I3. Payloads mirror queries/validation/circuit_placement.gql.
"""

from __future__ import annotations

from typing import Any, cast

from checks.carrier_circuit_ports import CheckCarrierCircuitPorts
from checks.circuit_placement import CheckCircuitPlacement
from generators.topology.circuit import CircuitEndpoint, CircuitLocation, plan_locations
from tests.unit.circuit_placement_check_helpers import KINDS, _edges, _place
from utils.data_cleaning import clean_data
from utils.internet_object import internet_findings

INTERNET = _place("INTERNET", "TopologyInternet")
ISP_A = _place("ISP-A", "TopologyExternalProviders")
ISP_B = _place("ISP-B", "TopologyExternalProviders")
DC90 = _place("DC90", "TopologyDataCenter")
WEBEX = _place("WEBEX-FRANKFURT", "TopologySaasRegion")
EUC1 = _place("eu-central-1", "TopologyCloudRegion")
BUS = "TopologyInternetInterface"


def _device(name: str, place: dict[str, Any] | None, *, region: str | None = None) -> dict[str, Any]:
    """A device node as the query selects it: a physical device (deployment), a transport (region or SaaS region) or
    THE INTERNET (neither)."""
    node: dict[str, Any] = {"id": f"id-{name}", "__typename": "X", "name": {"value": name}}
    if region == "region":
        node.update(region={"node": place}, saas_region={"node": None})
    elif region == "saas":
        node.update(region={"node": None}, saas_region={"node": place})
    elif place is not None:
        node["deployment"] = {"node": place}
    return node


def _fabric_device(name: str, metro: dict[str, Any]) -> dict[str, Any]:
    """The provider's port of a colocation metro: no deployment, its metro places it."""
    return {
        "id": f"id-{name}",
        "__typename": "TopologyColocationConnect",
        "name": {"value": name},
        "metro": {"node": metro},
    }


def _end(device: dict[str, Any], port: str, kind: str = "DcimPhysicalInterface") -> dict[str, Any]:
    return {"id": f"{device['id']}:{port}", "__typename": kind, "name": {"value": port}, "device": {"node": device}}


EDGE = _device("dc90-edge-01", DC90)
PE_A = _device("PE-A", ISP_A)
PE_B = _device("PE-B", ISP_B)
NET = {**_device("INTERNET", None), "__typename": "TopologyInternet"}
BUS_END = _end(NET, "bus", BUS)
WEBEX_TRANSPORT = _device("webex-pub", WEBEX, region="saas")
S3_TRANSPORT = _device("s3-pub", EUC1, region="region")


def _carrier(
    name: str, locations: list[dict[str, Any]], ends: list[dict[str, Any]], circuit_type: str = "internet", **extra: Any
) -> dict[str, Any]:
    return {
        "id": f"id-{name}",
        "name": {"value": name},
        "status": {"value": extra.get("status", "active")},
        "circuit_type": {"value": circuit_type},
        "locations": _edges(locations),
        "capabilities": _edges(ends),
    }


ACCESS_A = _carrier("INET-A", [DC90, ISP_A], [_end(EDGE, "Ethernet1/2"), _end(PE_A, "et-0/0/0")])
ACCESS_B = _carrier("INET-B", [DC90, ISP_B], [_end(EDGE, "Ethernet1/1"), _end(PE_B, "et-0/0/0")])
UPLINK_A = _carrier("UPL-A", [ISP_A, INTERNET], [_end(PE_A, "xe-0/1/0"), BUS_END])
UPLINK_B = _carrier("UPL-B", [ISP_B, INTERNET], [_end(PE_B, "xe-0/1/0"), BUS_END])
PUBLIC_WEBEX = _carrier("PUB-WEBEX", [WEBEX, INTERNET], [_end(WEBEX_TRANSPORT, "pub-1"), BUS_END])
PUBLIC_S3 = _carrier("PUB-S3", [EUC1, INTERNET], [_end(S3_TRANSPORT, "pub-1"), BUS_END])
MEETING_POINT = [ACCESS_A, ACCESS_B, UPLINK_A, UPLINK_B, PUBLIC_WEBEX, PUBLIC_S3]
BUS_NODE = {"id": "id-bus", "__typename": BUS, "name": {"value": "bus"}}


def _internet(*interfaces: dict[str, Any]) -> dict[str, Any]:
    """An INTERNET object as the query selects it, with its interfaces (the bus by default)."""
    return {"id": "id-INTERNET", "name": {"value": "INTERNET"}, "interfaces": _edges(list(interfaces or [BUS_NODE]))}


def _run(carriers: list[dict[str, Any]], internet: list[dict[str, Any]] | None = None) -> list[str]:
    """Validate the carriers (and the INTERNET objects, one by default) and return the logged messages."""
    check = cast(Any, CheckCircuitPlacement.__new__(CheckCircuitPlacement))
    findings: list[dict[str, Any]] = []
    check.log_error = lambda **kwargs: findings.append(kwargs)
    check.validate(
        {
            "TopologyInternet": _edges([_internet()] if internet is None else internet),
            "TopologyCarrierCircuit": _edges(carriers),
            "TopologySessionCircuit": _edges([]),
        }
    )
    for finding in findings:
        assert finding["object_id"] and finding["object_type"] in KINDS | {"TopologyInternet"}
    return [finding["message"] for finding in findings]


def _findings(carriers: list[dict[str, Any]], objects: list[dict[str, Any]] | None = None) -> list[str]:
    cleaned = clean_data({"c": _edges(carriers), "i": _edges([_internet()] if objects is None else objects)})
    return [message for _, message in internet_findings(cleaned["c"], cleaned["i"])]


def _ports_check(carriers: list[dict[str, Any]]) -> list[str]:
    """CheckCarrierCircuitPorts over the carriers (ports, their kind and device name, as its query selects them)."""
    check = cast(Any, CheckCarrierCircuitPorts.__new__(CheckCarrierCircuitPorts))
    findings: list[str] = []
    check.log_error = lambda **kwargs: findings.append(kwargs["message"])
    circuits = [
        {
            "id": c["id"],
            "circuit_id": c["name"],
            "circuit_type": c["circuit_type"],
            "locations": c["locations"],
            "capabilities": _edges(
                [
                    {**e["node"], "device": {"node": {"name": e["node"]["device"]["node"]["name"]}}}
                    for e in c["capabilities"]["edges"]
                ]
            ),
        }
        for c in carriers
    ]
    check.validate({"TopologyCarrierCircuit": _edges(circuits)})
    return findings


class TestAddCircuitPlacesTheInternetEnd:
    """add_circuit places an end on the INTERNET interface by the device itself: it is the facility."""

    def _endpoint(self, device: dict[str, Any], port: str, kind: str = "DcimPhysicalInterface") -> CircuitEndpoint:
        node = {**device, "display_label": device["name"]["value"]}
        parsed = CircuitEndpoint.parse(clean_data({**_end(device, port, kind), "device": {"node": node}}))
        assert parsed is not None
        return parsed

    def test_the_end_on_the_internet_is_placed_by_the_device_itself(self) -> None:
        end = self._endpoint(NET, "bus", BUS)

        assert end.deployment == CircuitLocation(id="id-INTERNET", kind="TopologyInternet", label="INTERNET")

    def test_an_isp_uplink_derives_both_locations_and_authors_none(self) -> None:
        ends = [self._endpoint(PE_A, "xe-0/1/0"), self._endpoint(NET, "bus", BUS)]

        derived, problem = plan_locations([], ends)

        assert problem is None
        assert {location.kind for location in derived} == {"TopologyExternalProviders", "TopologyInternet"}

    def test_a_public_side_derives_its_region_and_the_internet(self) -> None:
        ends = [self._endpoint(WEBEX_TRANSPORT, "pub-1"), self._endpoint(NET, "bus", BUS)]

        derived, problem = plan_locations([], ends)

        assert problem is None
        assert {location.kind for location in derived} == {"TopologySaasRegion", "TopologyInternet"}

    def test_an_authored_internet_location_is_kept_and_not_derived_twice(self) -> None:
        authored = CircuitLocation.parse(clean_data({**INTERNET, "display_label": "INTERNET"}))
        assert authored is not None
        ends = [self._endpoint(PE_A, "xe-0/1/0"), self._endpoint(NET, "bus", BUS)]

        derived, problem = plan_locations([authored], ends)

        assert problem is None
        assert [location.kind for location in derived] == ["TopologyExternalProviders"]


class TestSharedInterface:
    def test_two_isps_a_saas_and_a_cloud_public_side_share_one_interface_and_pass(self) -> None:
        """Four carriers end on the one interface: the placement check (INTERNET is the location of its end) and the
        port check (the kind is exempt from one-carrier-per-port) both report nothing."""
        assert _run(MEETING_POINT) == []
        assert _ports_check(MEETING_POINT) == []

    def test_the_exemption_is_by_kind_a_physical_port_still_ends_one_carrier(self) -> None:
        twice = _carrier("UPL-A2", [ISP_A, INTERNET], [_end(PE_A, "xe-0/1/0"), BUS_END])
        (message,) = _ports_check([UPLINK_A, twice])
        assert "Port 'PE-A:xe-0/1/0' is an end of 2 carrier circuits" in message
        assert "INTERNET:bus" not in message

    def test_the_same_name_on_another_kind_is_not_exempt(self) -> None:
        """A physical port called `bus` is a port like any other: two carriers on it are refused."""
        physical = _end(NET, "bus")
        one = _carrier("UPL-A", [ISP_A, INTERNET], [_end(PE_A, "xe-0/1/0"), physical])
        two = _carrier("UPL-B", [ISP_B, INTERNET], [_end(PE_B, "xe-0/1/0"), physical])
        (message,) = _ports_check([one, two])
        assert "Port 'INTERNET:bus' is an end of 2 carrier circuits" in message

    def test_the_internet_interface_is_an_allowed_end_kind(self) -> None:
        """Rule 2 (both ends physical) accepts the kind: no finding for an end that is not a physical port."""
        assert _ports_check([UPLINK_A]) == []

    def test_any_other_kind_is_still_refused_as_an_end(self) -> None:
        other = _carrier("UPL-X", [ISP_A, INTERNET], [_end(PE_A, "xe-0/1/0"), _end(NET, "lo", "DcimVirtualInterface")])
        (message,) = _ports_check([other])
        assert "is not a DcimPhysicalInterface" in message and "TopologyInternetInterface" not in message


class TestAttachments:
    def test_an_attachment_must_list_the_facility_of_its_end(self) -> None:
        wrong = _carrier("UPL-A", [DC90, INTERNET], [_end(PE_A, "xe-0/1/0"), BUS_END])
        (message,) = _run([ACCESS_A, wrong])
        assert "TopologyDataCenter 'DC90'" in message and "terminates on" in message

    def test_a_carrier_that_does_not_list_the_internet_still_needs_its_two_locations(self) -> None:
        wrong = _carrier("UPL-A", [ISP_A], [_end(PE_A, "xe-0/1/0"), BUS_END])
        (message,) = _run([wrong])
        assert "1 location(s)" in message

    def test_an_attachment_is_an_internet_circuit(self) -> None:
        wrong = _carrier("UPL-A", [ISP_A, INTERNET], [_end(PE_A, "xe-0/1/0"), BUS_END], "cross_connect")
        (message,) = _run([ACCESS_A, wrong])
        assert "ends on THE INTERNET but is a cross_connect" in message

    def test_a_dc_cannot_attach_directly(self) -> None:
        """An edge reaches the internet through an ISP access circuit of its own."""
        bad = _carrier("DC-NET", [DC90, INTERNET], [_end(EDGE, "Ethernet1/3"), BUS_END])
        (message,) = _run([bad])  # the placement is right (the DC and INTERNET, one end each): only I2 speaks
        assert "dc90-edge-01" in message and "an ISP router, the port of a cloud or SaaS transport" in message

    def test_both_ends_on_the_interface_are_refused(self) -> None:
        loop = _carrier("LOOP", [INTERNET, INTERNET], [BUS_END, BUS_END])
        assert any("its interface again" in message for message in _findings([loop]))

    def test_the_interface_must_sit_on_the_internet_object(self) -> None:
        stray = _end(PE_A, "xe-0/1/9", BUS)
        carrier = _carrier("UPL-X", [ISP_A, INTERNET], [stray, _end(PE_B, "xe-0/1/0")])
        assert any("a device that is not THE INTERNET" in message for message in _findings([carrier]))

    def test_an_isp_router_that_serves_access_needs_an_attachment(self) -> None:
        """B has access and no attachment: no public side is reachable through it. A is fine."""
        (message,) = _run([ACCESS_A, ACCESS_B, UPLINK_A, PUBLIC_S3])
        assert "ISP router 'PE-B' serves internet access ('INET-B') but has no attachment" in message

    def test_one_finding_per_router_however_many_accesses(self) -> None:
        second = _carrier("INET-B2", [DC90, ISP_B], [_end(EDGE, "Ethernet1/5"), _end(PE_B, "et-0/0/1")])
        assert len(_findings([ACCESS_B, second])) == 1

    def test_a_partner_access_to_an_attached_isp_needs_no_attachment_of_its_own(self) -> None:
        """A partner router (ISP kind) buys access from PE-A, which is attached: its access reaches the public sides."""
        partner = _device("PARTNER-EDGE", _place("PARTNER", "TopologyExternalProviders"))
        access = _carrier(
            "INET-P",
            [ISP_A, _place("PARTNER", "TopologyExternalProviders")],
            [_end(partner, "et-0/0/0"), _end(PE_A, "et-0/0/1")],
        )
        assert _run([ACCESS_A, UPLINK_A, access]) == []

    def test_a_partner_access_to_an_unattached_isp_is_one_finding_per_router(self) -> None:
        partner = _device("PARTNER-EDGE", _place("PARTNER", "TopologyExternalProviders"))
        access = _carrier(
            "INET-P",
            [ISP_B, _place("PARTNER", "TopologyExternalProviders")],
            [_end(partner, "et-0/0/0"), _end(PE_B, "et-0/0/1")],
        )
        assert len(_findings([access])) == 2

    def test_without_an_internet_object_the_attachment_rule_is_silent(self) -> None:
        """A dataset without the INTERNET object (the bootstrap always has it): an access carrier alone is not judged."""
        assert _run([ACCESS_A, ACCESS_B], internet=[]) == []

    def test_a_decommissioned_access_needs_no_attachment(self) -> None:
        gone = _carrier("INET-B", [DC90, ISP_B], ACCESS_B["capabilities"]["edges"], status="decommissioned")
        assert _findings([gone]) == []

    def test_a_decommissioned_attachment_does_not_count(self) -> None:
        uplink = _carrier("UPL-A", [ISP_A, INTERNET], [_end(PE_A, "xe-0/1/0"), BUS_END], status="decommissioned")
        (message,) = _findings([ACCESS_A, uplink])
        assert "ISP router 'PE-A'" in message


class TestTheObject:
    def test_a_second_internet_object_is_refused(self) -> None:
        assert _findings([], [_internet(), _internet()]) == [
            "There are 2 INTERNET objects (INTERNET, INTERNET): THE INTERNET is one."
        ]

    def test_a_second_interface_is_refused(self) -> None:
        other = {"id": "id-lo", "__typename": BUS, "name": {"value": "bus2"}}
        (message,) = _findings([], [_internet(BUS_NODE, other)])
        assert "has 2 interface(s)" in message

    def test_an_interface_of_another_kind_is_refused(self) -> None:
        physical = {"id": "id-p", "__typename": "DcimPhysicalInterface", "name": {"value": "Ethernet1"}}
        (message,) = _findings([], [_internet(physical)])
        assert "1 of another kind than TopologyInternetInterface" in message

    def test_no_interface_is_refused(self) -> None:
        (message,) = _findings([], [{"id": "id-INTERNET", "name": {"value": "INTERNET"}, "interfaces": _edges([])}])
        assert "has 0 interface(s)" in message


FRA = _place("FRA", "TopologyColocationMetro")
FABRIC_FRA = _fabric_device("EXT-FABRIC-FRA", FRA)


def test_the_connection_interface_of_a_colocation_port_is_an_attachment() -> None:
    """An Internet Access service on the Fabric port of a metro ends on THE INTERNET: a far end placed by its metro."""
    internet_access = _carrier(
        "EQX-INET-FRA",
        [_place("FRA-COLO1", "TopologyColocationZone"), INTERNET],
        [_end(FABRIC_FRA, "et-0/0/0.1301", "TopologyColocationInterface"), BUS_END],
    )
    assert _findings([internet_access]) == []


def test_a_far_end_placed_by_nothing_known_is_still_refused() -> None:
    """A device with a place of another kind (a DC) is refused as before, whatever kind its port has."""
    bad = _carrier("INET-BAD", [DC90, INTERNET], [_end(EDGE, "Ethernet1/2"), BUS_END])
    assert _findings([bad])
