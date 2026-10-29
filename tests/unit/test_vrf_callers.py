"""The callers of generators/helpers/vrf.py: where each generator converges VRF instances, and in which order."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from infrahub_sdk.generator import InfrahubGenerator

from generators.topology.circuit import PhysicalCircuitGenerator
from generators.topology.customer_dc import CustomerDeploymentDCExchangeGenerator
from generators.topology.segment import VxlanSegmentGenerator
from generators.topology.segment_ports import SegmentPortTagMixin
from generators.topology.virtual_circuit import VirtualCircuitSessionGenerator
from generators.vlan_domain import segment_lock_key
from tests.unit.customer_generator_helpers import make_generator
from tests.unit.simulators.circuit_fakes import (
    FakeClient,
    handoff_edge_port,
    handoff_party_port,
    handoff_payload,
    interconnect_payload,
    physical_generator,
    session_generator,
)

# --- segment reconcile --------------------------------------------------------------------------------------------


def _segment_gen() -> Any:
    gen: Any = VxlanSegmentGenerator.__new__(VxlanSegmentGenerator)
    gen.client = AsyncMock()
    gen.logger = MagicMock()
    gen.events = []

    @asynccontextmanager
    async def _lock(key: str) -> AsyncGenerator[None]:
        gen.events.append(f"lock:{key}")
        yield
        gen.events.append(f"unlock:{key}")

    gen.resource_lock = _lock
    gen._reconcile_segment_interface_tags = AsyncMock(return_value=(0, 0))
    return gen


def _state(*domains: str, members: dict[str, list[str]] | None = None) -> dict[str, Any]:
    return {
        "domain_devices": members or {d: [d] for d in domains},
        "segment": {"id": "seg-1", "capabilities": []},
        "deployments": [
            {"id": "sd-1", "deployment_id": "dc", "rows": {d: {"id": f"row-{d}", "vlan_id": 100} for d in domains}}
        ],
    }


class TestSegmentReconcile:
    def test_the_vrf_pass_covers_the_domains_the_rows_were_on_before_and_are_on_after(self) -> None:
        """A switch that lost its row (its domain is only in the 'before' state) is reconciled with the others."""
        gen = _segment_gen()
        gen._fetch_segment_vlan_state = AsyncMock(
            side_effect=[_state("dom-lost", "dom-kept"), _state("dom-kept", "dom-new")]
        )
        gen._reconcile_segment_vlan_domains_locked = AsyncMock(
            side_effect=lambda *_a: gen.events.append("rows") or True
        )
        gen.reconcile_vrfs = AsyncMock(side_effect=lambda ids: gen.events.append("vrfs"))

        asyncio.run(gen.reconcile_vxlan_segment("seg-1", "seg"))

        gen.reconcile_vrfs.assert_awaited_once()
        (device_ids,) = gen.reconcile_vrfs.call_args.args
        assert sorted(device_ids) == ["dom-kept", "dom-lost", "dom-new"]
        key = segment_lock_key("seg-1")
        assert gen.events == [f"lock:{key}", "rows", "vrfs", f"unlock:{key}"]

    def test_unchanged_rows_reuse_the_state(self) -> None:
        """No row written: the state read before serves as the 'after' too (one read)."""
        gen = _segment_gen()
        gen._fetch_segment_vlan_state = AsyncMock(return_value=_state("dom-1", members={"dom-1": ["d1"]}))
        gen._reconcile_segment_vlan_domains_locked = AsyncMock(return_value=False)
        gen.reconcile_vrfs = AsyncMock()

        asyncio.run(gen.reconcile_vxlan_segment("seg-1", "seg"))

        assert gen._fetch_segment_vlan_state.await_count == 1
        gen.reconcile_vrfs.assert_awaited_once_with({"d1"})

    def test_a_domain_is_a_device_or_an_mlag_of_two(self) -> None:
        """The state carries the devices of each domain; an MLAG's are its two members, a device's is itself."""
        state = _state("solo", "mlag-1", members={"solo": ["solo"], "mlag-1": ["m1", "m2"]})

        assert SegmentPortTagMixin._row_device_ids(state) == {"solo", "m1", "m2"}

    def test_no_row_has_no_device(self) -> None:
        """A segment with no row anywhere (or a deleted one: no state) has no switch to converge."""
        assert SegmentPortTagMixin._row_device_ids(_state()) == set()
        assert SegmentPortTagMixin._row_device_ids(None) == set()

    def test_the_state_reads_the_members_off_the_query(self) -> None:
        """segment_vlan_domains.gql returns the MLAG's capabilities with the row: no further read."""
        gen = _segment_gen()
        domain = {
            "node": {
                "id": "mlag-1",
                "__typename": "ManagedMLAG",
                "capabilities": {"edges": [{"node": {"id": "m1"}}, {"node": {"id": "m2"}}]},
            }
        }
        solo = {"node": {"id": "solo", "__typename": "DcimPhysicalDevice"}}
        rows = [
            {"node": {"id": "r1", "vlan_id": {"value": 100}, "vlan_domain": domain}},
            {"node": {"id": "r2", "vlan_id": {"value": 100}, "vlan_domain": solo}},
        ]
        gen.client.execute_graphql = AsyncMock(
            return_value={
                "ManagedVxlanSegment": {"edges": [{"node": {"id": "seg-1"}}]},
                "ManagedSegmentDeployment": {
                    "edges": [
                        {
                            "node": {
                                "id": "sd-1",
                                "deployment": {"node": {"id": "dc"}},
                                "vlan_domain_segments": {"edges": rows},
                            }
                        }
                    ]
                },
            }
        )

        state = asyncio.run(gen._fetch_segment_vlan_state("seg-1"))

        assert state is not None
        assert state["domain_devices"] == {"mlag-1": ["m1", "m2"], "solo": ["solo"]}

    def test_a_deleted_segment_reconciles_nothing(self) -> None:
        """No state: the rows and SVIs go by the schema cascade, a leftover instance by the device's next pass."""
        gen = _segment_gen()
        gen._fetch_segment_vlan_state = AsyncMock(return_value=None)
        gen.reconcile_vrfs = AsyncMock()

        assert asyncio.run(gen.reconcile_vxlan_segment("seg-1", "seg")) is None
        gen.reconcile_vrfs.assert_not_awaited()


