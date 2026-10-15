"""Unit tests for RulesPlanner (generators/helpers/rules.py) — pure logic,
no generator instance needed.

Covers rule naming (one rule per dependency and port), rule descriptions
(including target_fqdn external targets), dependency authorization (owner
approval, prod/non-prod separation),
rule-payload building, zone context, and the security_profile-driven mappings
(pick_zone_name).
"""

from __future__ import annotations

import pytest

from generators.helpers.rules import RulesPlanner

# ===========================================================================
# TestRuleName
# ===========================================================================


class TestRuleName:
    """A rule is named after its dependency, so each dependency gets its own rule."""

    SRC = {"label": "backend"}
    DST = {"label": "cache"}

    def test_rule_is_named_after_the_dependency(self) -> None:
        """The dependency name is the rule name."""
        dep = {"name": "c001-checkout-backend-to-cache-redis"}
        assert RulesPlanner.rule_name("c001-checkout-p", self.SRC, self.DST, dep) == dep["name"]

    def test_two_dependencies_between_one_pair_get_two_rules(self) -> None:
        """tcp/6379 and udp/30000-30010 from backend to cache no longer collapse into one rule."""
        tcp = RulesPlanner.rule_name("app", self.SRC, self.DST, {"name": "backend-to-cache-redis"})
        udp = RulesPlanner.rule_name("app", self.SRC, self.DST, {"name": "backend-to-cache-gossip"})
        assert tcp != udp

    def test_dependency_name_is_normalized(self) -> None:
        """Spaces and upper case are normalized like the component labels are."""
        assert RulesPlanner.rule_name("app", self.SRC, self.DST, {"name": " Web To API "}) == "web-to-api"

    def test_unnamed_dependency_falls_back_to_the_component_pair(self) -> None:
        """Without a dependency name the rule keeps the app-src-to-dst name."""
        assert RulesPlanner.rule_name("app", self.SRC, self.DST, {}) == "app-backend-to-cache"
        assert RulesPlanner.rule_name("app", self.SRC, self.DST) == "app-backend-to-cache"

    def test_unnamed_dependency_falls_back_to_component_fqdn(self) -> None:
        """Without a label the component fqdn names the pair (fqdn replaced slug)."""
        src = {"fqdn": "web.checkout.example", "name": "web"}
        dst = {"fqdn": "api.checkout.example", "name": "api"}
        assert RulesPlanner.rule_name("app", src, dst) == "app-web.checkout.example-to-api.checkout.example"

    def test_unnamed_dependency_falls_back_to_component_name(self) -> None:
        """Without label or fqdn the component name is used."""
        assert RulesPlanner.rule_name("app", {"name": "web"}, {"name": "api"}) == "app-web-to-api"

    @pytest.mark.parametrize(
        ("port", "expected"),
        [
            (("tcp", 8443, None), "backend-to-cache-tcp-8443"),
            (("udp", 30000, 30010), "backend-to-cache-udp-30000-30010"),
        ],
        ids=["single-port", "port-range"],
    )
    def test_port_is_suffixed_onto_the_dependency_name(self, port: tuple[str, int, int | None], expected: str) -> None:
        """One rule per port: the port, slash and dash both as dashes, follows the dependency name."""
        dep = {"name": "backend-to-cache"}
        assert RulesPlanner.rule_name("app", self.SRC, self.DST, dep, port) == expected

    def test_port_is_suffixed_onto_the_component_pair_fallback(self) -> None:
        """An unnamed dependency still gets a per-port name."""
        assert RulesPlanner.rule_name("app", self.SRC, self.DST, None, ("tcp", 6379, None)) == (
            "app-backend-to-cache-tcp-6379"
        )

    def test_each_port_of_one_dependency_gets_its_own_rule_name(self) -> None:
        """tcp/6379 and udp/30000-30010 of one dependency do not collide."""
        dep = {"name": "backend-to-cache"}
        names = {
            RulesPlanner.rule_name("app", self.SRC, self.DST, dep, port)
            for port in (("tcp", 6379, None), ("udp", 30000, 30010), ("udp", 6379, None))
        }
        assert len(names) == 3

    def test_no_port_keeps_the_bare_dependency_name(self) -> None:
        """The proxy path (one rule per dependency) passes no port and gets no suffix."""
        assert RulesPlanner.rule_name("app", self.SRC, {}, {"name": "backend-to-stripe"}) == "backend-to-stripe"


