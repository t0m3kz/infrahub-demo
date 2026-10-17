"""Unit tests for CheckLispFabric.

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


def _dc(**overrides: Any) -> dict[str, Any]:
    """A clean LISP data center: ospf-ibgp, IPv4, two cisco_xe borders, one cisco_xe leaf, key, gatewayed segment."""
    dc: dict[str, Any] = {
        "id": "dc-1",
        "name": "DC13",
        "overlay_control_plane": "lisp",
        "routing_strategy": "ospf-ibgp",
        "underlay_protocol": "ipv4",
        "lisp_site_key": "key-1",
        "devices": [
            _device("dc13-border-01", "border-leaf"),
            _device("dc13-border-02", "border-leaf"),
            _device("dc13-spine-01", "spine", "arista_eos"),
        ],
        "segment_deployments": [{"vni": 10100, "segment": {"name": "web", "gateway": "ip-1"}}],
    }
    dc.update(overrides)
    return dc


def _pod(*devices: dict[str, Any], parent_id: str = "dc-1") -> dict[str, Any]:
    """A cleaned pod row whose parent is the data center ``parent_id``."""
    return {"parent": {"id": parent_id, "__typename": "TopologyDataCenter"}, "devices": list(devices)}


def _run(dc: dict[str, Any], *pods: dict[str, Any], metros: list[dict[str, Any]] | None = None) -> list[str]:
    """Validate one data center (plus pods and metros) and return the logged errors."""
    check = _check()
    check.validate({"TopologyDataCenter": [dc], "TopologyPod": list(pods), "TopologyColocationMetro": metros or []})
    return check._captured_errors


class TestCleanFabric:
    def test_clean_lisp_fabric_is_silent(self) -> None:
        """A fabric inside the LISP profile logs nothing."""
        assert _run(_dc(), _pod(_device("dc13-leaf-01", "leaf"), _device("dc13-tor-01", "tor"))) == []

    def test_bgp_evpn_fabric_with_ebgp_ebgp_is_silent(self) -> None:
        """A bgp_evpn fabric is out of scope even when every LISP rule would fail."""
        dc = _dc(
            overlay_control_plane="bgp_evpn",
            routing_strategy="ebgp-ebgp",
            underlay_protocol="ipv6",
            lisp_site_key=None,
            devices=[_device("x-super-spine-01", "super-spine", "arista_eos")],
            segment_deployments=[{"vni": 1, "segment": {"name": "web", "gateway": None}}],
        )
        assert _run(dc) == []

    def test_missing_overlay_flag_means_bgp_evpn(self) -> None:
        """A row without the flag is the schema default, bgp_evpn."""
        dc = _dc(routing_strategy="ebgp-ebgp")
        del dc["overlay_control_plane"]
        assert _run(dc) == []

    def test_empty_response_is_silent(self) -> None:
        """No fabrics at all is nothing to validate."""
        check = _check()
        check.validate({})
        assert check._captured_errors == []


class TestProfile:
    @pytest.mark.parametrize("strategy", ["ebgp-ebgp", "ebgp-ibgp", None])
    def test_strategy_other_than_ospf_ibgp_is_an_error(self, strategy: str | None) -> None:
        """Only ospf-ibgp is rendered; a missing strategy is the ebgp-ebgp schema default."""
        errors = _run(_dc(routing_strategy=strategy))
        assert len(errors) == 1
        assert "DC13" in errors[0]
        assert "ospf-ibgp" in errors[0]

    @pytest.mark.parametrize("underlay", ["ipv6", None])
    def test_non_ipv4_underlay_is_an_error(self, underlay: str | None) -> None:
        """The OSPFv2 underlay is IPv4 only; a missing value is the ipv6 schema default."""
        errors = _run(_dc(underlay_protocol=underlay))
        assert len(errors) == 1
        assert "underlay_protocol" in errors[0]
        assert "IPv4" in errors[0]

    def test_one_border_leaf_is_an_error(self) -> None:
        """A single map-server is not enough."""
        errors = _run(_dc(devices=[_device("dc13-border-01", "border-leaf")]))
        assert len(errors) == 1
        assert "1 border-leaf" in errors[0]

    def test_pod_border_leaf_counts_toward_the_pair(self) -> None:
        """Devices reached through the pods belong to the data center."""
        dc = _dc(devices=[_device("dc13-border-01", "border-leaf")])
        assert _run(dc, _pod(_device("dc13-border-02", "border-leaf"))) == []

    def test_pod_of_another_data_center_is_not_counted(self) -> None:
        """A pod's devices belong to its own parent only."""
        dc = _dc(devices=[_device("dc13-border-01", "border-leaf")])
        errors = _run(dc, _pod(_device("dc14-border-02", "border-leaf"), parent_id="dc-2"))
        assert len(errors) == 1
        assert "1 border-leaf" in errors[0]


