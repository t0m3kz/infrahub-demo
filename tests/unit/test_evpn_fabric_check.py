"""Unit tests for CheckEvpnFabric.

Every case here is a fabric that renders valid config on every device and still
does not forward traffic — which is exactly why the check exists. The payloads are
in post-``clean_data`` shape; ``validate`` re-runs ``clean_data``, which is
idempotent on already-flat data.
"""

from __future__ import annotations

from typing import Any, cast

from checks.evpn_fabric import CheckEvpnFabric


def _check() -> Any:
    """CheckEvpnFabric with logging captured instead of sent to Infrahub.

    ``__new__`` skips ``InfrahubCheck.__init__``, which wants a client and a
    branch; ``validate`` needs neither. The lambdas swallow ``object_id`` /
    ``object_type`` so the test does not have to care which calls pass them.
    """
    check = cast(Any, CheckEvpnFabric.__new__(CheckEvpnFabric))
    errors: list[str] = []
    infos: list[str] = []
    check._captured_errors = errors
    check._captured_infos = infos
    check.log_error = lambda message, **_: errors.append(message)
    check.log_info = lambda message, **_: infos.append(message)
    return check


def _l2(segment_name: str, deployment_name: str, vni: int) -> dict[str, Any]:
    return {
        "name": segment_name,
        "segment_deployments": [{"vni": vni, "deployment": {"name": deployment_name}}],
    }


class TestRouteTargetAgreement:
    def test_ebgp_ebgp_without_rt_as_is_an_error(self) -> None:
        """Per-device overlay ASN + no fabric RT ASN = every VTEP gets its own RT."""
        check = _check()

        check.validate(
            {
                "TopologyDataCenter": [
                    {"id": "dc-1", "name": "DC11", "routing_strategy": "ebgp-ebgp", "evpn_rt_as": None}
                ]
            }
        )

        assert len(check._captured_errors) == 1
        assert "evpn_rt_as" in check._captured_errors[0]
        assert "DC11" in check._captured_errors[0]

    def test_ebgp_ebgp_with_rt_as_is_clean(self) -> None:
        """An explicit fabric-wide RT admin ASN is the fix, so no error."""
        check = _check()

        check.validate(
            {
                "TopologyDataCenter": [
                    {
                        "id": "dc-1",
                        "name": "DC11",
                        "routing_strategy": "ebgp-ebgp",
                        "evpn_rt_as": {"asn": 65000},
                    }
                ]
            }
        )

        assert check._captured_errors == []

    def test_missing_routing_strategy_defaults_to_ebgp_ebgp(self) -> None:
        """The schema default is ebgp-ebgp, so absence must be treated as the risky case."""
        check = _check()

        check.validate({"TopologyDataCenter": [{"id": "dc-1", "name": "DC11", "evpn_rt_as": None}]})

        assert len(check._captured_errors) == 1

    def test_ibgp_overlay_without_rt_as_is_allowed(self) -> None:
        """Under an iBGP overlay the shared overlay ASN is already fabric-wide."""
        check = _check()

        check.validate(
            {
                "TopologyDataCenter": [
                    {"id": "a", "name": "DC-ebgp-ibgp", "routing_strategy": "ebgp-ibgp", "evpn_rt_as": None},
                    {"id": "b", "name": "DC-ospf-ibgp", "routing_strategy": "ospf-ibgp", "evpn_rt_as": None},
                ]
            }
        )

        assert check._captured_errors == []

    def test_metro_without_rt_as_warns_but_does_not_fail(self) -> None:
        """A metro running no overlay yet is valid; the gap is worth saying out loud."""
        check = _check()

        check.validate({"TopologyColocationMetro": [{"id": "m-1", "name": "AMS-METRO", "evpn_rt_as": None}]})

        assert check._captured_errors == []
        assert len(check._captured_infos) == 1
        assert "AMS-METRO" in check._captured_infos[0]

    def test_metro_with_rt_as_is_silent(self) -> None:
        check = _check()

        check.validate({"TopologyColocationMetro": [{"id": "m-1", "name": "AMS-METRO", "evpn_rt_as": {"asn": 65100}}]})

        assert check._captured_errors == []
        assert check._captured_infos == []


