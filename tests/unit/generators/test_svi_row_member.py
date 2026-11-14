"""The segment SVI is a member of the VLAN row it realizes (the row's interface edge), never found by its name."""

from __future__ import annotations

import asyncio
from typing import Any

from generators.helpers.svi import plan_svis
from tests.unit.vrf_instances_helpers import facts_of, raw_device, raw_instance, raw_row, raw_svi
from tests.unit.vrf_reconcile_helpers import VrfHost, response
from utils.segment_vlan import svi_rows


def _leaf(**kwargs: Any) -> dict[str, Any]:
    return raw_device("d1", "leaf-1", **kwargs)


def _reconcile(host: VrfHost) -> None:
    asyncio.run(host.reconcile_vrfs(["d1"]))


def _edge_calls(host: VrfHost, mutation: str) -> list[dict[str, Any]]:
    """The ``data`` variable of every RelationshipAdd / RelationshipRemove the host issued."""
    return [q["variables"]["data"] for q in host.queries if mutation in q["query"]]


def _routed(**kwargs: Any) -> dict[str, Any]:
    return _leaf(rows=[raw_row("r1", 100, "PROD")], capabilities=[raw_instance("vrf-1", "leaf-1", "PROD")], **kwargs)


def test_svi_rows_reads_the_row_among_either_capability_key() -> None:
    """The rows of a cleaned SVI: the ManagedSegmentVlan of ``zone_capabilities`` and of ``capabilities``, once."""
    row = {"typename": "ManagedSegmentVlan", "id": "r1", "vlan_id": 100}
    vrf = {"typename": "ManagedVRF", "id": "v1"}
    assert svi_rows({"zone_capabilities": [vrf, row]}) == [row]
    assert svi_rows({"capabilities": [vrf, row], "zone_capabilities": [row]}) == [row]
    assert svi_rows({"capabilities": [vrf]}) == []
    assert svi_rows({}) == []


def test_the_row_of_an_svi_is_the_one_it_is_a_member_of_whatever_its_name() -> None:
    """A facts SVI named for another VLAN is still the SVI of its row; one in no row realizes nothing."""
    renamed = raw_svi("s1", 100, "seg", "vrf-1", "PROD", name="Vlan999")
    facts = facts_of(_routed(interfaces=[renamed]))
    (svi,) = facts.svis
    assert svi.row_ids == ("r1",)
    row = facts.svi_row(svi)
    assert row is not None and row.vlan_id == 100
    (loose,) = facts_of(_routed(interfaces=[raw_svi("s2", 100, "seg", row_id=None)])).svis
    assert loose.row_ids == () and facts.svi_row(loose) is None


def test_a_new_svi_joins_its_row_after_it_is_saved() -> None:
    """Created (in its VRF), then one RelationshipAdd of the row on its ``capabilities``."""
    host = VrfHost(response(_routed()))

    _reconcile(host)

    (created,) = host.written("DcimVirtualInterface")
    assert created["capabilities"] == [{"id": "vrf-1"}]
    adds = [data for data in _edge_calls(host, "RelationshipAdd") if data["name"] == "capabilities"]
    assert [data["nodes"] for data in adds] == [[{"id": "r1"}]]
    assert adds[0]["id"].startswith("new-")
    assert _edge_calls(host, "RelationshipRemove") == []


def test_a_settled_svi_writes_nothing_again() -> None:
    """Idempotent: the SVI is already a member of its row, so the second run issues no edge write."""
    host = VrfHost(response(_routed(interfaces=[raw_svi("svi-1", 100, "seg", "vrf-1", "PROD")])))

    _reconcile(host)

    assert host.created == [] and host.deleted == []
    assert _edge_calls(host, "RelationshipAdd") == [] and _edge_calls(host, "RelationshipRemove") == []


def test_an_svi_saved_in_place_joins_its_row_again() -> None:
    """A save replaces the SVI's edge set, so the row is added again after every write, also an update in place."""
    host = VrfHost(response(_routed(interfaces=[raw_svi("svi-1", 100, "old-gateway", "vrf-1", "PROD")])))

    _reconcile(host)

    assert [created["id"] for created in host.written("DcimVirtualInterface")] == ["svi-1"]
    assert [d["nodes"] for d in _edge_calls(host, "RelationshipAdd")] == [[{"id": "r1"}]]


def test_an_svi_that_is_also_a_member_of_another_row_leaves_it_edge_by_edge() -> None:
    """The SVI realizes r1; its membership of r9 is removed with one RelationshipRemove, the SVI itself stays."""
    svi = raw_svi("svi-1", 100, "seg", "vrf-1", "PROD", capabilities=[{"__typename": "ManagedSegmentVlan", "id": "r9"}])
    host = VrfHost(response(_routed(interfaces=[svi])))

    _reconcile(host)

    assert host.created == [] and host.deleted == []
    (removed,) = _edge_calls(host, "RelationshipRemove")
    assert removed["id"] == "svi-1" and removed["name"] == "capabilities" and removed["nodes"] == [{"id": "r9"}]


def test_an_svi_in_no_row_is_deleted_before_the_one_that_takes_its_name() -> None:
    """Named Vlan100 but a member of no row: it realizes nothing, and the name is unique on the device, so it goes
    first and the SVI of the row is created (and joins it)."""
    host = VrfHost(response(_routed(interfaces=[raw_svi("svi-1", 100, "seg", "vrf-1", "PROD", row_id=None)])))

    _reconcile(host)

    assert host.deleted == [("DcimVirtualInterface", "svi-1")]
    assert len(host.written("DcimVirtualInterface")) == 1
    assert [d["nodes"] for d in _edge_calls(host, "RelationshipAdd")] == [[{"id": "r1"}]]


def test_the_svi_of_another_switch_s_row_is_never_this_device_s() -> None:
    """A member of a row that is not one of this device's rows realizes none of its rows: it is replaced, not adopted."""
    host = VrfHost(response(_routed(interfaces=[raw_svi("svi-1", 100, "seg", "vrf-1", "PROD", row_id="r-other")])))

    _reconcile(host)

    assert host.deleted == [("DcimVirtualInterface", "svi-1")]
    assert [d["nodes"] for d in _edge_calls(host, "RelationshipAdd")] == [[{"id": "r1"}]]


def test_plan_pairs_each_row_with_one_svi_and_the_second_is_stale() -> None:
    """Two SVIs in one row: the first (by id) realizes it, the other is stale and goes."""
    facts = facts_of(
        _routed(
            interfaces=[
                raw_svi("svi-a", 100, "seg", "vrf-1", "PROD"),
                raw_svi("svi-b", 100, "seg", "vrf-1", "PROD", name="Vlan101"),
            ]
        )
    )

    plan = plan_svis(facts, {"PROD": "vrf-1"})

    assert plan.stale_ids == ("svi-b",) and plan.writes == () and plan.recreate_ids == ()
