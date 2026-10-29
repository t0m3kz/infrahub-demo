"""Unit tests for generators/helpers/vrf.py and svi.py: the one writer of VRF instances and segment SVIs."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from generators.helpers.svi import plan_svis
from tests.unit.vrf_instances_helpers import (
    facts_of,
    raw_device,
    raw_instance,
    raw_root_instance,
    raw_row,
    raw_svi,
)
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


def _reconcile(host: VrfHost, ids: list[str]) -> None:
    asyncio.run(host.reconcile_vrfs(ids))


def test_a_routed_row_creates_the_instance_and_its_svi() -> None:
    """The instance first (untracked, under its Parent), then its membership as an edge, then the SVI."""
    host = VrfHost(response(_leaf(rows=[raw_row("r1", 100, "PROD")])))

    _reconcile(host, ["d1"])

    assert host.written("ManagedVRF") == [
        {"name": "dc1-vrf-prod", "deployment": {"id": "dc-1"}, "namespace": {"id": "ns-PROD"}, "status": "active"}
    ]
    assert host.edge_ops == [("add", "new-1", "devices", ["d1"])]
    assert host.lock_keys == ["vrf-device-d1", "vrf-dc-1-PROD"]
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
    assert host.edge_ops == []


def test_a_steady_state_run_takes_no_instance_lock() -> None:
    """Only the keys a pass changes are locked: the settled device takes its own lock and nothing else."""
    host = VrfHost(response(_settled()))

    _reconcile(host, ["d1"])

    assert host.lock_keys == ["vrf-device-d1"]


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
    assert host.edge_ops == [("remove", "vrf-1", "devices", ["d1"])]
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

    assert [data["name"] for data in host.written("ManagedVRF")] == ["dc1-vrf-prod"]
    assert host.edge_ops == [("add", "new-1", "devices", ["d1"])]
    assert host.deleted == []
    assert host.store.vrfs["vrf-2"].members == {"d1"}


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
    assert host.queries[0]["variables"] == {"device_ids": ["d1", "d2"], "with_instances": False}
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


def test_the_membership_is_an_edge_add_never_a_save_of_the_list() -> None:
    """The instance is created without ``devices``; the device joins through the one-edge mutation."""
    host = VrfHost(response(_leaf(rows=[raw_row("r1", 100, "PROD")])))

    _reconcile(host, ["d1"])

    assert all("devices" not in data for data in host.written("ManagedVRF"))
    assert host.edge_ops == [("add", "new-1", "devices", ["d1"])]


def test_a_second_member_joins_the_existing_instance_without_a_create() -> None:
    """Another device already made the topology's instance: found by (deployment, namespace), only an edge is added."""
    other = raw_root_instance(raw_instance("vrf-1", "x", "PROD"), "d2")
    host = VrfHost(response(_leaf(rows=[raw_row("r1", 100, "PROD")]), instances=(other,)))

    _reconcile(host, ["d1"])

    assert host.written("ManagedVRF") == []
    assert host.edge_ops == [("add", "vrf-1", "devices", ["d1"])]
    assert host.store.vrfs["vrf-1"].members == {"d1", "d2"}
    assert host.written("DcimVirtualInterface")[0]["vrf"] == {"id": "vrf-1"}
    assert host.lock_keys == ["vrf-device-d1", "vrf-dc-1-PROD"]


def test_the_instance_is_looked_up_by_deployment_and_namespace_with_its_members() -> None:
    """One filtered read under the lock, with the members included."""
    host = VrfHost(response(_leaf(rows=[raw_row("r1", 100, "PROD")])))

    _reconcile(host, ["d1"])

    first = host.client.filters.await_args_list[0]
    assert first.kwargs["deployment__ids"] == ["dc-1"]
    assert first.kwargs["namespace__ids"] == ["ns-PROD"]
    assert first.kwargs["include"] == ["devices"]


def test_a_member_without_demand_leaves_and_the_instance_stays_for_the_others() -> None:
    """Prune of one member removes its edge only; the other member keeps the instance."""
    shared = raw_instance("vrf-1", "x", "PROD")
    host = VrfHost(response(_leaf(vrfs=[shared]), instances=(raw_root_instance(shared, "d1", "d2"),)))

    _reconcile(host, ["d1"])

    assert host.edge_ops == [("remove", "vrf-1", "devices", ["d1"])]
    assert host.deleted == []
    assert host.store.vrfs["vrf-1"].members == {"d2"}
    assert host.lock_keys == ["vrf-device-d1", "vrf-dc-1-PROD"]


def test_without_prune_a_member_without_demand_stays() -> None:
    """vrfs_held never removes a membership or an instance."""
    shared = raw_instance("vrf-1", "x", "PROD")
    host = VrfHost(response(_leaf(vrfs=[shared])))

    _ensure(host, {"d1": set()})

    assert host.edge_ops == []
    assert host.deleted == []


