"""Unit tests for CheckMlagDomains, fed canned mlag_domains responses in the raw GraphQL shape."""

from __future__ import annotations

from typing import Any, cast

from checks.mlag_domains import CheckMlagDomains, device_domain_errors, member_count_errors, peer_devices_errors
from utils.data_cleaning import clean_data
from utils.vlan_domain import mlag_peer_devices


def _domain(name: str, *devices: str, peer_devices: str | None = None) -> dict[str, Any]:
    """One ManagedMLAG edge whose members are the given device names (id is 'id-<name>').

    `peer_devices` defaults to the value the generator writes for these members.
    """
    members = [{"node": {"id": f"id-{device}", "name": {"value": device}}} for device in devices]
    return {
        "node": {
            "id": f"id-{name}",
            "name": {"value": name},
            "peer_devices": {"value": mlag_peer_devices(devices) if peer_devices is None else peer_devices},
            "capabilities": {"edges": members},
        }
    }


def _raw(*domains: dict[str, Any]) -> dict[str, Any]:
    """The raw query response."""
    return {"ManagedMLAG": {"edges": list(domains)}}


def _cleaned(*domains: dict[str, Any]) -> list[dict[str, Any]]:
    """The domains as the check sees them, after clean_data."""
    return clean_data(_raw(*domains))["ManagedMLAG"]


def _run(*domains: dict[str, Any]) -> list[str]:
    """Run the check against canned data; return the logged errors."""
    check = cast(Any, CheckMlagDomains.__new__(CheckMlagDomains))
    errors: list[str] = []
    check.log_error = lambda message: errors.append(message)
    check.validate(_raw(*domains))
    return errors


def test_m1_passes_when_each_device_is_in_one_domain() -> None:
    """Two disjoint pairs raise no M1 error."""
    assert device_domain_errors(_cleaned(_domain("a-b-mlag", "a", "b"), _domain("c-d-mlag", "c", "d"))) == []


def test_m1_fails_when_a_device_is_in_two_domains() -> None:
    """A device in two domains is reported with both domain names."""
    errors = device_domain_errors(_cleaned(_domain("a-b-mlag", "a", "b"), _domain("a-c-mlag", "a", "c")))
    assert len(errors) == 1
    assert "'a'" in errors[0]
    assert "a-b-mlag" in errors[0]
    assert "a-c-mlag" in errors[0]


def test_m2_passes_with_two_members() -> None:
    """A domain of two distinct devices raises no M2 error."""
    assert member_count_errors(_cleaned(_domain("a-b-mlag", "a", "b"))) == []


def test_m2_fails_with_one_member() -> None:
    """A one-member domain (accepted on create by the server) is reported."""
    errors = member_count_errors(_cleaned(_domain("a-mlag", "a")))
    assert len(errors) == 1
    assert "'a-mlag'" in errors[0]
    assert "1 distinct" in errors[0]


def test_m2_fails_with_no_members() -> None:
    """A domain with no member is reported with a count of 0."""
    errors = member_count_errors(_cleaned(_domain("empty-mlag")))
    assert len(errors) == 1
    assert "0 distinct" in errors[0]


def test_m2_fails_when_the_same_device_is_listed_twice() -> None:
    """Two edges to one device count as one distinct member."""
    errors = member_count_errors(_cleaned(_domain("a-a-mlag", "a", "a")))
    assert len(errors) == 1
    assert "1 distinct" in errors[0]


def test_check_passes_on_valid_domains() -> None:
    """The check logs nothing for well formed domains, and for none at all."""
    assert _run(_domain("a-b-mlag", "a", "b")) == []
    assert _run() == []


def test_check_logs_m1_and_m2_errors() -> None:
    """The check logs one error per violated rule."""
    errors = _run(_domain("a-b-mlag", "a", "b"), _domain("a-mlag", "a"))
    assert len(errors) == 2
    assert any("2 MLAG domains" in error for error in errors)
    assert any("'a-mlag' has 1" in error for error in errors)


def test_mlag_peer_devices_is_sorted_and_comma_joined() -> None:
    """The helper sorts the names, so the order members are listed in does not matter."""
    assert mlag_peer_devices(["dc1-leaf-02", "dc1-leaf-01"]) == "dc1-leaf-01,dc1-leaf-02"
    assert mlag_peer_devices(iter(["b", "a"])) == "a,b"


def test_m3_passes_when_peer_devices_matches_the_members() -> None:
    """A domain whose peer_devices is the sorted member names raises no M3 error."""
    assert peer_devices_errors(_cleaned(_domain("a-b-mlag", "b", "a"))) == []


def test_m3_fails_after_a_device_rename() -> None:
    """A member renamed after the domain was written leaves a stale peer_devices, reported with both values."""
    stale = _domain("a-b-mlag", "a", "b-renamed", peer_devices="a,b")
    errors = peer_devices_errors(_cleaned(stale))
    assert len(errors) == 1
    assert "'a-b-mlag'" in errors[0]
    assert "'a,b'" in errors[0]
    assert "'a,b-renamed'" in errors[0]


def test_m3_fails_on_a_hand_edit() -> None:
    """A hand-edited peer_devices that is not the sorted member names is reported."""
    errors = peer_devices_errors(_cleaned(_domain("a-b-mlag", "a", "b", peer_devices="b,a")))
    assert len(errors) == 1
    assert "'b,a'" in errors[0]
    assert "'a,b'" in errors[0]
    assert "Re-run the pod or DC generator" in errors[0] and "domain_pairing" in errors[0]


def test_check_logs_m3_error() -> None:
    """The check logs the M3 error alone when only peer_devices is wrong."""
    errors = _run(_domain("a-b-mlag", "a", "b", peer_devices="a,c"))
    assert len(errors) == 1
    assert "peer_devices" in errors[0]
