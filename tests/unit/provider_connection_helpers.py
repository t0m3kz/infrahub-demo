"""Builders and a recording fake client for the provider connection planner and generator tests.

The payloads have the shape ``generate()`` sees after ``clean_data`` (queries/topology/add/provider_connection.gql):
a relationship that selected only ``id`` is the id string. The client records every create, save and tracking-group
append in one ordered log, so a test asserts both what a run wrote and that a refused run wrote nothing at all.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from generators.topology.provider_connection import ProviderConnectionGenerator
from utils.provider_services import (
    ADDRESS_SOURCE_PROVIDER,
    END_US,
    FLAVOURS,
    PATH_DEDICATED,
    PATH_INTERNET_VPN,
    ConnectionFlavour,
)

CONNECTION_ID = "conn-1"
NEAR = "fac-near"
FAR = "fac-far"
NAMESPACE = "ns-prod"
PROVIDER = "prov-aws"
AWS_AS = "as-64512"
FAR_KIND = "TopologyCloudRegion"
#: The devices of the near facility (cage edges) and the service-edge appliance of the far region.
NEAR_DEVICES = (("dev-eg-1", "eg-1"), ("dev-eg-2", "eg-2"))
SERVICE_EDGE = ("dev-AWS-SE", "AWS-SE")


def pid(device: str, name: str) -> str:
    """The id of a port in these fixtures."""
    return f"p-{device}-{name}"


@pytest.fixture
def dual_link_profile(monkeypatch: pytest.MonkeyPatch) -> str:
    """Register a provider profile with two provider-assigned /30 links, dedicated and over the internet (test-only).

    The shipped table has one link per dedicated connection (a primary and a secondary leg are two connections) and
    two per VPN; the pairing of several links and link prefixes is exercised with these rows. Import the fixture and
    mark the module ``pytestmark = pytest.mark.usefixtures("dual_link_profile")``.
    """
    two_links = ConnectionFlavour(ADDRESS_SOURCE_PROVIDER, 30, 2, END_US)
    monkeypatch.setitem(FLAVOURS, ("dual", PATH_DEDICATED), two_links)
    monkeypatch.setitem(FLAVOURS, ("dual", PATH_INTERNET_VPN), two_links)
    return "dual"


def kind_name(kind: Any) -> str:
    """Protocol class or kind string -> kind name."""
    return kind if isinstance(kind, str) else kind.__name__


def bgp(process_id: str = "bgp-stub", as_id: str | None = AWS_AS) -> dict[str, Any]:
    """A ManagedBGP among a device's capabilities."""
    return {"id": process_id, "typename": "ManagedBGP", "name": process_id, "local_as": as_id}


