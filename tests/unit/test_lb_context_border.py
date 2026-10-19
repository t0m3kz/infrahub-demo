"""Load-balancer contexts on the border, the leaf and the group contracts (one-arm, SNAT automap).

- transforms/helpers/firewall.py: a load-balancer context tagged on a load-balancer port yields the pseudo segment
  activation of its one leg (``LB-<context>``, a VLAN name cut to 32 characters) and the VIP /26 static to the
  leg's floating .4;
- transforms/helpers/vxlan.py get_interfaces: that port trunks the leg VLAN;
- the leaf: a rule to a VIP /32 and the segment-less ``lb-<context>`` rule from the leg /29 are bypassed and
  answered by the existing ACL/PBR code, never redirected;
- transforms/helpers/group_policy/model.py: the segment-less rule is UNCLASSIFIED -> backend;
- the queries select what these read.
"""

from __future__ import annotations

import ipaddress
from pathlib import Path
from typing import Any

import pytest
from graphql import parse, print_ast
from graphql.language import FragmentDefinitionNode

from tests.smoke.generate_config_fixtures import (
    build_lb_context_bl_data,
    build_leaf_lb_client_data,
)
from tests.smoke.test_configs_smoke import _run_transform
from transforms.config.border_leaf import BorderLeaf
from transforms.config.leaf import Leaf
from transforms.helpers.firewall import (
    _service_context_caps,
    get_customer_pbr_rules,
    get_exchange_routes,
    get_exchange_transits,
)
from transforms.helpers.group_policy import UNCLASSIFIED, get_vrf_contracts
from transforms.helpers.vxlan import get_interfaces

_PROD = {"name": "PROD", "namespace_type": "prod", "l3_vni": 60000}
_NONPROD = {"name": "NON-PROD", "namespace_type": "non_prod", "l3_vni": 60001}


