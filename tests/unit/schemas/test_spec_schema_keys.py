"""Schema guard: verify every create(...) payload key emitted by generators/specs/ exists in schemas/."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from generators.specs import (
    BgpPeeringSpec,
    CableSpec,
    DeviceSpec,
    FirewallContextSpec,
    InterfaceSpec,
    IpSpec,
    LoadBalancerVipSpec,
    PlanReconciler,
    RoutingTagSpec,
    SecurityTagSpec,
    TopologyPlan,
    VrfSpec,
)
from tests.unit.generators.test_spec_registry_and_recovery import _StatefulFakeClient

_ROOT = Path(__file__).resolve().parents[3]


def _schema_fields_by_kind() -> dict[str, set[str]]:
    generics: dict[str, set[str]] = {"BuiltinIPAddress": {"address", "ip_namespace", "description"}}
    nodes: dict[str, tuple[list[str], set[str]]] = {}

    for yml in sorted((_ROOT / "schemas").rglob("*.y*ml")):
        doc = yaml.safe_load(yml.read_text()) or {}
        for gen in doc.get("generics") or []:
            kind = f"{gen['namespace']}{gen['name']}"
            fields = {a["name"] for a in gen.get("attributes") or [] if a.get("state") != "absent"}
            fields |= {r["name"] for r in gen.get("relationships") or [] if r.get("state") != "absent"}
            generics[kind] = fields
        for node in doc.get("nodes") or []:
            if node.get("state") == "absent":
                continue
            kind = f"{node['namespace']}{node['name']}"
            fields = {a["name"] for a in node.get("attributes") or [] if a.get("state") != "absent"}
            fields |= {r["name"] for r in node.get("relationships") or [] if r.get("state") != "absent"}
            nodes[kind] = (list(node.get("inherit_from") or []), fields)
        for ext in (doc.get("extensions") or {}).get("nodes") or []:
            kind = ext["kind"]
            if kind in nodes:
                _, fields = nodes[kind]
                fields |= {a["name"] for a in ext.get("attributes") or [] if a.get("state") != "absent"}
                fields |= {r["name"] for r in ext.get("relationships") or [] if r.get("state") != "absent"}

    resolved: dict[str, set[str]] = {"DcimInterface": {"id", "name", "ip_address", "status", "role", "description"}}
    for kind, (parents, own_fields) in nodes.items():
        merged = {"id", "member_of_groups", "profiles", *own_fields}
        for parent in parents:
            merged |= generics.get(parent, set())
        resolved[kind] = merged
    return resolved


class _RecordingClient(_StatefulFakeClient):
    def __init__(self) -> None:
        super().__init__()
        self.created_calls: list[tuple[str, dict[str, Any]]] = []

    async def create(self, *, kind: Any, data: dict[str, Any]) -> Any:
        kind_name = kind if isinstance(kind, str) else getattr(kind, "__name__", str(kind))
        self.created_calls.append((kind_name, dict(data)))
        return await super().create(kind=kind, data=data)


@pytest.mark.asyncio
async def test_all_spec_create_keys_exist_in_schema() -> None:
    schema_fields = _schema_fields_by_kind()
    client = _RecordingClient()

    class _Gen:
        def __init__(self, c: Any) -> None:
            self.client = c

        def fail(self, msg: str) -> None:
            raise AssertionError(msg)

    plan = TopologyPlan()
    plan.add(SecurityTagSpec(name="sec-1", group_id=101, description="desc"))
    plan.add(RoutingTagSpec(kind="ManagedVxlanSegment", node_id="seg-1", pool_name="Segment-Routing-Tag", name="rt-1"))
    plan.add(FirewallContextSpec(name="fw-ctx-1"))
    plan.add(LoadBalancerVipSpec(hostname="lb-vip-1"))
    plan.add(DeviceSpec(name="dev-1", role="leaf", status="active", description="leaf 1"))
    plan.add(DeviceSpec(name="dev-2", role="spine", status="active"))
    plan.add(InterfaceSpec(device="dev-1", name="Ethernet1", role="uplink", mtu=9214, enabled=True))
    plan.add(InterfaceSpec(device="dev-2", name="Ethernet1", role="downlink", mtu=9214, enabled=True))
    plan.add(InterfaceSpec(device="dev-1", name="Loopback0", flavor="loopback", role="loopback"))
    plan.add(IpSpec(device="dev-1", interface="Loopback0", address="10.255.0.1/32"))
    plan.add(VrfSpec(name="PROD", description="prod vrf"))
    plan.add(BgpPeeringSpec(name="peer-1"))
    plan.add(CableSpec(a_device="dev-1", a_interface="Ethernet1", b_device="dev-2", b_interface="Ethernet1"))

    await PlanReconciler(_Gen(client)).apply(plan)
    assert client.created_calls

    for kind_name, payload in client.created_calls:
        assert kind_name in schema_fields, f"Spec emitted unknown schema kind {kind_name!r}"
        unknown = set(payload) - schema_fields[kind_name]
        assert not unknown, f"{kind_name} received unknown schema keys from spec: {sorted(unknown)}"
