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


# ---------------------------------------------------------------------------
# Component group tags
# ---------------------------------------------------------------------------


def _tag(tag_id: str, group_id: int, *components: tuple[str, str]) -> dict[str, Any]:
    """One SecurityTag edge; ``components`` are (fqdn, segment id) pairs."""
    return {
        "node": {
            "id": tag_id,
            "name": _v(f"{tag_id}.example"),
            "group_id": _v(group_id),
            "components": {
                "edges": [
                    {"node": {"id": f"comp-{fqdn}", "fqdn": _v(fqdn), "network_segment": {"node": {"id": seg}}}}
                    for fqdn, seg in components
                ]
            },
        }
    }


def _tagged_component(fqdn: str, seg: str, tag_id: str | None) -> dict[str, Any]:
    """One AppComponent edge with its segment and (optional) tag."""
    return {
        "node": {
            "id": f"comp-{fqdn}",
            "name": _v(fqdn),
            "fqdn": _v(fqdn),
            "network_segment": {"node": {"id": seg, "name": _v(seg), "environment": _v("p")}},
            "security_tag": {"node": {"id": tag_id, "name": _v(tag_id)} if tag_id else None},
            "parent": {"node": {"id": "app", "name": _v("app"), "environment": _v("p")}},
        }
    }


def _profile(name: str, pool_id: str | None = "pool-sgt", pool_name: str = "SGT-POOL") -> dict[str, Any]:
    """One DcimGroupPolicyProfile edge naming a tag pool with the 16-4095 band (or none)."""
    pool = (
        {"node": {"id": pool_id, "name": _v(pool_name), "start_range": _v(16), "end_range": _v(4095)}}
        if pool_id
        else {"node": None}
    )
    return {"node": {"name": _v(name), "tag_pool": pool}}


_PROFILES = {"edges": [_profile("reference-cisco-nxos"), _profile("reference-arista-eos", pool_id=None)]}


def _run_tags(
    *,
    segments: list[dict[str, Any]] | None = None,
    components: list[dict[str, Any]] | None = None,
    tags: list[dict[str, Any]] | None = None,
    rules: list[dict[str, Any]] | None = None,
    profiles: dict[str, Any] | None = None,
) -> Any:
    """Run the check over a microsegmented segment "m" (plus ``segments``) and the given tags."""
    check = _check()
    check.validate(
        {
            "ManagedNetworkSegment": {"edges": [_segment("m", isolation_mode=_v("microsegmented")), *(segments or [])]},
            "SecurityPolicyRule": {"edges": rules or []},
            "AppComponent": {"edges": components or []},
            "SecurityTag": {"edges": tags or []},
            "DcimGroupPolicyProfile": _PROFILES if profiles is None else profiles,
        }
    )
    return check


def _tag_ref(tag_id: str) -> dict[str, Any]:
    return {"node": {"id": tag_id, "name": _v(tag_id)}}


