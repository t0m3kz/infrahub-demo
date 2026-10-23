"""Fakes for the circuit generators (generators/topology/circuit.py, virtual_circuit.py).

GraphQL payload builders shaped like queries/topology/add/circuit.gql and virtual_circuit.gql, and an in-memory
client that records every write (save, pool allocation, tracking-group append) in one ordered log, so a test can
assert both what a run wrote and that a refused run wrote nothing at all.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from generators.logger import GeneratorError
from generators.topology.circuit import PhysicalCircuitGenerator
from generators.topology.virtual_circuit import VirtualCircuitSessionGenerator

CIRCUIT_ID = "circ-df10"


def kind_name(kind: Any) -> str:
    """Protocol class or kind string -> kind name."""
    return kind if isinstance(kind, str) else kind.__name__


def place(kind: str, name: str, place_id: str | None = None) -> dict[str, Any]:
    """A location or deployment node, as the circuit query selects it (raw GraphQL)."""
    return {"id": place_id or f"loc-{name}", "__typename": kind, "display_label": name}


def dc_place(dc: str = "DC10") -> dict[str, Any]:
    """The TopologyDataCenter a dc= interface's device is deployed in (same id)."""
    return place("TopologyDataCenter", dc, f"dep-{dc}")


CAGE = place("TopologyColocationZone", "FR2")
SIBLING_CAGE = place("TopologyColocationZone", "FR6")
VIRTUAL_COLOCATION = place("TopologyVirtualColocation", "AM-NE")
METRO = place("TopologyColocationMetro", "FR", "dep-metro")
ISP = place("TopologyExternalProviders", "ORANGE-FR")
REGION = place("TopologyCloudRegion", "eu-central-1")


def interface(
    iface_id: str,
    name: str,
    device: str,
    *,
    dc: str | None = None,
    deployment: dict[str, Any] | None = None,
    no_deployment: bool = False,
) -> dict[str, Any]:
    """One endpoint interface, as the circuit query selects it (raw GraphQL).

    The device is deployed in DC ``dc`` when given, else in ``deployment`` (default: the colocation zone FR2,
    where the on-ramp edges are deployed); ``no_deployment`` leaves it unset.
    """
    if dc:
        deployment = dc_place(dc)
    elif deployment is None:
        deployment = CAGE
    if no_deployment:
        deployment = None
    return {
        "id": iface_id,
        "name": {"value": name},
        "device": {"node": {"name": {"value": device}, "deployment": {"node": deployment}}},
    }


def dc_border_leaf(**kwargs: Any) -> dict[str, Any]:
    return interface("if-bl", "Ethernet1/35", "bl-dc101101", dc="DC10", **kwargs)


def cage_edge(**kwargs: Any) -> dict[str, Any]:
    return interface("if-eg", "Ethernet1/10", "eg-fr201", **kwargs)