# ===========================================================================
# TestRuleDescription
# ===========================================================================


class TestRuleDescription:
    """The generated description names both ends; an external target is its fqdn."""

    SRC = {"name": "backend", "component_type": "backend"}

    def test_component_target_description(self) -> None:
        """Both component names and types appear."""
        dst = {"name": "db", "component_type": "database"}
        assert RulesPlanner.rule_description({}, self.SRC, dst) == "Auto-generated: backend (backend) -> db (database)"

    def test_target_fqdn_description_names_the_external_host(self) -> None:
        """No target component: the fqdn is the destination and its type is external."""
        dep = {"target_fqdn": "api.stripe.com"}
        assert RulesPlanner.rule_description(dep, self.SRC, {}) == (
            "Auto-generated: backend (backend) -> api.stripe.com (external)"
        )

    def test_explicit_description_wins_over_target_fqdn(self) -> None:
        """A hand-written description is used as-is."""
        dep = {"target_fqdn": "api.stripe.com", "description": "Card payments"}
        assert RulesPlanner.rule_description(dep, self.SRC, {}) == "Card payments"

    def test_missing_target_and_fqdn_defaults_to_unknown_backend(self) -> None:
        """Nothing about the destination known: placeholder name, backend type."""
        assert RulesPlanner.rule_description({}, self.SRC, {}) == "Auto-generated: backend (backend) -> ? (backend)"


# ===========================================================================
# TestOwnerAuthorization
# ===========================================================================


def _comp(app: str, owner: str, environment: str | None = None, *, segment: dict | None = None) -> dict:
    """A component of `app`; `environment` is the application's, `segment` its network segment."""
    parent: dict = {"name": app, "owner": {"org_id": owner}}
    if environment:
        parent["environment"] = environment
    comp: dict = {"parent": parent}
    if segment is not None:
        comp["network_segment"] = segment
    return comp


def _seg(environment: str | None, zone: str | None = None) -> dict:
    """A network segment in `environment`, optionally a member of security zone `zone`."""
    seg: dict = {"name": f"seg-{environment}"}
    if environment is not None:
        seg["environment"] = environment
    if zone:
        seg["security_zone"] = {"name": zone}
    return seg


class TestOwnerAuthorization:
    """Only the destination owner's approval opens a flow; inside one owner
    every flow is auto-approved."""

    @pytest.mark.parametrize(
        ("src", "dst"),
        [
            (_comp("checkout", "C001"), _comp("checkout", "C001")),
            (_comp("checkout", "C001"), _comp("authentication", "C001")),
            (_comp("checkout", "C001", "s"), _comp("checkout", "C001", "d")),
        ],
        ids=["same-app", "cross-app", "cross-non-prod-environment"],
    )
    def test_same_owner_is_auto_authorized(self, src: dict, dst: dict) -> None:
        """A flow inside one owner needs no approval."""
        allowed, reason = RulesPlanner.dependency_is_authorized(
            src_comp=src, dst_comp=dst, dep={"access_status": "auto"}
        )

        assert allowed is True
        assert reason is None

    @pytest.mark.parametrize("status", ["auto", "pending"])
    def test_cross_owner_waits_for_approval(self, status: str) -> None:
        """Calling another owner's application is held until that owner approves."""
        allowed, reason = RulesPlanner.dependency_is_authorized(
            src_comp=_comp("checkout", "C001"), dst_comp=_comp("ledger", "C002"), dep={"access_status": status}
        )

        assert allowed is False
        assert reason is not None
        assert "cross-owner flow C001->C002 requires access_status=approved" in reason

    def test_cross_owner_allowed_once_approved(self) -> None:
        """The destination owner's approval opens the flow."""
        allowed, reason = RulesPlanner.dependency_is_authorized(
            src_comp=_comp("checkout", "C001"), dst_comp=_comp("ledger", "C002"), dep={"access_status": "approved"}
        )

        assert allowed is True
        assert reason is None

    def test_denied_blocks_even_inside_one_owner(self) -> None:
        """An explicit denial overrides the same-owner auto-approval."""
        allowed, reason = RulesPlanner.dependency_is_authorized(
            src_comp=_comp("checkout", "C001"), dst_comp=_comp("checkout", "C001"), dep={"access_status": "denied"}
        )

        assert allowed is False
        assert reason == "explicitly denied"


