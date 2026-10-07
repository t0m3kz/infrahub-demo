"""Unit tests for _collapse_to_single_instance().

A router runs exactly ONE BGP ASN per routing instance. Under ebgp-ibgp the
underlay is eBGP on a per-device ASN and the overlay is iBGP on the fabric-wide
ASN, so two BGP configs survive the merge-by-ASN step and the templates would
emit two `router bgp` stanzas. The device rejects the second — or, on a platform
that accepts the first and ignores the rest, half the routing silently
disappears.

The collapse anchors on the OVERLAY ASN and moves the underlay ASN onto its own
sessions as `local_as_override`. The direction matters: anchoring on the underlay
ASN instead would make the EVPN sessions eBGP and break route reflection, so
several tests here assert the direction explicitly rather than just the count.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from transforms.helpers.bgp import _collapse_to_single_instance

UNDERLAY_ASN = 65001
OVERLAY_ASN = 65000


def _session(name: str, address_families: list[str]) -> dict[str, Any]:
    return {"name": name, "address_families": address_families, "remote_as": {"asn": 65100}}


def _config(asn: int, name: str, sessions: list[dict[str, Any]]) -> dict[str, Any]:
    return {"name": name, "local_as": {"asn": asn}, "sessions": sessions}


def _underlay() -> dict[str, Any]:
    return _config(
        UNDERLAY_ASN,
        "bgp-underlay",
        [_session("to-spine-1", ["ipv4"]), _session("to-spine-2", ["ipv4"])],
    )


def _overlay() -> dict[str, Any]:
    return _config(OVERLAY_ASN, "bgp-overlay", [_session("to-rr-1", ["evpn"])])


class TestNothingToCollapse:
    def test_single_config_is_returned_unchanged(self) -> None:
        """ebgp-ebgp: underlay and overlay already share the device's ASN."""
        configs = [_overlay()]
        assert _collapse_to_single_instance(configs) is configs

    def test_empty_input_is_returned_unchanged(self) -> None:
        assert _collapse_to_single_instance([]) == []


class TestOverlayAnchoredCollapse:
    def test_collapses_to_one_config(self) -> None:
        result = _collapse_to_single_instance([_underlay(), _overlay()])
        assert len(result) == 1

    def test_instance_asn_is_the_overlay_asn(self) -> None:
        """The whole point: the shared ASN must own the instance so the EVPN
        sessions stay natively iBGP and route reflection keeps working."""
        result = _collapse_to_single_instance([_underlay(), _overlay()])
        assert result[0]["local_as"]["asn"] == OVERLAY_ASN

    def test_anchor_is_chosen_by_evpn_not_by_input_order(self) -> None:
        """The overlay wins whether it came first or second out of the merge."""
        overlay_first = _collapse_to_single_instance([_overlay(), _underlay()])
        underlay_first = _collapse_to_single_instance([_underlay(), _overlay()])
        assert overlay_first[0]["local_as"]["asn"] == OVERLAY_ASN
        assert underlay_first[0]["local_as"]["asn"] == OVERLAY_ASN

    def test_all_sessions_survive_on_the_anchor(self) -> None:
        """Dropping a session here would silently delete underlay peerings."""
        result = _collapse_to_single_instance([_underlay(), _overlay()])
        assert sorted(s["name"] for s in result[0]["sessions"]) == ["to-rr-1", "to-spine-1", "to-spine-2"]

    def test_underlay_sessions_carry_the_local_as_override(self) -> None:
        """The eBGP neighbour's own `remote-as 65001` has not changed, so the
        folded sessions must keep presenting 65001."""
        result = _collapse_to_single_instance([_underlay(), _overlay()])
        folded = [s for s in result[0]["sessions"] if s["name"].startswith("to-spine")]
        assert len(folded) == 2
        assert all(s["local_as_override"] == {"asn": UNDERLAY_ASN} for s in folded)

    def test_overlay_sessions_carry_no_override(self) -> None:
        """An override on the EVPN session would turn it into eBGP — the exact
        failure this design avoids."""
        result = _collapse_to_single_instance([_underlay(), _overlay()])
        evpn = next(s for s in result[0]["sessions"] if s["name"] == "to-rr-1")
        assert "local_as_override" not in evpn

    def test_folding_is_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.INFO):
            _collapse_to_single_instance([_underlay(), _overlay()], device_name="dc1-leaf-1")
        assert "dc1-leaf-1" in caplog.text
        assert str(UNDERLAY_ASN) in caplog.text
        assert str(OVERLAY_ASN) in caplog.text