def test_a_moved_device_leaves_the_old_topology_and_joins_the_new_one() -> None:
    """Its hosting DC is now dc-2: the PROD instance of dc-2 is made and joined, the one of dc-1 (now empty) goes."""
    old = raw_instance("vrf-old", "x", "PROD")
    host = VrfHost(
        response(
            _leaf(
                hosting_id="dc-2",
                rows=[raw_row("r1", 100, "PROD")],
                vrfs=[old],
                interfaces=[raw_svi("svi-1", 100, "r1", "vrf-old", "PROD")],
            )
        )
    )

    _reconcile(host, ["d1"])

    assert [data["deployment"] for data in host.written("ManagedVRF")] == [{"id": "dc-2"}]
    assert host.edge_ops == [("add", "new-1", "devices", ["d1"]), ("remove", "vrf-old", "devices", ["d1"])]
    assert host.written("DcimVirtualInterface")[0]["id"] == "svi-1"
    assert host.written("DcimVirtualInterface")[0]["vrf"] == {"id": "new-1"}
    assert host.deleted == [("ManagedVRF", "vrf-old")]


def test_a_device_with_demand_and_no_hosting_deployment_writes_nothing_and_warns() -> None:
    """Rule g of the check reports it; the generator has no deployment to put an instance under."""
    host = VrfHost(response(_leaf(hosting_id=None, rows=[raw_row("r1", 100, "PROD")])))

    _reconcile(host, ["d1"])

    assert host.created == []
    assert host.edge_ops == []
    assert host.deleted == []
    assert host.lock_keys == ["vrf-device-d1"]
    logger: Any = host.logger
    assert "no hosting deployment" in logger.warning.call_args.args[0]


def test_an_instance_still_used_by_an_svi_is_not_deleted() -> None:
    """The backstop of the delete: no member but an SVI that points at it keeps the instance."""
    empty = raw_instance("vrf-1", "x", "PROD")
    host = VrfHost(response(instances=(raw_root_instance(empty),)))
    host.store.svi_refs["vrf-1"] = {"svi-x"}

    asyncio.run(host.prune_empty_vrfs(["dc-1"]))

    assert host.deleted == []


def test_the_sweep_deletes_the_empty_instances_of_the_deployments_it_touched() -> None:
    """A reconcile also removes an empty instance of its hosting DC (under its lock), not another DC's."""
    here = raw_root_instance(raw_instance("vrf-empty", "x", "NON-PROD"))
    elsewhere = raw_root_instance(raw_instance("vrf-far", "x", "PROD", deployment_id="dc-9"))
    host = VrfHost(response(_settled(), instances=(here, elsewhere)))

    _reconcile(host, ["d1"])

    assert host.deleted == [("ManagedVRF", "vrf-empty")]
    assert host.lock_keys == ["vrf-device-d1", "vrf-dc-1-NON-PROD"]


def test_vrfs_held_does_not_sweep() -> None:
    """Holding never deletes, an empty instance included."""
    empty = raw_root_instance(raw_instance("vrf-empty", "x", "NON-PROD"))
    host = VrfHost(response(_settled(), instances=(empty,)))

    _ensure(host, {"d1": {"PROD"}})

    assert host.deleted == []


def test_prune_empty_vrfs_without_deployments_reads_nothing() -> None:
    """No deployment: no read."""
    host = VrfHost(response())

    asyncio.run(host.prune_empty_vrfs([]))

    host.client.filters.assert_not_awaited()


def test_a_decommissioned_device_loses_its_svi_and_membership_and_the_empty_instance_goes() -> None:
    """Its routed rows still exist, but a decommissioned device is a non-carrier: nothing is created, its SVI is
    deleted, it leaves the instance and the instance, left empty, is deleted."""
    host = VrfHost(
        response(
            _leaf(
                status="decommissioned",
                rows=[raw_row("r1", 100, "PROD")],
                capabilities=[raw_instance("vrf-1", "leaf-1", "PROD")],
                interfaces=[raw_svi("svi-1", 100, "r1", "vrf-1", "PROD")],
            )
        )
    )

    _reconcile(host, ["d1"])

    assert host.created == []
    assert host.edge_ops == [("remove", "vrf-1", "devices", ["d1"])]
    assert host.deleted == [("DcimVirtualInterface", "svi-1"), ("ManagedVRF", "vrf-1")]


def test_a_decommissioned_device_without_instances_gets_nothing() -> None:
    """The re-run of a decommission: the rows remain, no SVI and no membership are recreated."""
    host = VrfHost(response(_leaf(status="decommissioned", rows=[raw_row("r1", 100, "PROD")])))

    _reconcile(host, ["d1"])

    assert host.created == []
    assert host.edge_ops == []
    assert host.deleted == []


def test_the_generator_query_skips_the_instances_root() -> None:
    """The generator reads the device's own `vrfs`, so it passes `with_instances: false`."""
    host = VrfHost(response(_settled()))

    _reconcile(host, ["d1"])

    assert host.queries[0]["variables"] == {"device_ids": ["d1"], "with_instances": False}


def test_an_instance_kept_alive_by_a_non_svi_interface_is_never_deleted() -> None:
    """``_delete_if_unused`` asks for ANY DcimVirtualInterface with ``vrf`` = the instance, not only role svi: a
    loopback or sub-interface that has ``vrf`` set and no member keeps the empty instance forever. Only rule (f) of
    checks/vrf_instances.py reports it; the generator does not own such an interface, so it leaves it alone."""
    empty = raw_instance("vrf-1", "x", "PROD")
    host = VrfHost(response(instances=(raw_root_instance(empty),)))
    host.store.svi_refs["vrf-1"] = {"lo-1"}

    asyncio.run(host.prune_empty_vrfs(["dc-1"]))
    asyncio.run(host.prune_empty_vrfs(["dc-1"]))

    assert host.deleted == []
    assert "vrf-1" in host.store.vrfs
