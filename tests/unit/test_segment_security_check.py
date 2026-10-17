"""Unit tests for CheckSegmentSecurity, fed canned segment_security query responses."""

from __future__ import annotations

from typing import Any, cast

import pytest

from checks.segment_security import CheckSegmentSecurity
from utils.exchange_transit import namespace_type_for_environment

_UNSET = object()


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
    namespace_type: Any = _UNSET,
    **extra: Any,
) -> dict[str, Any]:
    """One ManagedNetworkSegment edge in the raw query shape.

    The gateway prefix sits in a namespace of the environment's type unless
    ``namespace_type`` says otherwise (None: a namespace without a type).
    """
    if namespace_type is _UNSET:
        namespace_type = namespace_type_for_environment(env) if env else None
    namespace = {"node": {"name": _v(f"ns-{namespace_type}"), "namespace_type": _v(namespace_type)}}
    gateway = (
        {"node": {"ip_prefix": {"node": {"id": f"pfx-{seg_id}", "prefix": _v(prefix), "ip_namespace": namespace}}}}
        if prefix
        else {"node": None}
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


class TestGatewayNamespace:
    """A routed tenant segment's gateway prefix is in a namespace of its environment's type."""

    @pytest.mark.parametrize("kind", ["ManagedVlanSegment", "ManagedVxlanSegment"])
    @pytest.mark.parametrize(("env", "zone"), [("p", "PROD-ZONE"), ("d", "NONPROD-ZONE"), ("n", "NONPROD-ZONE")])
    def test_gateway_in_the_environments_namespace_passes(self, kind: str, env: str, zone: str) -> None:
        """PROD for p, NON-PROD for every other environment."""
        assert _run([_segment("a", kind, env=env, zone=zone)])._captured_errors == []

    @pytest.mark.parametrize("kind", ["ManagedVlanSegment", "ManagedVxlanSegment"])
    def test_prod_segment_addressed_in_non_prod_is_an_error(self, kind: str) -> None:
        """A p segment with a NON-PROD gateway is flagged, though its zone is right."""
        check = _run([_segment("a", kind, env="p", namespace_type="non_prod")])
        assert len(check._captured_errors) == 1
        assert "gateway prefix 10.0.0.0/24 in namespace 'ns-non_prod'" in check._captured_errors[0]
        assert "expected a 'prod' namespace" in check._captured_errors[0]

    def test_non_prod_segment_addressed_in_prod_is_an_error(self) -> None:
        """A dev segment with a PROD gateway is flagged."""
        check = _run([_segment("a", env="d", zone="NONPROD-ZONE", namespace_type="prod")])
        assert "expected a 'non_prod' namespace" in check._captured_errors[0]

    def test_gateway_in_an_untyped_namespace_is_an_error(self) -> None:
        """A tenant gateway in a namespace with no namespace_type (e.g. default) is flagged."""
        check = _run([_segment("a", namespace_type=None)])
        assert "(type 'None')" in check._captured_errors[0]

    def test_exchange_zone_segment_is_exempt(self) -> None:
        """The MANAGEMENT services segment is environment p and addressed in MANAGEMENT, by design."""
        check = _run(
            [_segment("svc", "ManagedVlanSegment", env="p", zone="MANAGEMENT-ZONE", namespace_type="management")]
        )
        assert check._captured_errors == []

    def test_l2_only_segment_has_nothing_to_check(self) -> None:
        """No gateway, no namespace to compare."""
        assert _run([_segment("a", prefix=None)])._captured_errors == []

    def test_cloud_segment_is_not_checked(self) -> None:
        """A cloud segment is addressed by its cidr_block, not a fabric gateway."""
        assert _run([_segment("c", "CloudNetworkSegment", namespace_type="non_prod")])._captured_errors == []

    def test_missing_environment_is_reported_once(self) -> None:
        """The zone rule reports a missing environment; the namespace rule adds nothing."""
        check = _run([_segment("a", env=None, namespace_type="prod")])
        assert len(check._captured_errors) == 1
        assert "has no environment" in check._captured_errors[0]


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

    def test_ipv6_only_segment_still_warns_because_the_rule_resolver_is_ipv4_only(self) -> None:
        """The warning is about the leaf ACL and firewall table (policy.rule_endpoint, IPv4 only), not the MSS-G
        classifier (utils.segment_addressing.classifier_prefix, IPv4 else IPv6): an IPv6-only endpoint renders as any."""
        check = _run([_segment("a"), _segment("b", prefix="2001:db8::/64")], [_rule("r", "a", "b")])
        assert len(check._captured_infos) == 1
        assert "destination segment 'b' resolves to no prefix" in check._captured_infos[0]

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


def _profile(
    name: str,
    pool_id: str | None = "pool-sgt",
    pool_name: str = "SGT-POOL",
    *,
    unclassified: int | None = None,
    unaware: int | None = None,
) -> dict[str, Any]:
    """One DcimGroupPolicyProfile edge naming a tag pool with the 16-4095 band (or none), and its reserved groups."""
    pool = (
        {"node": {"id": pool_id, "name": _v(pool_name), "start_range": _v(16), "end_range": _v(4095)}}
        if pool_id
        else {"node": None}
    )
    return {
        "node": {
            "name": _v(name),
            "unclassified_group_id": _v(unclassified),
            "unaware_group_id": _v(unaware),
            "tag_pool": pool,
        }
    }


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
        assert check._captured_infos == [
            "WARNING: security tag 't1.example' groups no component and no segment (orphan)"
        ]

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
    """A rule side's tag is a component of that side's segment, and only where that segment is microsegmented."""

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
            "Rule 'a-to-z': destination tag 't9' groups no component of its destination segment 'm'"
        ]

    def test_tag_that_groups_nothing_is_an_error(self) -> None:
        """A contract between tags that do not exist as groups."""
        rule = _rule("a-to-x", "m", "m", source_security_tag=_tag_ref("t1"), destination_security_tag=_tag_ref("tx"))
        check = _run_tags(components=self._COMPONENTS, tags=self._TAGS, rules=[rule])
        assert check._captured_errors == [
            "Rule 'a-to-x': destination tag 'tx' groups no component of its destination segment 'm'"
        ]

    def test_cross_segment_rule_tagged_on_its_microsegmented_side_passes(self) -> None:
        """A component dependency into a microsegmented segment names the target component."""
        rule = _rule("n-to-a", "n", "m", destination_security_tag=_tag_ref("t1"))
        check = _run_tags(segments=[_segment("n")], components=self._COMPONENTS, tags=self._TAGS, rules=[rule])
        assert check._captured_errors == []

    def test_tag_on_a_side_that_is_not_microsegmented_is_an_error(self) -> None:
        """A normal segment's hosts are all its segment group: a component tag there classifies nothing."""
        rule = _rule("a-to-n", "m", "n", source_security_tag=_tag_ref("t1"), destination_security_tag=_tag_ref("t2"))
        check = _run_tags(segments=[_segment("n")], components=self._COMPONENTS, tags=self._TAGS, rules=[rule])
        assert check._captured_errors == [
            "Rule 'a-to-n' carries a destination group tag but its destination segment 'n' is not microsegmented"
        ]


