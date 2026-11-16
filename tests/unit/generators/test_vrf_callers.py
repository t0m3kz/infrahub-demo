"""The callers of generators/capabilities/vrf.py: where each generator converges VRF instances, and in which order."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from infrahub_sdk.generator import InfrahubGenerator

from generators.segments import segment_ports
from generators.segments.segment import VxlanSegmentGenerator
from generators.segments.segment_ports import reconcile_vxlan_segment
from generators.segments.segment_vlan import fetch_segment_vlan_state, segment_lock_key
from tests.unit.simulators.circuit_fakes import (
    FakeClient,
    handoff_edge_port,
    handoff_party_port,
    handoff_payload,
    interconnect_payload,
    physical_generator,
    session_generator,
)

# --- segment reconcile


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
    return gen


@pytest.fixture
def no_port_work(monkeypatch: pytest.MonkeyPatch) -> None:
    """The port side of the segment reconcile finds nothing to tag: only the rows and the VRF pass are under test."""
    monkeypatch.setattr(segment_ports, "_derived_segment_port_ids", AsyncMock(return_value=set()))
    monkeypatch.setattr(segment_ports, "_port_devices", AsyncMock(return_value={}))
    monkeypatch.setattr(segment_ports, "_reconcile_segment_port_rows", AsyncMock(return_value=(0, 0)))


def _state(*devices: str) -> dict[str, Any]:
    return {
        "segment": {"id": "seg-1", "capabilities": []},
        "deployments": [
            {"id": "sd-1", "deployment_id": "dc", "rows": {d: {"id": f"row-{d}", "vlan_id": 100} for d in devices}}
        ],
    }


@pytest.mark.usefixtures("no_port_work")
class TestSegmentReconcile:
    def test_the_vrf_pass_covers_the_devices_the_rows_were_on_before_and_are_on_after(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A switch that lost its row (it is only in the 'before' state) is reconciled with the others."""
        gen = _segment_gen()
        monkeypatch.setattr(
            segment_ports,
            "fetch_segment_vlan_state",
            AsyncMock(side_effect=[_state("dev-lost", "dev-kept"), _state("dev-kept", "dev-new")]),
        )
        monkeypatch.setattr(
            segment_ports,
            "reconcile_segment_vlans",
            AsyncMock(side_effect=lambda *_a: gen.events.append("rows") or True),
        )
        vrfs = AsyncMock(side_effect=lambda _gen, ids: gen.events.append("vrfs"))
        monkeypatch.setattr(segment_ports, "reconcile_vrfs", vrfs)

        asyncio.run(reconcile_vxlan_segment(gen, "seg-1", "seg"))

        vrfs.assert_awaited_once()
        assert sorted(vrfs.call_args.args[1]) == ["dev-kept", "dev-lost", "dev-new"]
        key = segment_lock_key("seg-1")
        assert gen.events == [f"lock:{key}", "rows", "vrfs", f"unlock:{key}"]

    def test_unchanged_rows_reuse_the_state(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No row written: the state read before serves as the 'after' too (one read)."""
        gen = _segment_gen()
        fetch = AsyncMock(return_value=_state("d1"))
        monkeypatch.setattr(segment_ports, "fetch_segment_vlan_state", fetch)
        monkeypatch.setattr(segment_ports, "reconcile_segment_vlans", AsyncMock(return_value=False))
        vrfs = AsyncMock()
        monkeypatch.setattr(segment_ports, "reconcile_vrfs", vrfs)

        asyncio.run(reconcile_vxlan_segment(gen, "seg-1", "seg"))

        assert fetch.await_count == 1
        vrfs.assert_awaited_once_with(gen, {"d1"})

    def test_the_devices_are_the_row_devices_of_every_deployment(self) -> None:
        """The state rows are keyed by device: the devices of the VRF pass are exactly those keys."""
        state = _state("solo", "m1", "m2")

        assert segment_ports._row_device_ids(state) == {"solo", "m1", "m2"}

    def test_no_row_has_no_device(self) -> None:
        """A segment with no row anywhere (or a deleted one: no state) has no switch to converge."""
        assert segment_ports._row_device_ids(_state()) == set()
        assert segment_ports._row_device_ids(None) == set()

    def test_the_state_reads_the_row_device_off_the_query(self) -> None:
        """segment_vlans.gql returns the device with the row: no further read."""
        gen = _segment_gen()
        rows = [
            {"node": {"id": "r1", "vlan_id": {"value": 100}, "device": {"node": {"id": "m1"}}}},
            {"node": {"id": "r2", "vlan_id": {"value": 100}, "device": {"node": {"id": "solo"}}}},
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
                                "vlans": {"edges": rows},
                            }
                        }
                    ]
                },
            }
        )

        state = asyncio.run(fetch_segment_vlan_state(gen, "seg-1"))

        assert state is not None
        assert segment_ports._row_device_ids(state) == {"m1", "solo"}

    def test_a_deleted_segment_reconciles_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No state: the rows and SVIs go by the schema cascade, a leftover instance by the device's next pass."""
        gen = _segment_gen()
        monkeypatch.setattr(segment_ports, "fetch_segment_vlan_state", AsyncMock(return_value=None))
        vrfs = AsyncMock()
        monkeypatch.setattr(segment_ports, "reconcile_vrfs", vrfs)

        assert asyncio.run(reconcile_vxlan_segment(gen, "seg-1", "seg")) is None
        vrfs.assert_not_awaited()


# --- context legs


# --- handoff circuits


@pytest.fixture
def sdk_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """InfrahubGenerator.run is the SDK's tracking wrapper: here it is generate() alone, recording the end of the run."""

    async def _run(self: Any, identifier: str, data: dict[str, Any] | None = None) -> None:
        await self.generate(data)
        self.client.writes.append(("cleanup", identifier, {}))

    monkeypatch.setattr(InfrahubGenerator, "run", _run)


def _vrf_actions(client: FakeClient) -> list[tuple[str, str]]:
    """The VRF pass of the run, in order: ("vrf-lock" | "vrf" | "vrf-unlock" | "cleanup" | "allocate" | "save", detail).
    The FakeClient serves the pass its device read and records each; its world has no device that carries VRFs."""
    keep = {"vrf-lock", "vrf", "vrf-unlock", "cleanup", "allocate", "save"}
    return [(action, label) for action, label, _ in client.writes if action in keep]


class TestHandoffCircuit:
    @pytest.mark.asyncio
    async def test_both_ends_get_the_namespace_before_the_address_is_written(self) -> None:
        """The two end devices are read for the pass, under their locks, ahead of the prefix allocation."""
        client = FakeClient()
        gen = session_generator(client)

        await gen.generate(handoff_payload())

        assert [action for action, _ in _vrf_actions(client)[:5]] == [
            "vrf-lock",
            "vrf-lock",
            "vrf",
            "allocate",
            "save",
        ]
        assert [label for action, label in _vrf_actions(client) if action == "vrf-lock"] == [
            "vrf-device-dev-EXT-AWS-DX-FR2",
            "vrf-device-dev-eg-fr201",
        ]
        assert next(label for action, label in _vrf_actions(client) if action == "vrf") == (
            "dev-EXT-AWS-DX-FR2,dev-eg-fr201"
        )

    @pytest.mark.asyncio
    async def test_the_device_locks_are_held_until_the_addresses_are_saved(self) -> None:
        """The locks are released after the end interfaces are addressed, so a pruning pass cannot see an unused
        instance."""
        client = FakeClient()
        gen = session_generator(client)

        await gen.generate(handoff_payload())

        actions = [action for action, _ in _vrf_actions(client)]
        assert actions.index("vrf-unlock") > actions.index("allocate")
        assert "save" in actions[: actions.index("vrf-unlock")]

    @pytest.mark.asyncio
    async def test_an_interconnect_has_no_demand(self) -> None:
        """A DCI is in ``default``: no device is read for the pass and no end is remembered."""
        client = FakeClient()
        gen = session_generator(client)

        await gen.generate(interconnect_payload())

        assert [action for action, _ in _vrf_actions(client) if action.startswith("vrf")] == []
        assert gen._vrf_device_ids == set()

    @pytest.mark.asyncio
    async def test_the_ends_are_converged_after_the_runs_cleanup(self, sdk_run: None) -> None:
        """The /31 of a decommissioned circuit is deleted by the cleanup: the former ends are reconciled after it."""
        client = FakeClient()
        gen = session_generator(client)

        await gen.run("VC-C001-EQXFR-AWS", handoff_payload(status="decommissioned"))

        assert [(a, d) for a, d in _vrf_actions(client) if a in ("cleanup", "vrf")] == [
            ("cleanup", "VC-C001-EQXFR-AWS"),
            ("vrf", "dev-EXT-AWS-DX-FR2,dev-eg-fr201"),
        ]

    @pytest.mark.asyncio
    async def test_a_circuit_with_one_end_left_reconciles_the_listed_end(self, sdk_run: None) -> None:
        """Fewer than two ends: the cleanup removes the /31; the end still listed is reconciled (accepted gap: the other)."""
        client = FakeClient()
        gen = session_generator(client)

        await gen.run("x", handoff_payload(ends=[handoff_edge_port()]))

        assert [(a, d) for a, d in _vrf_actions(client) if a in ("cleanup", "vrf")][-1] == ("vrf", "dev-eg-fr201")

    @pytest.mark.asyncio
    async def test_a_built_circuit_is_reconciled_after_the_run_too(self, sdk_run: None) -> None:
        """A changed namespace leaves the old instance without demand once the old prefix is cleaned up."""
        client = FakeClient()
        gen = session_generator(client)

        await gen.run("x", handoff_payload(ends=[handoff_edge_port(), handoff_party_port()]))

        assert [(a, d) for a, d in _vrf_actions(client) if a in ("cleanup", "vrf")][-1] == (
            "vrf",
            "dev-EXT-AWS-DX-FR2,dev-eg-fr201",
        )


# --- carrier circuits


class TestCarrierCircuit:
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
                    {"node": _endpoint("if-2", "dev-2", "TopologyCloudRegion", "isp")},
                ]
            },
        }
        gen._add_locations = AsyncMock()

        await gen.generate({"TopologyCarrierCircuit": {"edges": [{"node": circuit}]}})

        assert [label for action, label, _ in client.writes if action == "vrf"] == ["dev-1,dev-2"]


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
