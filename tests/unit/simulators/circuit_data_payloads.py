"""Check payloads for the 30_all circuits, built from the object files instead of typed by hand.

``build_payloads`` reads the YAML the demo loads (``data/demos/30_all``, ``data/bootstrap`` namespaces) and shapes it
like the raw GraphQL response of ``queries/validation/circuit_placement.gql``: one root per circuit kind, every end
port with the physical circuits found on it (or on its parent interface), the cable on the port, the device with its
BGP processes and deployment. What the generators add at load time is rebuilt here by the same rules, so the real
``CheckCircuitPlacement`` can run over the shipped data without an Infrahub:

- the colocation edges (``eg-<zone><NN>``: a ``CloudNetworkAppliance`` in a virtual colocation, else a physical
  device), the DC border leaves (``bl-<dc>...``) and the SD-WAN offices' edges, each with its one ``overlay`` process,
- the facility locations of each physical circuit, derived by the real ``plan_locations`` of ``add_circuit``,
- per SD-WAN office the ``INET-<office>`` underlay and the ``<office>-SDWAN-<zone>`` tunnel ``add_sdwan_edge`` builds.

Anything the data names that this module cannot resolve (a device, a place, a namespace) raises ``KeyError``: a payload
that silently dropped an end would make "zero findings" meaningless.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from generators.helpers.naming import sdwan_edge_name
from generators.topology.circuit import CircuitEndpoint, CircuitLocation, plan_locations
from tests.integration.demo_data import ROOT, Declared, declared, distinct, name

ALL_DEMO = ROOT / "data" / "demos" / "30_all"
BOOTSTRAP = ROOT / "data" / "bootstrap"

PHYSICAL = "TopologyPhysicalCircuit"
TUNNEL = "TopologyTunnelCircuit"
INTERCONNECT = "TopologyInterconnectCircuit"
HANDOFF = "TopologyHandoffCircuit"

PHYSICAL_DEVICE = "DcimPhysicalDevice"
APPLIANCE = "CloudNetworkAppliance"
# Where the place kinds a circuit ends on are declared.
_PLACE_KINDS = (
    "TopologyDataCenter",
    "TopologyColocationZone",
    "TopologyVirtualColocation",
    "TopologyCloudRegion",
    "TopologyExternalProviders",
    "TopologyCustomerOffice",
    "TopologySaasRegion",
)
# add_sdwan_edge: the edge uplink, the ISP access device of the offices' internet underlay and its first free ports.
EDGE_UPLINK = "GE1"
ISP_ACCESS_DEVICE = "EXT-ORANGE"
ISP_ACCESS_LOCATION = "ISP-ORANGE"
GATEWAY_PUBLIC_PORT = "eth1"


def _edges(nodes: Iterable[dict[str, Any]]) -> dict[str, Any]:
    return {"edges": [{"node": node} for node in nodes]}


def _all(kind: str) -> list[Declared]:
    return distinct(declared(kind, ALL_DEMO))


class _Model:
    """The devices, places, ports, cables and physical circuits of the data, before they are shaped as a payload."""

    def __init__(self, without_cables: bool, without_circuits: frozenset[str]) -> None:
        self.without_circuits = without_circuits
        self.places = self._places()
        self.namespaces = {
            name(ns): str(ns.value("namespace_type")) for ns in distinct(declared("IpamNamespace", BOOTSTRAP))
        }
        self.sdwan_offices = [office for office in _all("TopologyCustomerOffice") if office.item.get("sdwan_gateway")]
        self.devices: dict[str, dict[str, Any]] = {}
        self._add_declared_devices()
        self._add_generated_devices()
        self.parents = {
            (str(i.item["device"]), name(i)): str(i.item["parent_interface"][1])
            for i in _all("DcimVirtualInterface")
            if i.item.get("parent_interface")
        }
        self.cabled: set[tuple[str, str]] = set()
        if not without_cables:
            for cable in _all("DcimCable"):
                for end in cable.item["endpoints"]:
                    self.cabled.add((str(end["data"]["device"]), str(end["data"]["name"])))
        self.physical = self._physical_circuits()

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
        return places

    def _add_declared_devices(self) -> None:
        roles: dict[str, list[str]] = {}
        for process in _all("ManagedBGP"):
            for device in process.item.get("capabilities") or []:
                roles.setdefault(str(device), []).append(str(process.value("process_role")))
        for device in _all(PHYSICAL_DEVICE):
            self.devices[name(device)] = {
                "kind": PHYSICAL_DEVICE,
                "deployment": device.item.get("deployment"),
                "bgp": roles.get(name(device), []),
            }

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
        for circuit in _all(INTERCONNECT):
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

    def port_id(self, device: str, port: str) -> str:
        if device not in self.devices:
            raise KeyError(f"device {device} is neither declared nor generated")
        return f"{device}:{port}"

    # -- physical circuits ---------------------------------------------------------------------------------------

    def _physical_circuits(self) -> list[dict[str, Any]]:
        """Declared physical circuits plus each office's INET-<office>, locations completed as add_circuit does."""
        rows: list[dict[str, Any]] = []
        for index, office in enumerate(self.sdwan_offices):
            rows.append(
                {
                    "name": f"INET-{name(office)}",
                    "status": "active",
                    "owner": str(office.item["owner"]),
                    "locations": [name(office), ISP_ACCESS_LOCATION],
                    "ends": [
                        (sdwan_edge_name(name(office)), EDGE_UPLINK),
                        (ISP_ACCESS_DEVICE, f"xe-0/1/{index}"),
                    ],
                }
            )
        for circuit in _all(PHYSICAL):
            rows.append(
                {
                    "name": str(circuit.value("circuit_id")),
                    "status": str(circuit.value("status")),
                    "owner": circuit.item.get("owner"),
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
                deployment=self._location(self.devices[device]["deployment"])
                if self.devices[device]["deployment"]
                else None,
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

    def deployment(self, device: str) -> dict[str, Any]:
        place = self.devices[device]["deployment"]
        if place is None:
            return {"node": None}
        return {"node": {key: self.places[place][key] for key in ("id", "__typename", "name")}}

    def carrier(self, row: dict[str, Any]) -> dict[str, Any]:
        """A physical circuit as the query selects it under a port."""
        return {
            "__typename": PHYSICAL,
            "id": f"id-{row['name']}",
            "name": {"value": row["name"]},
            "status": {"value": row["status"]},
            "locations": _edges(
                {key: self.places[place][key] for key in ("id", "__typename", "name")} for place in row["locations"]
            ),
            "capabilities": _edges(
                {
                    "id": self.port_id(device, port),
                    "name": {"value": port},
                    "device": {"node": {"name": {"value": device}}},
                }
                for device, port in row["ends"]
            ),
        }

    def carriers_on(self, device: str, port: str) -> list[dict[str, Any]]:
        """The physical circuits (not decommissioned or otherwise) that end on exactly this port."""
        return [self.carrier(row) for row in self.physical if (device, port) in row["ends"]]

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
                    "capabilities": _edges(
                        [{"__typename": "ManagedBGP", "process_role": {"value": role}} for role in info["bgp"]]
                    ),
                }
            },
        }
        parent = self.parents.get((device, port))
        if parent is None:
            node["cable"] = {"node": {"id": f"cable-{device}:{port}"} if (device, port) in self.cabled else None}
        else:
            node["parent_interface"] = {
                "node": {
                    "cable": {"node": {"id": f"cable-{device}:{parent}"} if (device, parent) in self.cabled else None},
                    "capabilities": _edges(self.carriers_on(device, parent)),
                }
            }
        return node

    def physical_payload(self, row: dict[str, Any]) -> dict[str, Any]:
        owner = row.get("owner")
        return {
            "id": f"id-{row['name']}",
            "name": {"value": row["name"]},
            "owner": {"node": {"id": f"id-{owner}", "org_id": {"value": owner}} if owner else None},
            "locations": _edges(self.places[place] for place in row["locations"]),
            "capabilities": _edges(
                {
                    "id": self.port_id(device, port),
                    "name": {"value": port},
                    "device": {
                        "node": {
                            "id": f"id-{device}",
                            "name": {"value": device},
                            "deployment": self.deployment(device),
                        }
                    },
                }
                for device, port in row["ends"]
            ),
        }

    def virtual_payload(
        self,
        circuit_name: str,
        locations: list[str],
        ends: list[tuple[str, str]],
        owner: str | None = None,
        *,
        facility_only: bool = False,
    ) -> dict[str, Any]:
        """The fields every virtual kind selects. A tunnel's office location carries its owner; facilities never do."""
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


