"""Unit tests for CheckVrfInstances: every rule passes and fails on raw-shape fixtures run through clean_data."""

from __future__ import annotations

from typing import Any, cast

from checks.vrf_instances import (
    CheckVrfInstances,
    duplicate_instance_errors,
    missing_instance_errors,
    namespace_errors,
    shared_instance_errors,
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
    raw_row,
    raw_svi,
)

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
        duplicate_instance_errors,
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


def test_two_instances_of_a_namespace_on_a_device_is_an_error() -> None:
    """(c) duplicates on one device."""
    twin = raw_instance("v9", "leaf-1", "PROD")
    errors = duplicate_instance_errors(facts_of(_leaf(capabilities=[raw_instance("v1", "leaf-1", "PROD"), twin])))
    assert len(errors) == 1 and "2 ManagedVRF" in errors[0]


def test_an_instance_on_two_devices_is_an_error() -> None:
    """(c) the same instance id under two devices."""
    shared = raw_instance("v1", "leaf-1", "PROD")
    a, b = facts_of(_leaf(capabilities=[shared])), facts_of(raw_device("d2", "leaf-2", capabilities=[shared]))
    errors = shared_instance_errors([a, b])
    assert len(errors) == 1 and "leaf-1, leaf-2" in errors[0]
    assert shared_instance_errors([a, facts_of(raw_device("d2", "leaf-2"))]) == []


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


def test_s3_vrf_must_be_an_own_instance_in_the_rows_namespace() -> None:
    """S3 unset, foreign, and wrong namespace; default rows may leave it unset."""
    assert len(svi_vrf_errors(facts_of(_leaf(interfaces=[raw_svi("s1", 100, "r1")])))) == 1
    assert len(svi_vrf_errors(facts_of(_leaf(interfaces=[raw_svi("s1", 100, "r1", "foreign", "PROD")])))) == 1
    wrong = raw_instance("v2", "leaf-1", "NON-PROD", 200, "non_prod")
    leaf = _leaf(capabilities=[wrong], interfaces=[raw_svi("s1", 100, "r1", "v2", "NON-PROD")])
    assert len(svi_vrf_errors(facts_of(leaf))) == 1
    default = _leaf(rows=[raw_row("r1", 100, "default")], capabilities=[], interfaces=[raw_svi("s1", 100, "r1")])
    assert svi_vrf_errors(facts_of(default)) == []


def test_s4_svi_with_address_or_capabilities_is_an_error() -> None:
    """S4 an SVI carries neither."""
    with_address = raw_svi("s1", 100, "r1", "v1", "PROD", addr=address("10.0.0.1/24", "PROD"))
    with_caps = raw_svi("s1", 100, "r1", "v1", "PROD", capabilities=[{"__typename": "ManagedVxlanSegment", "id": "c"}])
    assert len(svi_content_errors(facts_of(_leaf(interfaces=[with_address])))) == 1
    assert len(svi_content_errors(facts_of(_leaf(interfaces=[with_caps])))) == 1


def test_s5_svi_row_must_be_in_the_devices_domain() -> None:
    """S5 a row of another domain."""
    foreign = raw_svi("s1", 100, "r-elsewhere", "v1", "PROD")
    assert len(svi_domain_errors(facts_of(_leaf(interfaces=[foreign])))) == 1


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
    shared = raw_instance("v1", "leaf-1", "PROD")
    errors, _ = _run(_leaf(capabilities=[shared]), raw_device("d2", "leaf-2", role="spine", capabilities=[shared]))
    assert any("linked to 2 devices" in message for message in errors)