# ===========================================================================
# TestCrossEnvironmentAuthorization
# ===========================================================================


class TestCrossEnvironmentAuthorization:
    """Prod and non-prod are never joined; the decision is made from each
    component's own segment, the application environment is governance only."""

    @pytest.mark.parametrize(
        ("src_owner", "dst_owner", "src_app", "dst_app"),
        [
            ("C001", "C001", "checkout", "checkout"),
            ("C001", "C001", "checkout", "authentication"),
            ("C001", "C002", "checkout", "ledger"),
        ],
        ids=["same-app", "cross-app", "cross-owner"],
    )
    @pytest.mark.parametrize(("src_env", "dst_env"), [("p", "s"), ("d", "p")], ids=["p-to-s", "d-to-p"])
    def test_prod_and_non_prod_segments_are_never_joined_even_when_approved(
        self, src_owner: str, dst_owner: str, src_app: str, dst_app: str, src_env: str, dst_env: str
    ) -> None:
        """An approval opens a cross-owner flow, never prod <-> non-prod segments."""
        src_comp = _comp(src_app, src_owner, src_env, segment=_seg(src_env))
        dst_comp = _comp(dst_app, dst_owner, dst_env, segment=_seg(dst_env))
        dep = {"access_status": "approved"}

        allowed, reason = RulesPlanner.dependency_is_authorized(src_comp=src_comp, dst_comp=dst_comp, dep=dep)

        assert allowed is False
        assert reason is not None
        assert "joins prod and non-prod" in reason

    def test_prod_app_on_non_prod_segment_is_blocked(self) -> None:
        """Both applications say prod, but a segment is non-prod: the segment decides."""
        src_comp = _comp("checkout", "C001", "p", segment=_seg("p"))
        dst_comp = _comp("checkout", "C001", "p", segment=_seg("s"))

        allowed, reason = RulesPlanner.dependency_is_authorized(
            src_comp=src_comp, dst_comp=dst_comp, dep={"access_status": "auto"}
        )

        assert allowed is False
        assert reason is not None

    def test_differing_app_environments_on_same_namespace_type_segments_are_allowed(self) -> None:
        """The application environment is governance input only: two non-prod segments connect."""
        src_comp = _comp("checkout", "C001", "p", segment=_seg("s"))
        dst_comp = _comp("checkout", "C001", "d", segment=_seg("d"))

        allowed, reason = RulesPlanner.dependency_is_authorized(
            src_comp=src_comp, dst_comp=dst_comp, dep={"access_status": "auto"}
        )

        assert allowed is True
        assert reason is None

    def test_same_environment_is_auto_authorized(self) -> None:
        """Two prod segments connect."""
        src_comp = _comp("checkout", "C001", "p", segment=_seg("p"))
        dst_comp = _comp("checkout", "C001", "p", segment=_seg("p"))

        allowed, reason = RulesPlanner.dependency_is_authorized(
            src_comp=src_comp, dst_comp=dst_comp, dep={"access_status": "auto"}
        )

        assert allowed is True
        assert reason is None

    @pytest.mark.parametrize("missing_side", ["src", "dst"])
    def test_segment_without_environment_fails_closed(self, missing_side: str) -> None:
        """An unknown segment environment is never assumed to be safe."""
        src_comp = _comp("checkout", "C001", segment=_seg(None if missing_side == "src" else "p"))
        dst_comp = _comp("checkout", "C001", segment=_seg(None if missing_side == "dst" else "p"))

        allowed, reason = RulesPlanner.dependency_is_authorized(
            src_comp=src_comp, dst_comp=dst_comp, dep={"access_status": "auto"}
        )

        assert allowed is False
        assert reason is not None
        assert "unknown" in reason

    @pytest.mark.parametrize("zone", ["INTERNET-ZONE", "MANAGEMENT-ZONE"])
    def test_exchange_zone_segment_is_exempt(self, zone: str) -> None:
        """An INTERNET/MANAGEMENT segment is governed by EXCHANGE_PEERS, not by its environment."""
        src_comp = _comp("checkout", "C001", "p", segment=_seg("p"))
        dst_comp = _comp("checkout", "C001", "p", segment=_seg("s", zone))

        allowed, reason = RulesPlanner.dependency_is_authorized(
            src_comp=src_comp, dst_comp=dst_comp, dep={"access_status": "auto"}
        )

        assert allowed is True
        assert reason is None

    def test_flow_without_segments_is_not_compared(self) -> None:
        """A flow with no segment on a side (proxy to an FQDN) has no namespaces to join."""
        src_comp = _comp("checkout", "C001", "p", segment=_seg("p"))
        dst_comp = _comp("checkout", "C001", "s")

        allowed, reason = RulesPlanner.dependency_is_authorized(
            src_comp=src_comp, dst_comp=dst_comp, dep={"access_status": "auto"}
        )

        assert allowed is True
        assert reason is None


