"""Unit tests for the leaf-local firewall transit leg (PBR next hop is a connected VIP).

Covers:
- get_exchange_transits(contexts, legs=...) — shared by the border (all legs) and the leaf (used legs)
- get_leaf_firewall_transits() — scoped legs, determinism and the fail-closed raise
- BaseDeviceTransform._extra_config() — leaf transits become VLANs/VXLAN, never host ports or ACL/PBR input
- NX-OS / EOS leaf templates — the transit /29 deny, rendered only with leaf transits
- validate_transit_vlans() on a leaf
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import jinja2
import pytest

from checks.common import validate_transit_vlans
from transforms.common import BaseDeviceTransform
from transforms.config.leaf import Leaf
from transforms.helpers.firewall import (
    _firewall_context_caps,
    get_customer_pbr_rules,
    get_exchange_transits,
    get_leaf_firewall_transits,
)

_PROD_NS = {"name": "PROD", "namespace_type": "prod", "l3_vni": 50001}
_NONPROD_NS = {"name": "NON-PROD", "namespace_type": "non_prod", "l3_vni": 50002}


def _context(
    *,
    name: str = "dc1-fw-shared",
    vlan: int | None = 3000,
    ctx_id: str = "ctx-shared",
    tenant: str | None = None,
    legs: tuple[tuple[str, dict], ...] = (("100.66.0.5/29", _PROD_NS),),
    served: tuple[str, ...] = (),
) -> dict[str, Any]:
    """A ManagedFirewallContext as the leaf query returns it (legs on two HA members)."""
    iface_caps = []
    for address, namespace in legs:
        network = address.rsplit(".", 1)[0]
        for member, host in (("fw-01", 5), ("fw-02", 6)):
            iface_caps.append(
                {
                    "name": "eth1.3000",
                    "device": {"role": "firewall"},
                    "ip_address": {"address": f"{network}.{host}/29", "ip_namespace": namespace},
                }
            )
    return {
        "id": ctx_id,
        "name": name,
        "vlan_id": vlan,
        "tenant": {"id": tenant} if tenant else None,
        "served_deployments": [{"id": dep} for dep in served],
        "capabilities": iface_caps,
    }


def _activation(vlan: int = 100, *, dep: str = "dep-a", namespace: dict | None = None) -> dict[str, Any]:
    """A routed segment activation in a tenant VRF whose security_policy was queried."""
    namespace = namespace or _PROD_NS
    return {
        "vlan_id": vlan,
        "vni": 10000 + vlan,
        "segment": {
            "id": f"seg-{vlan}",
            "name": f"seg-{vlan}",
            "customer_name": f"seg-{vlan}",
            "customer_deployments": [{"id": dep}],
            "security_policy": None,
            "gateway": {
                "address": f"10.{vlan}.0.1/24",
                "ip_prefix": {"prefix": f"10.{vlan}.0.0/24", "ip_namespace": namespace},
            },
            "arp_suppression": True,
            "terminate_inline": False,
        },
    }


def _deployment(contexts: list[dict]) -> dict[str, Any]:
    return {"devices": [{"capabilities": [{"typename": "ManagedFirewallHA", "contexts": contexts}]}]}


def _device(contexts: list[dict], activations: list[dict], *, name: str = "leaf-01") -> dict[str, Any]:
    return {
        "name": name,
        "role": "leaf",
        "interfaces": [],
        "capabilities": [],
        "deployment": _deployment(contexts),
        "segment_deployments": activations,
    }


def _transform(role: str = "leaf") -> BaseDeviceTransform:
    transform = BaseDeviceTransform.__new__(BaseDeviceTransform)
    transform.device_role = role
    transform.template_subdir = "leafs"
    transform.root_directory = "/fake/root"
    return transform


def _rule(nexthop: str = "100.66.0.4", namespace: str | None = "PROD", vlan: int = 100) -> dict[str, Any]:
    return {"vlan_id": vlan, "segment_name": f"seg-{vlan}", "fw_nexthop": nexthop, "fw_namespace": namespace}


class TestGetExchangeTransitsLegs:
    """get_exchange_transits is the one implementation for border (all legs) and leaf (used legs)."""

    def test_no_legs_filter_keeps_every_leg(self) -> None:
        """The border passes no filter: both VRF legs of the context become transits."""
        ctx = _context(legs=(("100.66.0.5/29", _PROD_NS), ("100.66.32.5/29", _NONPROD_NS)))
        assert [t["vlan_id"] for t in get_exchange_transits([ctx])] == [3000, 3200]

    def test_legs_filter_keeps_only_the_wanted_namespace_and_vip(self) -> None:
        """The leaf asks for the (namespace, VIP) it redirects to."""
        ctx = _context(legs=(("100.66.0.5/29", _PROD_NS), ("100.66.32.5/29", _NONPROD_NS)))
        transits = get_exchange_transits([ctx], legs={("NON-PROD", "100.66.32.4")})
        assert [(t["vlan_id"], t["vni"]) for t in transits] == [(3200, 64200)]

    def test_legs_filter_with_other_vip_returns_nothing(self) -> None:
        """A (namespace, VIP) pair that matches no leg yields no transit."""
        assert get_exchange_transits([_context()], legs={("PROD", "100.66.9.4")}) == []

    def test_border_and_leaf_derivation_are_equal(self) -> None:
        """AC2: the leaf's transit for a leg equals the border's for the same context."""
        ctx = _context()
        border = get_exchange_transits(
            _firewall_context_caps(
                [{"role": "firewall", "capabilities": [{**ctx, "typename": "ManagedFirewallContext"}]}]
            )
        )
        leaf = get_exchange_transits([ctx], legs={("PROD", "100.66.0.4")})
        assert leaf == border


class TestGetLeafFirewallTransits:
    def test_rule_vip_gets_a_local_transit_leg(self) -> None:
        """The redirected VIP's leg is derived: VLAN ctx+0, VNI 61000+vlan... via the shared helper."""
        transits = get_leaf_firewall_transits([_rule()], [_context()], device_name="leaf-01")
        assert len(transits) == 1
        assert transits[0]["vlan_id"] == 3000
        assert transits[0]["vni"] == 64000
        assert transits[0]["segment"]["gateway"]["address"] == "100.66.0.1/29"

    def test_non_prod_leg_uses_its_slot(self) -> None:
        """The band VLAN follows the namespace type of the segment's own VRF."""
        ctx = _context(legs=(("100.66.32.5/29", _NONPROD_NS),))
        transits = get_leaf_firewall_transits([_rule("100.66.32.4", "NON-PROD")], [ctx], device_name="leaf-01")
        assert [t["vlan_id"] for t in transits] == [3200]

    def test_only_the_redirected_context_is_rendered(self) -> None:
        """AC3: an unused context and an unused leg of the used context stay out."""
        shared = _context(legs=(("100.66.0.5/29", _PROD_NS), ("100.66.32.5/29", _NONPROD_NS)))
        other = _context(name="dc1-fw-other", ctx_id="ctx-2", vlan=3010, legs=(("100.66.8.5/29", _PROD_NS),))
        transits = get_leaf_firewall_transits([_rule()], [shared, other], device_name="leaf-01")
        assert [t["vlan_id"] for t in transits] == [3000]

    def test_two_contexts_give_two_transits_sorted_by_vlan(self) -> None:
        """A leaf redirecting to a dedicated and a shared context holds both legs."""
        shared = _context()
        dedicated = _context(
            name="dc1-fw-c1", ctx_id="ctx-2", vlan=3010, tenant="dep-b", legs=(("100.66.8.5/29", _PROD_NS),)
        )
        rules = [_rule("100.66.8.4", vlan=200), _rule("100.66.0.4", vlan=100)]
        transits = get_leaf_firewall_transits(rules, [dedicated, shared], device_name="leaf-01")
        assert [(t["vlan_id"], t["transit_context"]) for t in transits] == [
            (3000, "dc1-fw-shared"),
            (3010, "dc1-fw-c1"),
        ]

    def test_context_order_does_not_change_the_result(self) -> None:
        """AC7: shuffled contexts and rules render the same transits."""
        shared = _context()
        dedicated = _context(
            name="dc1-fw-c1", ctx_id="ctx-2", vlan=3010, tenant="dep-b", legs=(("100.66.8.5/29", _PROD_NS),)
        )
        rules = [_rule("100.66.0.4", vlan=100), _rule("100.66.8.4", vlan=200)]
        forward = get_leaf_firewall_transits(rules, [shared, dedicated], device_name="l")
        backward = get_leaf_firewall_transits(rules[::-1], [dedicated, shared], device_name="l")
        assert forward == backward

    def test_rules_to_one_vip_dedupe_to_one_transit(self) -> None:
        """Several segments redirected to the same VIP share one transit."""
        rules = [_rule(vlan=100), _rule(vlan=200)]
        assert len(get_leaf_firewall_transits(rules, [_context()], device_name="leaf-01")) == 1

    def test_no_firewall_rule_means_no_transit(self) -> None:
        """Bypass-only leaves (no fw_nexthop) hold no transit leg."""
        rules = [{"vlan_id": 100, "fw_nexthop": None, "bypass_prefixes": ["10.0.2.0/24"]}]
        assert get_leaf_firewall_transits(rules, [_context()], device_name="leaf-01") == []
        assert get_leaf_firewall_transits([], [_context()], device_name="leaf-01") == []

    @pytest.mark.parametrize(
        ("mutation", "field"),
        [({"vlan": None}, "vlan_id"), ({"name": None}, "name")],
    )
    def test_context_without_vlan_or_name_raises_fail_closed(self, mutation: dict, field: str) -> None:
        """AC4: a missing context vlan_id/name raises, naming leaf, segment and VIP (never a silent no-op)."""
        ctx = _context(**mutation)
        with pytest.raises(ValueError, match=r"leaf 'leaf-01' has no local transit leg .*100\.66\.0\.4.*seg-100"):
            get_leaf_firewall_transits([_rule()], [ctx], device_name="leaf-01")

    def test_context_without_namespace_l3_vni_raises(self) -> None:
        """A leg whose VRF namespace carries no l3_vni cannot be bridged into: refuse."""
        ctx = _context(legs=(("100.66.0.5/29", {**_PROD_NS, "l3_vni": None}),))
        with pytest.raises(ValueError, match="no local transit leg"):
            get_leaf_firewall_transits([_rule()], [ctx], device_name="leaf-01")

    def test_raise_names_the_context(self) -> None:
        """The message names the context owning the next hop."""
        with pytest.raises(ValueError, match="context 'dc1-fw-shared'"):
            get_leaf_firewall_transits([_rule()], [_context(vlan=None)], device_name="leaf-01")

    def test_default_namespace_next_hop_raises(self) -> None:
        """A rule without a derivable (non-default) VRF leg raises."""
        with pytest.raises(ValueError, match="no local transit leg"):
            get_leaf_firewall_transits([_rule(namespace=None)], [_context()], device_name="leaf-01")

    def test_no_contexts_with_a_next_hop_raises(self) -> None:
        """A next hop with no context data at all still refuses."""
        with pytest.raises(ValueError, match="no local transit leg"):
            get_leaf_firewall_transits([_rule()], None, device_name="leaf-01")


