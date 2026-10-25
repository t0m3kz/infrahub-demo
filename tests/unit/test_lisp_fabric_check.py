"""Unit tests for CheckLispFabric (the LISP site of an office with a fabric).

The payloads are in post-``clean_data`` shape (``validate`` re-runs ``clean_data``,
which is idempotent on already-flat data): a presence-only relationship such as
``lisp_site_key { node { id } }`` or ``gateway { node { id } }`` arrives as the
bare id string, a platform as ``{"name": ...}``.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from checks.lisp_fabric import CheckLispFabric


def _check() -> Any:
    """CheckLispFabric with logging captured instead of sent to Infrahub."""
    check = cast(Any, CheckLispFabric.__new__(CheckLispFabric))
    errors: list[str] = []
    check._captured_errors = errors
    check.log_error = lambda message, **_: errors.append(message)
    return check


def _device(name: str, role: str, platform: str | None = "cisco_xe") -> dict[str, Any]:
    """A cleaned device row."""
    return {"name": name, "role": role, "platform": {"name": platform} if platform else None}


def _run_offices(*offices: dict[str, Any]) -> list[str]:
    """Validate offices and return the logged errors."""
    check = _check()
    check.validate({"TopologyCustomerOffice": list(offices)})
    return check._captured_errors


def _gateway(vrf: str | None = "VRF_A", l3_vni: int | None = 50001) -> dict[str, Any]:
    """A cleaned segment gateway whose prefix sits in the namespace ``vrf`` (None: no namespace at all)."""
    namespace = {"name": vrf, "l3_vni": l3_vni} if vrf else None
    return {"id": "ip-1", "ip_prefix": {"ip_namespace": namespace}}


def _office(**overrides: Any) -> dict[str, Any]:
    """A clean LISP office: fabric_templates, one building, two cisco_xe distribution-switches, an access-switch, a key.

    The office also holds its SD-WAN edge, which is not part of the LISP site and may be on any platform; with the
    BGP process of its WAN exit (add_office_fabric), as a settled office has it.
    """
    office: dict[str, Any] = {
        "id": "office-1",
        "name": "C005-OFFICE",
        "fabric_templates": ["tpl-1", "tpl-2"],
        "locations": [{"id": "loc-1", "name": "ams-branch"}],
        "lisp_site_key": {"id": "key-1"},
        "devices": [
            _device("ds-c005-office01", "distribution-switch"),
            _device("ds-c005-office02", "distribution-switch"),
            _device("as-c005-office01", "access-switch"),
            {**_device("C005-OFFICE-EDGE1", "edge", "linux"), "capabilities": [{"typename": "ManagedBGP"}]},
        ],
        "segment_deployments": [{"vni": 10100, "segment": {"name": "web", "gateway": _gateway()}}],
    }
    office.update(overrides)
    return office


class TestEmptyResponse:
    def test_empty_response_is_silent(self) -> None:
        """No offices at all is nothing to validate."""
        check = _check()
        check.validate({})
        assert check._captured_errors == []

    def test_a_data_center_or_metro_row_is_ignored(self) -> None:
        """The check knows no data center or colocation metro: a LISP profile exists for offices only."""
        check = _check()
        check.validate(
            {
                "TopologyDataCenter": [{"id": "dc-1", "name": "DC10", "devices": []}],
                "TopologyPod": [{"parent": {"id": "dc-1"}, "devices": []}],
                "TopologyColocationMetro": [{"id": "m-1", "name": "AMS"}],
            }
        )
        assert check._captured_errors == []


class TestOfficeFabric:
    def test_clean_office_is_silent(self) -> None:
        """An office inside the profile logs nothing, and its SD-WAN edge on another platform is not part of it."""
        assert _run_offices(_office()) == []

    def test_office_without_fabric_templates_is_not_a_lisp_footprint(self) -> None:
        """No opt-in, no rules: such an office has no fabric (only its SD-WAN edge)."""
        bare = _office(fabric_templates=[], devices=[], lisp_site_key=None, segment_deployments=[])
        assert _run_offices(bare) == []
        assert _run_offices({"id": "o", "name": "C001-OFFICE", "locations": [{"id": "loc-1", "name": "x"}]}) == []

    def test_office_without_distribution_switch_is_an_error(self) -> None:
        errors = _run_offices(_office(devices=[_device("as-c005-office01", "access-switch")]))
        assert len(errors) == 1
        assert "C005-OFFICE" in errors[0] and "0 distribution-switch" in errors[0]

    def test_an_sdwan_edge_without_a_bgp_process_means_the_wan_exit_is_missing(self) -> None:
        """The exit is built by add_office_fabric for the edge the office has; the edge's BGP process is its proof."""
        bare = {**_device("C005-OFFICE-EDGE1", "edge", "linux"), "capabilities": [{"typename": "ManagedOSPF"}]}
        devices = [d for d in _office()["devices"] if d["role"] != "edge"]

        errors = _run_offices(_office(devices=[*devices, bare]))

        assert len(errors) == 1
        assert "C005-OFFICE-EDGE1" in errors[0] and "no BGP process" in errors[0] and "WAN exit" in errors[0]

    @pytest.mark.parametrize("capabilities", [None, []])
    def test_an_sdwan_edge_with_no_capabilities_at_all_is_the_same_error(self, capabilities: Any) -> None:
        devices = [d for d in _office()["devices"] if d["role"] != "edge"]
        edge = {**_device("C005-OFFICE-EDGE1", "edge", "linux"), "capabilities": capabilities}

        errors = _run_offices(_office(devices=[*devices, edge]))

        assert len(errors) == 1 and "no BGP process" in errors[0]

    def test_an_office_without_an_sdwan_edge_has_no_exit_to_check(self) -> None:
        devices = [d for d in _office()["devices"] if d["role"] != "edge"]

        assert _run_offices(_office(devices=devices)) == []

    def test_an_office_without_a_fabric_is_not_asked_for_an_exit(self) -> None:
        """C001-C003 and C015 are a single SD-WAN edge with no fabric: no LISP footprint, no rule."""
        edge = {**_device("C001-OFFICE-EDGE1", "edge", "linux"), "capabilities": []}

        assert _run_offices(_office(fabric_templates=[], devices=[edge])) == []

    def test_one_distribution_switch_is_enough(self) -> None:
        assert _run_offices(_office(devices=[_device("ds-c005-office01", "distribution-switch")])) == []

    @pytest.mark.parametrize("role", ["access-switch", "distribution-switch"])
    @pytest.mark.parametrize("platform", ["arista_eos", None])
    def test_non_cisco_xe_fabric_device_is_an_error_naming_the_device(self, role: str, platform: str | None) -> None:
        devices = _office()["devices"] + [_device("c005-bad-01", role, platform)]
        errors = _run_offices(_office(devices=devices))
        assert len(errors) == 1
        assert "c005-bad-01" in errors[0] and role in errors[0] and "cisco_xe" in errors[0]

    def test_missing_site_key_with_a_fabric_is_an_error(self) -> None:
        errors = _run_offices(_office(lisp_site_key=None))
        assert len(errors) == 1
        assert "lisp_site_key" in errors[0]

    def test_missing_site_key_without_fabric_devices_is_not_an_error_by_itself(self) -> None:
        """The key is provisioned with the devices (the generator has not run yet)."""
        errors = _run_offices(_office(lisp_site_key=None, devices=[]))
        assert all("lisp_site_key" not in error for error in errors)

    def test_segment_without_gateway_is_an_error(self) -> None:
        office = _office(segment_deployments=[{"vni": 10200, "segment": {"name": "db", "gateway": None}}])
        errors = _run_offices(office)
        assert len(errors) == 1
        assert "db" in errors[0] and "10200" in errors[0] and "no gateway" in errors[0]

    @pytest.mark.parametrize("vrf", ["default", None])
    def test_segment_in_the_default_namespace_is_an_error(self, vrf: str | None) -> None:
        """A gateway outside a VRF would render a host SVI in the global table (the transform refuses it)."""
        office = _office(segment_deployments=[{"vni": 10200, "segment": {"name": "db", "gateway": _gateway(vrf)}}])
        errors = _run_offices(office)
        assert len(errors) == 1
        assert "db" in errors[0] and "10200" in errors[0] and "default namespace" in errors[0]

    def test_segment_in_a_vrf_without_l3_vni_is_an_error(self) -> None:
        office = _office(
            segment_deployments=[{"vni": 10200, "segment": {"name": "db", "gateway": _gateway("DEV", None)}}]
        )
        errors = _run_offices(office)
        assert len(errors) == 1
        assert "db" in errors[0] and "'DEV'" in errors[0] and "l3_vni" in errors[0]

    def test_a_gatewayless_segment_is_reported_once_not_also_as_vrf_less(self) -> None:
        office = _office(segment_deployments=[{"vni": 1, "segment": {"name": "a", "gateway": None}}])
        assert len(_run_offices(office)) == 1

    def test_every_offending_segment_is_reported(self) -> None:
        office = _office(
            segment_deployments=[
                {"vni": 1, "segment": {"name": "a", "gateway": None}},
                {"vni": 2, "segment": {"name": "b", "gateway": _gateway("default", None)}},
                {"vni": 3, "segment": {"name": "c", "gateway": _gateway()}},
                {"vni": 4, "segment": {"name": "d", "gateway": _gateway("DEV", None)}},
            ]
        )
        assert len(_run_offices(office)) == 3

    def test_two_lisp_offices_in_one_building_are_both_errors(self) -> None:
        first = _office()
        second = _office(id="office-2", name="C006-OFFICE")
        errors = _run_offices(first, second)
        assert len(errors) == 2
        assert any("C005-OFFICE" in e and "C006-OFFICE" in e and "ams-branch" in e for e in errors)
        assert any(e.startswith("Office 'C006-OFFICE'") and "C005-OFFICE" in e for e in errors)

    def test_lisp_offices_in_different_buildings_are_silent(self) -> None:
        second = _office(id="office-2", name="C006-OFFICE", locations=[{"id": "loc-2", "name": "fra-hq"}])
        assert _run_offices(_office(), second) == []

    def test_an_office_without_a_fabric_does_not_share_the_building(self) -> None:
        """Only footprints that run a fabric count: an SD-WAN-only office in the same building is fine."""
        sdwan_only = {"id": "office-2", "name": "C006-OFFICE", "locations": [{"id": "loc-1", "name": "ams-branch"}]}
        assert _run_offices(_office(), sdwan_only) == []

    def test_a_footprint_with_several_buildings_is_checked_per_building(self) -> None:
        wide = _office(locations=[{"id": "loc-1", "name": "ams-branch"}, {"id": "loc-9", "name": "fra-hq"}])
        other = _office(id="office-2", name="C006-OFFICE", locations=[{"id": "loc-9", "name": "fra-hq"}])
        errors = _run_offices(wide, other)
        assert len(errors) == 2
        assert all("fra-hq" in e and "ams-branch" not in e for e in errors)

    def test_every_problem_is_its_own_error(self) -> None:
        office = _office(
            lisp_site_key=None,
            devices=[_device("as-c005-office01", "access-switch", "arista_eos")],
            segment_deployments=[{"vni": 1, "segment": {"name": "a", "gateway": None}}],
        )
        # no distribution-switch, non-xe access-switch, site key, gatewayless segment
        assert len(_run_offices(office)) == 4

    def test_errors_carry_the_office_as_the_object(self) -> None:
        check = _check()
        recorded: list[dict[str, Any]] = []
        check.log_error = lambda message, **kwargs: recorded.append(kwargs)
        check.validate({"TopologyCustomerOffice": [_office(lisp_site_key=None)]})
        assert recorded == [{"object_id": "office-1", "object_type": "TopologyCustomerOffice"}]