class TestAmbiguousAnchor:
    """Neither of these should happen for the strategies this project generates.
    Producing ONE valid instance beats N invalid ones, but the arbitrary choice
    has to be visible.
    """

    def test_no_evpn_process_still_collapses_and_errors(self, caplog: pytest.LogCaptureFixture) -> None:
        configs = [
            _config(UNDERLAY_ASN, "bgp-a", [_session("to-a", ["ipv4"])]),
            _config(OVERLAY_ASN, "bgp-b", [_session("to-b", ["ipv4"])]),
        ]
        with caplog.at_level(logging.ERROR):
            result = _collapse_to_single_instance(configs, device_name="dc1-leaf-1")

        assert len(result) == 1
        assert "none carries the EVPN address-family" in caplog.text
        assert "dc1-leaf-1" in caplog.text

    def test_no_evpn_process_anchors_on_the_first_config(self) -> None:
        """Documents the fallback rather than endorsing it — get_bgp_profile
        sorts by ASN before calling this, so `configs[0]` is the lowest ASN."""
        configs = [
            _config(OVERLAY_ASN, "bgp-a", [_session("to-a", ["ipv4"])]),
            _config(UNDERLAY_ASN, "bgp-b", [_session("to-b", ["ipv4"])]),
        ]
        result = _collapse_to_single_instance(configs)
        assert result[0]["local_as"]["asn"] == OVERLAY_ASN

    def test_two_evpn_processes_errors(self, caplog: pytest.LogCaptureFixture) -> None:
        configs = [
            _config(UNDERLAY_ASN, "bgp-a", [_session("to-a", ["evpn"])]),
            _config(OVERLAY_ASN, "bgp-b", [_session("to-b", ["evpn"])]),
        ]
        with caplog.at_level(logging.ERROR):
            result = _collapse_to_single_instance(configs, device_name="dc1-leaf-1")

        assert len(result) == 1
        assert "2 of them carries the EVPN address-family" in caplog.text

    def test_sessions_are_never_lost_in_the_ambiguous_path(self) -> None:
        configs = [
            _config(UNDERLAY_ASN, "bgp-a", [_session("to-a", ["ipv4"])]),
            _config(OVERLAY_ASN, "bgp-b", [_session("to-b", ["ipv4"]), _session("to-c", ["ipv4"])]),
        ]
        result = _collapse_to_single_instance(configs)
        assert sorted(s["name"] for s in result[0]["sessions"]) == ["to-a", "to-b", "to-c"]


class TestThreeProcesses:
    def test_every_non_anchor_process_is_folded_with_its_own_override(self) -> None:
        """Not a shape this project generates today, but the fold must not assume
        exactly two processes — a silently dropped third would delete peerings."""
        configs = [
            _config(65001, "bgp-underlay", [_session("to-spine", ["ipv4"])]),
            _config(65002, "bgp-external", [_session("to-wan", ["ipv4"])]),
            _config(OVERLAY_ASN, "bgp-overlay", [_session("to-rr", ["evpn"])]),
        ]
        result = _collapse_to_single_instance(configs)

        assert len(result) == 1
        assert result[0]["local_as"]["asn"] == OVERLAY_ASN
        by_name = {s["name"]: s for s in result[0]["sessions"]}
        assert by_name["to-spine"]["local_as_override"] == {"asn": 65001}
        assert by_name["to-wan"]["local_as_override"] == {"asn": 65002}
        assert "local_as_override" not in by_name["to-rr"]
