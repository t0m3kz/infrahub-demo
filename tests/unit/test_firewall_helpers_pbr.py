"""Unit tests for the customer PBR rules in transforms/helpers/firewall_pbr.py."""

from tests.unit.firewall_helpers_shared import _make_pbr_activation
from transforms.helpers.firewall_contexts import _flatten_deployment_firewall_contexts
from transforms.helpers.firewall_pbr import get_customer_pbr_rules


def _make_pbr_rule(*, action: str = "permit", dst_prefix: str | None = "10.0.2.0/24", disabled: bool = False) -> dict:
    rule: dict = {"action": action, "disabled": disabled}
    if dst_prefix:
        rule["destination_segment"] = {"gateway": {"ip_prefix": {"prefix": dst_prefix}}}
    return rule


def _make_context_leg(
    *, fw_ip: str = "10.65.0.5/29", tenant_id: str | None = None, served: list[str] | None = None
) -> dict:
    """A ManagedFirewallContext dict as returned by
    _flatten_deployment_firewall_contexts() — device-scoped (this device's
    own deployment's firewall-role devices), not the rendered device's own
    interfaces. The firewall's OWN leg is what provides the nexthop, found
    by filtering capabilities legs to the one on a device with
    role="firewall"."""
    ctx: dict = {
        "capabilities": [
            {"device": {"role": "firewall"}, "ip_address": {"address": fw_ip, "ip_namespace": {"name": "PROD"}}}
        ],
    }
    if tenant_id:
        ctx["tenant"] = {"id": tenant_id}
    if served is not None:
        ctx["served_deployments"] = [{"id": d} for d in served]
    return ctx


# ===========================================================================
# _flatten_deployment_firewall_contexts()
# ===========================================================================


def _make_ha_capability(*, contexts: list[dict] | None = None) -> dict:
    return {"typename": "ManagedFirewallHA", "contexts": contexts or []}


def _make_fw_device(*, contexts: list[dict] | None = None, extra_capability: dict | None = None) -> dict:
    capabilities = [_make_ha_capability(contexts=contexts)]
    if extra_capability:
        capabilities.append(extra_capability)
    return {"capabilities": capabilities}


class TestFlattenDeploymentFirewallContexts:
    def test_none_deployment_returns_empty(self) -> None:
        assert _flatten_deployment_firewall_contexts(None) == []

    def test_empty_deployment_returns_empty(self) -> None:
        assert _flatten_deployment_firewall_contexts({}) == []

    def test_dc_tier_device_contexts_extracted(self) -> None:
        """DC-tier devices (border-leaf, firewall, spine) have contexts directly
        under deployment.devices — no parent hop needed."""
        ctx = {"id": "ctx-1"}
        deployment = {"devices": [_make_fw_device(contexts=[ctx])]}
        assert _flatten_deployment_firewall_contexts(deployment) == [ctx]

    def test_pod_tier_device_contexts_extracted_via_parent(self) -> None:
        """Pod-tier devices (leaf/tor/access-leaf) have their deployment set to
        the POD, so contexts live under deployment.parent.devices instead."""
        ctx = {"id": "ctx-1"}
        deployment = {"devices": [], "parent": {"devices": [_make_fw_device(contexts=[ctx])]}}
        assert _flatten_deployment_firewall_contexts(deployment) == [ctx]

    def test_non_firewall_ha_capability_ignored(self) -> None:
        deployment = {
            "devices": [_make_fw_device(contexts=[{"id": "ctx-1"}], extra_capability={"typename": "ManagedBGP"})]
        }
        result = _flatten_deployment_firewall_contexts(deployment)
        assert result == [{"id": "ctx-1"}]

    def test_dedup_across_dc_and_pod_tier_paths(self) -> None:
        """A context reachable both directly and via parent must not double-count."""
        ctx = {"id": "ctx-1"}
        deployment = {
            "devices": [_make_fw_device(contexts=[ctx])],
            "parent": {"devices": [_make_fw_device(contexts=[ctx])]},
        }
        result = _flatten_deployment_firewall_contexts(deployment)
        assert result == [ctx]

    def test_multiple_contexts_across_devices(self) -> None:
        ctx_a, ctx_b = {"id": "ctx-a"}, {"id": "ctx-b"}
        deployment = {"devices": [_make_fw_device(contexts=[ctx_a]), _make_fw_device(contexts=[ctx_b])]}
        result = _flatten_deployment_firewall_contexts(deployment)
        assert {c["id"] for c in result} == {"ctx-a", "ctx-b"}


