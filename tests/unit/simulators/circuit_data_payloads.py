"""Check payloads for the 30_all circuits, built from the object files instead of typed by hand."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from generators.helpers.naming import sdwan_edge_name
from generators.topology.circuit import CircuitEndpoint, CircuitLocation, plan_locations
from tests.integration.demo_data import ROOT, Declared, declared, distinct, name
from tests.unit.encapsulation_helpers import raw_encapsulation
from tests.unit.external_vrf_peers_helpers import _raw_vrf

ALL_DEMO = ROOT / "data" / "demos" / "30_all"
BOOTSTRAP = ROOT / "data" / "bootstrap"

CARRIER = "TopologyCarrierCircuit"
SESSION = "TopologySessionCircuit"
# The circuit_type of a session circuit.
TUNNEL = "tunnel"
INTERCONNECT = "dci"
HANDOFF = "handoff"

PHYSICAL_DEVICE = "DcimPhysicalDevice"
APPLIANCE = "CloudNetworkAppliance"
# A cloud transport has no deployment: its facility is its region.
TRANSPORT = "CloudDirectConnect"
PEERING = "CloudPeeringInterface"
# The provider's connect device has no deployment either: its facility is its location (a zone), and each connection
# that ends on it has an interface of its own.
PROVIDER_CONNECT = "TopologyProviderConnect"
PROVIDER_INTERFACE = "TopologyProviderInterface"
# THE INTERNET (bootstrap data): a device that is a facility itself, with one interface of its own kind.
INTERNET = "TopologyInternet"
INTERNET_BUS = "TopologyInternetInterface"
# Where the place kinds a circuit ends on are declared.
_PLACE_KINDS = (
    "TopologyDataCenter",
    "TopologyColocationZone",
    "TopologyVirtualColocation",
    "TopologyCloudRegion",
    "TopologyCustomerOffice",
    "TopologySaasRegion",
)
# add_sdwan_edge: the edge uplink, the ISP access device of the offices' internet underlay and its first free ports.
EDGE_UPLINK = "GE1"
ISP_ACCESS_DEVICE = "EXT-ORANGE"
ISP_ACCESS_PROVIDER = "P019"  # owner of the ISP access device: the provider of the offices' INET circuits
GATEWAY_PUBLIC_PORT = "eth1"


def _edges(nodes: Iterable[dict[str, Any]]) -> dict[str, Any]:
    return {"edges": [{"node": node} for node in nodes]}


def _encapsulation_data(item: dict[str, Any]) -> dict[str, Any] | None:
    """The ``data`` of the ManagedInterfaceEncapsulation an interface object file item authors under ``capabilities``."""
    return next(
        (
            capability["data"]
            for capability in item.get("capabilities") or []
            if isinstance(capability, dict) and capability.get("kind") == "ManagedInterfaceEncapsulation"
        ),
        None,
    )


def _all(kind: str) -> list[Declared]:
    return distinct(declared(kind, ALL_DEMO))


def session_circuits(circuit_type: str) -> list[Declared]:
    """The declared TopologySessionCircuit objects of one circuit_type."""
    return [circuit for circuit in _all(SESSION) if circuit.value("circuit_type") == circuit_type]


class _Model:
    """The devices, places, ports, cables and carrier circuits of the data, before they are shaped as a payload."""

    def __init__(self, without_cables: bool, without_circuits: frozenset[str]) -> None:
        self.without_circuits = without_circuits
        self.places = self._places()
        self.providers = {str(p.item["org_id"]) for p in distinct(declared("OrganizationProvider", BOOTSTRAP))}
        self.namespaces = {name(ns): str(ns.value("vrf_type")) for ns in distinct(declared("SecurityZone", BOOTSTRAP))}
        self.sdwan_offices = [office for office in _all("TopologyCustomerOffice") if office.item.get("sdwan_gateway")]
        self.devices: dict[str, dict[str, Any]] = {}
        self._add_declared_devices()
        self._add_generated_devices()
        self.peerings = {(str(i.item["device"]), name(i)): i for i in _all(PEERING)}
        sub_interfaces = {
            (str(i.item["device"]), name(i)): encapsulation
            for i in _all("DcimVirtualInterface")
            if (encapsulation := _encapsulation_data(i.item))
        }
        self.parents = {key: str(data["parent"][1]) for key, data in sub_interfaces.items()}
        self.tags = {key: data.get("vlan_id") for key, data in sub_interfaces.items()}
        self.cabled: set[tuple[str, str]] = set()
        if not without_cables:
            for cable in _all("DcimCable"):
                for end in cable.item["endpoints"]:
                    if end["kind"] == "DcimPhysicalInterface":  # a patch panel port end has a panel, not a device
                        self.cabled.add((str(end["data"]["device"]), str(end["data"]["name"])))
        self.carriers = self._carrier_circuits()

    # -- places and devices --------------------------------------------------------------------------------------

    @staticmethod
    def _places() -> dict[str, dict[str, Any]]:
        places: dict[str, dict[str, Any]] = {}
        for kind in _PLACE_KINDS:
            for place in _all(kind):
                node: dict[str, Any] = {"id": f"id-{name(place)}", "__typename": kind, "name": {"value": name(place)}}
                if kind == "TopologyCustomerOffice":
                    owner = str(place.item["owner"])
                    node["owner"] = {"node": {"id": f"id-{owner}", "org_id": {"value": owner}}}
                places[name(place)] = node
        for place in distinct(declared(INTERNET, BOOTSTRAP)):
            places[name(place)] = {"id": f"id-{name(place)}", "__typename": INTERNET, "name": {"value": name(place)}}
        return places

    def _add_declared_devices(self) -> None:
        roles: dict[str, list[str]] = {}
        for process in _all("ManagedBGP"):
            for device in process.item.get("capabilities") or []:
                roles.setdefault(str(device), []).append(str(process.value("process_role")))
        for net in distinct(declared(INTERNET, BOOTSTRAP)):
            self.devices[name(net)] = {"kind": INTERNET, "deployment": None, "region": None, "bgp": []}
        for kind in (PHYSICAL_DEVICE, APPLIANCE, TRANSPORT, PROVIDER_CONNECT):
            for device in _all(kind):
                self.devices[name(device)] = {
                    "kind": kind,
                    "deployment": device.item.get("deployment"),
                    "region": device.item.get("region"),
                    "location": device.item.get("location"),
                    "owner": device.item.get("owner"),
                    "bgp": roles.get(name(device), []),
                }
        self.provider_ports = {(str(i.item["device"]), name(i)) for i in _all(PROVIDER_INTERFACE)}

    def _add_generated_devices(self) -> None:
        """The edges add_colocation_zone / add_virtual_colocation create, the DC border leaves, the office edges."""
        virtual = {name(v) for v in _all("TopologyVirtualColocation")}
        for place in [*_all("TopologyColocationZone"), *_all("TopologyVirtualColocation")]:
            count = sum(int(row[0]) for row in place.item.get("fabric_templates") or [] if row[1] == "edge")
            for index in range(1, count + 1):
                self.devices[f"eg-{name(place).lower()}{index:02d}"] = {
                    "kind": APPLIANCE if name(place) in virtual else PHYSICAL_DEVICE,
                    "deployment": name(place),
                    "bgp": ["overlay"],
                }
        dcs = {name(dc) for dc in _all("TopologyDataCenter")}
        for circuit in session_circuits(INTERCONNECT):
            for device, _ in circuit.item["capabilities"]:
                if device.startswith("bl-"):
                    (dc,) = [d for d in dcs if device.startswith(f"bl-{d.lower()}")]
                    self.devices[device] = {"kind": PHYSICAL_DEVICE, "deployment": dc, "bgp": ["overlay"]}
        for office in self.sdwan_offices:
            self.devices[sdwan_edge_name(name(office))] = {
                "kind": PHYSICAL_DEVICE,
                "deployment": name(office),
                "bgp": [],
            }

    def facility(self, device: str) -> str | None:
        """Where a device sits: its ``deployment``, else (a cloud transport) its region, else."""
        info = self.devices[device]
        if info["kind"] == INTERNET:
            return device  # it is a facility itself
        return info["deployment"] or info.get("region") or info.get("location")

    def port_id(self, device: str, port: str) -> str:
        if device not in self.devices:
            raise KeyError(f"device {device} is neither declared nor generated")
        return f"{device}:{port}"

    def port_kind(self, device: str, port: str = "") -> str:
        """The kind of a port: THE INTERNET's only port and the end of a provider connection have a kind of their own,
        the rest are physical.
        """
        if (device, port) in self.provider_ports:
            return PROVIDER_INTERFACE
        return INTERNET_BUS if self.devices[device]["kind"] == INTERNET else "DcimPhysicalInterface"

    # -- carrier circuits ---------------------------------------------------------------------------------------

    def _carrier_circuits(self) -> list[dict[str, Any]]:
        """Declared carrier circuits plus each office's INET-<office>, locations completed as add_circuit does."""
        rows: list[dict[str, Any]] = []
        for index, office in enumerate(self.sdwan_offices):
            rows.append(
                {
                    "name": f"INET-{name(office)}",
                    "circuit_type": "internet",
                    "status": "active",
                    "owner": str(office.item["owner"]),
                    "locations": [name(office)],
                    "provider": ISP_ACCESS_PROVIDER,
                    "ends": [
                        (sdwan_edge_name(name(office)), EDGE_UPLINK),
                        (ISP_ACCESS_DEVICE, f"xe-0/1/{index}"),
                    ],
                }
            )
        for circuit in _all(CARRIER):
            rows.append(
                {
                    "name": str(circuit.value("circuit_id")),
                    "circuit_type": str(circuit.value("circuit_type")),
                    "status": str(circuit.value("status")),
                    "owner": circuit.item.get("owner"),
                    "provider": str(circuit.item["provider"]),
                    "locations": [str(location) for location in circuit.item["locations"]],
                    "ends": [(str(device), str(port)) for device, port in circuit.item["capabilities"]],
                }
            )
        rows = [row for row in rows if row["name"] not in self.without_circuits]
        for row in rows:
            row["locations"] = self._complete_locations(row)
        return rows

    def _complete_locations(self, row: dict[str, Any]) -> list[str]:
        """The authored locations plus the facilities add_circuit derives from the end devices' deployments."""
        current = [self._location(place) for place in row["locations"]]
        endpoints = [
            CircuitEndpoint(
                interface_id=self.port_id(device, port),
                interface_name=port,
                device_name=device,
                deployment=self._location(facility) if (facility := self.facility(device)) else None,
            )
            for device, port in row["ends"]
        ]
        missing, problem = plan_locations(current, endpoints)
        assert problem is None, f"{row['name']}: {problem}"
        return [*row["locations"], *(location.label for location in missing)]

    def _location(self, place: str) -> CircuitLocation:
        node = self.places[place]
        return CircuitLocation(id=node["id"], kind=node["__typename"], label=place)

    # -- shaping -------------------------------------------------------------------------------------------------

    def _place(self, place: str | None) -> dict[str, Any]:
        if place is None:
            return {"node": None}
        return {"node": {key: self.places[place][key] for key in ("id", "__typename", "name")}}

    def placement(self, device: str) -> dict[str, Any]:
        """The fields that say where a device sits: ``region`` for a cloud transport, else ``deployment``."""
        info = self.devices[device]
        if info["kind"] == TRANSPORT:
            return {"region": self._place(info["region"]), "saas_region": {"node": None}}
        if info["kind"] == PROVIDER_CONNECT:
            return {"location": self._place(info["location"])}
        return {"deployment": self._place(info["deployment"])}

    def carrier(self, row: dict[str, Any]) -> dict[str, Any]:
        """A carrier circuit as the query selects it under a port."""
        return {
            "__typename": CARRIER,
            "id": f"id-{row['name']}",
            "name": {"value": row["name"]},
            "status": {"value": row["status"]},
            "circuit_type": {"value": row["circuit_type"]},
            "locations": _edges(
                {key: self.places[place][key] for key in ("id", "__typename", "name")} for place in row["locations"]
            ),
            "capabilities": _edges(
                {
                    "id": self.port_id(device, port),
                    "__typename": self.port_kind(device, port),
                    "name": {"value": port},
                    "device": {"node": {"name": {"value": device}}},
                }
                for device, port in row["ends"]
            ),
        }

    def carriers_on(self, device: str, port: str) -> list[dict[str, Any]]:
        """The carrier circuits (not decommissioned or otherwise) that end on exactly this port."""
        return [self.carrier(row) for row in self.carriers if (device, port) in row["ends"]]

    def virtual_port(self, device: str, port: str) -> dict[str, Any]:
        """A virtual circuit's end port: carriers on it, parent interface of a sub-interface, device and processes."""
        info = self.devices[device]
        node: dict[str, Any] = {
            "id": self.port_id(device, port),
            "name": {"value": port},
            "capabilities": _edges([*self.carriers_on(device, port), {"__typename": "ManagedVxlanSegment"}]),
            "device": {
                "node": {
                    "id": f"id-{device}",
                    "__typename": info["kind"],
                    "name": {"value": device},
                    **self.placement(device),
                    "capabilities": _edges(
                        [{"__typename": "ManagedBGP", "process_role": {"value": role}} for role in info["bgp"]]
                    ),
                }
            },
        }
        peering = self.peerings.get((device, port))
        if peering is not None:
            node["__typename"] = PEERING
            node.update(raw_encapsulation((_encapsulation_data(peering.item) or {}).get("vlan_id"), None))
        parent = self.parents.get((device, port))
        if parent is None:
            node["cable"] = {"node": {"id": f"cable-{device}:{port}"} if (device, port) in self.cabled else None}
        else:
            node["__typename"] = "DcimVirtualInterface"
            node.update(
                raw_encapsulation(
                    self.tags[(device, port)],
                    {
                        "id": f"id-{device}:{parent}",
                        "cable": {
                            "node": {"id": f"cable-{device}:{parent}"} if (device, parent) in self.cabled else None
                        },
                        "capabilities": _edges(self.carriers_on(device, parent)),
                    },
                )
            )
        return node

    def carrier_payload(self, row: dict[str, Any]) -> dict[str, Any]:
        owner = row.get("owner")
        return {
            "id": f"id-{row['name']}",
            "name": {"value": row["name"]},
            "status": {"value": row["status"]},
            "circuit_type": {"value": row["circuit_type"]},
            "owner": {"node": {"id": f"id-{owner}", "org_id": {"value": owner}} if owner else None},
            "provider": {"node": {"id": f"id-{row['provider']}"}},
            "locations": _edges(self.places[place] for place in row["locations"]),
            "capabilities": _edges(
                {
                    "id": self.port_id(device, port),
                    "__typename": self.port_kind(device, port),
                    "name": {"value": port},
                    "device": {
                        "node": {
                            "id": f"id-{device}",
                            "__typename": self.devices[device]["kind"],
                            "name": {"value": device},
                            **self.placement(device),
                            **self.owner_of(device),
                        }
                    },
                }
                for device, port in row["ends"]
            ),
        }

    def owner_of(self, device: str) -> dict[str, Any]:
        """A managed device's owner as the placement query selects it (a provider or a customer)."""
        owner = self.devices[device].get("owner")
        if not owner:
            return {}
        kind = "OrganizationProvider" if str(owner) in self.providers else "OrganizationCustomer"
        return {"owner": {"node": {"id": f"id-{owner}", "__typename": kind}}}

    def virtual_payload(
        self,
        circuit_name: str,
        locations: list[str],
        ends: list[tuple[str, str]],
        owner: str | None = None,
        *,
        facility_only: bool = False,
    ) -> dict[str, Any]:
        """The fields every virtual kind selects."""
        places = [self.places[place] for place in locations]
        if facility_only:
            places = [{key: place[key] for key in ("id", "__typename", "name")} for place in places]
        return {
            "id": f"id-{circuit_name}",
            "name": {"value": circuit_name},
            "owner": {"node": {"id": f"id-{owner}", "org_id": {"value": owner}} if owner else None},
            "locations": _edges(places),
            "capabilities": _edges(self.virtual_port(device, port) for device, port in ends),
        }


