"""Unit tests for CheckSegmentSecurity, fed canned segment_security query responses."""

from __future__ import annotations

from typing import Any, cast

import pytest

from checks.segment_security import CheckSegmentSecurity


def _v(value: Any) -> dict[str, Any]:
    return {"value": value}


def _check() -> Any:
    """A check with log_error/log_info captured instead of sent to Infrahub."""
    check = cast(Any, CheckSegmentSecurity.__new__(CheckSegmentSecurity))
    errors: list[str] = []
    infos: list[str] = []
    check._captured_errors = errors
    check._captured_infos = infos
    check.log_error = lambda message: errors.append(message)
    check.log_info = lambda message: infos.append(message)
    return check


def _segment(
    seg_id: str,
    kind: str = "ManagedVxlanSegment",
    env: str | None = "p",
    zone: str | None = "PROD-ZONE",
    prefix: str | None = "10.0.0.0/24",
    name: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """One ManagedNetworkSegment edge in the raw query shape."""
    gateway = (
        {"node": {"ip_prefix": {"node": {"id": f"pfx-{seg_id}", "prefix": _v(prefix)}}}} if prefix else {"node": None}
    )
    return {
        "node": {
            "__typename": kind,
            "id": seg_id,
            "name": _v(name or seg_id),
            "environment": _v(env),
            "security_zone": {"node": {"id": f"zone-{zone}", "name": _v(zone)}} if zone else {"node": None},
            "gateway": gateway,
            **extra,
        }
    }


def _rule(rule_id: str, src: str | None, dst: str | None, **extra: Any) -> dict[str, Any]:
    """One SecurityPolicyRule edge in the raw query shape."""
    return {
        "node": {
            "id": rule_id,
            "name": _v(rule_id),
            "disabled": _v(False),
            "policy": {"node": {"id": "pol", "name": _v("pol"), "enabled": _v(True)}},
            "source_segment": {"node": {"id": src, "name": _v(src)} if src else None},
            "destination_segment": {"node": {"id": dst, "name": _v(dst)} if dst else None},
            "source_prefixes": {"edges": []},
            "destination_prefixes": {"edges": []},
            "source_ip_addresses": {"edges": []},
            "destination_ip_addresses": {"edges": []},
            **extra,
        }
    }


def _run(segments: list[dict[str, Any]], rules: list[dict[str, Any]] | None = None, components: Any = None) -> Any:
    check = _check()
    check.validate(
        {
            "ManagedNetworkSegment": {"edges": segments},
            "SecurityPolicyRule": {"edges": rules or []},
            "AppComponent": {"edges": components or []},
        }
    )
    return check


class TestZone:
    """Invariants 1-3: zone present, zone follows environment, External in an exchange zone."""

    @pytest.mark.parametrize("kind", ["ManagedVlanSegment", "ManagedVxlanSegment", "CloudNetworkSegment"])
    def test_tenant_segment_in_matching_zone_passes(self, kind: str) -> None:
        """Every tenant kind in the zone of its environment is clean."""
        assert _run([_segment("a", kind)])._captured_errors == []

    @pytest.mark.parametrize("env", ["n", "s", "d", "t"])
    def test_non_prod_environments_map_to_nonprod_zone(self, env: str) -> None:
        """Any environment but p belongs in NONPROD-ZONE."""
        assert _run([_segment("a", env=env, zone="NONPROD-ZONE")])._captured_errors == []

    @pytest.mark.parametrize(
        "kind", ["ManagedVlanSegment", "ManagedVxlanSegment", "ManagedExternalSegment", "CloudNetworkSegment"]
    )
    def test_missing_zone_is_an_error_for_every_kind(self, kind: str) -> None:
        """A segment without a security_zone is an error, whatever its kind."""
        check = _run([_segment("a", kind, zone=None)])
        assert len(check._captured_errors) == 1
        assert "has no security_zone" in check._captured_errors[0]

    def test_zone_contradicting_environment_is_an_error(self) -> None:
        """A prod segment in NONPROD-ZONE is flagged."""
        check = _run([_segment("a", env="p", zone="NONPROD-ZONE")])
        assert len(check._captured_errors) == 1
        assert "expected 'PROD-ZONE'" in check._captured_errors[0]

    def test_non_prod_segment_in_prod_zone_is_an_error(self) -> None:
        """A dev segment in PROD-ZONE is flagged."""
        check = _run([_segment("a", env="d", zone="PROD-ZONE")])
        assert "expected 'NONPROD-ZONE'" in check._captured_errors[0]

    def test_tenant_segment_in_exchange_zone_is_exempt(self) -> None:
        """A shared service segment sits in MANAGEMENT-ZONE by design, whatever its environment."""
        check = _run([_segment("svc", env="p", zone="MANAGEMENT-ZONE")])
        assert check._captured_errors == []

    def test_tenant_segment_without_environment_is_an_error(self) -> None:
        """The zone cannot be verified without an environment: fail closed."""
        check = _run([_segment("a", env=None)])
        assert "has no environment" in check._captured_errors[0]

    def test_external_segment_in_exchange_zone_passes(self) -> None:
        """INTERNET-ZONE is an exchange zone; the External segment's environment is irrelevant."""
        seg = _segment("net", "ManagedExternalSegment", env=None, zone="INTERNET-ZONE", prefix=None)
        assert _run([seg])._captured_errors == []

    def test_external_segment_in_tenant_zone_is_an_error(self) -> None:
        """An External segment in PROD-ZONE is not in an exchange zone."""
        check = _run([_segment("net", "ManagedExternalSegment", env=None, zone="PROD-ZONE", prefix=None)])
        assert "not an exchange zone" in check._captured_errors[0]


class TestUniqueNames:
    """Invariant 4: computed names are unique across kinds."""

    def test_distinct_names_pass(self) -> None:
        """Different names across kinds are clean."""
        assert _run([_segment("a", name="x"), _segment("b", "CloudNetworkSegment", name="y")])._captured_errors == []

    def test_same_name_across_kinds_is_an_error(self) -> None:
        """A VLAN and a cloud segment with one computed name collide."""
        check = _run(
            [_segment("a", "ManagedVlanSegment", name="dup"), _segment("b", "CloudNetworkSegment", name="dup")]
        )
        assert len(check._captured_errors) == 1
        assert "'dup' is used by 2 segments" in check._captured_errors[0]


class TestRuleEndpoints:
    """Invariant 5: a rule endpoint with no prefix would render as any."""

    def test_rule_between_segments_with_prefixes_passes(self) -> None:
        """Both endpoints settle on a gateway prefix."""
        check = _run([_segment("a"), _segment("b")], [_rule("r", "a", "b")])
        assert check._captured_errors == []

    def test_gatewayless_segment_outside_a_rule_passes(self) -> None:
        """A segment without a gateway is fine while no rule names it (the check fires on rule endpoints only)."""
        assert _run([_segment("a", prefix=None)])._captured_errors == []

    def test_rule_to_prefixless_segment_warns(self) -> None:
        """A destination with no gateway renders as any: a warning (L2-only segments have no gateway by design)."""
        check = _run([_segment("a"), _segment("b", prefix=None)], [_rule("r", "a", "b")])
        assert check._captured_errors == []
        assert len(check._captured_infos) == 1
        assert "destination segment 'b' resolves to no prefix" in check._captured_infos[0]

    def test_rule_from_prefixless_segment_warns(self) -> None:
        """The source side is checked too."""
        check = _run([_segment("a", prefix=None), _segment("b")], [_rule("r", "a", "b")])
        assert "source segment 'a'" in check._captured_infos[0]

    def test_internet_segment_without_prefix_passes(self) -> None:
        """The internet is prefix-less by design."""
        net = _segment("net", "ManagedExternalSegment", env=None, zone="INTERNET-ZONE", prefix=None)
        assert _run([_segment("a"), net], [_rule("r", "a", "net")])._captured_errors == []

    def test_cloud_segment_resolves_through_cidr_block(self) -> None:
        """A cloud segment has no gateway; its cidr_block is its prefix."""
        cloud = _segment("c", "CloudNetworkSegment", prefix=None, cidr_block=_v("10.30.1.0/24"))
        assert _run([_segment("a"), cloud], [_rule("r", "a", "c")])._captured_errors == []

    def test_explicit_selector_covers_a_prefixless_segment(self) -> None:
        """A rule with its own prefix selector does not render as any."""
        rule = _rule("r", "a", "b", destination_prefixes={"edges": [{"node": {"id": "pfx"}}]})
        assert _run([_segment("a"), _segment("b", prefix=None)], [rule])._captured_errors == []

    def test_disabled_rule_and_disabled_policy_are_skipped(self) -> None:
        """Rules that are not rendered cannot render as any."""
        off_rule = _rule("r1", "a", "b", disabled=_v(True))
        off_policy = _rule("r2", "a", "b", policy={"node": {"id": "p", "name": _v("p"), "enabled": _v(False)}})
        check = _run([_segment("a"), _segment("b", prefix=None)], [off_rule, off_policy])
        assert check._captured_errors == []

    def test_rule_without_segment_endpoint_is_ignored(self) -> None:
        """A rule with no segment on a side is not this check's concern."""
        assert _run([_segment("a")], [_rule("r", "a", None)])._captured_errors == []


class TestComponentEnvironment:
    """Invariant 7: app environment vs its segment's environment is a warning, never an error."""

    @staticmethod
    def _component(app_env: str, seg_env: str) -> dict[str, Any]:
        return {
            "node": {
                "id": "comp",
                "name": _v("web"),
                "network_segment": {"node": {"id": "a", "name": _v("a"), "environment": _v(seg_env)}},
                "parent": {"node": {"id": "app", "name": _v("shop"), "environment": _v(app_env)}},
            }
        }

    def test_matching_environment_is_silent(self) -> None:
        """Same environment on both sides emits nothing."""
        check = _run([_segment("a")], components=[self._component("p", "p")])
        assert check._captured_infos == []

    def test_mismatched_environment_warns_without_error(self) -> None:
        """A dev app in a prod segment is logged as a warning, not an error."""
        check = _run([_segment("a")], components=[self._component("d", "p")])
        assert check._captured_errors == []
        assert len(check._captured_infos) == 1
        assert check._captured_infos[0].startswith("WARNING:")
