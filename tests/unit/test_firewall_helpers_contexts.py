"""Unit tests for get_firewall_contexts() and place_policies_in_contexts() in transforms/helpers/firewall_contexts.py."""

from transforms.helpers.firewall_contexts import get_firewall_contexts, place_policies_in_contexts

# ===========================================================================
# get_firewall_contexts()
# ===========================================================================


def _make_fw_context_interface(
    *,
    iface_name: str = "ethernet1/1.3000",
    ip_addr: str | None = "10.65.0.1/30",
    parent_name: str = "ethernet1/1",
    context_id: str = "ctx-1",
    context_name: str = "dc10-shared",
    vlan_id: int = 3000,
    tenant_name: str | None = None,
    served: list[dict] | None = None,
) -> dict:
    tenant: dict = {}
    if tenant_name:
        tenant["name"] = tenant_name
    cap = {
        "typename": "ManagedFirewallContext",
        "id": context_id,
        "name": context_name,
        "vlan_id": vlan_id,
        "context_id": None,
        "tenant": tenant,
    }
    if served is not None:
        cap["served_deployments"] = served
    iface: dict = {
        "name": iface_name,
        "parent_interface": {"name": parent_name},
        "capabilities": [cap],
    }
    if ip_addr:
        iface["ip_address"] = {"address": ip_addr}
    return iface


class TestGetFirewallContexts:
    def test_snat_is_the_host_ip_or_none(self) -> None:
        plain = _make_fw_context_interface()
        assert get_firewall_contexts([plain])[0]["snat"] is None
        with_snat = _make_fw_context_interface()
        with_snat["capabilities"][0]["snat_address"] = {"address": "192.0.2.5/32"}
        assert get_firewall_contexts([with_snat])[0]["snat"] == "192.0.2.5"

    def test_none_returns_empty(self) -> None:
        assert get_firewall_contexts(None) == []

    def test_empty_list_returns_empty(self) -> None:
        assert get_firewall_contexts([]) == []

    def test_interface_without_context_capability_ignored(self) -> None:
        iface = {"name": "eth0", "capabilities": [{"typename": "ManagedVxlanSegment"}]}
        assert get_firewall_contexts([iface]) == []

    def test_single_context_extracted(self) -> None:
        result = get_firewall_contexts([_make_fw_context_interface()])
        assert len(result) == 1
        assert result[0]["name"] == "dc10-shared"
        assert result[0]["vlan_id"] == 3000
        assert result[0]["tenant_name"] is None
        assert result[0]["legs"] == []

    def test_dedicated_context_carries_tenant_name(self) -> None:
        result = get_firewall_contexts([_make_fw_context_interface(tenant_name="C005-P-DC10")])
        assert result[0]["tenant_name"] == "C005-P-DC10"

    def test_context_carries_its_id(self) -> None:
        """place_policies_in_contexts keys its tables by context id."""
        result = get_firewall_contexts([_make_fw_context_interface(context_id="ctx-9")])
        assert result[0]["id"] == "ctx-9"

    def test_context_without_ip_still_extracted(self) -> None:
        """inline connectivity_mode contexts have no addressed leg."""
        result = get_firewall_contexts([_make_fw_context_interface(ip_addr=None)])
        assert len(result) == 1
        assert result[0]["legs"] == []

    def test_cluster_context_without_any_interface_is_listed(self) -> None:
        """A colocation context has no interface on a member: it comes from the cluster."""
        served = [{"id": "dep-a", "network_segments": [{"id": "s1"}]}]
        cluster = [{"id": "ctx-colo", "name": "fr-fw-shared", "served_deployments": served}]
        result = get_firewall_contexts([], None, cluster)
        assert [(c["id"], c["name"], c["legs"]) for c in result] == [("ctx-colo", "fr-fw-shared", [])]
        assert [seg["id"] for seg in result[0]["segments"]] == ["s1"]

    def test_cluster_context_is_not_duplicated_by_its_interface(self) -> None:
        iface = _make_fw_context_interface(context_id="ctx-1")
        result = get_firewall_contexts([iface], None, [{"id": "ctx-1", "name": "dup"}])
        assert len(result) == 1
        assert result[0]["name"] == "dc10-shared"

    def test_duplicate_context_id_deduplicated(self) -> None:
        ifaces = [
            _make_fw_context_interface(context_id="ctx-1"),
            _make_fw_context_interface(context_id="ctx-1", iface_name="ethernet1/2.3000"),
        ]
        assert len(get_firewall_contexts(ifaces)) == 1

    def test_segments_of_served_deployments_collected(self) -> None:
        """Every segment of every served deployment, once; entries without an id are skipped."""
        served = [
            {"id": "dep-a", "network_segments": [{"id": "s1"}, {}, {"id": "s2"}]},
            {"id": "dep-b", "network_segments": [{"id": "s2"}, {"id": "s3"}]},
        ]
        iface = _make_fw_context_interface(served=served)
        assert [seg["id"] for seg in get_firewall_contexts([iface])[0]["segments"]] == ["s1", "s2", "s3"]

    def test_no_served_deployments_yields_no_segments(self) -> None:
        assert get_firewall_contexts([_make_fw_context_interface()])[0]["segments"] == []

    def test_multiple_contexts_sorted_by_name(self) -> None:
        ifaces = [
            _make_fw_context_interface(context_id="ctx-b", context_name="dc10-b-dedicated"),
            _make_fw_context_interface(context_id="ctx-a", context_name="dc10-a-dedicated"),
        ]
        result = get_firewall_contexts(ifaces)
        assert [c["name"] for c in result] == ["dc10-a-dedicated", "dc10-b-dedicated"]