class TestPbrRuleNamespace:
    def test_firewall_rule_carries_the_segments_namespace(self) -> None:
        """get_customer_pbr_rules names the leg (namespace) its fw_nexthop belongs to."""
        rules = get_customer_pbr_rules([_activation()], [_context(served=("dep-a",))])
        assert rules[0]["fw_nexthop"] == "100.66.0.4"
        assert rules[0]["fw_namespace"] == "PROD"


class TestLeafExtraConfig:
    def test_leaf_transit_becomes_vlan_and_vxlan_mapping(self) -> None:
        """The PBR next hop's leg is rendered through vlans / vxlan, with the explicit VNI and L3 VNI."""
        device = _device([_context()], [_activation()])
        config = _transform()._extra_config(device, "cisco_nxos")
        transit = next(v for v in config["vlans"] if v["vlan_id"] == 3000)
        assert (transit["vni"], transit["l3_vni"]) == (64000, 50001)
        assert 3000 in {m["vlan_id"] for m in config["vxlan"]["l2_vni_mappings"]}
        assert config["leaf_transit_prefixes"] == ["100.66.0.0/29"]

    def test_transit_is_never_acl_or_pbr_input(self) -> None:
        """AC5: the transit VLAN gets no ACL and no PBR rule."""
        device = _device([_context()], [_activation()])
        config = _transform()._extra_config(device, "cisco_nxos")
        assert 3000 not in {a["vlan_id"] for a in config["acls"]}
        assert 3000 not in {r["vlan_id"] for r in config["leaf_pbr_rules"]}
        assert next(v for v in config["vlans"] if v["vlan_id"] == 3000)["acl_name"] is None

    def test_transit_vlan_is_not_bound_to_host_ports(self) -> None:
        """The transit VLAN never reaches get_interfaces (host trunk/access ports)."""
        device = _device([_context()], [_activation()])
        host_port = {"name": "Ethernet1/1", "role": "customer", "status": "active", "capabilities": []}
        device["interfaces"] = [host_port]
        transform = _transform()
        config = transform._build_config(device, "cisco_nxos")
        transform._extra_config(device, "cisco_nxos")
        assert all(3000 not in (iface.get("vlans") or []) for iface in config["interfaces"])

    def test_leaf_without_firewall_pbr_is_unchanged(self) -> None:
        """A leaf with no context keeps its VLANs and gets no transit prefixes (non-PBR leaf stays identical)."""
        config = _transform()._extra_config(_device([], [_activation()]), "cisco_nxos")
        assert [v["vlan_id"] for v in config["vlans"]] == [100]
        assert config["leaf_transit_prefixes"] == []
        assert config["leaf_pbr_rules"] == []

    def test_leaf_redirecting_to_two_contexts_holds_both_legs(self) -> None:
        """Dedicated beats shared per segment; the leaf renders both legs."""
        shared = _context(served=("dep-a",))
        dedicated = _context(
            name="dc1-fw-c1", ctx_id="ctx-2", vlan=3010, tenant="dep-b", legs=(("100.66.8.5/29", _PROD_NS),)
        )
        device = _device([shared, dedicated], [_activation(100, dep="dep-a"), _activation(200, dep="dep-b")])
        config = _transform()._extra_config(device, "cisco_nxos")
        assert {3000, 3010} <= {v["vlan_id"] for v in config["vlans"]}
        assert config["leaf_transit_prefixes"] == ["100.66.0.0/29", "100.66.8.0/29"]

    def test_leaf_with_unresolvable_leg_raises(self) -> None:
        """AC4: a firewall next hop with no derivable leg refuses the config."""
        device = _device([_context(vlan=None)], [_activation()])
        with pytest.raises(ValueError, match="leaf 'leaf-01' has no local transit leg"):
            _transform()._extra_config(device, "cisco_nxos")

    @pytest.mark.parametrize("platform", ["sonic", "dell_sonic"])
    @pytest.mark.parametrize("role", ["leaf", "tor", "access-leaf"])
    def test_sonic_leaf_with_firewall_pbr_refuses(self, platform: str, role: str) -> None:
        """SONiC cannot render the transit leg: a firewall-PBR leaf is refused."""
        device = _device([_context()], [_activation()])
        with pytest.raises(ValueError, match="cannot render the inter-VRF exchange transit legs"):
            _transform(role)._extra_config(device, platform)

    def test_sonic_leaf_without_firewall_pbr_still_renders(self) -> None:
        """The refusal is scoped to firewall PBR: a SONiC leaf with no context renders."""
        config = _transform()._extra_config(_device([], [_activation()]), "sonic")
        assert config["leaf_transit_prefixes"] == []

    def test_sr_os_leaf_with_firewall_pbr_refuses(self) -> None:
        """SR OS keeps its PBR refusal once the leg is derivable."""
        device = _device([_context()], [_activation()])
        with pytest.raises(ValueError, match="cannot render PBR policy"):
            _transform()._extra_config(device, "nokia_sros")

    def test_border_leaf_keeps_refusing_pbr_and_holds_no_leaf_transit(self) -> None:
        """The border-leaf refusal stays; border roles never derive leaf transits."""
        transform = _transform("border-leaf")
        device = _device([_context()], [_activation()])
        with pytest.raises(ValueError, match="border-leaf cannot render PBR policy"):
            transform._extra_config(device, "cisco_nxos")
        _, leaf_transits = transform._firewall_pbr(device, device["segment_deployments"])
        assert leaf_transits == []


