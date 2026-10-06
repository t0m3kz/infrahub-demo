"""Fakes for the circuit generators (generators/topology/circuit.py).

GraphQL payload builders shaped like queries/topology/add/circuit.gql and
virtual_circuit.gql, and an in-memory client that records every write (save,
pool allocation, tracking-group append) in one ordered log, so a test can
assert both what a run wrote and that a refused run wrote nothing at all.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from generators.logger import GeneratorError
from generators.topology.circuit import PhysicalCircuitGenerator, VirtualCircuitGenerator

DC_KEY_ID = "key-dc10"
V6_POOL_ID = "pool-dci-v6"
V4_POOL_ID = "pool-dci-v4"
PREFIX_ID = "prefix-dci-1"


def kind_name(kind: Any) -> str:
    """Protocol class or kind string -> kind name."""
    return kind if isinstance(kind, str) else kind.__name__


def bgp_process(device: str, role: str) -> dict[str, Any]:
    return {
        "id": f"{device}-bgp-{role}",
        "__typename": "ManagedBGP",
        "name": {"value": f"{device}-bgp-{role}"},
        "process_role": {"value": role},
    }


def interface(
    iface_id: str,
    name: str,
    device: str,
    *,
    kind: str = "DcimPhysicalInterface",
    address: str | None = None,
    dc: str | None = None,
    underlay: str = "ipv6",
    roles: tuple[str, ...] = ("underlay", "overlay"),
) -> dict[str, Any]:
    """One endpoint interface, as the circuit queries select it (raw GraphQL)."""
    deployment = (
        {
            "id": f"dep-{dc}",
            "__typename": "TopologyDataCenter",
            "name": {"value": dc},
            "underlay_protocol": {"value": underlay},
        }
        if dc
        else {"id": "dep-metro", "__typename": "TopologyColocationMetro"}
    )
    capabilities = [bgp_process(device, role) for role in roles] + [
        {"id": f"{device}-ospf", "__typename": "ManagedOSPF"}
    ]
    return {
        "id": iface_id,
        "__typename": kind,
        "name": {"value": name},
        "ip_address": {"node": {"id": f"ip-{iface_id}", "address": {"value": address}} if address else None},
        "device": {
            "node": {
                "id": f"dev-{device}",
                "name": {"value": device},
                "deployment": {"node": deployment},
                "capabilities": {"edges": [{"node": cap} for cap in capabilities]},
            }
        },
    }


def dc_border_leaf(**kwargs: Any) -> dict[str, Any]:
    return interface("if-bl", "Ethernet1/35", "bl-dc101101", dc="DC10", **kwargs)


def cage_edge(**kwargs: Any) -> dict[str, Any]:
    return interface("if-eg", "Ethernet1/10", "eg-fr01", **kwargs)


def physical_payload(
    *,
    role: str | None = "dci",
    status: str = "active",
    customer: list[dict[str, Any]] | None = None,
    provider: list[dict[str, Any]] | None = None,
    circuit_id: str = "DF-DC10-EQXFR2",
) -> dict[str, Any]:
    customer = [dc_border_leaf()] if customer is None else customer
    provider = [cage_edge()] if provider is None else provider
    return {
        "TopologyPhysicalCircuit": {
            "edges": [
                {
                    "node": {
                        "id": "circ-df10",
                        "name": {"value": circuit_id},
                        "circuit_id": {"value": circuit_id},
                        "circuit_type": {"value": "dark_fiber"},
                        "status": {"value": status},
                        "peering_role": {"value": role},
                        "customer_interfaces": {"edges": [{"node": i} for i in customer]},
                        "provider_interfaces": {"edges": [{"node": i} for i in provider]},
                    }
                }
            ]
        }
    }


def sdwan_edge(**kwargs: Any) -> dict[str, Any]:
    return interface("if-edge", "GE1", "C001-EDGE1", address="10.99.1.2/30", roles=("underlay",), **kwargs)


def sdwan_gateway(**kwargs: Any) -> dict[str, Any]:
    kwargs.setdefault("address", "10.99.1.1/30")
    return interface(
        "if-gw", "eth0.1001", "EQX-FR2-SDWAN-GW1", kind="DcimVirtualInterface", roles=("underlay",), **kwargs
    )


def underlay_circuit(circuit_id: str = "INET-C001-WAW-FR2", status: str = "active") -> dict[str, Any]:
    return {
        "id": f"pc-{circuit_id}",
        "name": {"value": circuit_id},
        "circuit_id": {"value": circuit_id},
        "circuit_type": {"value": "internet"},
        "status": {"value": status},
        "provider": {"node": {"id": "prov-1", "name": {"value": "Orange"}}},
    }


def virtual_payload(
    *,
    role: str | None = "overlay",
    link_type: str = "sd_wan",
    status: str = "active",
    interfaces: list[dict[str, Any]] | None = None,
    physical: list[dict[str, Any]] | None = None,
    name: str = "C001-SDWAN-FR2",
) -> dict[str, Any]:
    interfaces = [sdwan_edge(), sdwan_gateway()] if interfaces is None else interfaces
    physical = [underlay_circuit()] if physical is None else physical
    return {
        "TopologyVirtualCircuit": {
            "edges": [
                {
                    "node": {
                        "id": "vc-1",
                        "name": {"value": name},
                        "status": {"value": status},
                        "link_type": {"value": link_type},
                        "transport_mode": {"value": "internet_backed"},
                        "peering_role": {"value": role},
                        "vni": {"value": None},
                        "tunnel_id": {"value": 1001},
                        "interface_capabilities": {"edges": [{"node": i} for i in interfaces]},
                        "physical_circuits": {"edges": [{"node": pc} for pc in physical]},
                    }
                }
            ]
        }
    }


class FakeClient:
    """In-memory stand-in for the SDK client the circuit generators use."""

    def __init__(self, *, key: bool = True, pools: bool = True, address_families: bool = True) -> None:
        self.writes: list[tuple[str, str, dict[str, Any]]] = []  # (action, label, kwargs)
        self.created: list[tuple[str, dict[str, Any], MagicMock]] = []
        self.group_context = MagicMock()
        self.group_context.related_node_ids = _RecordingList(self.writes)
        self.key = MagicMock(id=DC_KEY_ID) if key else None
        self.pools = {"DCI-Technical-IPv6": MagicMock(id=V6_POOL_ID), "DCI-Technical-IPv4": MagicMock(id=V4_POOL_ID)}
        if not pools:
            self.pools = {}
        self.address_families = address_families
        self.interfaces: dict[str, MagicMock] = {}
        self.allocations: list[dict[str, Any]] = []
        self.virtual_circuits: list[MagicMock] = []
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

        node.save = AsyncMock(side_effect=_save)
        return node

    def interface_node(
        self, iface_id: str, *, address_id: str | None = None, description: str = "", status: str = "free"
    ) -> MagicMock:
        node = self._node(iface_id)
        node.ip_address = MagicMock(id=address_id)
        node.description = MagicMock(value=description)
        node.status = MagicMock(value=status)
        self.interfaces[iface_id] = node
        return node

    async def get(self, kind: Any, raise_when_missing: bool = True, **kwargs: Any) -> Any:
        name = kind_name(kind)
        self.get_calls.append((name, kwargs))
        if name == "RoutingPassword":
            return self.key
        if name == "CoreIPPrefixPool":
            return self.pools.get(kwargs["name__value"])
        if name == "IpamIPAddress":
            return None
        if name in ("DcimPhysicalInterface", "DcimVirtualInterface"):
            iface_id = kwargs["id"]
            return self.interfaces.get(iface_id) or self.interface_node(iface_id)
        raise AssertionError(f"unexpected get({name}, {kwargs})")

    async def filters(self, kind: Any, **kwargs: Any) -> list[Any]:
        name = kind_name(kind)
        self.filter_calls.append((name, kwargs))
        if name == "RoutingBGPAddressFamily":
            if not self.address_families:
                return []
            return [MagicMock(id=f"af-{kwargs['afi__value']}-{kwargs['safi__value']}")]
        if name == "TopologyVirtualCircuit":
            return self.virtual_circuits
        raise AssertionError(f"unexpected filters({name}, {kwargs})")

    async def create(self, kind: Any, data: dict[str, Any]) -> MagicMock:
        name = kind_name(kind)
        label = f"{name}:{data.get('name') or data.get('address') or data.get('afi', '')}"
        node = self._node(label, data)
        self.created.append((name, data, node))
        return node

    async def allocate_next_ip_prefix(self, **kwargs: Any) -> MagicMock:
        self.allocations.append(kwargs)
        self.writes.append(("allocate", PREFIX_ID, kwargs))
        prefix = MagicMock(id=PREFIX_ID)
        network = "100.64.0.0/31" if kwargs["prefix_length"] == 31 else "fd00:2200::/127"
        prefix.prefix = MagicMock(value=network)
        prefix.ip_namespace = MagicMock(id="ns-default")
        return prefix

    def created_of(self, kind: str) -> list[tuple[dict[str, Any], MagicMock]]:
        return [(data, node) for name, data, node in self.created if name == kind]

    def saves(self) -> dict[str, dict[str, Any]]:
        """label -> kwargs of its (last) save."""
        return {label: kwargs for action, label, kwargs in self.writes if action == "save"}


class _RecordingList(list):
    """group_context.related_node_ids that also logs each append as a write."""

    def __init__(self, writes: list[tuple[str, str, dict[str, Any]]]) -> None:
        super().__init__()
        self._writes = writes

    def append(self, item: Any) -> None:
        self._writes.append(("track", item, {}))
        super().append(item)


def raising_logger() -> MagicMock:
    """A logger whose error() raises, like generators/logger.py's FailOnErrorLogger."""
    logger = MagicMock()

    def _error(msg: object, *args: object, **kwargs: object) -> None:
        raise GeneratorError(str(msg))

    logger.error = MagicMock(side_effect=_error)
    return logger


def make_generator(cls: Any, client: FakeClient) -> Any:
    """A circuit generator over the fake client, with a no-op resource lock and stubbed fan-out/wait."""
    gen: Any = cls.__new__(cls)
    gen.client = client
    gen.logger = raising_logger()
    gen.branch = "test-branch"
    gen.lock_keys = []

    @asynccontextmanager
    async def _lock(key: str):  # noqa: ANN202
        gen.lock_keys.append(key)
        yield

    gen.resource_lock = _lock
    gen.run_generator = AsyncMock()
    gen.wait_for_parent_generator_and_refetch = AsyncMock(return_value=None)
    return gen


def physical_generator(client: FakeClient) -> Any:
    """Typed Any so tests can reach the mocked attributes."""
    return make_generator(PhysicalCircuitGenerator, client)


def virtual_generator(client: FakeClient) -> Any:
    """Typed Any so tests can reach the mocked attributes."""
    return make_generator(VirtualCircuitGenerator, client)
