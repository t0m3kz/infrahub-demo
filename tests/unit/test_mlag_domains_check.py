"""Unit tests for CheckMlagDomains, fed canned mlag_domains responses in the raw GraphQL shape."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from checks.mlag_domains import (
    CheckMlagDomains,
    device_domain_errors,
    member_count_errors,
    mlag_lag_errors,
    pair_row_errors,
    peer_devices_errors,
)
from tests.unit.query_shape_helpers import query_tree, unselected
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


def _row(row_id: str, device: str, vlan_id: int, deployment: str = "sd-web", segment: str = "web") -> dict[str, Any]:
    """One raw ManagedVlanDomainSegment edge."""
    return {
        "node": {
            "id": row_id,
            "vlan_id": {"value": vlan_id},
            "segment_deployment": {"node": {"id": deployment, "segment": {"node": {"name": {"value": segment}}}}},
            "device": {"node": {"id": f"id-{device}", "name": {"value": device}}},
        }
    }


def _lag(
    lag_id: int,
    device: str,
    lacp: str = "active",
    min_links: int = 1,
    role: str = "leaf",
    mlag: str | None = "a-b-mlag",
) -> dict[str, Any]:
    """One raw DcimLAGInterface edge; ``mlag`` is the MLAG name (its id is 'id-<name>') or None."""
    return {
        "node": {
            "id": f"lag-{device}-{lag_id}",
            "name": {"value": f"Port-Channel{lag_id}"},
            "lag_id": {"value": lag_id},
            "lacp_mode": {"value": lacp},
            "minimum_links": {"value": min_links},
            "role": {"value": role},
            "device": {"node": {"id": f"id-{device}", "name": {"value": device}}},
            "mlag_domain": {"node": {"id": f"id-{mlag}"}} if mlag else {"node": None},
        }
    }


def _raw(
    *domains: dict[str, Any], rows: tuple[dict[str, Any], ...] = (), lags: tuple[dict[str, Any], ...] = ()
) -> dict[str, Any]:
    """The raw query response."""
    return {
        "ManagedMLAG": {"edges": list(domains)},
        "ManagedVlanDomainSegment": {"edges": list(rows)},
        "DcimLAGInterface": {"edges": list(lags)},
    }


def _rows(*rows: dict[str, Any]) -> list[dict[str, Any]]:
    """The rows as the check sees them, after clean_data."""
    return clean_data(_raw(rows=rows))["ManagedVlanDomainSegment"]


def _lags(*lags: dict[str, Any]) -> list[dict[str, Any]]:
    """The LAGs as the check sees them, after clean_data."""
    return clean_data(_raw(lags=lags))["DcimLAGInterface"]


def _cleaned(*domains: dict[str, Any]) -> list[dict[str, Any]]:
    """The domains as the check sees them, after clean_data."""
    return clean_data(_raw(*domains))["ManagedMLAG"]


def _run(
    *domains: dict[str, Any], rows: tuple[dict[str, Any], ...] = (), lags: tuple[dict[str, Any], ...] = ()
) -> list[str]:
    """Run the check against canned data; return the logged errors."""
    check = cast(Any, CheckMlagDomains.__new__(CheckMlagDomains))
    errors: list[str] = []
    check.log_error = lambda message: errors.append(message)
    check.validate(_raw(*domains, rows=rows, lags=lags))
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


_PAIR = _domain("a-b-mlag", "a", "b")


def test_m4_passes_with_equal_ids_and_when_neither_member_has_a_row() -> None:
    """Equal ids on both members raise nothing; a deployment with no row on either member is not looked at."""
    rows = _rows(_row("r1", "a", 100), _row("r2", "b", 100), _row("r3", "c", 300, deployment="sd-other"))
    assert pair_row_errors(_cleaned(_PAIR), rows) == []
    assert pair_row_errors(_cleaned(_PAIR), []) == []


def test_m4_fails_on_different_ids_the_late_pairing_case() -> None:
    """A row id 100 on A and 101 on B for one segment deployment names the segment, both devices and both ids."""
    errors = pair_row_errors(_cleaned(_PAIR), _rows(_row("r1", "a", 100), _row("r2", "b", 101)))
    assert len(errors) == 1
    for text in ("'a-b-mlag'", "'web'", "VLAN 100 on 'a'", "VLAN 101 on 'b'"):
        assert text in errors[0]


def test_m4_fails_when_a_member_has_no_row_where_its_peer_has_one() -> None:
    """A member without a row is reported with the segment and the peer's id, whichever member holds the row."""
    for holder, other in (("a", "b"), ("b", "a")):
        errors = pair_row_errors(_cleaned(_PAIR), _rows(_row("r1", holder, 100)))
        assert len(errors) == 1
        assert f"'{other}' has no row" in errors[0]
        assert "'web'" in errors[0] and "VLAN 100" in errors[0]