def build_payloads(without_cables: bool = False, without_circuits: Iterable[str] = ()) -> dict[str, Any]:
    """The raw GraphQL response of ``circuit_placement.gql`` over the 30_all data.

    ``without_cables`` drops every declared DcimCable and ``without_circuits`` the named physical circuits, to show the
    check reports what they carried (a payload that cannot fail proves nothing).
    """
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
        for circuit in _all(TUNNEL)
    ]
    interconnects = [
        model.virtual_payload(
            name(circuit),
            [str(p) for p in circuit.item["locations"]],
            [(str(d), str(p)) for d, p in circuit.item["capabilities"]],
            owner=str(circuit.item["owner"]) if circuit.item.get("owner") else None,
            facility_only=True,
        )
        for circuit in _all(INTERCONNECT)
    ]
    endpoints = {
        str(vif.item["virtual_circuit"]): {"name": {"value": name(vif)}, "vlan_id": {"value": vif.item["vlan_id"]}}
        for vif in _all("CloudVirtualInterface")
        if vif.item.get("virtual_circuit")
    }
    handoffs = []
    for circuit in _all(HANDOFF):
        payload = model.virtual_payload(
            name(circuit),
            [str(p) for p in circuit.item["locations"]],
            [(str(d), str(p)) for d, p in circuit.item["capabilities"]],
            facility_only=True,
        )
        del payload["owner"]
        namespace = str(circuit.item["ip_namespace"])
        payload["ip_namespace"] = {
            "node": {"name": {"value": namespace}, "namespace_type": {"value": model.namespaces[namespace]}}
        }
        payload["cloud_endpoint"] = {"node": endpoints.get(name(circuit))}
        handoffs.append(payload)

    return {
        PHYSICAL: _edges(model.physical_payload(row) for row in model.physical),
        TUNNEL: _edges(tunnels),
        INTERCONNECT: _edges(interconnects),
        HANDOFF: _edges(handoffs),
    }