# ===========================================================================
# TestBuildRulePayload / TestZoneContext
# ===========================================================================


class TestBuildRulePayload:
    def test_planner_build_rule_payload_contains_governance_and_switch_flag(self):
        dep = {"description": None, "access_status": "approved", "decision_reason": "ticket-123"}
        src_comp = {
            "name": "frontend",
            "component_type": "frontend",
            "parent": {"owner": {"org_id": "C001"}},
        }
        dst_comp = {
            "name": "api",
            "component_type": "backend",
            "parent": {"owner": {"org_id": "C002"}},
        }
        src_seg = {"id": "seg-src", "isolation_mode": "normal"}
        dst_seg = {"id": "seg-dst", "isolation_mode": "microsegmented"}

        payload = RulesPlanner.build_rule_payload(
            policy_id="policy-1",
            rule_name="rule-1",
            dep=dep,
            src_comp=src_comp,
            dst_comp=dst_comp,
            src_seg=src_seg,
            dst_seg=dst_seg,
            protocol="tcp",
            port_start=443,
            port_end=None,
            cross_zone=True,
        )

        assert payload["policy"] == {"id": "policy-1"}
        assert payload["source_segment"] == {"id": "seg-src"}
        assert payload["destination_segment"] == {"id": "seg-dst"}
        assert payload["apply_on_switch"] is True
        assert payload["port_start"] == 443
        assert "governance:" in payload["description"]


class TestRuleTags:
    """A rule carries its component's group tag on every side whose segment is microsegmented."""

    _SEG = {"id": "seg-m", "isolation_mode": "microsegmented"}
    _SRC = {"name": "scoring-api", "security_tag": {"id": "tag-a"}}
    _DST = {"name": "feature-cache", "security_tag": {"id": "tag-b"}}

    def test_intra_segment_rule_carries_both_tags(self) -> None:
        tags = RulesPlanner.rule_tags(src_comp=self._SRC, dst_comp=self._DST, src_seg=self._SEG, dst_seg=self._SEG)
        assert tags == {"source_security_tag": {"id": "tag-a"}, "destination_security_tag": {"id": "tag-b"}}

    def test_untagged_side_is_left_out(self) -> None:
        tags = RulesPlanner.rule_tags(
            src_comp=self._SRC, dst_comp={"name": "feature-cache"}, src_seg=self._SEG, dst_seg=self._SEG
        )
        assert tags == {"source_security_tag": {"id": "tag-a"}}

    def test_cross_segment_rule_tags_each_microsegmented_side(self) -> None:
        """Both segments microsegmented: the contract is narrowed to the two components."""
        other = {"id": "seg-o", "isolation_mode": "microsegmented"}
        tags = RulesPlanner.rule_tags(src_comp=self._SRC, dst_comp=self._DST, src_seg=self._SEG, dst_seg=other)
        assert tags == {"source_security_tag": {"id": "tag-a"}, "destination_security_tag": {"id": "tag-b"}}

    def test_cross_segment_rule_into_a_microsegmented_segment_tags_only_the_target(self) -> None:
        """C008 (normal) -> C005 web (microsegmented): the dependency opens the target component only."""
        normal = {"id": "seg-n", "isolation_mode": "normal"}
        tags = RulesPlanner.rule_tags(src_comp=self._SRC, dst_comp=self._DST, src_seg=normal, dst_seg=self._SEG)
        assert tags == {"destination_security_tag": {"id": "tag-b"}}

    def test_cross_segment_rule_out_of_a_microsegmented_segment_tags_only_the_source(self) -> None:
        normal = {"id": "seg-n", "isolation_mode": "normal"}
        tags = RulesPlanner.rule_tags(src_comp=self._SRC, dst_comp=self._DST, src_seg=self._SEG, dst_seg=normal)
        assert tags == {"source_security_tag": {"id": "tag-a"}}

    def test_side_without_a_segment_carries_no_tag(self) -> None:
        """A proxy/selector side has no segment, so no group to narrow to."""
        assert RulesPlanner.rule_tags(src_comp=self._SRC, dst_comp=self._DST, src_seg={}, dst_seg={}) == {}

    def test_segment_that_is_not_microsegmented_carries_no_tag(self) -> None:
        """A stale tag read from a component on a normal segment is never written to a rule."""
        seg = {"id": "seg-n", "isolation_mode": "normal"}
        assert RulesPlanner.rule_tags(src_comp=self._SRC, dst_comp=self._DST, src_seg=seg, dst_seg=seg) == {}

    def test_payload_of_an_intra_segment_rule_includes_the_tags(self) -> None:
        payload = RulesPlanner.build_rule_payload(
            policy_id="policy-1",
            rule_name="scoring-to-cache-tcp-6379",
            dep={"description": None, "access_status": "auto"},
            src_comp=self._SRC,
            dst_comp=self._DST,
            src_seg=self._SEG,
            dst_seg=self._SEG,
            protocol="tcp",
            port_start=6379,
            port_end=None,
            cross_zone=False,
        )
        assert payload["source_security_tag"] == {"id": "tag-a"}
        assert payload["destination_security_tag"] == {"id": "tag-b"}