class TestGetCustomerPbrRules:
    def test_none_activations_returns_empty(self) -> None:
        assert get_customer_pbr_rules(None, [_make_context_leg()]) == []

    def test_no_firewall_context_leg_returns_empty(self) -> None:
        activations = [_make_pbr_activation(security_policies=[])]
        assert get_customer_pbr_rules(activations, []) == []

    def test_segment_without_security_policies_key_is_skipped(self) -> None:
        """Missing 'security_policy' key (not queried) means no PBR rule — same
        gate get_acls() uses."""
        activations = [_make_pbr_activation(security_policies=None)]
        contexts = [_make_context_leg()]
        assert get_customer_pbr_rules(activations, contexts) == []

    def test_shared_context_nexthop_used_when_no_tenant(self) -> None:
        activations = [_make_pbr_activation(deployment_id="dep-a", security_policies=[])]
        contexts = [_make_context_leg(fw_ip="10.65.0.5/29")]
        result = get_customer_pbr_rules(activations, contexts)
        assert len(result) == 1
        assert result[0]["fw_nexthop"] == "10.65.0.4"
        assert result[0]["vlan_id"] == 100
        assert result[0]["bypass_prefixes"] == []

    def test_one_rule_per_vlan_sorted_by_vlan_id(self) -> None:
        """Repeated activations of one VLAN yield one rule, and the rules come out in VLAN order."""
        activations = [
            _make_pbr_activation(vlan_id=300, security_policies=[]),
            _make_pbr_activation(vlan_id=100, security_policies=[]),
            _make_pbr_activation(vlan_id=300, security_policies=[]),
        ]
        result = get_customer_pbr_rules(activations, [_make_context_leg()])
        assert [rule["vlan_id"] for rule in result] == [100, 300]

    def test_dedicated_context_nexthop_preferred_over_shared(self) -> None:
        activations = [_make_pbr_activation(deployment_id="dep-a", security_policies=[])]
        contexts = [
            _make_context_leg(fw_ip="10.65.0.5/29"),  # shared
            _make_context_leg(fw_ip="10.66.0.5/29", tenant_id="dep-a"),  # dedicated
        ]
        result = get_customer_pbr_rules(activations, contexts)
        assert result[0]["fw_nexthop"] == "10.66.0.4"

    def test_dedicated_context_for_other_tenant_not_used(self) -> None:
        """A dedicated context for a DIFFERENT deployment must not leak as this
        segment's nexthop — falls back to shared (or nothing) instead."""
        activations = [_make_pbr_activation(deployment_id="dep-a", security_policies=[])]
        contexts = [_make_context_leg(fw_ip="10.66.0.5/29", tenant_id="dep-b")]
        assert get_customer_pbr_rules(activations, contexts) == []

    def test_stretched_vxlan_segment_matches_by_any_of_its_deployments(self) -> None:
        """VxlanSegment.customer_deployments is cardinality many (stretch across
        DCs) — a dedicated context keyed to ANY one of them must match, not
        just the first."""
        activations = [
            {
                "vlan_id": 100,
                "segment": {
                    "id": "seg-1",
                    "name": "seg-1",
                    "customer_name": "web",
                    "customer_deployments": [{"id": "dep-a"}, {"id": "dep-b"}],
                    "security_policy": None,
                    "gateway": {"ip_prefix": {"prefix": "10.0.1.0/24", "ip_namespace": {"name": "PROD"}}},
                },
            }
        ]
        contexts = [_make_context_leg(fw_ip="10.66.0.5/29", tenant_id="dep-b")]
        result = get_customer_pbr_rules(activations, contexts)
        assert result[0]["fw_nexthop"] == "10.66.0.4"

    def test_owner_org_id_never_matches_tenant_deployment_id(self) -> None:
        """Regression: FirewallContext.tenant peers TopologyCustomer (a
        deployment footprint), not OrganizationCustomer (the org) — segment.owner
        must never be used for this match even if it happens to look like an id."""
        activations = [
            {
                "vlan_id": 100,
                "segment": {
                    "id": "seg-1",
                    "name": "seg-1",
                    "customer_name": "web",
                    "owner": {"id": "dep-a"},
                    "customer_deployments": [],
                    "security_policy": None,
                },
            }
        ]
        contexts = [_make_context_leg(fw_ip="10.66.0.5/29", tenant_id="dep-a")]
        assert get_customer_pbr_rules(activations, contexts) == []

    def test_context_without_firewall_leg_is_skipped(self) -> None:
        """inline connectivity_mode never allocates an IP on the firewall leg —
        a context with no addressed firewall-role leg contributes no nexthop."""
        activations = [_make_pbr_activation(deployment_id="dep-a", security_policies=[])]
        contexts = [{"capabilities": [{"device": {"role": "firewall"}, "ip_address": {}}]}]
        assert get_customer_pbr_rules(activations, contexts) == []

    def test_permit_rule_becomes_bypass_prefix(self) -> None:
        policies = [{"enabled": True, "rules": [_make_pbr_rule(dst_prefix="10.0.2.0/24")]}]
        activations = [_make_pbr_activation(security_policies=policies)]
        contexts = [_make_context_leg()]
        result = get_customer_pbr_rules(activations, contexts)
        assert result[0]["bypass_prefixes"] == ["10.0.2.0/24"]

    def test_deny_rule_is_not_a_bypass(self) -> None:
        policies = [{"enabled": True, "rules": [_make_pbr_rule(action="deny", dst_prefix="10.0.2.0/24")]}]
        activations = [_make_pbr_activation(security_policies=policies)]
        contexts = [_make_context_leg()]
        result = get_customer_pbr_rules(activations, contexts)
        assert result[0]["bypass_prefixes"] == []

    def test_disabled_rule_is_not_a_bypass(self) -> None:
        policies = [{"enabled": True, "rules": [_make_pbr_rule(disabled=True)]}]
        activations = [_make_pbr_activation(security_policies=policies)]
        contexts = [_make_context_leg()]
        result = get_customer_pbr_rules(activations, contexts)
        assert result[0]["bypass_prefixes"] == []

    def test_disabled_policy_contributes_no_bypass(self) -> None:
        policies = [{"enabled": False, "rules": [_make_pbr_rule()]}]
        activations = [_make_pbr_activation(security_policies=policies)]
        contexts = [_make_context_leg()]
        result = get_customer_pbr_rules(activations, contexts)
        assert result[0]["bypass_prefixes"] == []

    def test_permit_to_the_internet_zone_is_no_bypass(self) -> None:
        """The internet has no prefix: the leaf keeps redirecting to the firewall."""
        rule = {"action": "permit", "disabled": False, "destination_zone": {"name": "INTERNET-ZONE"}}
        policies = [{"enabled": True, "rules": [rule]}]
        activations = [_make_pbr_activation(security_policies=policies)]
        result = get_customer_pbr_rules(activations, [_make_context_leg()])
        assert result[0]["fw_nexthop"] == "10.65.0.4"  # the VIP of the leg's /29
        assert result[0]["bypass_prefixes"] == []

    def test_cross_owner_permit_still_bypasses(self) -> None:
        """A permit rule bypasses PBR regardless of whether source/destination
        share an owner — intra- and inter-customer traffic use one model."""
        policies = [{"enabled": True, "rules": [_make_pbr_rule(dst_prefix="10.9.0.0/24")]}]
        activations = [_make_pbr_activation(deployment_id="dep-a", security_policies=policies)]
        contexts = [_make_context_leg()]
        result = get_customer_pbr_rules(activations, contexts)
        assert "10.9.0.0/24" in result[0]["bypass_prefixes"]

    @staticmethod
    def _with_inbound(*inbound: dict) -> list[dict]:
        activation = _make_pbr_activation(security_policies=[])
        activation["segment"]["inbound_rules"] = list(inbound)
        return [activation]

    @staticmethod
    def _inbound(
        src_prefix: str | None = "10.0.1.0/24", action: str = "permit", disabled: bool = False, enabled: bool = True
    ) -> dict:
        src = {"id": "seg-a", "gateway": {"ip_prefix": {"prefix": src_prefix}}} if src_prefix else None
        return {"action": action, "disabled": disabled, "policy": {"enabled": enabled}, "source_segment": src}

    def test_inbound_permit_source_is_bypassed_for_the_reply(self) -> None:
        """A -> B bypassed the firewall on A's leaf, so B's reply to A bypasses it too."""
        result = get_customer_pbr_rules(self._with_inbound(self._inbound()), [_make_context_leg()])
        assert result[0]["bypass_prefixes"] == ["10.0.1.0/24"]

    def test_inactive_inbound_rules_are_not_bypassed(self) -> None:
        inbound = (
            self._inbound(src_prefix="10.0.1.0/24", action="deny"),
            self._inbound(src_prefix="10.0.3.0/24", disabled=True),
            self._inbound(src_prefix="10.0.4.0/24", enabled=False),
            self._inbound(src_prefix=None),
        )
        result = get_customer_pbr_rules(self._with_inbound(*inbound), [_make_context_leg()])
        assert result[0]["bypass_prefixes"] == []

    def test_own_and_inbound_bypasses_merge_without_duplicates(self) -> None:
        activations = self._with_inbound(self._inbound(src_prefix="10.0.2.0/24"))
        activations[0]["segment"]["security_policy"] = {
            "enabled": True,
            "rules": [_make_pbr_rule(dst_prefix="10.0.2.0/24")],
        }
        result = get_customer_pbr_rules(activations, [_make_context_leg()])
        assert result[0]["bypass_prefixes"] == ["10.0.2.0/24"]

    def test_duplicate_vlan_deduplicated(self) -> None:
        activations = [
            _make_pbr_activation(vlan_id=100, security_policies=[]),
            _make_pbr_activation(vlan_id=100, security_policies=[]),
        ]
        contexts = [_make_context_leg()]
        result = get_customer_pbr_rules(activations, contexts)
        assert len(result) == 1

    def test_results_sorted_by_vlan_id(self) -> None:
        activations = [
            _make_pbr_activation(vlan_id=200, security_policies=[]),
            _make_pbr_activation(vlan_id=100, security_policies=[]),
        ]
        contexts = [_make_context_leg()]
        result = get_customer_pbr_rules(activations, contexts)
        assert [r["vlan_id"] for r in result] == [100, 200]

    def test_customer_name_and_environment_passed_through(self) -> None:
        activations = [_make_pbr_activation(security_policies=[], environment="s")]
        contexts = [_make_context_leg()]
        result = get_customer_pbr_rules(activations, contexts)
        assert result[0]["customer_name"] == "web"
        assert result[0]["environment"] == "s"

    def test_environment_none_when_absent(self) -> None:
        activations = [_make_pbr_activation(security_policies=[])]
        contexts = [_make_context_leg()]
        result = get_customer_pbr_rules(activations, contexts)
        assert result[0]["environment"] is None