def _handoff(
    model: _Model,
    circuit_name: str,
    locations: list[str],
    ends: list[tuple[str, str]],
    namespace: str,
) -> dict[str, Any]:
    """A handoff as the query selects it: a circuit has no owner."""
    payload = model.virtual_payload(circuit_name, locations, ends, facility_only=True)
    del payload["owner"]
    payload["vrf"] = {"node": _raw_vrf(namespace)}
    for edge in payload["capabilities"]["edges"]:
        port = edge["node"]
        device = port["device"]["node"]
        if any(
            process["node"].get("process_role", {}).get("value") == "overlay"
            for process in device.get("capabilities", {}).get("edges", [])
        ):
            # The end is in the instance: the ManagedVRF among its capabilities (alias zone_capabilities).
            port["zone_capabilities"] = {"edges": [{"node": {"__typename": "ManagedVRF", **payload["vrf"]["node"]}}]}
    return payload


def build_payloads(without_cables: bool = False, without_circuits: Iterable[str] = ()) -> dict[str, Any]:
    """The raw GraphQL response of ``circuit_placement.gql`` over the 30_all data."""
    model = _Model(without_cables, frozenset(without_circuits))

    tunnels = [
        model.virtual_payload(
            f"{name(office)}-SDWAN-{model.devices[str(office.item['sdwan_gateway'])]['deployment']}",
            [name(office), str(model.devices[str(office.item["sdwan_gateway"])]["deployment"])],
            [(str(office.item["sdwan_gateway"]), GATEWAY_PUBLIC_PORT), (sdwan_edge_name(name(office)), EDGE_UPLINK)],
            owner=str(office.item["owner"]),
        )
        for office in model.sdwan_offices
    ]
    tunnels += [
        model.virtual_payload(
            name(circuit),
            [str(p) for p in circuit.item["locations"]],
            [(str(d), str(p)) for d, p in circuit.item["capabilities"]],
            owner=str(circuit.item["owner"]) if circuit.item.get("owner") else None,
        )
        for circuit in session_circuits(TUNNEL)
    ]
    interconnects = [
        model.virtual_payload(
            name(circuit),
            [str(p) for p in circuit.item["locations"]],
            [(str(d), str(p)) for d, p in circuit.item["capabilities"]],
            owner=str(circuit.item["owner"]) if circuit.item.get("owner") else None,
            facility_only=True,
        )
        for circuit in session_circuits(INTERCONNECT)
    ]
    handoffs = []
    for circuit in session_circuits(HANDOFF):
        handoffs.append(
            _handoff(
                model,
                name(circuit),
                [str(p) for p in circuit.item["locations"]],
                [(str(d), str(p)) for d, p in circuit.item["capabilities"]],
                str(circuit.item["vrf"][1]),
            )
        )

    sessions = [
        {**circuit, "circuit_type": {"value": circuit_type}}
        for circuit_type, circuits in ((TUNNEL, tunnels), (INTERCONNECT, interconnects), (HANDOFF, handoffs))
        for circuit in circuits
    ]
    internet = [
        {
            "id": f"id-{name(net)}",
            "__typename": INTERNET,
            "name": {"value": name(net)},
            "interfaces": _edges(
                {"id": f"id-{name(net)}:{name(port)}", "__typename": INTERNET_BUS, "name": {"value": name(port)}}
                for port in distinct(declared(INTERNET_BUS, BOOTSTRAP))
            ),
        }
        for net in distinct(declared(INTERNET, BOOTSTRAP))
    ]
    return {
        CARRIER: _edges(model.carrier_payload(row) for row in model.carriers),
        SESSION: _edges(sessions),
        INTERNET: _edges(internet),
    }