# --- context legs -------------------------------------------------------------------------------------------------


def _dc_payload() -> dict[str, Any]:
    """A DC footprint with its parent DC (the boarding query's shape, only what generate() reads before the legs)."""
    return {
        "TopologyCustomerDC": [
            {
                "id": "cust-1",
                "name": "C005-P-DC10",
                "environment": "p",
                "owner": {"org_id": "C005", "name": "Drentec BV"},
                "parent": {"id": "dc10-id", "name": "DC10"},
                "design": {"dedicated_firewall": {"value": False}, "dedicated_loadbalancer": {"value": False}},
            }
        ]
    }


class TestContextLegs:
    @pytest.mark.asyncio
    async def test_the_border_devices_follow_the_legs_not_precede_them(self) -> None:
        """After _ensure_transit_legs returned (its context lock released), once per context that was built."""
        gen = make_generator(CustomerDeploymentDCExchangeGenerator)
        events: list[str] = []
        gen._resolve_context_target = AsyncMock(return_value=MagicMock())
        gen._ensure_transit_legs = AsyncMock(side_effect=lambda **_: events.append("legs") or "ctx-1")
        gen._reconcile_border_vrfs = AsyncMock(side_effect=lambda ctx: events.append(f"vrfs:{ctx}"))
        gen.client.filters = AsyncMock(return_value=[MagicMock(id="cluster-1")])

        await gen.generate(_dc_payload())

        assert events == ["legs", "vrfs:ctx-1"] * len(gen._context_specs)

    @pytest.mark.asyncio
    async def test_a_context_that_was_not_built_reconciles_nothing(self) -> None:
        """A failed leg run returns no context id: there is nothing tagged to follow."""
        gen = make_generator(CustomerDeploymentDCExchangeGenerator)
        gen._resolve_context_target = AsyncMock(return_value=MagicMock())
        gen._ensure_transit_legs = AsyncMock(return_value=None)
        gen._reconcile_border_vrfs = AsyncMock()

        await gen.generate(_dc_payload())

        gen._reconcile_border_vrfs.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_devices_are_the_ones_whose_ports_carry_the_context(self) -> None:
        """One read of the ports (physical or LAG) tagged with the context, then the pass over their devices."""
        gen = make_generator(CustomerDeploymentDCExchangeGenerator)
        # make_generator stubs the hook for the other tests; use the real one here.
        gen._reconcile_border_vrfs = CustomerDeploymentDCExchangeGenerator._reconcile_border_vrfs.__get__(gen)
        gen.reconcile_vrfs = AsyncMock()
        ports = [
            SimpleNamespace(device=SimpleNamespace(id="bl-1"), get_kind=lambda: "DcimPhysicalInterface"),
            SimpleNamespace(device=SimpleNamespace(id="bl-2"), get_kind=lambda: "DcimLAGInterface"),
            SimpleNamespace(device=SimpleNamespace(id="bl-1"), get_kind=lambda: "DcimPhysicalInterface"),
            SimpleNamespace(device=SimpleNamespace(id="fw-1"), get_kind=lambda: "DcimVirtualInterface"),
        ]
        gen.client.filters = AsyncMock(return_value=ports)

        await gen._reconcile_border_vrfs("ctx-1")

        assert gen.client.filters.call_args.kwargs["capabilities__ids"] == ["ctx-1"]
        gen.reconcile_vrfs.assert_awaited_once_with({"bl-1", "bl-2"})