def _peer(prefix: str, deployment_id: str | None) -> dict:
    peer: dict = {"id": f"seg-{prefix}", "gateway": {"ip_prefix": {"prefix": prefix}}}
    if deployment_id:
        peer["customer_deployments"] = [{"id": deployment_id}]
    return peer


def _profiled_permit(peer: dict, profile: str | None = "strict") -> dict:
    rule: dict = {"action": "permit", "disabled": False, "destination_segment": peer}
    if profile:
        rule["security_profile"] = {"name": profile}
    return rule


class TestCustomerPbrNeverBypassesExchangePeers:
    @staticmethod
    def _rules(zone: str | None) -> list[dict]:
        rule = _make_pbr_rule(dst_prefix="10.0.2.0/24")
        if zone:
            rule["destination_segment"]["security_zone"] = {"name": zone}
        seg_policy = {"enabled": True, "rules": [rule]}
        activation = _make_pbr_activation(security_policies=[seg_policy])
        return get_customer_pbr_rules([activation], [_make_context_leg()])

    def test_permit_to_a_tenant_zone_segment_is_still_a_bypass(self) -> None:
        assert self._rules("PROD-ZONE")[0]["bypass_prefixes"] == ["10.0.2.0/24"]

    def test_permit_to_a_segment_without_zone_is_still_a_bypass(self) -> None:
        assert self._rules(None)[0]["bypass_prefixes"] == ["10.0.2.0/24"]

    def test_permit_to_an_exchange_peer_zone_segment_is_not_a_bypass(self) -> None:
        assert self._rules("INTERNET-ZONE")[0]["bypass_prefixes"] == []

    def test_permit_to_the_management_zone_is_not_a_bypass(self) -> None:
        assert self._rules("MANAGEMENT-ZONE")[0]["bypass_prefixes"] == []