class TestPlatformAndRoles:
    @pytest.mark.parametrize("role", ["leaf", "access-leaf", "tor", "border-leaf"])
    def test_non_cisco_xe_edge_device_is_an_error_naming_the_device(self, role: str) -> None:
        """Every role that carries a LISP role must be on cisco_xe, and the message names the device."""
        errors = _run(_dc(), _pod(_device("dc13-bad-01", role, "arista_eos")))
        assert len(errors) == 1
        assert "dc13-bad-01" in errors[0]
        assert "arista_eos" in errors[0]

    def test_device_without_platform_is_an_error(self) -> None:
        """A device with no platform cannot be rendered as cisco_xe."""
        errors = _run(_dc(), _pod(_device("dc13-leaf-01", "leaf", None)))
        assert len(errors) == 1
        assert "dc13-leaf-01" in errors[0]

    def test_non_cisco_xe_spine_is_fine(self) -> None:
        """Spine is transit only, so its platform is not constrained to cisco_xe."""
        assert _run(_dc(devices=_dc()["devices"] + [_device("dc13-spine-02", "spine", "nokia_sros")])) == []

    @pytest.mark.parametrize("role", ["super-spine", "hyper-spine", "border-spine"])
    def test_spine_overlay_roles_are_an_error(self, role: str) -> None:
        """LISP has no spine overlay role; the tiers that need one are refused."""
        errors = _run(_dc(devices=_dc()["devices"] + [_device("dc13-top-01", role, "arista_eos")]))
        assert len(errors) == 1
        assert "dc13-top-01" in errors[0]
        assert role in errors[0]


class TestSiteKeyAndSegments:
    def test_missing_site_key_with_devices_is_an_error(self) -> None:
        """Devices exist but the ETRs have no key to register with."""
        errors = _run(_dc(lisp_site_key=None))
        assert len(errors) == 1
        assert "lisp_site_key" in errors[0]

    def test_missing_site_key_without_devices_is_not_an_error_by_itself(self) -> None:
        """The key is provisioned with the devices; before any exist it is not yet missing."""
        errors = _run(_dc(lisp_site_key=None, devices=[]))
        assert all("lisp_site_key" not in error for error in errors)

    def test_segment_without_gateway_is_an_error(self) -> None:
        """No L2 flooding is rendered, so a gateway-less segment cannot work."""
        dc = _dc(segment_deployments=[{"vni": 10200, "segment": {"name": "db", "gateway": None}}])
        errors = _run(dc)
        assert len(errors) == 1
        assert "db" in errors[0]
        assert "10200" in errors[0]
        assert "gateway" in errors[0]

    def test_every_gatewayless_segment_is_reported(self) -> None:
        """One error per offending segment, none for the gatewayed one."""
        dc = _dc(
            segment_deployments=[
                {"vni": 1, "segment": {"name": "a", "gateway": None}},
                {"vni": 2, "segment": {"name": "b", "gateway": "ip-2"}},
                {"vni": 3, "segment": {"name": "c", "gateway": None}},
            ]
        )
        assert len(_run(dc)) == 2


class TestColocationMetro:
    def test_metro_with_lisp_is_an_error(self) -> None:
        """LISP is supported on data centers only."""
        errors = _run(
            _dc(overlay_control_plane="bgp_evpn"),
            metros=[{"id": "m-1", "name": "AMS", "overlay_control_plane": "lisp"}],
        )
        assert len(errors) == 1
        assert "AMS" in errors[0]

    def test_metro_with_bgp_evpn_is_silent(self) -> None:
        """The default control plane is fine on a metro."""
        assert _run(_dc(), metros=[{"id": "m-1", "name": "AMS", "overlay_control_plane": "bgp_evpn"}]) == []


class TestMultipleProblems:
    def test_every_problem_is_its_own_error(self) -> None:
        """A fabric far outside the profile reports each violation separately."""
        dc = _dc(
            routing_strategy="ebgp-ebgp",
            underlay_protocol="ipv6",
            lisp_site_key=None,
            devices=[_device("dc13-border-01", "border-leaf", "nxos"), _device("dc13-ss-01", "super-spine")],
        )
        # strategy, underlay, <2 borders, non-xe border, super-spine, site key
        assert len(_run(dc)) == 6