# ---------------------------------------------------------------------------
# Segment group tags and the contract builder consistency
# ---------------------------------------------------------------------------


def _segment_tag(tag_id: str, group_id: int, *segments: str, components: tuple[tuple[str, str], ...] = ()) -> dict:
    """One SecurityTag edge grouping ``segments`` (and, wrongly, ``components``: (fqdn, segment id))."""
    edge = _tag(tag_id, group_id, *components)
    edge["node"]["network_segments"] = {"edges": [{"node": {"id": seg, "name": _v(seg)}} for seg in segments]}
    return edge


def _policy_rule(name: str, dst: str | None, **extra: Any) -> dict[str, Any]:
    """One rule of a segment's own policy, as SegmentGroupPolicyFields selects it."""
    return {
        "node": {
            "id": f"rule-{name}",
            "index": _v(100),
            "name": _v(name),
            "action": _v("permit"),
            "protocol": _v("tcp"),
            "port_start": _v(443),
            "port_end": _v(None),
            "disabled": _v(False),
            "apply_on_switch": _v(False),
            "destination_segment": {"node": {"id": dst} if dst else None},
            **extra,
        }
    }


def _tenant(seg_id: str, tag_id: str | None, *rules: dict[str, Any], isolation: str = "normal") -> dict[str, Any]:
    """A routed PROD tenant segment with its group tag and its own rules (SegmentGroupPolicyFields)."""
    edge = _segment(seg_id, isolation_mode=_v(isolation))
    edge["node"]["gateway"]["node"]["ip_prefix"]["node"]["ip_namespace"] = {
        "node": {"name": _v("PROD"), "namespace_type": _v("prod")}
    }
    edge["node"]["owner"] = {"node": {"org_id": _v(seg_id.split("-")[0].upper())}}
    edge["node"]["security_tag"] = (
        {"node": {"id": tag_id, "name": _v(f"{tag_id}-name"), "group_id": _v(100)}} if tag_id else {"node": None}
    )
    edge["node"]["security_policy"] = {
        "node": {"name": _v(f"seg-{seg_id}-egress"), "enabled": _v(True), "rules": {"edges": list(rules)}}
    }
    return edge