def _leg_iface(ns_type: str, address: str, *, vlan: int = 3000, ns_name: str | None = None) -> dict:
    """A context sub-interface in a VRF namespace (an exchange transit leg)."""
    iface = _make_fw_context_interface(iface_name=f"eth1.{vlan}", ip_addr=address, parent_name="eth1", vlan_id=vlan)
    iface["ip_address"]["ip_namespace"] = {"name": ns_name or ns_type.upper(), "namespace_type": ns_type}
    return iface


_HA = {"name": "fw-ha", "members": ["fw-01", "fw-02"]}


class TestGetFirewallContextsLegs:
    def test_namespaced_sub_interfaces_become_legs_ordered_by_slot(self) -> None:
        ifaces = [_leg_iface("internet", "100.66.32.5/29"), _leg_iface("prod", "100.66.0.5/29")]
        legs = get_firewall_contexts(ifaces)[0]["legs"]
        assert [leg["ns_type"] for leg in legs] == ["prod", "internet"]

    def test_leg_vlan_is_the_transit_vlan_of_the_context_vlan(self) -> None:
        ifaces = [
            _leg_iface("prod", "100.66.0.5/29"),
            _leg_iface("non_prod", "100.66.16.5/29"),
            _leg_iface("internet", "100.66.32.5/29"),
        ]
        legs = get_firewall_contexts(ifaces)[0]["legs"]
        assert [leg["vlan"] for leg in legs] == [3000, 3200, 3400]

    def test_leg_carries_zone_nameif_vrf_and_anycast(self) -> None:
        leg = get_firewall_contexts([_leg_iface("non_prod", "100.66.16.5/29", ns_name="NON-PROD")])[0]["legs"][0]
        assert leg["zone"] == "NONPROD-ZONE"
        assert leg["nameif"] == "nonprod"
        assert leg["vrf"] == "NON-PROD"
        assert leg["anycast"] == "100.66.16.1"
        assert leg["own_ip"] == "100.66.16.5/29"
        assert leg["interface"] == "eth1.3000"

    def test_ha_pair_gets_the_vip(self) -> None:
        leg = get_firewall_contexts([_leg_iface("prod", "100.66.0.5/29")], _HA)[0]["legs"][0]
        assert leg["virtual_ip"] == "100.66.0.4/29"

    def test_standalone_firewall_has_no_vip(self) -> None:
        leg = get_firewall_contexts([_leg_iface("prod", "100.66.0.5/29")])[0]["legs"][0]
        assert leg["virtual_ip"] is None

    def test_one_context_over_several_interfaces_is_one_context(self) -> None:
        ifaces = [_leg_iface("prod", "100.66.0.5/29"), _leg_iface("internet", "100.66.32.5/29", vlan=3400)]
        contexts = get_firewall_contexts(ifaces)
        assert len(contexts) == 1
        assert len(contexts[0]["legs"]) == 2

    def test_default_namespace_sub_interface_is_not_a_leg(self) -> None:
        iface = _make_fw_context_interface()
        iface["ip_address"]["ip_namespace"] = {"name": "default"}
        assert get_firewall_contexts([iface])[0]["legs"] == []

    def test_ipv6_sub_interface_is_not_a_leg(self) -> None:
        assert get_firewall_contexts([_leg_iface("prod", "fd00::5/127")])[0]["legs"] == []

    def test_legacy_context_has_no_legs(self) -> None:
        assert get_firewall_contexts([_make_fw_context_interface()])[0]["legs"] == []