def _lb_context(
    *,
    name: str = "dc10-lb-01-PROD",
    vlan: int = 3100,
    namespace: dict[str, Any] = _PROD,
    leg: str = "100.67.0.0/29",
    vip_prefix: str | None = "100.68.0.0/26",
    extra_legs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """A cleaned ManagedLoadbalancerContext as LoadbalancerContextInterfaceCapabilityFields returns it.

    Its capabilities are the two members' leg sub-interfaces (.5/.6) and the border port tagged with it.
    """
    net = ipaddress.ip_network(leg)
    members = [
        {
            "name": f"1.1.{vlan}",
            "device": {"name": member, "role": "load-balancer"},
            "ip_address": {"address": f"{net[host]}/{net.prefixlen}", "ip_namespace": namespace},
        }
        for member, host in (("dc10-lb-01a", 5), ("dc10-lb-01b", 6))
    ]
    border_port = {"name": "Ethernet21", "device": {"name": "dc10-bl-01", "role": "border-leaf"}}
    return {
        "typename": "ManagedLoadbalancerContext",
        "id": f"id-{name}",
        "name": name,
        "vlan_id": vlan,
        "vip_prefix": {"prefix": vip_prefix} if vip_prefix else None,
        "capabilities": [*members, *(extra_legs or []), border_port],
    }


def _port(role: str | None, *contexts: dict[str, Any], name: str = "Ethernet21") -> dict[str, Any]:
    """A border service port of ``role`` tagged with ``contexts``."""
    return {"name": name, "role": role, "capabilities": list(contexts)}


class TestServiceContextCaps:
    """The one selector of the contexts a border device carries: kind and port role must agree."""

    def test_load_balancer_context_on_a_load_balancer_port_is_carried(self) -> None:
        ctx = _lb_context()
        assert _service_context_caps([_port("load-balancer", ctx)]) == [ctx]

    @pytest.mark.parametrize("role", ["firewall", "server", None])
    def test_load_balancer_context_on_any_other_port_is_not(self, role: str | None) -> None:
        assert _service_context_caps([_port(role, _lb_context())]) == []

    def test_firewall_context_on_a_load_balancer_port_is_not(self) -> None:
        firewall = {"typename": "ManagedFirewallContext", "id": "fw-1", "name": "dc10-fw", "vlan_id": 3000}
        assert _service_context_caps([_port("load-balancer", firewall)]) == []

    def test_a_context_on_both_member_ports_is_carried_once(self) -> None:
        ctx = _lb_context()
        ports = [_port("load-balancer", ctx, name="Ethernet21"), _port("load-balancer", ctx, name="Ethernet22")]
        assert _service_context_caps(ports) == [ctx]


class TestLoadBalancerLeg:
    """The leg becomes a VLAN + L2VNI + anycast .1 in its tenant VRF, the same formulas as a firewall leg."""

    @staticmethod
    def _transits(*contexts: dict[str, Any]) -> list[dict[str, Any]]:
        return get_exchange_transits(_service_context_caps([_port("load-balancer", *contexts)]))

    def test_prod_leg_is_the_context_vlan_and_its_transit_vni(self) -> None:
        (transit,) = self._transits(_lb_context())
        assert (transit["vlan_id"], transit["vni"], transit["transit_context"]) == (3100, 64100, "dc10-lb-01-PROD")
        segment = transit["segment"]
        # A shared context's name already ends in its namespace: the leg does not repeat it.
        assert segment["name"] == segment["customer_name"] == segment["vlan_description"] == "LB-dc10-lb-01-PROD"
        assert segment["gateway"] == {
            "address": "100.67.0.1/29",
            "ip_prefix": {
                "prefix": "100.67.0.0/29",
                "ip_namespace": {"name": "PROD", "l3_vni": 60000, "namespace_type": "prod"},
            },
        }

    def test_non_prod_leg_takes_the_non_prod_slot(self) -> None:
        ctx = _lb_context(name="dc10-lb-01-NON-PROD", vlan=3101, namespace=_NONPROD, leg="100.67.16.0/29")
        (transit,) = self._transits(ctx)
        assert (transit["vlan_id"], transit["vni"]) == (3301, 64301)

    def test_a_leg_name_past_32_characters_is_cut_with_a_digest_for_the_vlan_name(self) -> None:
        """The VLAN name fits NX-OS (32), deterministically; the full name stays the segment's and the SVI's."""
        name = "dc1-lb-01-dc1-lb-02-ha-NON-PROD"
        ctx = _lb_context(name=name, vlan=3101, namespace=_NONPROD, leg="100.67.16.0/29")
        (transit,) = self._transits(ctx)
        segment = transit["segment"]
        assert segment["name"] == segment["vlan_description"] == f"LB-{name}"
        assert segment["customer_name"] == "LB-dc1-lb-01-dc1-lb-02-ha-N-7de0"
        assert len(segment["customer_name"]) == 32
        assert self._transits(ctx)[0]["segment"]["customer_name"] == segment["customer_name"]

    def test_two_long_names_sharing_a_stem_get_distinct_vlan_names(self) -> None:
        first = _lb_context(name="dc1-lb-01-dc1-lb-02-ha-NON-PROD", vlan=3101, namespace=_NONPROD)
        second = _lb_context(name="dc1-lb-01-dc1-lb-02-ha-NON-PROD-B", vlan=3102, namespace=_NONPROD)
        names = [t["segment"]["customer_name"] for t in self._transits(first, second)]
        assert len(set(names)) == 2

    def test_a_long_firewall_leg_name_is_not_cut(self) -> None:
        """Only the load-balancer legs are cut; the XCHG names stay as they are."""
        fw = {
            **_lb_context(name="dc1-fw-01-dc1-fw-02-ha-NON-PROD"),
            "typename": "ManagedFirewallContext",
        }
        (transit,) = get_exchange_transits([fw])
        assert transit["segment"]["customer_name"] == "XCHG-dc1-fw-01-dc1-fw-02-ha-NON-PROD-PROD"

    def test_the_border_port_tagged_with_the_context_is_no_leg(self) -> None:
        """Only the members' (role load-balancer) addressed sub-interfaces are legs."""
        assert len(self._transits(_lb_context())) == 1

    def test_the_leg_port_is_a_trunk_of_the_leg_vlans(self) -> None:
        prod = _lb_context()
        nonprod = _lb_context(name="dc10-lb-01-NON-PROD", vlan=3101, namespace=_NONPROD, leg="100.67.16.0/29")
        port = _port("load-balancer", prod, nonprod)
        transits = get_exchange_transits(_service_context_caps([port]))
        (iface,) = get_interfaces([port], activations=transits)
        assert (iface["mode"], iface["vlans"], iface["dot1q_vlan"]) == ("trunk", [3100, 3301], None)

    def test_the_leg_is_no_policy_input(self) -> None:
        """No ACL, SGT or PBR is built from a leg activation (it carries no security_policy)."""
        assert get_customer_pbr_rules(self._transits(_lb_context()), []) == []


class TestVipRoute:
    """The VIP /26 is a static to the leg's floating .4 in the leg's VRF, always exported."""

    def test_vip_prefix_to_the_floating_address_in_the_tenant_vrf(self) -> None:
        routes = get_exchange_routes([_port("load-balancer", _lb_context())], export_tenant_default=False)
        assert routes == [{"vrf": "PROD", "l3_vni": 60000, "prefix": "100.68.0.0/26", "nexthop": "100.67.0.4"}]

    def test_each_context_routes_its_own_range(self) -> None:
        nonprod = _lb_context(
            name="dc10-lb-01-NON-PROD", vlan=3101, namespace=_NONPROD, leg="100.67.16.0/29", vip_prefix="100.68.16.0/26"
        )
        routes = get_exchange_routes([_port("load-balancer", _lb_context(), nonprod)])
        assert [(r["vrf"], r["prefix"], r["nexthop"]) for r in routes] == [
            ("NON-PROD", "100.68.16.0/26", "100.67.16.4"),
            ("PROD", "100.68.0.0/26", "100.67.0.4"),
        ]

    def test_no_vip_prefix_no_route_and_no_default(self) -> None:
        """The LB never leaves its VRF: no 0/0, no SNAT /32, nothing without a VIP range."""
        assert get_exchange_routes([_port("load-balancer", _lb_context(vip_prefix=None))]) == []

    def test_a_context_on_a_firewall_port_routes_nothing(self) -> None:
        assert get_exchange_routes([_port("firewall", _lb_context())]) == []

    def test_legs_in_two_vrfs_are_refused(self) -> None:
        stray = {
            "name": "1.1.3301",
            "device": {"name": "dc10-lb-01a", "role": "load-balancer"},
            "ip_address": {"address": "100.67.16.5/29", "ip_namespace": _NONPROD},
        }
        with pytest.raises(ValueError, match=r"legs in 2 VRFs \(NON-PROD, PROD\).*100\.68\.0\.0/26"):
            get_exchange_routes([_port("load-balancer", _lb_context(extra_legs=[stray]))])


class TestBorderLeafRender:
    """The border-leaf transform end to end (the smoke fixture's border leaf)."""

    def test_nxos_border_renders_both_legs_and_both_vip_statics(self) -> None:
        config = _run_transform(BorderLeaf, build_lb_context_bl_data(device_name="dc1-bl-01", platform="cisco_nxos"))
        assert "  name LB-dc1-lb-01-dc1-lb-02-ha-PROD\n" in config
        assert "  name LB-dc1-lb-01-dc1-lb-02-ha-N-7de0\n" in config
        assert "  description LB-dc1-lb-01-dc1-lb-02-ha-NON-PROD\n" in config
        assert "  switchport trunk allowed vlan 3100,3301" in config
        assert "  ip route 100.66.96.0/26 100.66.64.4" in config
        assert "  ip route 100.66.112.0/26 100.66.80.4" in config
        assert "ip prefix-list PL-VRF-STATIC-PROD seq 10 permit 100.66.96.0/26" in config
        assert "0.0.0.0/0" not in config


class TestLeafClientAndBackend:
    """Both ends of a load-balanced flow on a PBR leaf: the existing ACL/PBR code covers them, no LB PBR."""

    @pytest.fixture(scope="class")
    @classmethod
    def config(cls) -> str:
        return _run_transform(Leaf, build_leaf_lb_client_data(device_name="dc1-leaf-01", platform="cisco_nxos"))

    def test_client_permits_and_bypasses_the_vip(self, config: str) -> None:
        assert "  100 permit tcp 10.110.0.0/24 100.68.0.10/32 eq 443 ! web-to-app-vip-tcp-443" in config
        assert "ip access-list PBR-BYPASS-VLAN110\n  permit ip any 100.68.0.10/32\n" in config

    def test_backend_answers_the_leg_established_and_bypasses_it(self, config: str) -> None:
        assert "  5000 permit tcp 10.120.0.0/24 eq 8443 100.67.0.0/29 established" in config
        assert "ip access-list PBR-BYPASS-VLAN120\n  permit ip any 100.67.0.0/29\n" in config

    def test_the_only_next_hop_is_the_firewall(self, config: str) -> None:
        hops = {line.strip() for line in config.splitlines() if "set ip next-hop" in line}
        assert hops == {"set ip next-hop 100.66.0.4"}


def _gp_seg(seg_id: str, rules: list[dict[str, Any]], inbound: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """A cleaned SegmentGroupPolicyFields segment in PROD, tagged after its id."""
    return {
        "id": seg_id,
        "typename": "ManagedVxlanSegment",
        "name": seg_id,
        "owner": {"org_id": "C005"},
        "security_zone": {"name": "PROD-ZONE"},
        "security_tag": {"id": f"tag-{seg_id}", "name": seg_id, "group_id": 40 + len(seg_id)},
        "gateway": {"ip_prefix": {"prefix": "10.0.0.0/24", "ip_namespace": {"name": "PROD", "namespace_type": "prod"}}},
        "security_policy": {"name": f"seg-{seg_id}", "enabled": True, "default_action": "deny", "rules": rules},
        "inbound_rules": inbound or [],
        "segment_deployments": [{"deployment": {"id": "dc10"}}],
    }


def _gp_rule(name: str, dst: str | None, port: int) -> dict[str, Any]:
    """A cleaned GroupPolicyRuleFields rule."""
    return {
        "id": f"id-{name}",
        "name": name,
        "action": "permit",
        "protocol": "tcp",
        "port_start": port,
        "port_end": None,
        "disabled": False,
        "source_segment": None,
        "destination_segment": {"id": dst} if dst else None,
    }


def _inbound(rule: dict[str, Any], policy_segment: str | None, *, enabled: bool = True) -> dict[str, Any]:
    """``rule`` as one of its destination's inbound_rules: with its policy (``policy_segment`` None = segment-less)."""
    return {**rule, "policy": {"enabled": enabled, "segment": {"id": policy_segment} if policy_segment else None}}


class TestSegmentLessPolicyContracts:
    """R_client is client -> UNCLASSIFIED (already), R_server (segment-less lb-<context>) UNCLASSIFIED -> backend."""

    def _rows(self, *, enabled: bool = True) -> dict[tuple[str, str], list[int]]:
        client = _gp_seg("web", [_gp_rule("web-to-vip-tcp-443", None, 443)])
        backend = _gp_seg(
            "app", [], inbound=[_inbound(_gp_rule("lb-to-app-tcp-8443", "app", 8443), None, enabled=enabled)]
        )
        rows = get_vrf_contracts([client, backend], [])
        return {(row["src"], row["dst"]): [e["port_start"] for e in row["entries"]] for row in rows}

    def test_client_to_the_vip_and_the_leg_to_the_backend(self) -> None:
        assert self._rows() == {("web", UNCLASSIFIED): [443], (UNCLASSIFIED, "app"): [8443]}

    def test_a_disabled_segment_less_policy_adds_nothing(self) -> None:
        assert self._rows(enabled=False) == {("web", UNCLASSIFIED): [443]}

    def test_an_inbound_rule_of_a_segment_policy_is_read_from_its_source_only(self) -> None:
        """web -> app in web's policy is also one of app's inbound_rules: the same rows, once."""
        own = _gp_rule("web-to-app-tcp-443", "app", 443)
        rows = get_vrf_contracts([_gp_seg("web", [own]), _gp_seg("app", [], inbound=[_inbound(own, "web")])], [])
        assert [(row["src"], row["dst"], row["rules"]) for row in rows] == [
            (UNCLASSIFIED, "app", ["web-to-app-tcp-443"]),
            ("web", "app", ["web-to-app-tcp-443"]),
            ("web", UNCLASSIFIED, ["web-to-app-tcp-443"]),
        ]


class TestQueries:
    """The fragments select what the border transform and the group contract builder read."""

    @staticmethod
    def _fragment(root_dir: Path, path: str, name: str) -> str:
        document = parse((root_dir / path).read_text())
        return next(
            print_ast(item)
            for item in document.definitions
            if isinstance(item, FragmentDefinitionNode) and item.name.value == name
        )

    def test_interface_edge_carries_the_load_balancer_context(self, root_dir: Path) -> None:
        path = "queries/fragments/interface_capability_fields.gql"
        spread = self._fragment(root_dir, path, "InterfaceCapabilitiesWithSegmentFields")
        assert "...LoadbalancerContextInterfaceCapabilityFields" in spread
        fragment = self._fragment(root_dir, path, "LoadbalancerContextInterfaceCapabilityFields")
        assert fragment.startswith(
            "fragment LoadbalancerContextInterfaceCapabilityFields on ManagedLoadbalancerContext"
        )
        for selection in (
            "vlan_id {",
            "vip_prefix {\n    node {\n      prefix {",
            "role {",
            "... on DcimVirtualInterface",
            "namespace_type {",
            "l3_vni {",
        ):
            assert selection in fragment

    def test_group_policy_segments_read_inbound_rules_with_their_policy_segment(self, root_dir: Path) -> None:
        fragment = self._fragment(root_dir, "queries/fragments/group_policy.gql", "SegmentGroupPolicyFields")
        assert "inbound_rules {" in fragment
        inbound = fragment[fragment.index("inbound_rules {") :]
        assert "...GroupPolicyRuleFields" in inbound
        assert "policy {\n          node {\n            enabled {" in inbound
        assert "segment {\n              node {\n                id" in inbound