class TestSegmentTags:
    """A segment tag groups exactly one segment, never components too."""

    def test_tag_of_one_segment_passes(self) -> None:
        check = _run_tags(segments=[_tenant("c1-a", "ta")], tags=[_segment_tag("ta", 30, "c1-a")])
        assert check._captured_errors == []
        assert check._captured_infos == []

    def test_tag_shared_by_two_segments_is_an_error(self) -> None:
        """One number for two segments would merge their groups: every contract of one opens the other."""
        check = _run_tags(
            segments=[_tenant("c1-a", "ta"), _tenant("c2-b", "ta")], tags=[_segment_tag("ta", 30, "c1-a", "c2-b")]
        )
        assert check._captured_errors == ["Security tag 'ta.example' is shared by 2 segments: ['c1-a', 'c2-b']"]

    def test_tag_of_a_segment_and_a_component_is_an_error(self) -> None:
        """What a tag groups is the relationship that holds it: never both."""
        check = _run_tags(
            components=[_tagged_component("a", "m", "ta")],
            segments=[_tenant("c1-a", "ta")],
            tags=[_segment_tag("ta", 30, "c1-a", components=(("a", "m"),))],
        )
        assert check._captured_errors == [
            "Security tag 'ta.example' groups both components ['a'] and segments ['c1-a']"
        ]


class TestReservedGroups:
    """A profile's reserved groups must sit outside the pool band a tag is drawn from."""

    def test_reserved_groups_outside_the_band_pass(self) -> None:
        profiles = {"edges": [_profile("reference-cisco-nxos", unclassified=0, unaware=15)]}
        check = _run_tags(tags=[_segment_tag("ta", 30, "c1-a")], profiles=profiles)
        assert check._captured_errors == []

    @pytest.mark.parametrize(
        ("reserved", "value", "profile"),
        [
            ("unclassified", 16, _profile("reference-cisco-nxos", unclassified=16)),
            ("unaware", 4095, _profile("reference-cisco-nxos", unaware=4095)),
        ],
    )
    def test_reserved_group_inside_the_band_is_an_error(self, reserved: str, value: int, profile: dict) -> None:
        """A drawn tag could then mean the reserved group on the wire."""
        profiles = {"edges": [profile]}
        check = _run_tags(tags=[_segment_tag("ta", 30, "c1-a")], profiles=profiles)
        assert check._captured_errors == [
            f"Group-policy profile 'reference-cisco-nxos' {reserved} group {value} is inside SGT-POOL 16-4095: "
            "a drawn tag could mean it"
        ]