class TestComponentTags:
    """Every component of a microsegmented segment has exactly one group tag of its own, in the pool band."""

    def test_tagged_components_in_band_pass(self) -> None:
        """Two components, one tag each, inside 16-4095: no error, no warning."""
        check = _run_tags(
            components=[_tagged_component("a", "m", "t1"), _tagged_component("b", "m", "t2")],
            tags=[_tag("t1", 16, ("a", "m")), _tag("t2", 4095, ("b", "m"))],
        )
        assert check._captured_errors == []
        assert check._captured_infos == []

    def test_untagged_component_of_a_microsegmented_segment_is_an_error(self) -> None:
        check = _run_tags(components=[_tagged_component("a", "m", None)])
        assert check._captured_errors == ["Component 'a' in microsegmented segment 'm' has no security_tag"]

    def test_untagged_component_of_a_normal_segment_is_fine(self) -> None:
        """Only a microsegmented segment needs tags."""
        check = _run_tags(segments=[_segment("n")], components=[_tagged_component("a", "n", None)])
        assert check._captured_errors == []

    def test_tag_shared_by_two_components_is_an_error(self) -> None:
        """A shared tag would merge two groups (two tenants' components, possibly)."""
        check = _run_tags(
            components=[_tagged_component("a", "m", "t1"), _tagged_component("b", "m", "t1")],
            tags=[_tag("t1", 20, ("a", "m"), ("b", "m"))],
        )
        assert check._captured_errors == ["Security tag 't1.example' is shared by 2 components: ['a', 'b']"]

    def test_orphan_tag_is_a_warning(self) -> None:
        check = _run_tags(tags=[_tag("t1", 20)])
        assert check._captured_errors == []
        assert check._captured_infos == ["WARNING: security tag 't1.example' groups no component (orphan)"]

    @pytest.mark.parametrize("group_id", [15, 4096, 65000])
    def test_group_id_outside_the_pool_band_is_an_error(self, group_id: int) -> None:
        """The band is the one of the pool the profile rows name, read from data."""
        check = _run_tags(components=[_tagged_component("a", "m", "t1")], tags=[_tag("t1", group_id, ("a", "m"))])
        assert check._captured_errors == [f"Security tag 't1.example' group_id {group_id} is outside SGT-POOL 16-4095"]

    def test_missing_pool_is_an_error_when_tags_exist(self) -> None:
        """The band cannot be checked without a profile row naming the pool: fail closed."""
        check = _run_tags(
            components=[_tagged_component("a", "m", "t1")],
            tags=[_tag("t1", 20, ("a", "m"))],
            profiles={"edges": []},
        )
        assert check._captured_errors == [
            "No group tag pool (the group-policy profiles name 0 tag pools [], expected one): "
            "the band of 1 group tag(s) cannot be checked"
        ]

    def test_profiles_naming_two_pools_is_an_error_when_tags_exist(self) -> None:
        """Group IDs are fabric-wide: two pools would let one number mean two groups."""
        check = _run_tags(
            components=[_tagged_component("a", "m", "t1")],
            tags=[_tag("t1", 20, ("a", "m"))],
            profiles={"edges": [_profile("a"), _profile("b", pool_id="pool-other", pool_name="OTHER-POOL")]},
        )
        assert check._captured_errors == [
            "No group tag pool (the group-policy profiles name 2 tag pools ['OTHER-POOL', 'SGT-POOL'], expected one): "
            "the band of 1 group tag(s) cannot be checked"
        ]


class TestRuleTags:
    """A rule's tags are the tags of components in the rule's own segment, on intra-segment rules only."""

    _TAGS = [_tag("t1", 20, ("a", "m")), _tag("t2", 21, ("b", "m")), _tag("t9", 29, ("z", "other"))]
    _COMPONENTS = [_tagged_component("a", "m", "t1"), _tagged_component("b", "m", "t2")]

    def test_intra_segment_rule_with_its_components_tags_passes(self) -> None:
        rule = _rule("a-to-b", "m", "m", source_security_tag=_tag_ref("t1"), destination_security_tag=_tag_ref("t2"))
        check = _run_tags(components=self._COMPONENTS, tags=self._TAGS, rules=[rule])
        assert check._captured_errors == []

    def test_untagged_rule_is_ignored(self) -> None:
        check = _run_tags(components=self._COMPONENTS, tags=self._TAGS, rules=[_rule("a-to-b", "m", "m")])
        assert check._captured_errors == []

    def test_tag_of_another_segment_is_an_error(self) -> None:
        rule = _rule("a-to-z", "m", "m", source_security_tag=_tag_ref("t1"), destination_security_tag=_tag_ref("t9"))
        check = _run_tags(components=self._COMPONENTS, tags=self._TAGS, rules=[rule])
        assert check._captured_errors == [
            "Rule 'a-to-z': destination tag 't9' groups no component of the rule's segment"
        ]

    def test_tag_that_groups_nothing_is_an_error(self) -> None:
        """A contract between tags that do not exist as groups."""
        rule = _rule("a-to-x", "m", "m", source_security_tag=_tag_ref("t1"), destination_security_tag=_tag_ref("tx"))
        check = _run_tags(components=self._COMPONENTS, tags=self._TAGS, rules=[rule])
        assert check._captured_errors == [
            "Rule 'a-to-x': destination tag 'tx' groups no component of the rule's segment"
        ]

    def test_tagged_cross_segment_rule_is_an_error(self) -> None:
        rule = _rule("a-to-n", "m", "n", source_security_tag=_tag_ref("t1"))
        check = _run_tags(segments=[_segment("n")], components=self._COMPONENTS, tags=self._TAGS, rules=[rule])
        assert check._captured_errors == ["Rule 'a-to-n' carries group tags but is not an intra-segment rule"]
