"""add_provider_connection on a decommissioned connection, and a link that left the plan: the teardown it runs.

A recording fake client holds the objects the teardown follows by edge (end interfaces, their address and prefix, the
peering on them, the generated carrier) and records every delete in order. The pure decisions are pinned in
test_provider_teardown_planner.py; here the generator is asserted to run them in a safe order, to touch nothing shared
and to converge on a rerun. Not measured offline: the real deletion order on a server, the lock behaviour and the pool
allocation release (see tests/integration/test_67_all_demo_provider_connections.py).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from tests.unit import provider_connection_helpers as h
from tests.unit.provider_connection_helpers import dual_link_profile  # noqa: F401

pytestmark = pytest.mark.usefixtures("dual_link_profile")

NEAR_PORT = h.pid("eg-1", "Ethernet1/1")
NIC = h.pid("AWS-SE", "vnic-AWS-FR2")
CARRIER = "AWS-FR2-XC"


def _ref(ref_id: str | None) -> SimpleNamespace:
    return SimpleNamespace(id=ref_id)


def _virtual(node_id: str, name: str, parent: str | None, device: str, address: str | None) -> SimpleNamespace:
    """A DcimVirtualInterface as the teardown reads it (ip_address, parent_interface, capabilities and device loaded)."""
    return SimpleNamespace(
        id=node_id,
        name=SimpleNamespace(value=name),
        typename="DcimVirtualInterface",
        ip_address=_ref(address),
        parent_interface=_ref(parent),
        device=_ref(device),
        capabilities=SimpleNamespace(peers=[]),
    )


class TeardownClient(h.FakeClient):
    """The recording client plus what the teardown reads by edge, and ``delete``."""

    def __init__(self) -> None:
        super().__init__()
        self.deleted: list[tuple[str, str]] = []
        self.vnodes: dict[str, SimpleNamespace] = {}
        self.peerings: dict[str, str] = {}
        self.addresses: dict[str, str] = {}  # address id -> prefix id
        self.prefixes: dict[str, tuple[set[str], set[str]]] = {}  # prefix id -> (address ids, child ids)
        self.carrier_nodes: dict[str, SimpleNamespace] = {}
        self.saved_status: list[tuple[str, str]] = []

    def leg(self, session_id: str, name: str, suffix: str = "") -> list[dict[str, Any]]:
        """Two sub-interfaces with an address each in one /30, a peering on both; returns the session's ends."""
        near = _virtual(f"near{suffix}", f"Ethernet1/1.{name}", NEAR_PORT, "dev-eg-1", f"a1{suffix}")
        far = _virtual(f"far{suffix}", f"vnic-AWS-FR2.{name}", NIC, "dev-AWS-SE", f"a2{suffix}")
        for node in (near, far):
            node.capabilities.peers.append(SimpleNamespace(id=f"bgp{suffix}", typename="ManagedBGPPeering"))
            self.vnodes[node.id] = node
        self.interfaces[NEAR_PORT] = ("Ethernet1/1", "dev-eg-1", "eg-1")
        self.add_far_end()
        self.peerings[f"bgp{suffix}"] = f"{session_id}-bgp"
        self.addresses.update({f"a1{suffix}": f"p{suffix}", f"a2{suffix}": f"p{suffix}"})
        self.prefixes[f"p{suffix}"] = ({f"a1{suffix}", f"a2{suffix}"}, set())
        self.subs.extend([h.sub(near.id, near.name.value, NEAR_PORT), h.sub(far.id, far.name.value, NIC)])
        return [h.end(near.id, near.name.value, "eg-1"), h.end(far.id, far.name.value, "AWS-SE")]

    def add_generated_carrier(
        self, *, circuit_id: str = CARRIER, status: str = "provisioning", name: str = CARRIER
    ) -> dict:
        """A carrier ``<connection>-XC`` between the near port and the NIC; returns its payload for the connection."""
        self.carrier_nodes["xc"] = SimpleNamespace(
            id="xc",
            circuit_id=SimpleNamespace(value=circuit_id),
            status=SimpleNamespace(value=status),
            provider_connections=SimpleNamespace(peers=[_ref(h.CONNECTION_ID)]),
        )
        self.add_carrier("xc", [NEAR_PORT, NIC], name=name)
        return h.carrier(
            "xc",
            name,
            [h.port(NEAR_PORT, "Ethernet1/1", "eg-1", h.NEAR), h.port(NIC, "vnic-AWS-FR2", "AWS-SE", h.FAR)],
            circuit_type="virtual_connection",
            status=status,
        )

    async def filters(self, kind: Any, **kwargs: Any) -> list[Any]:
        name = h.kind_name(kind)
        if name == "DcimVirtualInterface" and "ids" in kwargs:
            return [self.vnodes[i] for i in kwargs["ids"] if i in self.vnodes]
        if name == "ManagedBGPPeering":
            return [SimpleNamespace(id=i, name=SimpleNamespace(value=self.peerings[i])) for i in kwargs["ids"]]
        if name == "IpamIPAddress":
            return [SimpleNamespace(id=i, ip_prefix=_ref(self.addresses[i])) for i in kwargs["ids"]]
        if name == "IpamPrefix":
            return [
                SimpleNamespace(
                    id=i,
                    ip_addresses=SimpleNamespace(peers=[_ref(a) for a in self.prefixes[i][0]]),
                    children=SimpleNamespace(peers=[_ref(c) for c in self.prefixes[i][1]]),
                )
                for i in kwargs["ids"]
            ]
        found = await super().filters(kind, **kwargs)
        if name == "DcimInterface":
            for node in found:
                node.typename = (
                    "DcimVirtualInterface" if node.name.value.startswith("vnic-") else "DcimPhysicalInterface"
                )
        return found

    async def get(self, kind: Any, **kwargs: Any) -> Any:
        if h.kind_name(kind) == "TopologyCarrierCircuit":
            node = self.carrier_nodes.get(kwargs["id"])
            if node is not None:

                async def _save(**save_kwargs: Any) -> None:
                    self.saved_status.append((node.id, node.status.value))

                node.save = _save
            return node
        return await super().get(kind, **kwargs)

    async def delete(self, kind: Any, id: str) -> None:  # noqa: A002
        self.deleted.append((h.kind_name(kind), id))
        self.subs = [sub for sub in self.subs if sub.id != id]