def _render(platform: str, **overrides: object) -> str:
    """Render the leaf template of ``platform`` with a one-VRF VXLAN context."""
    template_dir = Path(__file__).parents[2] / "templates" / "configs"
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(template_dir)))
    context: dict[str, object] = {
        "hostname": "leaf",
        "name": "leaf",
        "vlans": [],
        "interfaces": [],
        "loopback_name": "Loopback0" if platform == "arista_eos" else "loopback0",
        "acls": [],
        "vxlan": {
            "enabled": True,
            "l2_vni_mappings": [],
            "l3_vni_mappings": [{"vrf_name": "PROD", "l3_vni": 50001, "route_target": "65000:50001"}],
            "evpn": {},
            "vtep": {"ipv4": "10.0.0.1"},
        },
        "vrf_routes": [],
        "bgp": [
            {
                "local_as": {"asn": 65000},
                "router_ip": "10.0.0.1",
                "peer_groups": [],
                "sessions": [],
                "vrf_sessions": {},
                "vrf_af_sessions": {},
            }
        ],
        "ospf": [],
        "mlag": None,
        "leaf_pbr_rules": [],
        **overrides,
    }
    return env.get_template(f"leafs/{platform}.j2").render(**context)


class TestLeafTemplates:
    def test_nxos_denies_the_leaf_transit_prefixes_in_direct_to_evpn(self) -> None:
        """AC6 (NX-OS): one prefix-list entry per /29, deny 5 before the permit-all 10."""
        rendered = _render("cisco_nxos", leaf_transit_prefixes=["100.66.0.0/29", "100.66.8.0/29"])
        assert "ip prefix-list PL-XCHG-TRANSIT seq 10 permit 100.66.0.0/29" in rendered
        assert "ip prefix-list PL-XCHG-TRANSIT seq 20 permit 100.66.8.0/29" in rendered
        assert rendered.index("RM-VRF-DIRECT-2-EVPN deny 5") < rendered.index("RM-VRF-DIRECT-2-EVPN permit 10")
        assert "documented-not-lab-verified" in rendered

    def test_nxos_without_leaf_transits_renders_no_deny(self) -> None:
        """AC6 (NX-OS): no leaf transit, no prefix-list and no deny entry."""
        rendered = _render("cisco_nxos", leaf_transit_prefixes=[])
        assert "PL-XCHG-TRANSIT" not in rendered
        assert "RM-VRF-DIRECT-2-EVPN deny" not in rendered
        assert "route-map RM-VRF-DIRECT-2-EVPN permit 10" in rendered

    def test_eos_redistributes_connected_through_the_deny_route_map(self) -> None:
        """AC6 (EOS): the VRF's connected redistribution uses the route-map that denies the transit /29."""
        rendered = _render("arista_eos", leaf_transit_prefixes=["100.66.0.0/29"])
        assert "ip prefix-list PL-XCHG-TRANSIT seq 10 permit 100.66.0.0/29" in rendered
        assert "route-map RM-VRF-CONN-2-EVPN deny 5" in rendered

    def test_eos_golden_redistributes_through_the_route_map_only_with_leaf_transits(self) -> None:
        """AC6 (EOS): the PBR golden uses the route-map; a leaf without transits keeps the bare statement."""
        configs = Path(__file__).parents[1] / "smoke" / "configs"
        pbr = (configs / "leaf_arista_eos_pbr" / "output.txt").read_text()
        plain = (configs / "leaf_arista_eos_microsegmented" / "output.txt").read_text()
        assert "redistribute connected route-map RM-VRF-CONN-2-EVPN" in pbr
        assert "RM-VRF-CONN-2-EVPN" not in plain

    def test_eos_without_leaf_transits_keeps_plain_redistribute(self) -> None:
        """AC6 (EOS): without leaf transits the VRF stays on a bare `redistribute connected`."""
        rendered = _render("arista_eos", leaf_transit_prefixes=[])
        assert "RM-VRF-CONN-2-EVPN" not in rendered
        assert "PL-XCHG-TRANSIT" not in rendered