# ===========================================================================
# place_policies_in_contexts()
# ===========================================================================


def _placement_rule(rule_id: str, src: str, dst: str | None = None, index: int = 10) -> dict:
    """A rule of ``src``'s policy, as SecurityPolicyRuleFields returns it (cleaned)."""
    return {
        "id": rule_id,
        "index": index,
        "name": rule_id,
        "action": "permit",
        "source_segment": {"id": src},
        "destination_segment": {"id": dst} if dst else None,
    }


def _segments(*rules: dict, ids: tuple[str, ...] = (), policy_names: dict[str, str] | None = None) -> dict[str, dict]:
    """Segment id -> the segment as SegmentRulesFields returns it (cleaned):
    its own policy holding the rules whose source it is, and the rules into
    it carrying their policy. ``ids`` adds segments without rules."""
    names = policy_names or {}
    segments: dict[str, dict] = {}

    def _segment(seg_id: str) -> dict:
        return segments.setdefault(seg_id, {"id": seg_id, "security_policy": None, "inbound_rules": []})

    def _header(seg_id: str) -> dict:
        name = names.get(seg_id, f"seg-{seg_id}-egress")
        return {"id": f"pol-{seg_id}", "name": name, "enabled": True, "segment": {"id": seg_id}}

    for seg_id in ids:
        _segment(seg_id)
    for rule in rules:
        src = rule["source_segment"]["id"]
        owner = _segment(src)
        if owner["security_policy"] is None:
            owner["security_policy"] = {**_header(src), "rules": []}
        owner["security_policy"]["rules"].append(rule)
        dst = (rule.get("destination_segment") or {}).get("id")
        if dst:
            _segment(dst)["inbound_rules"].append({**rule, "policy": _header(src)})
    return segments


def _ctx(ctx_id: str, *segments: dict) -> dict:
    return {"id": ctx_id, "segments": list(segments)}


def _table(policies: list[dict]) -> list[tuple[str, list[str]]]:
    return [(policy["name"], [rule["name"] for rule in policy["rules"]]) for policy in policies]


