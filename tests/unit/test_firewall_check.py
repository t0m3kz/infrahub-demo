"""Unit tests for CheckFirewall: it validates the rules the firewall renders
(Firewall.collect_policies), fed the raw firewall_config query shape."""

from __future__ import annotations

from typing import Any, cast

from checks.firewall import CheckFirewall


def _v(value: Any) -> dict[str, Any]:
    return {"value": value}


def _node(inner: dict[str, Any] | None) -> dict[str, Any]:
    return {"node": inner}


def _edges(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    return {"edges": [{"node": node} for node in nodes]}


def _check() -> Any:
    check = cast(Any, CheckFirewall.__new__(CheckFirewall))
    errors: list[str] = []
    infos: list[str] = []
    check._captured_errors = errors
    check._captured_infos = infos
    check.log_error = lambda message: errors.append(message)
    check.log_info = lambda message: infos.append(message)
    return check


def _ref(seg_id: str, zone: str | None = "zone-a") -> dict[str, Any]:
    """A rule end (PolicyRuleSegmentFields)."""
    return {"id": seg_id, "name": _v(seg_id), "security_zone": _node({"name": _v(zone)} if zone else None)}


def _rule(name: str, src: str | None = "seg-a", dst: str | None = "seg-b", **extra: Any) -> dict[str, Any]:
    return {
        "id": f"rule-{name}",
        "index": _v(10),
        "name": _v(name),
        "action": _v("permit"),
        "disabled": _v(False),
        "source_segment": _node(_ref(src) if src else None),
        "destination_segment": _node(_ref(dst) if dst else None),
        "destination_zone": _node(None),
        "destination_prefixes": _edges([]),
        "destination_ip_addresses": _edges([]),
        **extra,
    }


def _policy(rules: list[dict[str, Any]], owner: str = "seg-a", enabled: bool = True) -> dict[str, Any]:
    return {
        "id": "pol-1",
        "name": _v("policy-1"),
        "enabled": _v(enabled),
        "segment": _node({"id": owner, "name": _v(owner)}),
        "rules": _edges(rules),
    }


def _carried(seg_id: str, policy: dict[str, Any] | None, inbound: list[dict[str, Any]] | None = None) -> dict:
    """A segment on a firewall interface (NetworkSegmentFields + SegmentRulesFields)."""
    return {
        "__typename": "ManagedVxlanSegment",
        "id": seg_id,
        "name": _v(seg_id),
        "status": _v("active"),
        "segment_deployments": _edges([{"vlan_id": _v(10), "vni": _v(10010)}]),
        "security_policy": _node(policy),
        "inbound_rules": _edges(inbound or []),
    }


def _payload(*capabilities: dict[str, Any]) -> dict[str, Any]:
    interfaces = [
        {
            "name": _v(f"eth0.{i}"),
            # A carried segment is the sub-interface's inline_segment; a context stays on its capabilities.
            "inline_segment": _node(None if capability["__typename"] == "ManagedFirewallContext" else capability),
            "capabilities": _edges([capability] if capability["__typename"] == "ManagedFirewallContext" else []),
        }
        for i, capability in enumerate(capabilities)
    ]
    return {"DcimDevice": _edges([{"name": _v("fw-1"), "interfaces": _edges(interfaces)}])}


def _validate(*capabilities: dict[str, Any]) -> Any:
    check = _check()
    check.validate(_payload(*capabilities))
    return check


class TestFirewallCheck:
    def test_rule_in_its_source_segments_policy_passes(self) -> None:
        """A rule between two zoned segments, held by its source's policy, raises nothing."""
        check = _validate(_carried("seg-a", _policy([_rule("rule-1")])))
        assert check._captured_errors == []
        assert check._captured_infos == []

    def test_rule_of_another_segment_in_the_policy_raises_error(self) -> None:
        check = _validate(_carried("seg-a", _policy([_rule("rule-1", src="seg-x")])))
        assert len(check._captured_errors) == 1
        assert "source segment 'seg-x' is not the policy's segment" in check._captured_errors[0]

    def test_policy_without_segment_raises_error(self) -> None:
        policy = {**_policy([_rule("rule-1")]), "segment": _node(None)}
        check = _validate(_carried("seg-a", policy))
        assert len(check._captured_errors) == 1
        assert "is not the policy's segment" in check._captured_errors[0]

    def test_lb_server_rule_into_a_carried_segment_passes(self) -> None:
        """R_server (leg /29 -> backend, in the segment-less policy lb-<context>) reaches the backend
        segment's inbound_rules, but the LB never sits behind a firewall: it is not validated as a
        firewall rule, so it raises nothing."""
        r_server = {
            **_rule("lb-r-server", src=None, dst="seg-a"),
            "source_prefixes": _edges([{"prefix": _v("100.66.64.0/29")}]),
            "policy": _node({"id": "pol-lb", "name": _v("lb-dc1-lb-01-dc1-lb-02-ha-PROD"), "segment": _node(None)}),
        }
        check = _validate(_carried("seg-a", _policy([_rule("rule-1")]), inbound=[r_server]))
        assert check._captured_errors == []

    def test_missing_destination_selector_raises_error(self) -> None:
        check = _validate(_carried("seg-a", _policy([_rule("rule-1", dst=None)])))
        assert len(check._captured_errors) == 1
        assert "has no destination" in check._captured_errors[0]

    def test_apply_on_switch_to_the_internet_zone_raises_error(self) -> None:
        rule = _rule(
            "rule-1", dst=None, apply_on_switch=_v(True), destination_zone=_node({"name": _v("INTERNET-ZONE")})
        )
        check = _validate(_carried("seg-a", _policy([rule])))
        assert len(check._captured_errors) == 1
        assert "apply_on_switch to the internet zone" in check._captured_errors[0]

    def test_rule_to_the_internet_zone_is_a_complete_destination(self) -> None:
        """A zone is a destination of its own: no segment, prefix or address needed."""
        rule = _rule("rule-1", dst=None, destination_zone=_node({"name": _v("INTERNET-ZONE")}))
        check = _validate(_carried("seg-a", _policy([rule])))
        assert check._captured_errors == []

    def test_apply_on_switch_to_a_segment_with_a_prefix_passes(self) -> None:
        dst = {**_ref("seg-b"), "gateway": _node({"ip_prefix": _node({"prefix": _v("10.2.0.0/24")})})}
        rule = _rule("rule-1", apply_on_switch=_v(True), destination_segment=_node(dst))
        assert _validate(_carried("seg-a", _policy([rule])))._captured_errors == []

    def test_permit_to_the_internet_zone_without_apply_on_switch_passes(self) -> None:
        rule = _rule(
            "rule-1", dst=None, apply_on_switch=_v(False), destination_zone=_node({"name": _v("INTERNET-ZONE")})
        )
        check = _validate(_carried("seg-a", _policy([rule])))
        assert check._captured_errors == []

    def test_prefix_destination_counts_as_selector(self) -> None:
        """A prefix-only destination is a complete selector and needs no zone."""
        rule = _rule(
            "rule-1", dst=None, destination_prefixes=_edges([{"id": "pfx-1", "prefix": _v("198.51.100.0/24")}])
        )
        check = _validate(_carried("seg-a", _policy([rule])))
        assert check._captured_errors == []
        assert check._captured_infos == []

    def test_segment_without_zone_is_an_error(self) -> None:
        """A segment with no security_zone is an error: zone-based firewalls match any zone there."""
        rule = {**_rule("rule-1"), "source_segment": _node(_ref("seg-a", zone=None))}
        check = _validate(_carried("seg-a", _policy([rule])))
        assert check._captured_infos == []
        assert len(check._captured_errors) == 1
        assert "source_segment 'seg-a' has no security_zone" in check._captured_errors[0]

    def test_disabled_policy_and_rule_are_skipped(self) -> None:
        bad = _rule("bad", src="seg-x", dst=None)
        disabled_rule = {**bad, "id": "rule-off", "disabled": _v(True)}
        check = _validate(
            _carried("seg-a", _policy([disabled_rule])),
            _carried("seg-c", {**_policy([bad], owner="seg-c", enabled=False), "id": "pol-2", "name": _v("p-2")}),
        )
        assert check._captured_errors == []

    def test_inbound_rules_are_validated_against_their_own_policy(self) -> None:
        """The ingress leg of another segment's rule is checked with that rule's policy."""
        inbound = {**_rule("in-1", src="seg-x", dst="seg-a"), "policy": _node(_policy([], owner="seg-y"))}
        check = _validate(_carried("seg-a", None, inbound=[inbound]))
        assert len(check._captured_errors) == 1
        assert "'seg-x' is not the policy's segment" in check._captured_errors[0]

    def test_rule_reached_twice_is_reported_once(self) -> None:
        bad = _rule("rule-1", dst=None)
        inbound = {**bad, "policy": _node(_policy([]))}
        check = _validate(_carried("seg-a", _policy([bad]), inbound=[inbound]))
        assert len(check._captured_errors) == 1

    def test_rules_of_segments_the_firewall_does_not_serve_are_not_validated(self) -> None:
        """Only the served segments' rules are this firewall's: a context serving
        nothing and no carried segment leave nothing to check."""
        context = {
            "__typename": "ManagedFirewallContext",
            "id": "ctx-1",
            "name": _v("ctx-1"),
            "served_deployments": _edges([{"id": "dep-1", "network_segments": _edges([])}]),
        }
        check = _validate(context)
        assert check._captured_errors == []

    def test_context_served_segment_rules_are_validated(self) -> None:
        served = {
            "id": "seg-a",
            "security_policy": _node(_policy([_rule("rule-1", dst=None)])),
            "inbound_rules": _edges([]),
        }
        context = {
            "__typename": "ManagedFirewallContext",
            "id": "ctx-1",
            "name": _v("ctx-1"),
            "served_deployments": _edges([{"id": "dep-1", "network_segments": _edges([served])}]),
        }
        check = _validate(context)
        assert len(check._captured_errors) == 1
        assert "has no destination" in check._captured_errors[0]


def _leg_interface(name: str, address: str, namespace: str, ns_type: str, exchange: dict, context: dict) -> dict:
    """A firewall sub-interface in the raw firewall_config shape (leg of one exchange)."""
    return {
        "name": _v(name),
        "ip_address": _node(
            {"address": _v(address), "ip_namespace": _node({"name": _v(namespace), "namespace_type": _v(ns_type)})}
        ),
        "capabilities": _edges([context, exchange]),
    }


def _exchange_payload(internet_vlan: str, snat: str | None = "192.0.2.5/32") -> dict[str, Any]:
    context = {
        "__typename": "ManagedFirewallContext",
        "id": "ctx-1",
        "name": _v("ctx-1"),
        "vlan_id": _v(3005),
        "snat_address": _node({"address": _v(snat)}) if snat else {"node": None},
    }
    exchange = {
        "__typename": "TopologyRoutedExchange",
        "id": "xchg-1",
        "name": _v("ctx-1-PROD-INTERNET"),
        # The exchange's own namespace_a / namespace_z come back null through capabilities;
        # the check reads the namespaces off the legs' addresses instead.
        "namespace_a": {"node": None},
        "namespace_z": {"node": None},
        "gateway": _node({"id": "ctx-1"}),
    }
    interfaces = [
        _leg_interface("eth1.3005", "100.66.0.5/29", "PROD", "prod", exchange, context),
        _leg_interface(f"eth1.{internet_vlan}", "100.66.32.5/29", "INTERNET", "internet", exchange, context),
    ]
    return {"DcimDevice": _edges([{"name": _v("fw-1"), "interfaces": _edges(interfaces)}])}


class TestFirewallExchangeLegs:
    def test_valid_legs_raise_nothing(self) -> None:
        check = _check()
        check.validate(_exchange_payload("3405"))
        assert check._captured_errors == []

    def test_wrong_transit_vlan_is_logged_as_error(self) -> None:
        check = _check()
        check.validate(_exchange_payload("3005"))
        assert len(check._captured_errors) == 1
        assert "expected 3405" in check._captured_errors[0]

    def test_internet_leg_without_snat_address_is_logged_as_error(self) -> None:
        check = _check()
        check.validate(_exchange_payload("3405", snat=None))
        assert len(check._captured_errors) == 1
        assert "no snat_address" in check._captured_errors[0]

    def test_private_snat_address_is_logged_as_error(self) -> None:
        check = _check()
        check.validate(_exchange_payload("3405", snat="10.0.0.5/32"))
        assert len(check._captured_errors) == 1
        assert "not a public address" in check._captured_errors[0]


class TestContextRoutesUseSegmentOwnGateway:
    """firewall.gql fetches each served segment's own gateway prefix and zone;
    apply_context_routes prefers them to the SecurityZone root."""

    @staticmethod
    def _context(segment: dict[str, Any]) -> dict[str, Any]:
        leg = {
            "ns_type": "prod",
            "zone": "PROD-ZONE",
            "anycast": "100.66.0.1",
            "interface": "eth1.3005",
            "vrf": "PROD",
        }
        return {"legs": [leg], "segments": [segment]}

    def test_own_prefix_wins_over_root(self) -> None:
        from transforms.config.firewall import apply_context_routes

        segment = {
            "id": "s1",
            "gateway": {"ip_prefix": {"prefix": "10.1.0.0/24"}},
            "security_zone": {"name": "PROD-ZONE"},
        }
        context = self._context(segment)
        apply_context_routes(context, {"s1": ("PROD-ZONE", "10.9.9.0/24")})
        assert [r["destination"] for r in context["routes"]] == ["10.1.0.0/24"]

    def test_root_fills_a_segment_without_gateway(self) -> None:
        from transforms.config.firewall import apply_context_routes

        context = self._context({"id": "s1"})
        apply_context_routes(context, {"s1": ("PROD-ZONE", "10.9.9.0/24")})
        assert [r["destination"] for r in context["routes"]] == ["10.9.9.0/24"]

    def test_no_root_needed_when_segment_carries_its_own(self) -> None:
        from transforms.config.firewall import apply_context_routes

        segment = {
            "id": "s1",
            "gateway": {"ip_prefix": {"prefix": "10.1.0.0/24"}},
            "security_zone": {"name": "PROD-ZONE"},
        }
        context = self._context(segment)
        apply_context_routes(context, {})
        assert context["legs"][0]["prefixes"] == ["10.1.0.0/24"]


class TestContextRoutesTenantLegsAreExchangeTenants:
    """A MANAGEMENT leg is an exchange peer, never a tenant leg."""

    def test_zoneless_segment_with_a_management_leg_still_takes_the_only_tenant_leg(self) -> None:
        from transforms.config.firewall import apply_context_routes

        prod = {"ns_type": "prod", "zone": "PROD-ZONE", "anycast": "100.66.0.1", "interface": "e.3000", "vrf": "PROD"}
        mgmt = {
            "ns_type": "management",
            "zone": "MANAGEMENT-ZONE",
            "anycast": "100.66.48.1",
            "interface": "e.3600",
            "vrf": "MANAGEMENT",
        }
        segment = {"id": "s1", "gateway": {"ip_prefix": {"prefix": "10.1.0.0/24"}}}
        context = {"legs": [prod, mgmt], "segments": [segment]}
        apply_context_routes(context, {})
        assert prod["prefixes"] == ["10.1.0.0/24"]
        assert mgmt["prefixes"] == []
        assert [r["vrf"] for r in context["routes"]] == ["PROD"]

    def test_segment_in_the_management_zone_is_routed_to_the_management_leg_not_as_a_tenant_prefix(self) -> None:
        from transforms.config.firewall import apply_context_routes

        prod = {"ns_type": "prod", "zone": "PROD-ZONE", "anycast": "100.66.0.1", "interface": "e.3000", "vrf": "PROD"}
        mgmt = {
            "ns_type": "management",
            "zone": "MANAGEMENT-ZONE",
            "anycast": "100.66.48.1",
            "interface": "e.3600",
            "vrf": "MANAGEMENT",
        }
        segment = {
            "id": "s1",
            "gateway": {"ip_prefix": {"prefix": "10.9.0.0/24"}},
            "security_zone": {"name": "MANAGEMENT-ZONE"},
        }
        context = {"legs": [prod, mgmt], "segments": [segment]}
        apply_context_routes(context, {})
        assert prod["prefixes"] == []
        assert mgmt["prefixes"] == ["10.9.0.0/24"]
        assert context["routes"] == [
            {"destination": "10.9.0.0/24", "nexthop": "100.66.48.1", "interface": "e.3600", "vrf": "MANAGEMENT"}
        ]


def _legs(*ns_types: str) -> list[dict]:
    zones = {
        "prod": "PROD-ZONE",
        "non_prod": "NONPROD-ZONE",
        "internet": "INTERNET-ZONE",
        "management": "MANAGEMENT-ZONE",
    }
    return [
        {
            "ns_type": ns_type,
            "zone": zones[ns_type],
            "nameif": zones[ns_type].removesuffix("-ZONE").lower(),
            "anycast": f"100.66.{i}.1",
            "vip": f"100.66.{i}.4",
            "interface": f"e.{3000 + i}",
            "vrf": ns_type.upper(),
        }
        for i, ns_type in enumerate(ns_types)
    ]


class TestManagementPeerRoutesAndIsolation:
    """The MANAGEMENT peer: zone CIDRs routed to its leg, tenant-initiated only."""

    def test_zone_cidrs_from_the_root_route_to_the_management_anycast_between_tenant_and_default(self) -> None:
        from transforms.config.firewall import apply_context_routes

        legs = _legs("prod", "internet", "management")
        segment = {
            "id": "s1",
            "gateway": {"ip_prefix": {"prefix": "10.1.0.0/24"}},
            "security_zone": {"name": "PROD-ZONE"},
        }
        context = {"legs": legs, "segments": [segment]}
        roots = {"infra-b": ("MANAGEMENT-ZONE", "10.251.0.0/24"), "infra-a": ("MANAGEMENT-ZONE", "10.250.0.0/24")}
        apply_context_routes(context, {**roots, "other": ("PROD-ZONE", "10.77.0.0/24")})
        assert [(r["destination"], r["nexthop"], r["vrf"]) for r in context["routes"]] == [
            ("10.1.0.0/24", "100.66.0.1", "PROD"),
            ("10.250.0.0/24", "100.66.2.1", "MANAGEMENT"),
            ("10.251.0.0/24", "100.66.2.1", "MANAGEMENT"),
            ("0.0.0.0/0", "100.66.1.1", "INTERNET"),
        ]
        assert legs[2]["prefixes"] == ["10.250.0.0/24", "10.251.0.0/24"]

    def test_no_management_leg_means_no_management_route_and_no_management_deny(self) -> None:
        from transforms.config.firewall import apply_context_routes

        context = {"legs": _legs("prod", "internet"), "segments": []}
        apply_context_routes(context, {"infra": ("MANAGEMENT-ZONE", "10.250.0.0/24")})
        assert [r["destination"] for r in context["routes"]] == ["0.0.0.0/0"]
        assert context["isolation"] == []

    def test_management_leg_denies_management_towards_every_held_tenant_zone(self) -> None:
        from transforms.config.firewall import apply_context_routes

        context = {"legs": _legs("prod", "non_prod", "internet", "management"), "segments": []}
        apply_context_routes(context, {})
        assert context["isolation"] == [
            {"src_zone": "PROD-ZONE", "dst_zone": "NONPROD-ZONE"},
            {"src_zone": "NONPROD-ZONE", "dst_zone": "PROD-ZONE"},
            {"src_zone": "MANAGEMENT-ZONE", "dst_zone": "PROD-ZONE"},
            {"src_zone": "MANAGEMENT-ZONE", "dst_zone": "NONPROD-ZONE"},
        ]

    def test_single_tenant_context_denies_only_management_towards_it(self) -> None:
        from transforms.config.firewall import apply_context_routes

        context = {"legs": _legs("prod", "internet", "management"), "segments": []}
        apply_context_routes(context, {})
        assert context["isolation"] == [{"src_zone": "MANAGEMENT-ZONE", "dst_zone": "PROD-ZONE"}]

    def test_prod_non_prod_pair_is_unchanged_without_a_management_leg(self) -> None:
        from transforms.config.firewall import apply_context_routes

        context = {"legs": _legs("prod", "non_prod", "internet"), "segments": []}
        apply_context_routes(context, {})
        assert context["isolation"] == [
            {"src_zone": "PROD-ZONE", "dst_zone": "NONPROD-ZONE"},
            {"src_zone": "NONPROD-ZONE", "dst_zone": "PROD-ZONE"},
        ]