async def _run(payload: dict[str, Any], client: TeardownClient) -> AsyncMock:
    """Run once; returns the stand-in of the VRF reconcile (its own tests: the instances are not the subject here)."""
    generator: Any = h.make_generator(client)
    reconcile = AsyncMock()
    generator.reconcile_vrfs = reconcile
    await generator.generate(payload)
    return reconcile


def _decommissioned(client: TeardownClient, *, sessions: list[dict[str, Any]], carriers: list[dict[str, Any]]) -> dict:
    return h.connection(status="decommissioned", vlan_id=100, sessions=sessions, carriers=carriers)


@pytest.mark.asyncio
async def test_decommissioning_removes_what_the_connection_built_in_a_safe_order() -> None:
    client = TeardownClient()
    ends = client.leg("AWS-FR2-L1", "100")
    carrier = client.add_generated_carrier()
    session = h.session("AWS-FR2-L1", ends=ends, session_id="s1")

    reconcile = await _run(_decommissioned(client, sessions=[session], carriers=[carrier]), client)

    # The devices of the removed legs have their VRF membership converged once the addresses are gone.
    reconcile.assert_awaited_once_with({"dev-eg-1", "dev-AWS-SE"})
    assert client.deleted == [
        ("ManagedBGPPeering", "bgp"),
        ("IpamIPAddress", "a1"),
        ("IpamIPAddress", "a2"),
        ("IpamPrefix", "p"),
        ("TopologySessionCircuit", "s1"),
        ("DcimVirtualInterface", "near"),
        ("DcimVirtualInterface", "far"),
        ("TopologyCarrierCircuit", "xc"),
        ("DcimVirtualInterface", NIC),
    ]
    assert client.of("create") == [] and client.of("save") == []
    assert client.group_context.related_node_ids == [h.CONNECTION_ID]


@pytest.mark.asyncio
async def test_nothing_shared_is_deleted() -> None:
    """Physical ports, the stub device and its Loopback0 / BGP process are never a delete; the near port stays."""
    client = TeardownClient()
    ends = client.leg("AWS-FR2-L1", "100")
    carrier = client.add_generated_carrier()

    await _run(
        _decommissioned(client, sessions=[h.session("AWS-FR2-L1", ends=ends, session_id="s1")], carriers=[carrier]),
        client,
    )

    deleted_ids = {node_id for _, node_id in client.deleted}
    assert NEAR_PORT not in deleted_ids
    assert {kind for kind, _ in client.deleted} <= {
        "ManagedBGPPeering",
        "IpamIPAddress",
        "IpamPrefix",
        "TopologySessionCircuit",
        "DcimVirtualInterface",
        "TopologyCarrierCircuit",
    }
    assert not any(node_id.startswith(("dev-", "lo-", "proc-")) for node_id in deleted_ids)


@pytest.mark.asyncio
async def test_a_rerun_on_a_decommissioned_connection_with_nothing_left_writes_nothing() -> None:
    client = TeardownClient()

    await _run(_decommissioned(client, sessions=[], carriers=[]), client)

    assert client.deleted == [] and client.log == []
    assert client.group_context.related_node_ids == [h.CONNECTION_ID]