class TestPlacePoliciesInContexts:
    """A firewall enforces the rules of the segments it serves, per table."""

    def test_nothing_served_returns_empty(self) -> None:
        assert place_policies_in_contexts([], []) == ([], {})

    def test_without_contexts_the_carried_segments_rules_are_root(self) -> None:
        """Its egress rules and the rules into it (ingress leg), policies sorted by name."""
        segs = _segments(_placement_rule("out", "s1", "s2"), _placement_rule("in", "s3", "s1"))
        root, by_ctx = place_policies_in_contexts([], [segs["s1"]])
        assert _table(root) == [("seg-s1-egress", ["out"]), ("seg-s3-egress", ["in"])]
        assert by_ctx == {}

    def test_rules_of_segments_served_nowhere_here_are_left_out(self) -> None:
        segs = _segments(_placement_rule("own", "s1"), _placement_rule("elsewhere", "s9", "s8"))
        root, _ = place_policies_in_contexts([], [segs["s1"]])
        assert _table(root) == [("seg-s1-egress", ["own"])]

    def test_without_contexts_a_bound_zone_counts_as_served(self) -> None:
        """A transit firewall routes a bound zone's CIDRs, so rules of that zone's segments stay."""
        segs = _segments(_placement_rule("zone-rule", "s2", "s9"), ids=("leg",))
        carried = {**segs["leg"], "security_zone": {"name": "dmz", "network_segments": [segs["s2"]]}}
        root, _ = place_policies_in_contexts([], [carried])
        assert _table(root) == [("seg-s2-egress", ["zone-rule"])]

    def test_with_contexts_a_bound_zone_does_not_count(self) -> None:
        """The zone's other segments terminate on contexts (here or on another firewall)."""
        segs = _segments(_placement_rule("zone-rule", "s2", "s9"), ids=("leg",))
        carried = {**segs["leg"], "security_zone": {"name": "dmz", "network_segments": [segs["s2"]]}}
        root, by_ctx = place_policies_in_contexts([_ctx("ctx-a")], [carried])
        assert root == []
        assert by_ctx == {"ctx-a": []}

    def test_context_gets_its_segments_egress_and_ingress_rules(self) -> None:
        segs = _segments(_placement_rule("out", "s1", "s2"), _placement_rule("in", "s3", "s1"))
        root, by_ctx = place_policies_in_contexts([_ctx("ctx-a", segs["s1"])], [])
        assert root == []
        assert _table(by_ctx["ctx-a"]) == [("seg-s1-egress", ["out"]), ("seg-s3-egress", ["in"])]

    def test_cross_context_rule_is_in_both_contexts(self) -> None:
        """Between two tenants the flow crosses both contexts, each denying by default."""
        segs = _segments(_placement_rule("a-to-b", "s1", "s2"))
        _, by_ctx = place_policies_in_contexts([_ctx("ctx-a", segs["s1"]), _ctx("ctx-b", segs["s2"])], [])
        assert _table(by_ctx["ctx-a"]) == [("seg-s1-egress", ["a-to-b"])]
        assert _table(by_ctx["ctx-b"]) == [("seg-s1-egress", ["a-to-b"])]

    def test_lb_server_rule_into_a_served_segment_is_left_out(self) -> None:
        """R_server of a load-balancer context (segment-less policy lb-<context>, leg /29 -> backend) is
        an inbound rule of the backend segment, but the LB never sits behind a firewall: neither the
        context's table nor the root table takes it."""
        segs = _segments(_placement_rule("out", "s1", "s2"))
        r_server = {
            **_placement_rule("lb-r-server", "s1", "s1"),
            "source_segment": None,
            "source_prefixes": [{"prefix": "100.66.64.0/29"}],
            "policy": {"id": "pol-lb", "name": "lb-dc1-lb-01-dc1-lb-02-ha-PROD", "enabled": True, "segment": None},
        }
        backend = {**segs["s1"], "inbound_rules": [r_server]}
        root, by_ctx = place_policies_in_contexts([_ctx("ctx-a", backend)], [backend])
        assert _table(by_ctx["ctx-a"]) == [("seg-s1-egress", ["out"])]
        assert root == []

    def test_rule_between_two_served_segments_appears_once(self) -> None:
        """Reached as s1's egress rule and as s2's inbound rule: de-duplicated by id."""
        segs = _segments(_placement_rule("a-to-b", "s1", "s2"))
        _, by_ctx = place_policies_in_contexts([_ctx("ctx-a", segs["s1"], segs["s2"])], [])
        assert _table(by_ctx["ctx-a"]) == [("seg-s1-egress", ["a-to-b"])]

    def test_carried_segment_served_by_a_context_is_not_repeated_in_root(self) -> None:
        segs = _segments(_placement_rule("r1", "s1"))
        root, by_ctx = place_policies_in_contexts([_ctx("ctx-a", segs["s1"])], [segs["s1"]])
        assert root == []
        assert _table(by_ctx["ctx-a"]) == [("seg-s1-egress", ["r1"])]

    def test_carried_segment_no_context_serves_stays_in_root(self) -> None:
        segs = _segments(_placement_rule("served", "s1"), _placement_rule("carried", "s2"))
        root, by_ctx = place_policies_in_contexts([_ctx("ctx-a", segs["s1"])], [segs["s2"]])
        assert _table(root) == [("seg-s2-egress", ["carried"])]
        assert _table(by_ctx["ctx-a"]) == [("seg-s1-egress", ["served"])]

    def test_policy_order_is_by_name_not_input_order(self) -> None:
        """The rule table is numbered across policies, so their order must be stable."""
        segs = _segments(
            _placement_rule("r-b", "s1"), _placement_rule("r-a", "s2"), policy_names={"s1": "b-pol", "s2": "a-pol"}
        )
        root, _ = place_policies_in_contexts([], [segs["s1"], segs["s2"]])
        assert _table(root) == [("a-pol", ["r-a"]), ("b-pol", ["r-b"])]

    def test_ruleless_policy_is_kept(self) -> None:
        segment = {"id": "s1", "security_policy": {"id": "p", "name": "empty", "rules": []}, "inbound_rules": []}
        root, _ = place_policies_in_contexts([], [segment])
        assert _table(root) == [("empty", [])]

    def test_policy_whose_rules_are_all_in_a_context_leaves_root(self) -> None:
        segs = _segments(_placement_rule("a-to-b", "s1", "s2"))
        root, _ = place_policies_in_contexts([_ctx("ctx-a", segs["s1"])], [segs["s2"]])
        assert root == []

    def test_input_segments_are_not_mutated(self) -> None:
        segs = _segments(_placement_rule("r1", "s1", "s2"), _placement_rule("r2", "s2", "s1"))
        place_policies_in_contexts([_ctx("ctx-a", segs["s1"])], [segs["s2"]])
        assert [rule["id"] for rule in segs["s1"]["security_policy"]["rules"]] == ["r1"]
        assert [rule["id"] for rule in segs["s1"]["inbound_rules"]] == ["r2"]