class TestCustomerPbrPrefixSelectorBypass:
    """A permit to a pure prefix selector (no peer segment) is bypassed symmetrically in the segment's own VRF."""

    @staticmethod
    def _bypass(selector: dict | None, *, inbound: bool = False) -> list[str]:
        """The bypass prefixes of a PROD segment whose only rule carries ``selector``."""
        activation = _make_pbr_activation(security_policies=[{"enabled": True, "rules": []}])
        if inbound:
            activation["segment"]["inbound_rules"] = [
                {"action": "permit", "disabled": False, "policy": {"enabled": True}, "source_prefixes": [selector]}
            ]
        else:
            rule = {"action": "permit", "disabled": False, "destination_prefixes": [selector] if selector else []}
            activation["segment"]["security_policy"]["rules"] = [rule]
        return get_customer_pbr_rules([activation], [_make_context_leg()])[0]["bypass_prefixes"]

    def test_selector_in_the_segments_own_vrf_is_bypassed(self) -> None:
        """Forward and reply both stay on the fabric: the firewall never sees half a session."""
        selector = {"prefix": "10.40.0.0/16", "ip_namespace": {"name": "PROD"}}
        assert self._bypass(selector) == ["10.40.0.0/16"]

    def test_reply_leg_of_a_selector_source_is_bypassed(self) -> None:
        """An inbound permit from a selector in the same VRF is answered past the firewall too."""
        selector = {"prefix": "10.40.0.0/16", "ip_namespace": {"name": "PROD"}}
        assert self._bypass(selector, inbound=True) == ["10.40.0.0/16"]

    def test_selector_in_another_vrf_stays_on_the_firewall(self) -> None:
        """A prefix the tenant VRF cannot route is redirected, as before."""
        selector = {"prefix": "198.51.100.0/24", "ip_namespace": {"name": "INTERNET"}}
        assert self._bypass(selector) == []

    def test_selector_without_namespace_stays_on_the_firewall(self) -> None:
        """Unknown VRF: fail towards inspection, not towards a blackholing bypass."""
        assert self._bypass({"prefix": "10.40.0.0/16"}) == []

    def test_ip_address_selector_in_the_own_vrf_is_bypassed(self) -> None:
        """A host selector is a /32-style CIDR and follows the same rule."""
        activation = _make_pbr_activation(security_policies=[{"enabled": True, "rules": []}])
        activation["segment"]["security_policy"]["rules"] = [
            {
                "action": "permit",
                "disabled": False,
                "destination_ip_addresses": [{"address": "10.40.0.9/32", "ip_namespace": {"name": "PROD"}}],
            }
        ]
        assert get_customer_pbr_rules([activation], [_make_context_leg()])[0]["bypass_prefixes"] == ["10.40.0.9/32"]