class TestVniUniqueness:
    def test_two_segments_sharing_an_l2_vni_in_one_deployment(self) -> None:
        """Same VNI, same fabric: the two tenants get bridged together."""
        check = _check()

        check.validate(
            {
                "ManagedVxlanSegment": [
                    _l2("tenant-a-web", "DC11", 10100),
                    _l2("tenant-b-web", "DC11", 10100),
                ]
            }
        )

        assert len(check._captured_errors) == 1
        message = check._captured_errors[0]
        assert "10100" in message
        assert "tenant-a-web" in message and "tenant-b-web" in message

    def test_same_vni_in_different_deployments_is_fine(self) -> None:
        """VNIs are allocated per deployment, so reuse across fabrics is expected."""
        check = _check()

        check.validate(
            {
                "ManagedVxlanSegment": [
                    _l2("tenant-a-web", "DC11", 10100),
                    _l2("tenant-b-web", "DC12", 10100),
                ]
            }
        )

        assert check._captured_errors == []

    def test_stretched_segment_with_per_fabric_vnis_is_fine(self) -> None:
        """One segment in two fabrics holding two VNIs is the normal stretched case."""
        check = _check()

        check.validate(
            {
                "ManagedVxlanSegment": [
                    {
                        "name": "tenant-a-stretched",
                        "segment_deployments": [
                            {"vni": 10100, "deployment": {"name": "DC11"}},
                            {"vni": 10200, "deployment": {"name": "DC12"}},
                        ],
                    }
                ]
            }
        )

        assert check._captured_errors == []

    def test_unallocated_vni_is_ignored(self) -> None:
        """A segment whose VNI the generator has not allocated yet is not a collision."""
        check = _check()

        check.validate(
            {
                "ManagedVxlanSegment": [
                    {"name": "tenant-a-web", "segment_deployments": [{"vni": None, "deployment": {"name": "DC11"}}]},
                    {"name": "tenant-b-web", "segment_deployments": [{"vni": None, "deployment": {"name": "DC11"}}]},
                ]
            }
        )

        assert check._captured_errors == []

    def test_two_namespaces_sharing_an_l3_vni(self) -> None:
        """L3 VNIs are global, so any duplicate routes two VRFs into each other."""
        check = _check()

        check.validate(
            {
                "IpamNamespace": [
                    {"name": "production", "l3_vni": 50001},
                    {"name": "development", "l3_vni": 50001},
                ]
            }
        )

        assert len(check._captured_errors) == 1
        assert "50001" in check._captured_errors[0]

    def test_namespace_without_l3_vni_is_ignored(self) -> None:
        """The underlay namespace deliberately has no L3 VNI."""
        check = _check()

        check.validate(
            {
                "IpamNamespace": [
                    {"name": "default", "l3_vni": None},
                    {"name": "underlay", "l3_vni": None},
                ]
            }
        )

        assert check._captured_errors == []

    def test_l2_and_l3_vni_collision(self) -> None:
        """One 24-bit field on the wire: an L2/L3 overlap mixes bridged and routed traffic."""
        check = _check()

        check.validate(
            {
                "ManagedVxlanSegment": [_l2("tenant-a-web", "DC11", 50001)],
                "IpamNamespace": [{"name": "production", "l3_vni": 50001}],
            }
        )

        assert len(check._captured_errors) == 1
        message = check._captured_errors[0]
        assert "L2 VNI" in message and "L3 VNI" in message
        assert "50001" in message

    def test_disjoint_l2_and_l3_ranges_are_clean(self) -> None:
        """The shipped pools (L2 10001-39999 + 40000-49999, L3 50001-59999) must not trip anything."""
        check = _check()

        check.validate(
            {
                "ManagedVxlanSegment": [_l2("tenant-a-web", "DC11", 10100)],
                "IpamNamespace": [{"name": "production", "l3_vni": 50001}],
            }
        )

        assert check._captured_errors == []


class TestVniEncodability:
    def test_l2_vni_above_16_bits_is_rejected(self) -> None:
        """A 4-byte-ASN route-target leaves 16 bits, so 65536 cannot be expressed."""
        check = _check()

        check.validate({"ManagedVxlanSegment": [_l2("tenant-a-web", "DC11", 65536)]})

        assert len(check._captured_errors) == 1
        assert "65535" in check._captured_errors[0]

    def test_l3_vni_above_16_bits_is_rejected(self) -> None:
        check = _check()

        check.validate({"IpamNamespace": [{"name": "production", "l3_vni": 70000}]})

        assert len(check._captured_errors) == 1
        assert "70000" in check._captured_errors[0]

    def test_boundary_vni_is_accepted(self) -> None:
        """65535 fits exactly; the check must not be off by one."""
        check = _check()

        check.validate({"ManagedVxlanSegment": [_l2("tenant-a-web", "DC11", 65535)]})

        assert check._captured_errors == []


class TestEmptyPayloads:
    def test_no_data_is_not_an_error(self) -> None:
        """A branch that touches none of these kinds must pass silently."""
        check = _check()

        check.validate({})

        assert check._captured_errors == []
        assert check._captured_infos == []