# --- handoff circuits ---------------------------------------------------------------------------------------------


def _record(client: FakeClient, gen: Any) -> None:
    gen.reconcile_vrfs.side_effect = lambda ids, **kwargs: client.writes.append(("vrf", ",".join(sorted(ids)), kwargs))


@pytest.fixture
def sdk_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """InfrahubGenerator.run is the SDK's tracking wrapper: here it is generate() alone, recording the end of the run."""

    async def _run(self: Any, identifier: str, data: dict[str, Any] | None = None) -> None:
        await self.generate(data)
        self.client.writes.append(("cleanup", identifier, {}))

    monkeypatch.setattr(InfrahubGenerator, "run", _run)


def _record_held(client: FakeClient, gen: Any) -> None:
    """Make the held block write 'ensure' on entry and 'released' on exit into the client's ordered writes."""

    @asynccontextmanager
    async def _held(needs: dict[str, set[str]]) -> AsyncGenerator[None]:
        gen.vrfs_held_calls.append(needs)
        client.writes.append(("ensure", "", {}))
        yield
        client.writes.append(("released", "", {}))

    gen.vrfs_held = _held


class TestHandoffCircuit:
    @pytest.mark.asyncio
    async def test_both_ends_get_the_namespace_before_the_address_is_written(self) -> None:
        """vrfs_held for the two end devices and the circuit's namespace, ahead of the prefix allocation."""
        client = FakeClient()
        gen = session_generator(client)
        _record_held(client, gen)

        await gen.generate(handoff_payload())

        assert gen.vrfs_held_calls[0] == {"dev-eg-fr201": {"PROD"}, "dev-EXT-AWS-DX-FR2": {"PROD"}}
        assert [action for action, _, _ in client.writes[:2]] == ["ensure", "allocate"]

    @pytest.mark.asyncio
    async def test_the_device_locks_are_held_until_the_addresses_are_saved(self) -> None:
        """The held block ends after the end interfaces are addressed, so a pruning pass cannot see an unused instance."""
        client = FakeClient()
        gen = session_generator(client)
        _record_held(client, gen)

        await gen.generate(handoff_payload())

        actions = [action for action, _, _ in client.writes]
        assert actions.index("released") > actions.index("allocate")
        assert "save" in actions[: actions.index("released")]

    @pytest.mark.asyncio
    async def test_an_interconnect_has_no_demand(self) -> None:
        """A DCI is in ``default``: no instance is ensured and no end is remembered."""
        client = FakeClient()
        gen = session_generator(client)

        await gen.generate(interconnect_payload())

        assert gen.vrfs_held_calls == [{}]
        assert gen._vrf_device_ids == set()

    @pytest.mark.asyncio
    async def test_the_ends_are_converged_after_the_runs_cleanup(self, sdk_run: None) -> None:
        """The /31 of a decommissioned circuit is deleted by the cleanup: the former ends are reconciled after it."""
        client = FakeClient()
        gen = session_generator(client)
        _record(client, gen)

        await gen.run("VC-C001-EQXFR-AWS", handoff_payload(status="decommissioned"))

        assert [action for action, _, _ in client.writes] == ["cleanup", "vrf"]
        assert client.writes[1][1] == "dev-EXT-AWS-DX-FR2,dev-eg-fr201"

    @pytest.mark.asyncio
    async def test_a_circuit_with_one_end_left_reconciles_the_listed_end(self, sdk_run: None) -> None:
        """Fewer than two ends: the cleanup removes the /31; the end still listed is reconciled (accepted gap: the other)."""
        client = FakeClient()
        gen = session_generator(client)
        _record(client, gen)

        await gen.run("x", handoff_payload(ends=[handoff_edge_port()]))

        assert client.writes[-1][:2] == ("vrf", "dev-eg-fr201")

    @pytest.mark.asyncio
    async def test_a_built_circuit_is_reconciled_after_the_run_too(self, sdk_run: None) -> None:
        """A changed namespace leaves the old instance without demand once the old prefix is cleaned up."""
        client = FakeClient()
        gen = session_generator(client)
        _record(client, gen)

        await gen.run("x", handoff_payload(ends=[handoff_edge_port(), handoff_party_port()]))

        assert client.writes[-1][:2] == ("vrf", "dev-EXT-AWS-DX-FR2,dev-eg-fr201")


