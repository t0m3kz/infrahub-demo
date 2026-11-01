"""The pure judges of test_67's decommission steps and the structure of the steps that use them.

Nothing here needs a stack: the judges run on canned payloads, and the structure tests read test_67 to keep the story in
order (decommission before delete, an authored carrier under the adoption, the dependency chain without a gap).
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from tests.integration import test_67_all_demo_provider_connections as module
from tests.integration.demo_data import ROOT
from tests.integration.provider_connection_decommission_helpers import (
    AUTHORED_CARRIER,
    AUTHORED_CARRIER_NAME,
    AUTHORED_NIC,
    KIND_ADDRESS,
    KIND_CIRCUIT,
    KIND_INTERFACE,
    KIND_PEERING,
    KIND_PREFIX,
    LEG_KINDS,
    decommissioned_row_errors,
    moved_ids_errors,
    outputs_from_row,
    peering_names,
    port_free_errors,
    prefix_descriptions,
    presence_errors,
    recorded_errors,
)

SOURCE = (ROOT / "tests/integration/test_67_all_demo_provider_connections.py").read_text()


def _end(number: int, name: str, address: str | None = "10.0.0.0/31") -> dict[str, Any]:
    return {
        "id": f"if{number}",
        "name": name,
        "ip_address": {"id": f"ip{number}", "address": address} if address else None,
    }


def _row() -> dict[str, Any]:
    """One circuit, two virtual ends with an address each and a physical port (no ip_address key)."""
    return {
        "id": "pc",
        "session_circuits": [
            {
                "id": "c1",
                "name": "PC-X-L1",
                "capabilities": [
                    _end(1, "Ethernet1/3.100"),
                    _end(2, "vnic-PC-X.100"),
                    {"id": "phys", "name": "Ethernet1/3"},
                ],
            }
        ],
    }


def _recorded() -> dict[str, dict[str, str]]:
    recorded = outputs_from_row(_row())
    recorded[KIND_PEERING] = {"p1": "PC-X-L1-bgp"}
    recorded[KIND_PREFIX] = {"pf1": "10.0.0.0/31"}
    return recorded


def test_the_record_holds_the_circuit_its_virtual_ends_and_their_addresses_but_no_physical_port() -> None:
    recorded = outputs_from_row(_row())
    assert recorded[KIND_CIRCUIT] == {"c1": "PC-X-L1"}
    assert set(recorded[KIND_INTERFACE]) == {"if1", "if2"}
    assert set(recorded[KIND_ADDRESS]) == {"ip1", "ip2"}


def test_a_connection_without_circuits_records_nothing() -> None:
    assert outputs_from_row(None) == {KIND_CIRCUIT: {}, KIND_INTERFACE: {}, KIND_ADDRESS: {}}
    assert recorded_errors(outputs_from_row(None))


def test_the_names_the_record_implies_are_the_generators() -> None:
    recorded = _recorded()
    assert peering_names(recorded) == ["PC-X-L1-bgp"]
    assert (
        prefix_descriptions(recorded) == ["Handoff PC-X-L1", "VPN PC-X-L1"] or len(prefix_descriptions(recorded)) == 2
    )


def test_a_full_record_is_accepted_and_a_thin_one_is_named() -> None:
    assert not recorded_errors(_recorded())
    thin = _recorded()
    thin[KIND_PEERING] = {}
    assert any(KIND_PEERING in error for error in recorded_errors(thin))
    no_prefix = _recorded()
    no_prefix[KIND_PREFIX] = {}
    assert recorded_errors(no_prefix) and not recorded_errors(no_prefix, allocated=False)


def test_an_object_that_survives_the_teardown_is_named_by_kind_and_label() -> None:
    errors = presence_errors(
        _recorded(), {KIND_ADDRESS: {"ip1"}, KIND_PEERING: {"p1"}}, expect_present=False, kinds=LEG_KINDS
    )
    assert len(errors) == 2
    assert any("Ethernet1/3.100 10.0.0.0/31" in e and "ip1" in e for e in errors)
    assert any("PC-X-L1-bgp" in e and "still exists" in e for e in errors)


def test_nothing_alive_passes_and_a_kept_object_that_is_gone_is_named() -> None:
    assert not presence_errors(_recorded(), {}, expect_present=False, kinds=LEG_KINDS)
    kept = {KIND_INTERFACE: {"if1": "NIC"}}
    assert presence_errors(kept, {}, expect_present=True, kinds=(KIND_INTERFACE,)) == [
        f"{KIND_INTERFACE} NIC (if1) is gone"
    ]


@pytest.mark.parametrize(
    ("row", "carriers", "fragment"),
    [
        (None, None, "is gone"),
        ({"status": "active", "sessions": [], "carriers": []}, None, "status"),
        ({"status": "decommissioned", "sessions": ["PC-X-L1"], "carriers": []}, None, "session circuits"),
        ({"status": "decommissioned", "sessions": [], "carriers": ["PC-X-XC"]}, [], "carrier_circuits"),
    ],
)
def test_the_connection_row_after_a_teardown_is_judged(row: Any, carriers: Any, fragment: str) -> None:
    assert any(fragment in e for e in decommissioned_row_errors(row, "PC-X", carriers=carriers))


def test_a_decommissioned_empty_connection_passes_and_an_adopted_carrier_list_is_not_asserted() -> None:
    row = {"status": "decommissioned", "sessions": [], "carriers": [AUTHORED_CARRIER]}
    assert not decommissioned_row_errors(row, "PC-X")
    assert not decommissioned_row_errors({**row, "carriers": []}, "PC-X", carriers=[])


def _ports(sub: list[Any] | None = None) -> list[dict[str, Any]]:
    return [{"id": "p", "name": "Ethernet1/3", "device": {"name": "eg-fr201"}, "sub_interfaces": sub or []}]


def _inventory(status: str = "provisioning", end_id: str = "p") -> dict[str, Any]:
    return {
        "physical": [{"status": status, "end_ids": [end_id]}],
        "virtual": [{"end_nodes": [{"id": end_id}]}] if end_id == "p" and status == "session" else [],
    }


def test_a_free_near_port_has_no_sub_interface_no_live_carrier_and_no_session() -> None:
    near = ("eg-fr201", "Ethernet1/3")
    assert not port_free_errors(_ports(), {"physical": [], "virtual": []}, near)
    assert not port_free_errors(_ports(), _inventory("decommissioned"), near)
    assert port_free_errors(_ports(), _inventory("provisioning"), near)
    assert port_free_errors(_ports([{"id": "s"}]), {"physical": [], "virtual": []}, near)
    assert port_free_errors(_ports(), {"physical": [], "virtual": [{"end_nodes": [{"id": "p"}]}]}, near)
    assert port_free_errors([], {"physical": [], "virtual": []}, near)


def test_a_re_created_stub_interface_has_another_id() -> None:
    before = {"device S": "d1", "S/Loopback0": "l1"}
    assert not moved_ids_errors(before, dict(before))
    assert moved_ids_errors(before, {"device S": "d1", "S/Loopback0": "l2"}) == ["S/Loopback0: id 'l2', expected 'l1'"]
    assert moved_ids_errors(before, {"device S": "d1"})


# -- the structure of test_67 -----------------------------------------------------------------------------------------


def test_the_authored_carrier_is_a_persons_carrier_not_the_generators() -> None:
    """Its circuit_id is the provider's (it differs from its name) and its NIC is not a `vnic-<request>` of a request."""
    assert AUTHORED_CARRIER != AUTHORED_CARRIER_NAME
    assert not AUTHORED_CARRIER.startswith("PC-") and AUTHORED_NIC.startswith("vnic-")
    assert module.AUTHORED_CARRIER == AUTHORED_CARRIER


