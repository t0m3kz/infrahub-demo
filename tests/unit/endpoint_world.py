"""A small world for the endpoint generator on the HFID fake (tests/unit/plan_graph_helpers.py): a server, the ToR switches of
its rack with their ports, and the cables the generator writes, which the next read sees on the ports.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from tests.unit.plan_graph_helpers import HfidClient, HfidNode
from tests.unit.plan_reconciler_helpers import FakeNode, kind_name

SERVER_ID = "ep-1"
RACK_ID = "rack-1"
SFP = "10gbase-x-sfpp"


class WorldNode(HfidNode):
    """A stored node that reads an unset ``cable`` or ``lag`` as None, as the SDK does for a port with no cable."""

    def __getattr__(self, attribute: str) -> Any:
        if attribute in ("cable", "lag") and self.data.get(attribute) is None:
            return None
        if attribute == "cable":
            cable = self._client.store[self.data["cable"]["id"]]
            return SimpleNamespace(id=cable.id, name=SimpleNamespace(value=cable.data["name"]))
        if attribute == "lag":
            lag = self._client.store.get(self.data["lag"]["id"])
            return SimpleNamespace(id=self.data["lag"]["id"], peer=lag)
        return super().__getattr__(attribute)


class EndpointWorld(HfidClient):
    """The store of a rack with a server (``ep-1``) and switches, answering the reads of the endpoint generator."""

    def __init__(self) -> None:
        super().__init__()
        self.platform = self.seed("DcimPlatform", name="eos")
        self.read_kinds: list[str] = []

    def seed(self, kind: str, **data: Any) -> HfidNode:
        node = WorldNode(self, kind, {"id": self._next_id(kind), **data})
        self.store[node.id] = node
        return node

    async def create(self, kind: Any, data: dict[str, Any]) -> Any:
        if kind_name(kind) == "IpamIPAddress":
            return await super().create(kind, data)
        return WorldNode(self, kind_name(kind), data)

    def add_device(self, name: str, role: str, *, device_id: str | None = None, rack: str | None = RACK_ID) -> FakeNode:
        """A device in the rack (a switch, or the server)."""
        return self.seed(
            "DcimPhysicalDevice",
            **({"id": device_id} if device_id else {}),
            name=name,
            role=role,
            status="active",
            rack={"id": rack} if rack else None,
            platform={"id": self.platform.id},
            capabilities=[],
        )

    def add_ports(self, device: FakeNode, names: list[str], *, role: str, interface_type: str = SFP) -> list[FakeNode]:
        """Free ports of the device."""
        return [
            self.seed(
                "DcimPhysicalInterface",
                device={"id": device.id},
                name=name,
                role=role,
                status="free",
                interface_type=interface_type,
                cable=None,
            )
            for name in names
        ]

    def add_switches(self, count: int = 2, ports: int = 4) -> list[FakeNode]:
        """``count`` ToR switches ``tor-1``.. with ``ports`` customer ports ``Ethernet1/1``.. each."""
        switches = []
        for number in range(1, count + 1):
            switch = self.add_device(f"tor-{number}", "tor")
            self.add_ports(switch, [f"Ethernet1/{port}" for port in range(1, ports + 1)], role="customer")
            switches.append(switch)
        return switches

    def add_mlag_domain(self, *switches: FakeNode) -> FakeNode:
        """A ManagedMLAG the switches are members of."""
        domain = self.seed("ManagedMLAG", name="mlag-tor")
        for switch in switches:
            switch.data["capabilities"] = [{"id": domain.id}]
        return domain

    def cable(self, first: FakeNode, second: FakeNode, name: str) -> FakeNode:
        """A cable between two ports, seen on both."""
        cable = self.seed("DcimCable", name=name, endpoints=[{"id": first.id}, {"id": second.id}])
        for port in (first, second):
            port.data["cable"] = {"id": cable.id}
            port.data["status"] = "active"
        return cable

    def ports(self, device: str) -> dict[str, FakeNode]:
        """The ports of the device by name."""
        device_id = next(
            n.id for n in self.store.values() if n.kind == "DcimPhysicalDevice" and n.data["name"] == device
        )
        return {
            n.data["name"]: n
            for n in self.store.values()
            if n.kind == "DcimPhysicalInterface" and n.data["device"]["id"] == device_id
        }

    def of_kind(self, kind: str) -> list[FakeNode]:
        """The stored nodes of a kind."""
        return [node for node in self.store.values() if node.kind == kind]

    def record_save(self, node: FakeNode, kwargs: dict[str, Any]) -> None:
        super().record_save(node, kwargs)
        if node.kind == "DcimCable":
            for end in node.data.get("endpoints", []):
                self.store[end["id"]].data["cable"] = {"id": node.id}
        if node.kind == "DcimLAGInterface":
            for member in node.data.get("member_interfaces", []):
                self.store[member["id"]].data["lag"] = {"id": node.id}

    async def filters(self, kind: Any, **flt: Any) -> list[Any]:
        wanted = kind_name(kind)
        self.read_kinds.append(wanted)
        if wanted in ("DcimPhysicalInterface", "DcimLAGInterface") and "device__ids" in flt:
            return [
                node
                for node in self.store.values()
                if node.kind == wanted
                and node.data["device"]["id"] in flt["device__ids"]
                and _matches(node, "role", flt)
                and _matches(node, "status", flt)
                and _matches(node, "interface_type", flt)
            ]
        if wanted == "DcimPhysicalDevice" and "rack__ids" in flt:
            return [
                node
                for node in self.store.values()
                if node.kind == wanted
                and (node.data.get("rack") or {}).get("id") in flt["rack__ids"]
                and _matches(node, "role", flt)
                and _matches(node, "status", flt)
            ]
        if wanted in ("DcimVirtualDevice", "ManagedControllerVirtual", "AppComponent"):
            return []
        return await super().filters(kind, **flt)


def _matches(node: FakeNode, attribute: str, flt: dict[str, Any]) -> bool:
    """Whether the node's attribute satisfies the ``<attribute>__value`` and ``<attribute>__values`` filters given."""
    value = node.data.get(attribute)
    if f"{attribute}__value" in flt and value != flt[f"{attribute}__value"]:
        return False
    return f"{attribute}__values" not in flt or value in flt[f"{attribute}__values"]