class TestContracts:
    """Every rule between two groups is a row of the one contract builder."""

    _SEGMENTS = [
        _tenant("c8-portal", "t8", _policy_rule("c8-to-c5", "c5-web"), _policy_rule("c8-to-net", None)),
        _tenant(
            "c5-web",
            "t5",
            _policy_rule(
                "web-to-session",
                "c5-web",
                source_security_tag=_tag_ref("k-web"),
                destination_security_tag=_tag_ref("k-session"),
            ),
            isolation="microsegmented",
        ),
        _tenant("c9-other", None, _policy_rule("c9-to-c5", "c5-web")),
    ]
    _TAGS = [
        _segment_tag("t8", 30, "c8-portal"),
        _segment_tag("t5", 31, "c5-web"),
        _tag("k-web", 32, ("web", "c5-web")),
        _tag("k-session", 33, ("session", "c5-web")),
    ]
    _COMPONENTS = [_tagged_component("web", "c5-web", "k-web"), _tagged_component("session", "c5-web", "k-session")]

    def test_rules_between_groups_are_contract_rows(self) -> None:
        """The cross-customer rule and the intra-segment component rule are rows; untagged ends need none."""
        check = _run_tags(segments=self._SEGMENTS, tags=self._TAGS, components=self._COMPONENTS)
        assert check._captured_errors == []

    def test_hand_written_rule_into_a_microsegmented_segment_warns(self) -> None:
        """No component named: it opens the segment group and both components."""
        check = _run_tags(segments=self._SEGMENTS, tags=self._TAGS, components=self._COMPONENTS)
        assert [info for info in check._captured_infos if "into microsegmented" in info] == [
            "WARNING: rule 'c8-to-c5' into microsegmented segment 'c5-web' names no component: "
            "it opens all 3 groups of the segment",
            "WARNING: rule 'c9-to-c5' into microsegmented segment 'c5-web' names no component: "
            "it opens all 3 groups of the segment",
        ]

    def test_component_dependency_into_a_microsegmented_segment_is_a_row_without_warning(self) -> None:
        """The generator's rule names the target component: the row is to that component only."""
        segments = [
            _tenant(
                "c8-portal",
                "t8",
                _policy_rule("c8-to-c5-web", "c5-web", destination_security_tag=_tag_ref("k-web")),
            ),
            self._SEGMENTS[1],
        ]
        check = _run_tags(segments=segments, tags=self._TAGS, components=self._COMPONENTS)
        assert check._captured_errors == []
        assert not any("into microsegmented" in info for info in check._captured_infos)

    def test_a_component_row_the_builder_dropped_is_an_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The narrowed row is checked too: to the target component, not to the segment group."""
        monkeypatch.setattr("checks.segment_security.get_vrf_contracts", lambda segments, tags: [])
        segments = [
            _tenant(
                "c8-portal",
                "t8",
                _policy_rule("c8-to-c5-web", "c5-web", destination_security_tag=_tag_ref("k-web")),
            ),
            _tenant("c5-web", "t5", isolation="microsegmented"),
        ]
        check = _run_tags(segments=segments, tags=self._TAGS, components=self._COMPONENTS)
        assert check._captured_errors == [
            "Rule 'c8-to-c5-web' between groups t8-name -> k-web.example has no group contract row"
        ]

    def test_a_rule_the_builder_dropped_is_an_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Builder drift: group policy would deny what L0 permits."""
        monkeypatch.setattr("checks.segment_security.get_vrf_contracts", lambda segments, tags: [])
        check = _run_tags(segments=self._SEGMENTS, tags=self._TAGS, components=self._COMPONENTS)
        assert check._captured_errors == [
            "Rule 'web-to-session' between groups k-web.example -> k-session.example has no group contract row",
            "Rule 'c8-to-c5' between groups t8-name -> t5-name has no group contract row",
        ]


def test_all_demo_gateways_sit_in_their_environment_namespace() -> None:
    """The gateway-namespace rule run on the 30_all segments with an authored gateway (zones as declared; a
    missing one is derived from the environment by the generators, so only a declared exchange zone exempts a
    segment). A VXLAN tenant segment declares none: add_vxlan_segment allocates it from the pool of its
    environment's namespace (tests/unit/test_tenant_segment_addressing.py holds that pool choice)."""
    from tests.integration.demo_data import ROOT, declared, declared_in, name

    namespace_types = {
        namespace.item["name"]: namespace.item.get("namespace_type")
        for namespace in declared("IpamNamespace", ROOT / "data" / "bootstrap")
    }
    check = _check()
    checked = 0
    for obj in declared_in(ROOT / "data" / "demos" / "30_all"):
        gateway = (obj.item.get("gateway") or {}).get("data") or {}
        if obj.kind not in {"ManagedVlanSegment", "ManagedVxlanSegment"} or not gateway:
            continue
        prefix = (gateway.get("ip_prefix") or {}).get("data") or {}
        checked += 1
        check._validate_gateway_namespace(
            {
                "typename": obj.kind,
                "name": name(obj),
                "environment": obj.value("environment"),
                "security_zone": {"name": obj.item.get("security_zone")},
                "gateway": {
                    "ip_prefix": {
                        "prefix": prefix.get("prefix"),
                        "ip_namespace": {
                            "name": prefix.get("ip_namespace"),
                            "namespace_type": namespace_types.get(prefix.get("ip_namespace")),
                        },
                    }
                },
            }
        )
    assert checked, "the scan found no authored gateway (colocation VLAN segments, the MANAGEMENT services)"
    assert check._captured_errors == []