class TestCustomerPbrInspectedFlows:
    """Option 1: a profiled permit inside one context crosses the firewall
    both ways; every other permit stays on the fabric."""

    SHARED = "10.65.0.5/29"
    DEDICATED = "10.66.0.5/29"

    def _contexts(self) -> list[dict]:
        return [
            _make_context_leg(fw_ip=self.SHARED, served=["dep-a", "dep-b"]),
            _make_context_leg(fw_ip=self.DEDICATED, tenant_id="dep-c", served=["dep-c"]),
        ]

    @staticmethod
    def _segment(rules: list[dict], inbound: list[dict] | None = None, deployment_id: str = "dep-a") -> list[dict]:
        activation = _make_pbr_activation(
            deployment_id=deployment_id, security_policies=[{"enabled": True, "rules": rules}]
        )
        activation["segment"]["inbound_rules"] = inbound or []
        return [activation]

    def test_profiled_permit_within_one_context_is_not_bypassed(self) -> None:
        segment = self._segment([_profiled_permit(_peer("10.0.2.0/24", "dep-b"))])
        result = get_customer_pbr_rules(segment, self._contexts())
        assert result[0]["bypass_prefixes"] == []
        assert result[0]["fw_nexthop"] == "10.65.0.4"

    def test_reply_of_an_inspected_inbound_flow_goes_back_through_the_firewall(self) -> None:
        """The destination's leaf keeps the source off its bypass, so the reply meets the session."""
        inbound = {
            "action": "permit",
            "disabled": False,
            "policy": {"enabled": True},
            "security_profile": {"name": "strict"},
            "source_segment": _peer("10.0.1.0/24", "dep-b"),
        }
        result = get_customer_pbr_rules(self._segment([], [inbound]), self._contexts())
        assert result[0]["bypass_prefixes"] == []

    def test_profiled_permit_across_contexts_stays_on_the_fabric(self) -> None:
        segment = self._segment([_profiled_permit(_peer("10.0.3.0/24", "dep-c"))])
        result = get_customer_pbr_rules(segment, self._contexts())
        assert result[0]["bypass_prefixes"] == ["10.0.3.0/24"]

    def test_profiled_permit_to_a_segment_served_elsewhere_stays_on_the_fabric(self) -> None:
        """Another DC's deployment is not this DC's shared context, even without a dedicated one."""
        segment = self._segment([_profiled_permit(_peer("10.9.0.0/24", "dep-other-dc"))])
        result = get_customer_pbr_rules(segment, self._contexts())
        assert result[0]["bypass_prefixes"] == ["10.9.0.0/24"]

    def test_peer_without_deployment_stays_on_the_fabric(self) -> None:
        segment = self._segment([_profiled_permit(_peer("10.0.4.0/24", None))])
        result = get_customer_pbr_rules(segment, self._contexts())
        assert result[0]["bypass_prefixes"] == ["10.0.4.0/24"]

    def test_permit_without_profile_is_bypassed_within_one_context(self) -> None:
        segment = self._segment([_profiled_permit(_peer("10.0.2.0/24", "dep-b"), profile=None)])
        result = get_customer_pbr_rules(segment, self._contexts())
        assert result[0]["bypass_prefixes"] == ["10.0.2.0/24"]

    def test_inspected_flow_wins_over_a_plain_one_to_the_same_peer(self) -> None:
        """The bypass is per prefix, so one inspected flow keeps the peer off it."""
        peer = _peer("10.0.2.0/24", "dep-b")
        segment = self._segment([_profiled_permit(peer, profile=None), _profiled_permit(peer)])
        result = get_customer_pbr_rules(segment, self._contexts())
        assert result[0]["bypass_prefixes"] == []

    def test_contexts_without_served_links_never_inspect_through_the_shared_one(self) -> None:
        """Legacy data: without served_deployments nothing proves both ends share the shared context."""
        segment = self._segment([_profiled_permit(_peer("10.0.2.0/24", "dep-b"))])
        result = get_customer_pbr_rules(segment, [_make_context_leg(fw_ip=self.SHARED)])
        assert result[0]["bypass_prefixes"] == ["10.0.2.0/24"]

    def test_dedicated_context_inspects_its_own_tenant_flows(self) -> None:
        segment = self._segment([_profiled_permit(_peer("10.0.5.0/24", "dep-c"))], deployment_id="dep-c")
        result = get_customer_pbr_rules(segment, self._contexts())
        assert result[0]["fw_nexthop"] == "10.66.0.4"
        assert result[0]["bypass_prefixes"] == []