class TestZoneContext:
    def test_planner_zone_context_handles_missing_zone_as_cross_zone(self):
        src_seg = {"security_zone": {"name": "internal"}}
        dst_seg = {}
        src_zone, dst_zone, cross_zone = RulesPlanner.zone_context(src_seg=src_seg, dst_seg=dst_seg, dep={})

        assert src_zone == "internal"
        assert dst_zone is None
        assert cross_zone is True


# ===========================================================================
# TestPickZoneName / TestZoneSeed
# ===========================================================================


class TestPickZoneName:
    def test_pick_zone_name_maps_production_environment(self):
        assert RulesPlanner.pick_zone_name("p") == "PROD-ZONE"

    def test_pick_zone_name_maps_every_non_production_environment_to_nonprod(self):
        for environment in ("n", "s", "d", "t"):
            assert RulesPlanner.pick_zone_name(environment) == "NONPROD-ZONE"


# ===========================================================================
# TestPickProfileName
# ===========================================================================


class TestPickProfileName:
    """fintech_strict is a data-sensitivity/compliance requirement (always
    scan for malware/DLP) independent of network topology, unlike
    internet_exposed's "strict" handling, which is specifically about the
    perimeter/exposure boundary and so stays gated on cross_zone."""

    def test_fintech_strict_is_always_strict_even_same_zone(self):
        assert RulesPlanner.pick_profile_name("fintech_strict", cross_zone=False) == "strict"

    def test_fintech_strict_is_strict_across_zones_too(self):
        assert RulesPlanner.pick_profile_name("fintech_strict", cross_zone=True) == "strict"

    def test_internet_exposed_is_strict_only_when_crossing_zones(self):
        assert RulesPlanner.pick_profile_name("internet_exposed", cross_zone=True) == "strict"
        assert RulesPlanner.pick_profile_name("internet_exposed", cross_zone=False) is None

    def test_internal_standard_is_standard_only_when_crossing_zones(self):
        assert RulesPlanner.pick_profile_name("internal_standard", cross_zone=True) == "standard"
        assert RulesPlanner.pick_profile_name("internal_standard", cross_zone=False) is None

    def test_unknown_profile_falls_back_to_standard_when_crossing_zones(self):
        assert RulesPlanner.pick_profile_name("unknown", cross_zone=True) == "standard"
        assert RulesPlanner.pick_profile_name("unknown", cross_zone=False) is None


# ===========================================================================
# TestPickIsolationMode
# ===========================================================================


class TestPickIsolationMode:
    def test_fintech_strict_gets_microsegmented(self):
        assert RulesPlanner.pick_isolation_mode("fintech_strict") == "microsegmented"

    def test_every_other_profile_gets_normal(self):
        for profile in ("internal_standard", "internet_exposed", "unknown"):
            assert RulesPlanner.pick_isolation_mode(profile) == "normal"