# --- physical circuits --------------------------------------------------------------------------------------------


class TestPhysicalCircuit:
    @pytest.mark.asyncio
    async def test_the_end_devices_are_reconciled(self) -> None:
        """The authored INTERNET addresses of the cage edges demand an instance; add_circuit is the run that sees them."""
        client = FakeClient()
        gen = physical_generator(client)
        gen.client.get = AsyncMock()
        gen._fan_out_sessions = AsyncMock()
        circuit = {
            "id": "pc-1",
            "name": {"value": "XC-1"},
            "circuit_id": {"value": "XC-1"},
            "locations": {"edges": []},
            "capabilities": {
                "edges": [
                    {"node": _endpoint("if-1", "dev-1", "TopologyColocationZone", "z1")},
                    {"node": _endpoint("if-2", "dev-2", "TopologyExternalProviders", "isp")},
                ]
            },
        }
        gen._add_locations = AsyncMock()

        await gen.generate({"TopologyPhysicalCircuit": {"edges": [{"node": circuit}]}})

        gen.reconcile_vrfs.assert_awaited_once_with({"dev-1", "dev-2"})


def _endpoint(port_id: str, device_id: str, deployment_kind: str, deployment_id: str) -> dict[str, Any]:
    return {
        "id": port_id,
        "name": {"value": "Ethernet1"},
        "device": {
            "node": {
                "id": device_id,
                "name": {"value": device_id},
                "deployment": {
                    "node": {"id": deployment_id, "__typename": deployment_kind, "display_label": deployment_id}
                },
            }
        },
    }


def test_every_host_composes_the_mixin_without_shadowing_it() -> None:
    """The hosts inherit the one VrfMixin implementation (no stub body shadows it in the MRO)."""
    from generators.helpers.vrf import VrfMixin
    from generators.topology.office_fabric import OfficeFabricGenerator

    for host in (
        PhysicalCircuitGenerator,
        VirtualCircuitSessionGenerator,
        VxlanSegmentGenerator,
        CustomerDeploymentDCExchangeGenerator,
        OfficeFabricGenerator,
    ):
        assert host.reconcile_vrfs is VrfMixin.reconcile_vrfs
        assert host.vrfs_held is VrfMixin.vrfs_held
