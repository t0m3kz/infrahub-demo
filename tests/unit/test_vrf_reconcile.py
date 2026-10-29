"""Unit tests for generators/helpers/vrf.py and svi.py: the one writer of VRF instances and segment SVIs."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from generators.helpers.svi import plan_svis
from tests.unit.vrf_instances_helpers import facts_of, raw_device, raw_instance, raw_row, raw_svi
from tests.unit.vrf_reconcile_helpers import VrfHost, response

_UNTRACKED = {"allow_upsert": True, "update_group_context": False}


def _leaf(**kwargs: Any) -> dict[str, Any]:
    return raw_device("d1", "leaf-1", **kwargs)


def _settled() -> dict[str, Any]:
    """A leaf that routes PROD on VLAN 100 and has everything for it."""
    return _leaf(
        rows=[raw_row("r1", 100, "PROD")],
        capabilities=[raw_instance("vrf-1", "leaf-1", "PROD")],
        interfaces=[raw_svi("svi-1", 100, "r1", "vrf-1", "PROD")],
    )


def _ensure(host: VrfHost, needs: dict[str, set[str]]) -> None:
    async def _hold() -> None:
        async with host.vrfs_held(needs):
            pass

    asyncio.run(_hold())


def _reconcile(host: VrfHost, ids: list[str], **kwargs: Any) -> None:
    asyncio.run(host.reconcile_vrfs(ids, **kwargs))


def test_a_routed_row_creates_the_instance_and_its_svi() -> None:
    """The instance first (from the VRF side, untracked), then the SVI pointing at it by the id just created."""
    host = VrfHost(response(_leaf(rows=[raw_row("r1", 100, "PROD")])))

    _reconcile(host, ["d1"])

    assert host.written("ManagedVRF") == [
        {"name": "leaf-1-vrf-prod", "namespace": {"id": "ns-PROD"}, "capabilities": [{"id": "d1"}], "status": "active"}
    ]
    assert host.written("DcimVirtualInterface") == [
        {
            "name": "Vlan100",
            "device": {"id": "d1"},
            "role": "svi",
            "status": "active",
            "vlan_domain_segment": {"id": "r1"},
            "vrf": {"id": "new-1"},
        }
    ]
    assert [kind for kind, _ in host.created] == ["ManagedVRF", "DcimVirtualInterface"]
    assert host.saves == [_UNTRACKED, _UNTRACKED]
    assert host.deleted == []


def test_nothing_is_written_when_nothing_changed() -> None:
    """A re-run finds the instance and the SVI and writes nothing (idempotent)."""
    host = VrfHost(response(_settled()))

    _reconcile(host, ["d1"])

    assert host.created == []
    assert host.deleted == []
    assert host.saves == []


def test_a_second_caller_with_the_same_demand_creates_no_duplicate() -> None:
    """The first pass creates the instance, the second (a concurrent caller, after it) finds it by namespace."""
    first = VrfHost(response(_leaf(rows=[raw_row("r1", 100, "PROD")])))
    _reconcile(first, ["d1"])
    second = VrfHost(response(_settled()))

    _ensure(second, {"d1": {"PROD"}})
    _reconcile(second, ["d1"])

    assert len(first.written("ManagedVRF")) == 1
    assert second.created == []


def test_removing_the_last_demand_deletes_the_svi_before_the_instance() -> None:
    """The row is gone: the SVI goes first, then the instance in the same pass (demand ignores the deleted SVI)."""
    host = VrfHost(
        response(
            _leaf(
                capabilities=[raw_instance("vrf-1", "leaf-1", "PROD")],
                interfaces=[raw_svi("svi-1", 100, "r-gone", "vrf-1", "PROD")],
            )
        )
    )

    _reconcile(host, ["d1"])

    assert host.deleted == [("DcimVirtualInterface", "svi-1"), ("ManagedVRF", "vrf-1")]
    assert host.created == []


def test_an_instance_with_demand_left_is_kept() -> None:
    """An address in the namespace on the device's own interface keeps the instance after its SVI is gone."""
    from tests.unit.vrf_instances_helpers import address, raw_interface

    host = VrfHost(
        response(
            _leaf(
                capabilities=[raw_instance("vrf-1", "leaf-1", "PROD")],
                interfaces=[
                    raw_svi("svi-1", 100, "r-gone", "vrf-1", "PROD"),
                    raw_interface("eth1", "Ethernet1.1900", addr=address("100.66.0.1/31", "PROD")),
                ],
            )
        )
    )

    _reconcile(host, ["d1"])

    assert host.deleted == [("DcimVirtualInterface", "svi-1")]


def test_ensure_never_deletes_and_creates_what_the_caller_is_about_to_need() -> None:
    """The extra demand (an address not written yet) gets its instance; an instance with no demand stays."""
    host = VrfHost(response(_leaf(capabilities=[raw_instance("vrf-2", "leaf-1", "NON-PROD")])))

    _ensure(host, {"d1": {"PROD", "default"}})

    assert [data["name"] for data in host.written("ManagedVRF")] == ["leaf-1-vrf-prod"]
    assert host.deleted == []


def test_a_device_that_carries_no_vrf_gets_nothing_even_with_extra() -> None:
    """A spine (or a party's router) is never given an instance, whatever the caller asks for."""
    host = VrfHost(response(raw_device("s1", "spine-1", role="spine")))

    _ensure(host, {"s1": {"PROD"}})

    assert host.created == []
    assert host.deleted == []