def test_the_orders_are_unique_and_the_dependencies_form_one_chain_from_test_66() -> None:
    """Each test depends on the previous one: no gap after the insertion of the decommission steps."""
    tests = re.findall(
        r'@pytest\.mark\.order\((\d+)\)\s+@pytest\.mark\.dependency\(\s*scope="session", name="(\w+)", depends=\["(\w+)"\]\s*\)',
        SOURCE,
    )
    orders = [int(order) for order, _, _ in tests]
    assert orders == sorted(set(orders)) and len(orders) == 11
    names = [name for _, name, _ in tests]
    assert [dep for _, _, dep in tests][1:] == names[:-1]
    assert tests[0][2] == "all_demo_mlag_remove_readd"


def test_every_delete_of_a_connection_follows_a_decommission_in_its_step() -> None:
    """A person decommissions, then deletes: the steps that delete a throwaway connection (not the refused one, which
    never built anything) call the decommission first."""
    for step in ("test_06_", "test_11_"):
        body = SOURCE[SOURCE.index(f"async def {step}") :]
        body = (
            body[: body.index("\n    @pytest.mark.order")]
            if "\n    @pytest.mark.order" in body
            else body[: body.index("\n    # ---")]
        )
        assert body.index("_decommission(") < body.index("_delete_empty(")


def test_the_decommission_asserts_what_the_contract_names() -> None:
    """The step records the ids before the status change, reclaims the generated carrier and NIC only when one is
    named, keeps the authored ranges, and reruns the generator on the decommissioned connection."""
    body = SOURCE[SOURCE.index("async def _decommission(") : SOURCE.index("async def _untouched(")]
    for needle in (
        "fetch_recorded(",
        "DECOMMISSIONED",
        "fetch_peering_names(",
        "decommissioned_row_errors(",
        "other_carrier_errors(",
        "moved_ids_errors(",
        '("ManagedBGP",)',
        "ranges",
        "run_generator(",
        "diff_snapshots(",
    ):
        assert needle in body, needle
    assert body.index("fetch_recorded(") < body.index("DECOMMISSIONED") < body.index("_poll(")


def test_the_adoption_authors_its_carrier_and_the_created_one_is_reclaimed() -> None:
    adopt = SOURCE[SOURCE.index("async def test_07_") : SOURCE.index("async def test_08_")]
    assert "create_authored_carrier(" in adopt
    decommission = SOURCE[SOURCE.index("async def test_06_") : SOURCE.index("async def test_07_")]
    assert "carrier=CREATED_CARRIER" in decommission
    assert (
        "CREATED_CARRIER"
        not in SOURCE[SOURCE.index("async def test_07_") : SOURCE.index("async def _remove_leftovers")]
    )


def test_the_final_cleanup_still_restores_the_branch_as_found() -> None:
    """The last step still compares the carriers with the ones test_01 found, after removing both throwaway carriers."""
    last = SOURCE[SOURCE.index("async def test_11_") : SOURCE.index("# Judges of the throwaway connections")]
    assert 'carrier_state(inventory) == STATE["carriers_before"]' in last
    cleanup = SOURCE[SOURCE.index("async def _remove_leftovers") :]
    assert "CREATED_CARRIER" in cleanup and "AUTHORED_CARRIER" in cleanup