def physical_payload(
    *,
    ends: list[dict[str, Any]] | None = None,
    circuit_id: str = "DF-DC10-EQXFR2",
    locations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """A physical circuit payload: its ends are one capabilities list (default: DC border leaf, cage edge),
    its locations already complete by default (the authored cage and the DC derived on an earlier run)."""
    ends = [dc_border_leaf(), cage_edge()] if ends is None else ends
    locations = [CAGE, dc_place()] if locations is None else locations
    return {
        "TopologyPhysicalCircuit": {
            "edges": [
                {
                    "node": {
                        "id": CIRCUIT_ID,
                        "name": {"value": circuit_id},
                        "circuit_id": {"value": circuit_id},
                        "locations": {"edges": [{"node": loc} for loc in locations]},
                        "capabilities": {"edges": [{"node": i} for i in ends]},
                    }
                }
            ]
        }
    }


# ---------------------------------------------------------------------------
# Session circuits (queries/topology/add/virtual_circuit.gql)
# ---------------------------------------------------------------------------

VC_ID = "vc-1"
DC_KEY_ID = "key-existing"
V6_POOL_NETWORK = "fd00:2200::/127"
V4_POOL_NETWORK = "100.64.0.0/31"
HANDOFF_POOL_NETWORK = "10.255.128.0/31"
# Pool name -> the /31 (/127) the fake allocates from it.
POOL_NETWORKS = {
    "DCI-Technical-IPv6": V6_POOL_NETWORK,
    "DCI-Technical-IPv4": V4_POOL_NETWORK,
    "Handoff-PROD-IPv4": HANDOFF_POOL_NETWORK,
}


def bgp_process(device: str, role: str) -> dict[str, Any]:
    """A ManagedBGP of a device, as the query selects it under the device's capabilities."""
    return {
        "id": f"{device}-bgp-{role}",
        "__typename": "ManagedBGP",
        "name": {"value": f"{device}-bgp-{role}"},
        "process_role": {"value": role},
    }


def loopback(
    device: str, name: str, role: str, address: str | None, kind: str = "DcimVirtualInterface"
) -> dict[str, Any]:
    """A loopback of a device, as the query selects it (the address only under DcimVirtualInterface)."""
    node: dict[str, Any] = {
        "id": f"lo-{device}-{name}",
        "__typename": kind,
        "name": {"value": name},
        "role": {"value": role},
    }
    if kind == "DcimVirtualInterface":
        node["ip_address"] = {"node": {"address": {"value": address}} if address else None}
    return node


def deployment(kind: str, name: str, *, underlay: str = "ipv6") -> dict[str, Any]:
    """A device's deployment as virtual_circuit.gql selects it (a DC also its name and underlay_protocol)."""
    node: dict[str, Any] = {"__typename": kind}
    if kind == "TopologyDataCenter":
        node["name"] = {"value": name}
        node["underlay_protocol"] = {"value": underlay}
    return node


def underlay_circuit(circuit_id: str = "DF-DC10-EQXFR2", status: str = "active") -> dict[str, Any]:
    """A physical circuit as virtual_circuit.gql selects it under a port's (or its parent's) capabilities."""
    return {
        "__typename": "TopologyPhysicalCircuit",
        "id": f"pc-{circuit_id}",
        "name": {"value": circuit_id},
        "circuit_id": {"value": circuit_id},
        "status": {"value": status},
    }


def _capabilities(circuits: list[dict[str, Any]]) -> dict[str, Any]:
    """A port's ``capabilities`` edge: the carrier(s) plus a non-circuit entry, which arrives as a bare typename."""
    return {"edges": [{"node": peer} for peer in [{"__typename": "ManagedBGP"}, *circuits]]}


def session_port(
    port_id: str,
    name: str,
    device: str,
    *,
    kind: str = "DcimPhysicalInterface",
    where: dict[str, Any] | None = None,
    roles: tuple[str, ...] = ("underlay", "overlay"),
    loopbacks: list[dict[str, Any]] | None = None,
    circuits: list[dict[str, Any]] | None = None,
    parent_circuits: list[dict[str, Any]] | None = None,
    cabled: bool = False,
    parent_cabled: bool = False,
) -> dict[str, Any]:
    """One end of a session circuit, as virtual_circuit.gql selects it (raw GraphQL).

    ``circuits`` terminate on the port itself, ``parent_circuits`` on its parent interface, which a sub-interface
    (``kind="DcimVirtualInterface"``) reads its carrier from. A DcimCable sits on a physical port (``cabled``) or on
    the parent of a sub-interface (``parent_cabled``): only those two carry the ``cable`` selection.
    """
    port: dict[str, Any] = {"id": port_id, "__typename": kind, "name": {"value": name}}
    if kind == "DcimVirtualInterface":
        port["parent_interface"] = {
            "node": {
                "cable": {"node": {"id": f"cable-{port_id}"} if parent_cabled else None},
                "capabilities": _capabilities(parent_circuits or []),
            }
        }
    else:
        port["cable"] = {"node": {"id": f"cable-{port_id}"} if cabled else None}
    port["capabilities"] = _capabilities(circuits or [])
    port["device"] = {
        "node": {
            "id": f"dev-{device}",
            "name": {"value": device},
            "deployment": {"node": where},
            "capabilities": {
                "edges": [
                    {"node": cap}
                    for cap in [
                        *(bgp_process(device, role) for role in roles),
                        {"id": f"{device}-ospf", "__typename": "ManagedOSPF"},
                    ]
                ]
            },
            "interfaces": {"edges": [{"node": lo} for lo in loopbacks or []]},
        }
    }
    return port


def dc_border_port(**kwargs: Any) -> dict[str, Any]:
    """The DC border leaf port of a DCI: physical, in DC10 (ipv6), a VTEP loopback and a multisite VIP."""
    kwargs.setdefault("where", deployment("TopologyDataCenter", "DC10"))
    kwargs.setdefault("circuits", [underlay_circuit()])
    kwargs.setdefault(
        "loopbacks",
        [
            loopback("bl-dc101101", "loopback1", "loopback", "fd00:10::1/128"),
            loopback("bl-dc101101", "loopback2", "loopback-vtep", "fd00:10::2/128"),
            loopback("bl-dc101101", "loopback3", "multisite-vip", "fd00:10::99/128"),
        ],
    )
    return session_port("if-bl", "Ethernet1/35", "bl-dc101101", **kwargs)


def colo_edge_port(**kwargs: Any) -> dict[str, Any]:
    """The colocation edge port of a DCI: physical, in the zone FR2, an IPv6 VTEP loopback only."""
    kwargs.setdefault("where", deployment("TopologyColocationZone", "FR2"))
    kwargs.setdefault("circuits", [underlay_circuit()])
    kwargs.setdefault("loopbacks", [loopback("eg-fr201", "loopback1", "loopback-vtep", "fd00:20::1/128")])
    return session_port("if-eg", "Ethernet1/10", "eg-fr201", **kwargs)


def handoff_edge_port(**kwargs: Any) -> dict[str, Any]:
    """Our end of a handoff: the VLAN 1900 sub-interface of the cage edge's port, whose parent has the carrier."""
    kwargs.setdefault("where", deployment("TopologyColocationZone", "FR2"))
    kwargs.setdefault("parent_circuits", [underlay_circuit("XC-FR2-AWS")])
    return session_port("if-hs", "Ethernet1/10.1900", "eg-fr201", kind="DcimVirtualInterface", **kwargs)


def handoff_party_port(**kwargs: Any) -> dict[str, Any]:
    """The party's end of a handoff: a sub-interface of the provider router, one ManagedBGP (its remote ASN)."""
    kwargs.setdefault("where", deployment("TopologyCloudRegion", "eu-central-1"))
    kwargs.setdefault("roles", ("regular",))
    return session_port("if-aws", "Ethernet1.1900", "EXT-AWS-DX-FR2", kind="DcimVirtualInterface", **kwargs)


def session_payload(
    kind: str,
    ends: list[dict[str, Any]],
    *,
    name: str | None = None,
    status: str = "active",
    namespace: str | None = "PROD",
    maximum_routes: int | None = 100,
    password: str | None = None,
    circuit_id: str = VC_ID,
) -> dict[str, Any]:
    """A virtual circuit payload of ``kind``; a handoff also carries its namespace, maximum_routes and password.

    ``circuit_id`` is for a second circuit on the same client (see ``FakeClient.circuit_ids``)."""
    node: dict[str, Any] = {
        "id": circuit_id,
        "__typename": kind,
        "name": {
            "value": name or ("VC-DCI-DC10-FR2" if kind == "TopologyInterconnectCircuit" else "VC-C001-EQXFR-AWS")
        },
        "status": {"value": status},
    }
    if kind == "TopologyHandoffCircuit":
        node["ip_namespace"] = {"node": {"name": {"value": namespace}} if namespace else None}
        node["maximum_routes"] = {"value": maximum_routes}
        node["password"] = {"node": {"id": password} if password else None}
    node["capabilities"] = {"edges": [{"node": port} for port in ends]}
    return {"TopologyVirtualCircuit": {"edges": [{"node": node}]}}


def interconnect_payload(**kwargs: Any) -> dict[str, Any]:
    """A DCI between the DC border leaf and the colocation edge (override with ``ends=``)."""
    ends = kwargs.pop("ends", None) or [dc_border_port(), colo_edge_port()]
    return session_payload("TopologyInterconnectCircuit", ends, **kwargs)


def handoff_payload(**kwargs: Any) -> dict[str, Any]:
    """A handoff between the cage edge sub-interface and the provider router's (override with ``ends=``)."""
    ends = kwargs.pop("ends", None) or [handoff_edge_port(), handoff_party_port()]
    return session_payload("TopologyHandoffCircuit", ends, **kwargs)


def tunnel_payload() -> dict[str, Any]:
    """A tunnel circuit: the generator reads nothing but its typename and name."""
    return session_payload("TopologyTunnelCircuit", [], name="C001-SDWAN-FR2")


# ---------------------------------------------------------------------------
# The client
# ---------------------------------------------------------------------------


class _FakeInterface:
    """A fetched interface: ``ip_address`` reads as a relationship (``.id``) and accepts an id on assignment."""

    def __init__(self, client: FakeClient, iface_id: str, *, description: str, status: str, mtu: int) -> None:
        self.id = iface_id
        self._ip_id: str | None = None
        self.description = MagicMock(value=description)
        self.status = MagicMock(value=status)
        self.mtu = MagicMock(value=mtu)

        async def _save(**kwargs: Any) -> None:
            client.writes.append(("save", iface_id, kwargs))

        self.save = AsyncMock(side_effect=_save)

    @property
    def ip_address(self) -> SimpleNamespace:
        return SimpleNamespace(id=self._ip_id)

    @ip_address.setter
    def ip_address(self, value: str) -> None:
        self._ip_id = value


class _RecordingList(list):
    """group_context.related_node_ids that also logs each append as a write.

    A circuit itself (``VC_ID``, or any id in ``circuit_ids``) is the run's target, not an output: the generator adds it
    to the group on the runs that clean up or build. It is logged in ``targets`` and not as a write, so "the run wrote
    nothing" assertions and ``FakeClient.claimed()`` keep describing outputs only.
    """

    def __init__(
        self, writes: list[tuple[str, str, dict[str, Any]]], targets: list[str], circuit_ids: set[str]
    ) -> None:
        super().__init__()
        self._writes = writes
        self._targets = targets
        self._circuit_ids = circuit_ids

    def append(self, item: Any) -> None:
        if item in self._circuit_ids:
            self._targets.append(item)
        else:
            self._writes.append(("track", item, {}))
        super().append(item)


class FakeClient:
    """In-memory stand-in for the SDK client the circuit generators use.

    State persists across runs on one client (interfaces, addresses, keys, prefixes by identifier), so a test can
    run a generator twice and assert the second run wrote nothing new.
    """

    def __init__(self, *, key: bool = False, pools: bool = True, address_families: bool = True) -> None:
        self.writes: list[tuple[str, str, dict[str, Any]]] = []  # (action, label, kwargs)
        self.created: list[tuple[str, dict[str, Any], MagicMock]] = []
        self.group_context = MagicMock()
        self.targets: list[str] = []  # the circuit ids a run added to its tracking group as its target
        self.circuit_ids: set[str] = {VC_ID}  # ids of the circuits under test: tracking one is a target, not an output
        self.group_context.related_node_ids = _RecordingList(self.writes, self.targets, self.circuit_ids)
        # One real lock per resource_lock key, shared by every generator on this client: concurrent runs contend.
        self.locks: dict[str, asyncio.Lock] = {}
        self.keys: dict[str, MagicMock] = {"VC-DCI-DC10-FR2-dci-key": MagicMock(id=DC_KEY_ID)} if key else {}
        self.pools = (
            {name: SimpleNamespace(id=f"pool-{name}", pool_name=name) for name in POOL_NETWORKS} if pools else {}
        )
        self.address_families = address_families
        self.interfaces: dict[str, _FakeInterface] = {}
        self.addresses: dict[str, MagicMock] = {}
        self.prefixes: dict[str, MagicMock] = {}
        self.allocations: list[dict[str, Any]] = []
        # Session circuits ending on a port: kind name -> [(id, name) or (id, name, status)], returned by
        # filters(capabilities__ids=...); the status defaults to active.
        self.session_circuits: dict[str, list[tuple[str, ...]]] = {}
        # DcimVirtualInterface ids whose parent is a port, returned by filters(parent_interface__ids=...).
        self.sub_interfaces: list[str] = []
        self.filter_calls: list[tuple[str, dict[str, Any]]] = []
        self.get_calls: list[tuple[str, dict[str, Any]]] = []

    def _node(self, label: str, data: dict[str, Any] | None = None) -> MagicMock:
        node = MagicMock()
        node.id = label
        data = data or {}
        node.name = MagicMock(value=data.get("name", label))
        node.address = MagicMock(value=data.get("address"))

        async def _save(**kwargs: Any) -> None:
            self.writes.append(("save", label, kwargs))
            if label.startswith("IpamIPAddress:"):
                self.addresses[data["address"]] = node
            if label.startswith("RoutingPassword:"):
                self.keys[data["name"]] = node

        node.save = AsyncMock(side_effect=_save)
        return node

    def interface_node(self, iface_id: str) -> _FakeInterface:
        node = self.interfaces.get(iface_id)
        if node is None:
            node = _FakeInterface(self, iface_id, description="", status="free", mtu=9000)
            self.interfaces[iface_id] = node
        return node

    def circuit_node(self, circuit_id: str) -> MagicMock:
        """A fetched physical circuit whose add_relationships() is logged as a ("relate", id, kwargs) write."""
        node = self._node(circuit_id)

        async def _relate(**kwargs: Any) -> None:
            self.writes.append(("relate", circuit_id, kwargs))

        node.add_relationships = AsyncMock(side_effect=_relate)
        return node

    def related(self) -> list[tuple[str, dict[str, Any]]]:
        """(node id, kwargs) of every add_relationships() call, in order."""
        return [(label, kwargs) for action, label, kwargs in self.writes if action == "relate"]

    async def get(self, kind: Any, raise_when_missing: bool = True, **kwargs: Any) -> Any:
        name = kind_name(kind)
        self.get_calls.append((name, kwargs))
        if name == "TopologyPhysicalCircuit":
            return self.circuit_node(kwargs["id"])
        if name == "RoutingPassword":
            return self.keys.get(kwargs["name__value"])
        if name == "CoreIPPrefixPool":
            return self.pools.get(kwargs["name__value"])
        if name == "IpamIPAddress":
            return self.addresses.get(kwargs["address__value"])
        raise AssertionError(f"unexpected get({name}, {kwargs})")

    async def filters(self, kind: Any, **kwargs: Any) -> list[Any]:
        name = kind_name(kind)
        self.filter_calls.append((name, kwargs))
        await asyncio.sleep(0)  # a round trip: other runs on this client may go on meanwhile
        if name == "RoutingBGPAddressFamily":
            if not self.address_families:
                return []
            return [MagicMock(id=f"af-{kwargs['afi__value']}-{kwargs['safi__value']}")]
        if name in ("DcimPhysicalInterface", "DcimVirtualInterface"):
            if "parent_interface__ids" in kwargs:
                return [self.interface_node(iface_id) for iface_id in self.sub_interfaces]
            return [self.interface_node(iface_id) for iface_id in kwargs["ids"]]
        if name in ("TopologyInterconnectCircuit", "TopologyHandoffCircuit"):
            matches = []
            for circuit_id, circuit_name, *rest in self.session_circuits.get(name, []):
                other = MagicMock(id=circuit_id)
                other.name = MagicMock(value=circuit_name)
                other.status = MagicMock(value=rest[0] if rest else "active")
                matches.append(other)
            return matches
        raise AssertionError(f"unexpected filters({name}, {kwargs})")

    async def create(self, kind: Any, data: dict[str, Any]) -> MagicMock:
        name = kind_name(kind)
        label = f"{name}:{data.get('name') or data.get('address') or data.get('afi', '')}"
        node = self._node(label, data)
        self.created.append((name, data, node))
        return node

    async def allocate_next_ip_prefix(self, **kwargs: Any) -> MagicMock:
        self.allocations.append(kwargs)
        identifier = kwargs["identifier"]
        prefix = self.prefixes.get(identifier)
        if prefix is None:
            prefix = MagicMock(id=f"prefix-{identifier}")
            prefix.prefix = MagicMock(value=POOL_NETWORKS[kwargs["resource_pool"].pool_name])
            prefix.ip_namespace = MagicMock(id="ns-pool")
            self.prefixes[identifier] = prefix
        self.writes.append(("allocate", prefix.id, kwargs))
        return prefix

    def created_of(self, kind: str) -> list[tuple[dict[str, Any], MagicMock]]:
        return [(data, node) for name, data, node in self.created if name == kind]

    def saves(self) -> dict[str, dict[str, Any]]:
        """label -> kwargs of its (last) save."""
        return {label: kwargs for action, label, kwargs in self.writes if action == "save"}

    def claimed(self, since: int = 0) -> set[str]:
        """The outputs the tracking group gets from ``writes[since:]``: tracked saves plus appended ids.

        The circuit itself (the run's target) is not an output, see ``tracked_ids`` for the whole group.
        """
        return {
            label
            for action, label, kwargs in self.writes[since:]
            if (action == "save" and kwargs.get("update_group_context") is not False) or action == "track"
        }

    def tracked_ids(self, since: int = 0, targets_since: int = 0) -> set[str]:
        """Everything a run adds to its tracking group: its outputs plus the circuit it targets."""
        return self.claimed(since) | set(self.targets[targets_since:])


class SdkTrackingGroup:
    """One generator's tracking group across runs, with the cleanup semantics of infrahub_sdk (v1.23) ``update_group``.

    ``finish_run`` mirrors ``InfrahubGroupContext.update_group``: it returns WITHOUT any cleanup when the run tracked
    nothing (``if not members: return`` comes before the unused members are computed), else it deletes the previous
    members this run did not reach and keeps the current ones as the next run's baseline.
    """

    def __init__(self, client: FakeClient) -> None:
        self.client = client
        self.previous: set[str] = set()
        self._writes_at_start = 0
        self._targets_at_start = 0

    def start_run(self) -> None:
        """Mark where the next run's writes begin."""
        self._writes_at_start = len(self.client.writes)
        self._targets_at_start = len(self.client.targets)

    def finish_run(self) -> set[str]:
        """End the run: the ids the SDK would delete as unused (empty when it skips the cleanup)."""
        members = self.client.tracked_ids(self._writes_at_start, self._targets_at_start)
        if not members:
            return set()
        deleted = self.previous - members
        self.previous = members
        return deleted


def raising_logger() -> MagicMock:
    """A logger whose error() raises, like generators/logger.py's FailOnErrorLogger."""
    logger = MagicMock()

    def _error(msg: object, *args: object, **kwargs: object) -> None:
        raise GeneratorError(str(msg))

    logger.error = MagicMock(side_effect=_error)
    return logger


def make_generator(cls: Any, client: FakeClient) -> Any:
    """A circuit generator over the fake client, with the client's keyed resource locks and stubbed fan-out/wait."""
    gen: Any = cls.__new__(cls)
    gen.client = client
    gen.logger = raising_logger()
    gen.branch = "test-branch"
    gen.lock_keys = []

    @asynccontextmanager
    async def _lock(key: str):  # noqa: ANN202
        async with client.locks.setdefault(key, asyncio.Lock()):
            await asyncio.sleep(0)  # acquiring is a round trip: other runs may go on meanwhile
            gen.lock_keys.append(key)
            yield

    gen.resource_lock = _lock
    gen.run_generator = AsyncMock()
    gen.wait_for_parent_generator_and_refetch = AsyncMock(return_value=None)
    return gen


def physical_generator(client: FakeClient) -> Any:
    """add_circuit over the fake client (typed Any so tests can reach the mocked attributes)."""
    return make_generator(PhysicalCircuitGenerator, client)


def session_generator(client: FakeClient) -> Any:
    """add_virtual_circuit over the fake client (typed Any so tests can reach the mocked attributes)."""
    return make_generator(VirtualCircuitSessionGenerator, client)