class TestValidateLeafTransitVlans:
    def test_distinct_vlans_pass(self) -> None:
        """A leaf whose transit VLAN differs from every segment VLAN has no finding."""
        device = _device([_context()], [_activation()])
        with patch.object(Leaf, "_collect_activations_from_interfaces", return_value=device["segment_deployments"]):
            assert validate_transit_vlans(device, transform_cls=Leaf) == []

    def test_collision_with_a_segment_vlan_is_reported(self) -> None:
        """A segment on the transit VLAN would silently drop one of the two."""
        device = _device([_context()], [_activation(3000)])
        with patch.object(Leaf, "_collect_activations_from_interfaces", return_value=device["segment_deployments"]):
            errors = validate_transit_vlans(device, transform_cls=Leaf)
        assert len(errors) == 1
        assert "VLAN 3000" in errors[0]
        assert "leaf-01" in errors[0]

    def test_unresolvable_leg_is_reported_not_raised(self) -> None:
        """The check turns the transform's fail-closed raise into a finding."""
        device = _device([_context(vlan=None)], [_activation()])
        with patch.object(Leaf, "_collect_activations_from_interfaces", return_value=device["segment_deployments"]):
            errors = validate_transit_vlans(device, transform_cls=Leaf)
        assert len(errors) == 1
        assert "no local transit leg" in errors[0]