def test_m4_ignores_a_domain_that_m2_reports_and_non_members() -> None:
    """A one-member domain is M2's; rows of devices outside a domain raise nothing."""
    assert pair_row_errors(_cleaned(_domain("a-mlag", "a")), _rows(_row("r1", "a", 100))) == []
    assert pair_row_errors(_cleaned(_PAIR), _rows(_row("r1", "x", 100), _row("r2", "y", 101))) == []


def test_m5_passes_for_a_mirrored_pair_of_port_channels() -> None:
    """One port-channel per member with equal attributes; the peer-link and a plain LAG are skipped."""
    lags = _lags(
        _lag(1, "a"),
        _lag(1, "b"),
        _lag(100, "a", role="mlag-peer", lacp="on"),
        _lag(100, "b", role="mlag-peer", min_links=9),
        _lag(7, "a", mlag=None),
    )
    assert mlag_lag_errors(_cleaned(_PAIR), lags) == []


def test_m5_fails_with_a_single_port_channel() -> None:
    """A lag_id on one member only is reported with the lag_id and the MLAG."""
    errors = mlag_lag_errors(_cleaned(_PAIR), _lags(_lag(1, "a")))
    assert len(errors) == 1
    assert "'a-b-mlag'" in errors[0] and "lag_id 1" in errors[0] and "1 port-channel" in errors[0]


def test_m5_fails_with_two_port_channels_on_one_member_or_a_third() -> None:
    """Two on one device, or three in all, is not one per member."""
    assert len(mlag_lag_errors(_cleaned(_PAIR), _lags(_lag(1, "a"), _lag(1, "a")))) == 1
    assert len(mlag_lag_errors(_cleaned(_PAIR), _lags(_lag(1, "a"), _lag(1, "b"), _lag(1, "b")))) == 1


def test_m5_fails_when_lacp_mode_or_minimum_links_differ() -> None:
    """Each differing attribute is named with both values."""
    errors = mlag_lag_errors(_cleaned(_PAIR), _lags(_lag(1, "a"), _lag(1, "b", lacp="passive", min_links=2)))
    assert len(errors) == 2
    assert any("lacp_mode differs (a=active, b=passive)" in error for error in errors)
    assert any("minimum_links differs (a=1, b=2)" in error for error in errors)


def test_check_logs_m4_and_m5_errors() -> None:
    """The check class runs both new rules on the raw response."""
    errors = _run(_PAIR, rows=(_row("r1", "a", 100), _row("r2", "b", 101)), lags=(_lag(1, "a"),))
    assert len(errors) == 2
    assert _run(_PAIR, rows=(_row("r1", "a", 100), _row("r2", "b", 100)), lags=(_lag(1, "a"), _lag(1, "b"))) == []


def test_the_fixtures_only_emit_fields_the_query_selects() -> None:
    """Query-shape pin: the raw fixtures stay inside queries/validation/mlag_domains.gql."""
    tree = query_tree(Path(__file__).parents[2] / "queries" / "validation" / "mlag_domains.gql")
    raw = _raw(_PAIR, rows=(_row("r1", "a", 100),), lags=(_lag(1, "a"),))
    assert unselected(raw, tree) == []
    # the pair rule needs the segment deployment, the row's device and the LAG's MLAG
    assert {"segment_deployment", "device", "vlan_id"} <= set(tree["ManagedVlanDomainSegment"]["edges"]["node"])
    assert {"lag_id", "lacp_mode", "minimum_links", "role", "mlag_domain"} <= set(
        tree["DcimLAGInterface"]["edges"]["node"]
    )