def port(
    port_id: str,
    name: str,
    device: str,
    deployment: str,
    *,
    processes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """A carrier end: an interface with its device, the device's deployment and BGP processes."""
    return {
        "id": port_id,
        "name": name,
        "device": {
            "id": f"dev-{device}",
            "name": device,
            "deployment": {"id": deployment, "typename": "TopologyColocationZone"},
            "capabilities": processes or [],
        },
    }


def carrier(
    carrier_id: str,
    name: str,
    ends: list[dict[str, Any]],
    *,
    circuit_type: str = "cross_connect",
    status: str = "active",
) -> dict[str, Any]:
    """A carrier circuit with its ends."""
    return {
        "id": carrier_id,
        "name": name,
        "typename": "TopologyCarrierCircuit",
        "circuit_type": circuit_type,
        "status": status,
        "capabilities": ends,
    }


def own_carrier(
    near: str = "eg-1",
    near_if: str = "Ethernet1/1",
    *,
    far_if: str = "vnic-AWS-FR2",
    stub_processes: list[dict[str, Any]] | None = None,
    carrier_id: str = "xc-1",
    status: str = "provisioning",
    circuit_type: str = "virtual_connection",
) -> dict[str, Any]:
    """The carrier a connection lists (its own output): our cage edge port to a NIC of the service edge."""
    processes = [bgp()] if stub_processes is None else stub_processes
    return carrier(
        carrier_id,
        carrier_id.upper(),
        [
            port(pid(near, near_if), near_if, near, NEAR),
            port(pid("AWS-SE", far_if), far_if, "AWS-SE", FAR, processes=processes),
        ],
        circuit_type=circuit_type,
        status=status,
    )


def provider(profile: str | None = "aws") -> dict[str, Any]:
    """The account's provider with its connection profile."""
    return {"id": PROVIDER, "name": "AWS", "connection_profile": profile}


def stub_device(
    stub_id: str = SERVICE_EDGE[0], name: str = SERVICE_EDGE[1], processes: list[dict[str, Any]] | None = None
) -> dict:
    """A device of the far facility, as the query lists it."""
    return {"id": stub_id, "name": name, "capabilities": [bgp()] if processes is None else processes}


def internet_carrier(device: str = "edge-1", *, status: str = "active") -> dict[str, Any]:
    """An internet carrier circuit of the near facility ending on one of our devices."""
    return carrier(
        f"inet-{device}",
        f"INET-{device}",
        [port(f"uplink-{device}", "eth0", device, NEAR)],
        circuit_type="internet",
        status=status,
    )


def session(
    name: str,
    *,
    circuit_type: str = "handoff",
    status: str = "active",
    ends: list[dict[str, Any]],
    link_prefix: str | None = None,
    password: str | None = None,
    groups: tuple[str, ...] = ("virtual_circuits",),
    session_id: str | None = None,
) -> dict[str, Any]:
    """A session circuit of the connection as an earlier run left it."""
    return {
        "id": session_id or f"sess-{name}",
        "name": name,
        "circuit_type": circuit_type,
        "status": status,
        "provider": PROVIDER,
        "ip_namespace": NAMESPACE,
        "connection": CONNECTION_ID,
        "link_prefix": link_prefix,
        "password": password,
        "locations": [{"id": NEAR}, {"id": FAR}],
        "member_of_groups": [{"name": group} for group in groups],
        "capabilities": ends,
    }


def end(end_id: str, name: str, device: str) -> dict[str, Any]:
    """An end of an existing session circuit."""
    return {"id": end_id, "name": name, "device": f"dev-{device}"}


def connection(
    *,
    path_type: str = "dedicated",
    name: str = "AWS-FR2",
    conn_id: str = CONNECTION_ID,
    status: str = "active",
    vlan_id: int | None = None,
    profile: str | None = "aws",
    near_port: str | None = None,
    carriers: list[dict[str, Any]] | None = None,
    internet: list[dict[str, Any]] | None = None,
    stubs: list[dict[str, Any]] | None = None,
    link_prefixes: list[dict[str, Any]] | None = None,
    provider_as: str | None = None,
    password: str | None = None,
    sessions: list[dict[str, Any]] | None = None,
    with_provider: bool = True,
    far_kind: str = FAR_KIND,
    near_load: list[dict[str, Any]] | None = None,
    near_devices: tuple[tuple[str, str], ...] = NEAR_DEVICES,
    bandwidth: int | None = None,
) -> dict[str, Any]:
    """The payload of one TopologyProviderConnection: a dedicated AWS request with no port and no carrier by default."""
    return {
        "TopologyProviderConnection": [
            {
                "id": conn_id,
                "name": name,
                "path_type": path_type,
                "status": status,
                "vlan_id": vlan_id,
                "bandwidth": bandwidth,
                "near_port": near_port,
                "ip_namespace": NAMESPACE,
                "provider_as": provider_as,
                "password": password,
                "link_prefixes": link_prefixes or [],
                "account": {"provider": provider(profile)} if with_provider else None,
                "near": {
                    "id": NEAR,
                    "circuits": internet or [],
                    "devices": [{"id": device_id, "name": device} for device_id, device in near_devices],
                    "near_connections": near_load or [],
                },
                "far": {
                    "id": FAR,
                    "typename": far_kind,
                    "appliances": [stub_device()] if stubs is None else stubs,
                },
                "carrier_circuits": carriers or [],
                "session_circuits": sessions or [],
            }
        ]
    }


def other_connection(conn_id: str, *devices: str) -> dict[str, Any]:
    """Another connection of the near facility whose carrier ends on these devices (for the load)."""
    return {
        "id": conn_id,
        "carrier_circuits": [{"capabilities": [{"device": f"dev-{device}"} for device in devices]}],
        "session_circuits": [],
    }


def prefix(prefix_id: str, text: str) -> dict[str, Any]:
    """A link prefix of the connection."""
    return {"id": prefix_id, "prefix": text}


def sub(sub_id: str, name: str, parent_id: str) -> SimpleNamespace:
    """A sub-interface node as ``filters`` returns it."""
    return SimpleNamespace(id=sub_id, name=SimpleNamespace(value=name), parent_interface=SimpleNamespace(id=parent_id))


def carrier_node(
    carrier_id: str,
    end_ids: list[str],
    *,
    name: str | None = None,
    circuit_type: str = "virtual_connection",
    status: str = "active",
    claimed_by: tuple[str, ...] = (),
) -> SimpleNamespace:
    """A TopologyCarrierCircuit node as ``filters`` returns it (included capabilities and provider_connections)."""
    return SimpleNamespace(
        id=carrier_id,
        name=SimpleNamespace(value=name or carrier_id.upper()),
        circuit_type=SimpleNamespace(value=circuit_type),
        status=SimpleNamespace(value=status),
        capabilities=SimpleNamespace(peers=[SimpleNamespace(id=i) for i in end_ids]),
        provider_connections=SimpleNamespace(peers=[SimpleNamespace(id=i) for i in claimed_by]),
    )


def session_node(session_id: str, end_ids: list[str], *, connection: str | None = None) -> SimpleNamespace:
    """A session circuit node as the near-port facts read it (included capabilities and connection)."""
    return SimpleNamespace(
        id=session_id,
        capabilities=SimpleNamespace(peers=[SimpleNamespace(id=i) for i in end_ids]),
        connection=SimpleNamespace(id=connection, initialized=connection is not None),
    )


class FakeClient:
    """In-memory client: ordered ``log`` of creates/saves, ``group_context`` appends, canned reads."""

    def __init__(self, *, subs: list[SimpleNamespace] | None = None, taken_tunnels: dict[str, list[str]] | None = None):
        self.log: list[tuple[str, str, dict[str, Any]]] = []
        self.group_context = SimpleNamespace(related_node_ids=[])
        self.subs = subs or []
        self.taken_tunnels = taken_tunnels or {}
        self.foreign: list[SimpleNamespace] = []
        #: session circuits found on the ports by the near-port choice (included capabilities and connection).
        self.port_sessions: list[SimpleNamespace] = []
        #: device id -> the names of the virtual interfaces (NICs, loopback) the device already holds.
        self.nics: dict[str, list[str]] = {}
        #: device id -> the Loopback0 node an earlier run or the data left; device id -> its ManagedBGP nodes.
        self.loopbacks: dict[str, SimpleNamespace] = {}
        self.processes: dict[str, list[SimpleNamespace]] = {}
        #: The stub loopback pool (None: the data does not define it).
        self.pool: SimpleNamespace | None = SimpleNamespace(id="pool-stub-lo")
        #: interface id -> (name, device id, device name); physical ports also in ``physical`` (as nodes).
        self.interfaces: dict[str, tuple[str, str, str]] = {}
        self.physical: list[SimpleNamespace] = []
        self.carriers: list[SimpleNamespace] = []
        #: (device id, name) -> id of the NICs already on a service-edge appliance.
        self.service_edge_nics: dict[tuple[str, str], str] = {}
        self.events: list[str] = []
        self._counter = 0
        for device_id, device in NEAR_DEVICES:
            for index in (1, 2, 3):
                self.add_port(device, f"Ethernet1/{index}", role="cloud")

    def add_port(
        self,
        device: str,
        name: str,
        *,
        role: str | None = "cloud",
        cabled: bool = False,
        lag: bool = False,
        device_id: str | None = None,
    ) -> str:
        """A physical port of a device; returns its id (``pid``)."""
        port_id = pid(device, name)
        device_id = device_id or f"dev-{device}"
        self.interfaces[port_id] = (name, device_id, device)
        self.physical.append(
            SimpleNamespace(
                id=port_id,
                name=SimpleNamespace(value=name),
                role=SimpleNamespace(value=role),
                device=SimpleNamespace(id=device_id, display_label=device),
                cable=SimpleNamespace(id="cable-1" if cabled else None, initialized=cabled),
                lag=SimpleNamespace(id="lag-1" if lag else None, initialized=lag),
            )
        )
        return port_id

    def add_carrier(self, carrier_id: str, end_ids: list[str], **kwargs: Any) -> None:
        """A carrier circuit already in the database, with its ends (interface ids, registered by ``add_port`` or
        ``add_far_end``)."""
        self.carriers.append(carrier_node(carrier_id, end_ids, **kwargs))

    def add_far_end(self, name: str = "vnic-AWS-FR2", device: str = SERVICE_EDGE[1]) -> str:
        """A virtual NIC of a far device (known to ``filters(DcimInterface)``); returns its id."""
        port_id = pid(device, name)
        self.interfaces[port_id] = (name, f"dev-{device}", device)
        self.service_edge_nics[(f"dev-{device}", name)] = port_id
        return port_id

    def of(self, action: str, kind: str | None = None) -> list[tuple[str, dict[str, Any]]]:
        """(kind, data or kwargs) of the log entries of one action (``create`` or ``save``), optionally one kind."""
        return [(k, d) for a, k, d in self.log if a == action and (kind is None or k == kind)]

    @property
    def writes(self) -> int:
        """How many saves the run made."""
        return len(self.of("save"))

    def _interface(self, interface_id: str) -> SimpleNamespace:
        name, device_id, device = self.interfaces.get(interface_id, (interface_id, "dev-unknown", "unknown"))
        return SimpleNamespace(
            id=interface_id,
            name=SimpleNamespace(value=name),
            device=SimpleNamespace(id=device_id, display_label=device),
        )

    async def filters(self, kind: Any, **kwargs: Any) -> list[Any]:
        name = kind_name(kind)
        self.events.append(f"filters:{name}")
        if name == "DcimPhysicalInterface":
            if "ids" in kwargs:
                return [p for p in self.physical if p.id in kwargs["ids"]]
            role = kwargs.get("role__value")
            return [
                p
                for p in self.physical
                if p.device.id in kwargs["device__ids"] and (role is None or p.role.value == role)
            ]
        if name == "DcimInterface":
            return [self._interface(i) for i in kwargs["ids"] if i in self.interfaces]
        if name == "TopologyCarrierCircuit":
            wanted = set(kwargs["capabilities__ids"])
            return [c for c in self.carriers if wanted & {peer.id for peer in c.capabilities.peers}]
        if name == "DcimVirtualInterface" and "parent_interface__ids" in kwargs:
            return [s for s in self.subs if s.parent_interface.id in kwargs["parent_interface__ids"]]
        if name == "DcimVirtualInterface" and kwargs.get("name__values") == ["Loopback0"]:
            return [self.loopbacks[i] for i in kwargs["device__ids"] if i in self.loopbacks]
        if name == "ManagedBGP":
            return [p for i in kwargs["capabilities__ids"] for p in self.processes.get(i, [])]
        if name == "DcimVirtualInterface" and (kwargs.get("name__values") or [""])[0].startswith("vnic-"):
            device_id = kwargs["device__ids"][0]
            return [
                SimpleNamespace(id=nic_id, name=SimpleNamespace(value=nic))
                for (dev, nic), nic_id in self.service_edge_nics.items()
                if dev == device_id and nic in kwargs["name__values"]
            ]
        if name == "DcimVirtualInterface" and "name__values" not in kwargs and "device__ids" in kwargs:
            return [
                SimpleNamespace(id=f"nic-{n}", name=SimpleNamespace(value=n), device=SimpleNamespace(id=device_id))
                for device_id in kwargs["device__ids"]
                for n in self.nics.get(device_id, [])
            ]
        if name == "DcimVirtualInterface" and "device__ids" in kwargs:
            names = self.taken_tunnels.get(kwargs["device__ids"][0], [])
            return [SimpleNamespace(id=f"t-{n}", name=SimpleNamespace(value=n)) for n in names]
        if name == "TopologySessionCircuit":
            return self.port_sessions if "include" in kwargs else self.foreign
        return []

    def _remember_stub_objects(self, label: str, node_id: str, data: dict[str, Any]) -> None:
        """What a stub step saved is what the next run reads back: its Loopback0 and its BGP process."""
        if label == "ManagedBGP":
            device = data["capabilities"][0]["id"]
            self.processes.setdefault(device, []).append(process_node(data["name"], data["local_as"]["id"]))
        elif label == "DcimVirtualInterface" and data.get("name") == "Loopback0":
            device = data["device"]["id"]
            self.loopbacks[device] = SimpleNamespace(
                id=node_id,
                device=SimpleNamespace(id=device),
                ip_address=SimpleNamespace(id=data["ip_address"]["id"], initialized=True),
            )

    async def get(self, kind: Any, **kwargs: Any) -> Any:
        self.events.append(f"get:{kind_name(kind)}")
        if kind_name(kind) == "CoreIPAddressPool" and self.pool is None and not kwargs.get("raise_when_missing", True):
            return None
        return self.pool

    async def allocate_next_ip_address(self, **kwargs: Any) -> SimpleNamespace:
        self.log.append(("allocate", "IpamIPAddress", kwargs))
        self._counter += 1
        return SimpleNamespace(id=f"addr-{self._counter}", address=SimpleNamespace(value="10.255.65.1/32"))

    async def create(self, kind: Any, data: dict[str, Any]) -> MagicMock:
        label = kind_name(kind)
        self.log.append(("create", label, data))
        self._counter += 1
        node = MagicMock()
        node.id = data.get("id") or f"new-{label}-{self._counter}"
        if label == "DcimVirtualInterface" and "device" in data:
            self.interfaces[node.id] = (data["name"], data["device"]["id"], data["device"]["id"])
        node.name = SimpleNamespace(value=data.get("name"))

        async def _save(**kwargs: Any) -> None:
            self.log.append(("save", label, {"id": node.id, "data": data, **kwargs}))
            self._remember_stub_objects(label, node.id, data)
            if kwargs.get("update_group_context", True):
                self.group_context.related_node_ids.append(node.id)

        node.save = _save
        return node


def make_generator(client: FakeClient) -> ProviderConnectionGenerator:
    """A generator on the fake client: a recording resource_lock, a mock logger, nothing in flight to wait for.

    ``ensure_vlan_subinterface`` is the real one (generators/connections.py): it creates and saves through the client."""
    gen: Any = ProviderConnectionGenerator.__new__(ProviderConnectionGenerator)
    gen.client = client
    gen.logger = MagicMock()
    gen.wait_for_parent_generator_and_refetch = AsyncMock(return_value=None)

    @asynccontextmanager
    async def _lock(key: str) -> AsyncGenerator[None]:
        client.events.append(f"lock:{key}")
        try:
            yield
        finally:
            client.events.append(f"unlock:{key}")

    gen.resource_lock = _lock
    return gen


def loopback(device: str, address_id: str | None = "addr-authored") -> SimpleNamespace:
    """A Loopback0 node of a device as ``filters`` returns it (included ip_address and device)."""
    return SimpleNamespace(
        id=f"lo-{device}",
        device=SimpleNamespace(id=f"dev-{device}"),
        ip_address=SimpleNamespace(id=address_id, initialized=address_id is not None),
    )


def process_node(name: str, as_id: str | None = AWS_AS) -> SimpleNamespace:
    """A ManagedBGP node as ``filters`` returns it (included local_as)."""
    return SimpleNamespace(id=f"proc-{name}", name=SimpleNamespace(value=name), local_as=SimpleNamespace(id=as_id))