def test_a_row_moved_to_another_row_updates_the_svi_in_place() -> None:
    """Same name, another row: the SVI is written with its own id, not deleted and created."""
    host = VrfHost(
        response(
            _leaf(
                rows=[raw_row("r2", 100, "PROD")],
                capabilities=[raw_instance("vrf-1", "leaf-1", "PROD")],
                interfaces=[raw_svi("svi-1", 100, "r1", "vrf-1", "PROD")],
            )
        )
    )

    _reconcile(host, ["d1"])

    assert host.written("DcimVirtualInterface") == [
        {
            "id": "svi-1",
            "name": "Vlan100",
            "device": {"id": "d1"},
            "role": "svi",
            "status": "active",
            "vlan_domain_segment": {"id": "r2"},
            "vrf": {"id": "vrf-1"},
        }
    ]
    assert host.deleted == []


def test_an_svi_that_would_lose_its_vrf_is_created_again() -> None:
    """A gateway moved to ``default`` (the global table): a relationship is not cleared by a save, so delete + create."""
    host = VrfHost(
        response(
            _leaf(
                rows=[raw_row("r1", 100, "default")],
                capabilities=[raw_instance("vrf-1", "leaf-1", "PROD")],
                interfaces=[raw_svi("svi-1", 100, "r1", "vrf-1", "PROD")],
            )
        )
    )

    _reconcile(host, ["d1"])

    assert host.deleted[0] == ("DcimVirtualInterface", "svi-1")
    created = host.written("DcimVirtualInterface")
    assert len(created) == 1
    assert "vrf" not in created[0]
    assert "id" not in created[0]


def test_an_svi_of_a_row_that_is_no_longer_routed_is_deleted() -> None:
    """Another desired SVI stays; the one whose segment lost its gateway goes."""
    host = VrfHost(
        response(
            _leaf(
                rows=[raw_row("r1", 100, "PROD"), raw_row("r2", 200, None)],
                capabilities=[raw_instance("vrf-1", "leaf-1", "PROD")],
                interfaces=[raw_svi("svi-1", 100, "r1", "vrf-1", "PROD"), raw_svi("svi-2", 200, "r2", "vrf-1", "PROD")],
            )
        )
    )

    _reconcile(host, ["d1"])

    assert host.deleted == [("DcimVirtualInterface", "svi-2")]
    assert host.created == []


def test_plan_svis_is_empty_for_a_settled_device() -> None:
    """The pure planner: the settled device needs no write."""
    facts = facts_of(_settled())

    assert not plan_svis(facts, {"PROD": "vrf-1"})


def test_locks_are_taken_once_each_in_sorted_order_and_the_query_runs_by_path() -> None:
    """Sorted, deduplicated device ids: the lock keys and the $device_ids variable are the same list."""
    host = VrfHost(response())

    _reconcile(host, ["d2", "d1", "d2"])

    assert host.lock_keys == ["vrf-device-d1", "vrf-device-d2"]
    assert host.queries[0]["variables"] == {"device_ids": ["d1", "d2"]}
    assert "query vrf_instances" in host.queries[0]["query"]


def test_no_device_ids_runs_nothing() -> None:
    """Nothing to converge: no lock, no query."""
    host = VrfHost(response())

    _reconcile(host, [])

    assert host.lock_keys == []
    assert host.queries == []


def test_an_unknown_namespace_fails_before_any_write() -> None:
    """The instance is created by namespace id: a namespace the data does not define is an error."""
    host = VrfHost(response(_leaf(rows=[raw_row("r1", 100, "MISSING")])))

    with pytest.raises(RuntimeError, match="MISSING"):
        _reconcile(host, ["d1"])

    assert host.created == []


def test_an_svi_name_taken_by_another_role_is_skipped_with_a_warning() -> None:
    """The MLAG control SVI Vlan4094 (or a hand-made VlanN) is never overwritten; the other SVIs still converge."""
    from tests.unit.vrf_instances_helpers import raw_interface

    host = VrfHost(
        response(
            _leaf(
                rows=[raw_row("r1", 4094, "PROD"), raw_row("r2", 200, "PROD")],
                capabilities=[raw_instance("vrf-1", "leaf-1", "PROD")],
                interfaces=[raw_interface("peer", "Vlan4094", role="peer-link")],
            )
        )
    )

    _reconcile(host, ["d1"])

    assert [data["name"] for data in host.written("DcimVirtualInterface")] == ["Vlan200"]
    assert host.deleted == []
    logger: Any = host.logger
    assert "Vlan4094" in logger.warning.call_args.args[0]


def test_an_svi_whose_row_cascaded_away_is_deleted_by_the_next_pass_with_its_instance() -> None:
    """A deleted segment: the row is gone, an SVI that survived the cascade has no row. Both go in one pass."""
    host = VrfHost(
        response(
            _leaf(
                capabilities=[raw_instance("vrf-1", "leaf-1", "PROD")],
                interfaces=[raw_svi("svi-1", 100, None, "vrf-1", "PROD")],
            )
        )
    )

    _reconcile(host, ["d1"])

    assert host.deleted == [("DcimVirtualInterface", "svi-1"), ("ManagedVRF", "vrf-1")]