@pytest.mark.asyncio
async def test_a_half_torn_state_converges() -> None:
    """The peering, addresses and prefix are gone already (an earlier run stopped): the rest is removed."""
    client = TeardownClient()
    ends = client.leg("AWS-FR2-L1", "100")
    for node in client.vnodes.values():
        node.ip_address = _ref(None)
        node.capabilities.peers.clear()

    await _run(
        _decommissioned(client, sessions=[h.session("AWS-FR2-L1", ends=ends, session_id="s1")], carriers=[]), client
    )

    assert client.deleted == [
        ("TopologySessionCircuit", "s1"),
        ("DcimVirtualInterface", "near"),
        ("DcimVirtualInterface", "far"),
    ]


@pytest.mark.asyncio
async def test_a_carrier_a_person_filled_in_is_decommissioned_not_deleted() -> None:
    client = TeardownClient()
    carrier = client.add_generated_carrier(circuit_id="EQX-FAB-1", status="active")

    await _run(_decommissioned(client, sessions=[], carriers=[carrier]), client)

    assert client.deleted == []
    assert client.saved_status == [("xc", "decommissioned")]


@pytest.mark.asyncio
async def test_an_authored_carrier_on_the_near_port_is_left_alone() -> None:
    client = TeardownClient()
    carrier = client.add_generated_carrier(name="AWS-DX-FRA-1", circuit_id="dx-1", status="active")

    await _run(_decommissioned(client, sessions=[], carriers=[carrier]), client)

    assert client.deleted == [] and client.saved_status == []


@pytest.mark.asyncio
async def test_a_carrier_end_that_still_carries_another_connections_leg_keeps_the_carrier() -> None:
    """Two connections on one stub: the NIC of another connection's sub-interface is not this connection's to remove."""
    client = TeardownClient()
    carrier = client.add_generated_carrier()
    client.subs.append(h.sub("other-sub", "vnic-AWS-FR2.200", NIC))

    await _run(_decommissioned(client, sessions=[], carriers=[carrier]), client)

    assert client.deleted == []


@pytest.mark.asyncio
async def test_a_sub_interface_another_session_still_ends_on_is_kept_and_tracked() -> None:
    """Kept on purpose, and added to the run's group so the SDK cleanup does not delete it behind the plan."""
    client = TeardownClient()
    ends = client.leg("AWS-FR2-L1", "100")
    other = SimpleNamespace(id="other-session", capabilities=SimpleNamespace(peers=[_ref("near")]))
    client.port_sessions = [other]

    await _run(
        _decommissioned(client, sessions=[h.session("AWS-FR2-L1", ends=ends, session_id="s1")], carriers=[]), client
    )

    assert ("DcimVirtualInterface", "near") not in client.deleted
    assert ("DcimVirtualInterface", "far") in client.deleted
    assert "near" in client.group_context.related_node_ids


@pytest.mark.asyncio
async def test_a_link_that_left_the_plan_is_torn_down_while_the_kept_links_are_not() -> None:
    """A session circuit of the connection that no link of the plan names goes with what it owns."""
    client = TeardownClient()
    client.add_far_end()
    kept_ends = client.leg("AWS-FR2-L1", "100", "-k")
    stale_ends = client.leg("AWS-FR2-L9", "100", "-s")
    carrier = h.own_carrier()
    payload = h.connection(
        vlan_id=100,
        carriers=[carrier],
        sessions=[
            h.session("AWS-FR2-L1", ends=kept_ends, session_id="keep"),
            h.session("AWS-FR2-L9", ends=stale_ends, session_id="stale"),
        ],
    )

    await _run(payload, client)

    assert ("TopologySessionCircuit", "stale") in client.deleted
    assert ("TopologySessionCircuit", "keep") not in client.deleted
    assert ("ManagedBGPPeering", "bgp-s") in client.deleted
    assert ("ManagedBGPPeering", "bgp-k") not in client.deleted


@pytest.mark.asyncio
async def test_a_path_change_to_a_vpn_reclaims_the_old_legs_and_the_generated_carrier() -> None:
    """Dedicated -> VPN: the kept circuit loses its sub-interfaces (the interface goes, the address stays with the
    kept session), then nothing rides the generated carrier any more, so it goes with its NIC."""
    client = TeardownClient()
    ends = client.leg("AWS-FR2-L1", "100")
    carrier = client.add_generated_carrier()
    payload = h.connection(
        path_type="internet_vpn",
        internet=[h.internet_carrier("edge-1")],
        link_prefixes=[h.prefix("p-1", "169.254.9.0/30"), h.prefix("p-2", "169.254.10.0/30")],
        carriers=[carrier],
        sessions=[h.session("AWS-FR2-L1", ends=ends, session_id="sess-1")],
        profile="dual",
    )

    await _run(payload, client)

    assert ("DcimVirtualInterface", "near") in client.deleted
    assert ("DcimVirtualInterface", "far") in client.deleted
    assert ("IpamIPAddress", "a1") not in client.deleted
    assert ("TopologySessionCircuit", "sess-1") not in client.deleted
    assert client.deleted[-2:] == [("TopologyCarrierCircuit", "xc"), ("DcimVirtualInterface", NIC)]