class TestFootprintServedByTwoColocationZones:
    """A colocation footprint is served by one shared context per zone with a firewall pair: each zone's firewall
    renders its own context (served_deployments is the other end of serving_firewall_contexts) and places the
    footprint's full policy in it."""

    @staticmethod
    def _zone_firewall(context_id: str, segments: list[dict]) -> list[dict]:
        """get_firewall_contexts() of one zone's firewall: its cluster's context, serving the footprint."""
        served = [{"id": "footprint-1", "network_segments": segments}]
        cap = {"typename": "ManagedFirewallContext", "id": context_id, "name": f"shared-{context_id}", "vlan_id": None}
        return get_firewall_contexts([], cluster_contexts=[{**cap, "served_deployments": served}])

    def test_each_zones_context_holds_the_same_rules_in_its_own_table(self) -> None:
        segs = _segments(_placement_rule("a-to-b", "s1", "s2"), _placement_rule("b-to-a", "s2", "s1", index=20))
        footprint_segments = [segs["s1"], segs["s2"]]

        tables = {}
        for zone, context_id in (("FR2", "ctx-fr2"), ("FR3", "ctx-fr3")):
            contexts = self._zone_firewall(context_id, footprint_segments)
            assert [c["id"] for c in contexts] == [context_id]
            root, by_ctx = place_policies_in_contexts(contexts, [])
            assert root == []
            tables[zone] = _table(by_ctx[context_id])

        assert tables["FR2"] == tables["FR3"] == [("seg-s1-egress", ["a-to-b"]), ("seg-s2-egress", ["b-to-a"])]

    def test_a_zone_with_no_context_of_the_footprint_places_nothing_of_it(self) -> None:
        """FR6 has no firewall pair, hence no context: the footprint's rules are not in its (empty) config."""
        root, by_ctx = place_policies_in_contexts([], [])

        assert (root, by_ctx) == ([], {})
