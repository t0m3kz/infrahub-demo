"""Unit tests for CheckVrfInstances: every rule passes and fails on raw-shape fixtures run through clean_data."""

from __future__ import annotations

from typing import Any, cast

from checks.vrf_instances import (
    CheckVrfInstances,
    empty_instance_warnings,
    membership_errors,
    missing_instance_errors,
    namespace_errors,
    no_hosting_errors,
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


def _leaf(**kwargs: Any) -> dict[str, Any]:
    """A leaf with one routed PROD row, its instance and its SVI unless overridden."""
    defaults: dict[str, Any] = {
        "rows": [ROW],
        "capabilities": [raw_instance("v1", "leaf-1", "PROD")],
        "interfaces": [raw_svi("s1", 100, "r1", "v1", "PROD")],
    }
    return raw_device("d1", "leaf-1", **{**defaults, **kwargs})


def test_clean_device_has_no_findings() -> None:
    """A consistent leaf passes every rule."""
    facts = facts_of(_leaf())
    for rule in (
        missing_instance_errors,
        unneeded_instance_warnings,
        no_hosting_errors,
        membership_errors,
        namespace_errors,
        uncarried_instance_errors,
        svi_missing_errors,
        svi_orphan_warnings,
        svi_vrf_errors,
        svi_content_errors,
        svi_domain_errors,
    ):
        assert rule(facts) == [], rule.__name__


def test_a_missing_instance_is_an_error_naming_the_reason() -> None:
    """(a) demand without an instance."""
    errors = missing_instance_errors(facts_of(_leaf(capabilities=[])))
    assert len(errors) == 1
    assert "leaf-1" in errors[0] and "'PROD'" in errors[0] and "routed segment row" in errors[0]


def test_an_address_in_a_namespace_demands_an_instance() -> None:
    """(a) the reason names the address."""
    iface = raw_interface("i1", addr=address("203.0.113.2/31", "INTERNET"))
    errors = missing_instance_errors(facts_of(raw_device("d1", "edge-1", role="edge", interfaces=[iface])))
    assert "an address" in errors[0]


def test_an_unneeded_instance_is_a_warning() -> None:
    """(b) an instance without demand."""
    extra = raw_instance("v2", "leaf-1", "NON-PROD", 200, "non_prod")
    warnings = unneeded_instance_warnings(facts_of(_leaf(capabilities=[raw_instance("v1", "leaf-1", "PROD"), extra])))
    assert len(warnings) == 1 and "NON-PROD" in warnings[0]


def test_unneeded_instance_on_a_non_carrying_device_is_left_to_rule_e() -> None:
    """(b) does not repeat (e)."""
    facts = facts_of(raw_device("d1", "spine-1", role="spine", capabilities=[raw_instance("v1", "spine-1", "PROD")]))
    assert unneeded_instance_warnings(facts) == []


def test_a_demand_without_a_membership_in_the_hosting_instance_is_an_error() -> None:
    """(a) a membership in the instance of another deployment does not satisfy the demand."""
    elsewhere = raw_instance("v1", "leaf-1", "PROD", deployment_id="dc-2")
    errors = missing_instance_errors(facts_of(_leaf(capabilities=[elsewhere])))
    assert len(errors) == 1 and "'DC1'" in errors[0]


def test_a_member_of_another_deployments_instance_is_an_error() -> None:
    """(c) a moved device or a hand edit: the instance belongs to another deployment."""
    elsewhere = raw_instance("v9", "leaf-1", "NON-PROD", 200, "non_prod", deployment_id="dc-2")
    errors = membership_errors(facts_of(_leaf(capabilities=[raw_instance("v1", "leaf-1", "PROD"), elsewhere])))
    assert len(errors) == 1 and "another deployment" in errors[0]


def test_a_member_of_two_instances_of_a_namespace_is_an_error() -> None:
    """(c) two instances of one namespace on a device."""
    twin = raw_instance("v9", "leaf-1", "PROD")
    errors = membership_errors(facts_of(_leaf(capabilities=[raw_instance("v1", "leaf-1", "PROD"), twin])))
    assert len(errors) == 1 and "2 ManagedVRF" in errors[0]


def test_demand_without_a_hosting_deployment_is_an_error_g() -> None:
    """(g) a leaf whose deployment is not under a DC, metro or office; (a) stays silent, there is nothing to name."""
    facts = facts_of(_leaf(capabilities=[], hosting_id=None))
    assert len(no_hosting_errors(facts)) == 1 and "VRF (g)" in no_hosting_errors(facts)[0]
    assert missing_instance_errors(facts) == []
    assert no_hosting_errors(facts_of(raw_device("d1", "spine-1", role="spine", hosting_id=None))) == []


def test_an_instance_with_no_member_is_a_warning() -> None:
    """(f) read from the ManagedVRF root, which lists members; a populated instance is silent."""
    empty = raw_root_instance(raw_instance("v2", "x", "NON-PROD", 200, "non_prod"))
    response = clean_data(raw_response(_leaf(), instances=(empty,)))
    warnings = empty_instance_warnings(all_instances(response))
    assert len(warnings) == 1 and "dc1-vrf-non-prod" in warnings[0]


def test_instance_namespace_without_l3_vni_type_or_default_is_an_error() -> None:
    """(d) an unrealizable namespace."""
    for vrf in (
        raw_instance("v1", "leaf-1", "PROD", l3_vni=None),
        raw_instance("v1", "leaf-1", "PROD", ns_type=None),
        raw_instance("v1", "leaf-1", "default"),
    ):
        assert len(namespace_errors(facts_of(_leaf(capabilities=[vrf])))) == 1


def test_an_instance_on_a_device_that_never_carries_one_is_an_error() -> None:
    """(e) spine, firewall, party device."""
    for device in (
        raw_device("d1", "spine-1", role="spine", capabilities=[raw_instance("v1", "spine-1", "PROD")]),
        raw_device("d2", "fw-1", role="firewall", capabilities=[raw_instance("v2", "fw-1", "PROD")]),
        raw_device(
            "d3",
            "pe-1",
            role="edge",
            deployment_kind="TopologyExternalProviders",
            capabilities=[raw_instance("v3", "pe-1", "PROD")],
        ),
    ):
        assert len(uncarried_instance_errors(facts_of(device))) == 1
    assert uncarried_instance_errors(facts_of(_leaf())) == []


def test_s1_a_routed_row_without_exactly_one_svi_is_an_error() -> None:
    """S1 missing, wrong name, linked to another row, and duplicated."""
    assert len(svi_missing_errors(facts_of(_leaf(interfaces=[])))) == 1
    assert (
        len(svi_missing_errors(facts_of(_leaf(interfaces=[raw_svi("s1", 100, "r1", "v1", "PROD", name="Vlan101")]))))
        == 1
    )
    assert len(svi_missing_errors(facts_of(_leaf(interfaces=[raw_svi("s1", 100, "other", "v1", "PROD")])))) == 1
    twice = [raw_svi("s1", 100, "r1", "v1", "PROD"), raw_svi("s2", 100, "r1", "v1", "PROD")]
    assert len(svi_missing_errors(facts_of(_leaf(interfaces=twice)))) == 1


def test_s1_ignores_an_unrouted_row() -> None:
    """S1 asks for no SVI on a pure-L2 row."""
    facts = facts_of(_leaf(rows=[raw_row("r2", 200, None)], capabilities=[], interfaces=[]))
    assert svi_missing_errors(facts) == []


def test_s2_svi_without_row_or_on_unrouted_row_is_a_warning() -> None:
    """S2 orphan SVIs."""
    no_row = facts_of(
        _leaf(interfaces=[raw_svi("s1", 100, "r1", "v1", "PROD"), raw_svi("s2", 300, None, "v1", "PROD")])
    )
    assert len(svi_orphan_warnings(no_row)) == 1
    unrouted = facts_of(_leaf(rows=[raw_row("r2", 200, None)], interfaces=[raw_svi("s2", 200, "r2")]))
    assert len(svi_orphan_warnings(unrouted)) == 1


def test_s3_vrf_must_be_the_hosting_instance_of_the_rows_namespace() -> None:
    """S3 unset, not a member, wrong namespace, other deployment; default rows may leave it unset."""
    assert len(svi_vrf_errors(facts_of(_leaf(interfaces=[raw_svi("s1", 100, "r1")])))) == 1
    assert len(svi_vrf_errors(facts_of(_leaf(interfaces=[raw_svi("s1", 100, "r1", "foreign", "PROD")])))) == 1
    wrong = raw_instance("v2", "leaf-1", "NON-PROD", 200, "non_prod")
    leaf = _leaf(capabilities=[wrong], interfaces=[raw_svi("s1", 100, "r1", "v2", "NON-PROD")])
    assert len(svi_vrf_errors(facts_of(leaf))) == 1
    other = raw_svi("s1", 100, "r1", "v5", "PROD", vrf_deployment_id="dc-2")
    assert len(svi_vrf_errors(facts_of(_leaf(interfaces=[other])))) == 1
    default = _leaf(rows=[raw_row("r1", 100, "default")], capabilities=[], interfaces=[raw_svi("s1", 100, "r1")])
    assert svi_vrf_errors(facts_of(default)) == []


def test_s4_svi_with_address_or_capabilities_is_an_error() -> None:
    """S4 an SVI carries neither."""
    with_address = raw_svi("s1", 100, "r1", "v1", "PROD", addr=address("10.0.0.1/24", "PROD"))
    with_caps = raw_svi("s1", 100, "r1", "v1", "PROD", capabilities=[{"__typename": "ManagedVxlanSegment", "id": "c"}])
    assert len(svi_content_errors(facts_of(_leaf(interfaces=[with_address])))) == 1
    assert len(svi_content_errors(facts_of(_leaf(interfaces=[with_caps])))) == 1


def test_s5_svi_row_must_be_in_the_devices_domain() -> None:
    """S5 an SVI linked to a row of another device (a peer's row included) is an error; its own row is fine."""
    foreign = raw_svi("s1", 100, "r-elsewhere", "v1", "PROD")
    errors = svi_domain_errors(facts_of(_leaf(interfaces=[foreign])))
    assert len(errors) == 1
    assert "row of another device" in errors[0]
    assert svi_domain_errors(facts_of(_leaf(interfaces=[raw_svi("s1", 100, "r1", "v1", "PROD")]))) == []


def _run(*devices: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Run the check class on a raw response; return (errors, warnings)."""
    check = cast(Any, CheckVrfInstances.__new__(CheckVrfInstances))
    errors: list[str] = []
    warnings: list[str] = []
    check.log_error = lambda message: errors.append(message)
    check.log_warning = lambda message: warnings.append(message)
    check.validate(raw_response(*devices))
    return errors, warnings


def test_check_logs_nothing_for_a_clean_fabric() -> None:
    """End to end: a consistent device and an unrelated spine."""
    assert _run(_leaf(), raw_device("d9", "spine-1", role="spine")) == ([], [])


def test_check_logs_errors_and_warnings_end_to_end() -> None:
    """End to end: a missing instance and SVI are errors, an orphan SVI a warning, a shared instance an error."""
    broken = _leaf(capabilities=[], interfaces=[raw_svi("s9", 900, None)])
    errors, warnings = _run(broken)
    assert any("(a)" in message for message in errors) and any("S1" in message for message in errors)
    assert len(warnings) == 1 and "S2" in warnings[0]
    elsewhere = raw_instance("v1", "leaf-1", "PROD", deployment_id="dc-2")
    errors, _ = _run(_leaf(capabilities=[elsewhere], interfaces=[]), raw_device("d2", "leaf-2", hosting_id=None))
    assert any("VRF (c)" in message for message in errors)
    _, warnings = _run(_leaf(), raw_device("d9", "spine-1", role="spine"))
    assert warnings == []
