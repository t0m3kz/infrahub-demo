"""CheckVrfInstances over a decommissioned device, and the rules (c), (d) and (f) edge cases."""

from __future__ import annotations

from typing import Any, cast

from checks.vrf_instances import (
    CheckVrfInstances,
    empty_instance_warnings,
    membership_errors,
    namespace_errors,
    svi_content_errors,
    svi_domain_errors,
    svi_missing_errors,
    svi_orphan_warnings,
    svi_vrf_errors,
    uncarried_instance_errors,
    unneeded_instance_warnings,
)
from tests.unit.vrf_instances_helpers import (
    address,
    facts_of,
    raw_device,
    raw_instance,
    raw_interface,
    raw_response,
    raw_root_instance,
    raw_row,
    raw_svi,
)
from utils.data_cleaning import clean_data
from utils.vrf_demand import all_instances

ROW = raw_row("r1", 100, "PROD", segment="web")


def _old(**kwargs: Any) -> dict[str, Any]:
    """A decommissioned leaf that still has its row, its membership and its SVI."""
    defaults: dict[str, Any] = {
        "rows": [ROW],
        "vrfs": [raw_instance("v1", "old-1", "PROD")],
        "interfaces": [raw_svi("s1", 100, "r1", "v1", "PROD")],
        "status": "decommissioned",
    }
    return raw_device("d1", "old-1", **{**defaults, **kwargs})


def _run(*devices: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Run the check class on a raw response; return (errors, warnings)."""
    check = cast(Any, CheckVrfInstances.__new__(CheckVrfInstances))
    errors: list[str] = []
    warnings: list[str] = []
    check.log_error = lambda message: errors.append(message)
    check.log_warning = lambda message: warnings.append(message)
    check.validate(raw_response(*devices))
    return errors, warnings


def test_a_decommissioned_device_has_no_demand_and_no_desired_svi() -> None:
    """(a), (g), S1: nothing is demanded of a decommissioned device even though its rows still exist."""
    facts = facts_of(_old(vrfs=[], interfaces=[]))
    assert facts.demand == frozenset() and facts.desired_svis() == {}
    assert svi_missing_errors(facts) == []


def test_a_decommissioned_member_is_a_b_warning_and_not_an_e_error() -> None:
    """(b) reports the stale membership, (e) skips the device."""
    facts = facts_of(_old())
    warnings = unneeded_instance_warnings(facts)
    assert len(warnings) == 1 and "decommissioned device 'old-1' is still a member" in warnings[0]
    assert uncarried_instance_errors(facts) == []


def test_leftover_svis_of_a_decommissioned_device_are_warnings_never_errors() -> None:
    """S2 warns for every leftover SVI; S3, S4 and S5 stay silent."""
    foreign = raw_svi("s2", 200, "r-elsewhere", "foreign", "PROD", addr=address("10.0.0.1/24", "PROD"))
    facts = facts_of(_old(interfaces=[raw_svi("s1", 100, "r1", "v1", "PROD"), foreign]))
    warnings = svi_orphan_warnings(facts)
    assert len(warnings) == 2 and all("decommissioned" in message for message in warnings)
    assert svi_vrf_errors(facts) == [] and svi_content_errors(facts) == [] and svi_domain_errors(facts) == []


def test_the_check_reports_a_decommissioned_device_with_warnings_only() -> None:
    """End to end: no error, a (b) and an S2 warning; the same device in service stays clean."""
    errors, warnings = _run(_old())
    assert errors == []
    assert any("VRF (b)" in message for message in warnings) and any("SVI (S2)" in message for message in warnings)
    live = _old(status="active", vrfs=[raw_instance("v1", "old-1", "PROD")])
    assert _run(live) == ([], [])


def test_rule_c_a_membership_without_a_deployment_node_is_an_error() -> None:
    """(c) an instance with no deployment is not the device's hosting one."""
    orphan = raw_instance("v1", "leaf-1", "PROD", deployment_id=None)
    facts = facts_of(raw_device("d1", "leaf-1", vrfs=[orphan]))
    errors = membership_errors(facts)
    assert len(errors) == 1 and "another deployment" in errors[0]


def test_rule_c_a_membership_of_another_deployment_is_an_error_end_to_end() -> None:
    """(c) through the check class: a device.vrfs entry of dc-2 for a device hosted by dc-1."""
    elsewhere = raw_instance("v9", "leaf-1", "PROD", deployment_id="dc-2")
    errors, _ = _run(raw_device("d1", "leaf-1", rows=[ROW], vrfs=[elsewhere]))
    assert any("VRF (c)" in message for message in errors)


def test_rule_d_a_realizable_namespace_passes() -> None:
    """(d) an l3_vni, a namespace_type and a non-default name raise nothing."""
    assert namespace_errors(facts_of(raw_device("d1", "leaf-1", vrfs=[raw_instance("v1", "leaf-1", "PROD")]))) == []


def test_rule_f_an_instance_referenced_only_by_a_non_svi_virtual_interface_is_empty() -> None:
    """(f) needs only an empty ``devices``: a sub-interface pointing its ``vrf`` at it does not make a member."""
    instance = raw_instance("v2", "x", "NON-PROD", 200, "non_prod")
    sub = raw_interface("i9", "Ethernet1.200", "leaf", typename="DcimVirtualInterface")
    sub["vrf"] = {
        "node": {
            "id": "v2",
            "deployment": {"node": {"id": "dc-1"}},
            "namespace": {"node": {"name": {"value": "NON-PROD"}}},
        }
    }
    device = raw_device("d1", "leaf-1", interfaces=[sub])
    response = clean_data(raw_response(device, instances=(raw_root_instance(instance),)))
    warnings = empty_instance_warnings(all_instances(response))
    assert len(warnings) == 1 and "dc1-vrf-non-prod" in warnings[0]
