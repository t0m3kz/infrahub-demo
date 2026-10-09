"""Unit tests for SecurityTag / SGT helpers.

Covers:
  - get_sgt_rules()               — tier contracts derived from the segments' permit rules
  - sgt/sgt_name in VLAN dicts   — _vlans_from_activations with security_tag
  - sgt/sgt_name in L2 VNI dicts — _l2_from_activations with security_tag
  - Arista EOS template           — mac security sgt-policy blocks rendered
  - Cisco NX-OS template          — cts role-based permissions blocks rendered
"""

from pathlib import Path

import jinja2

from transforms.helpers.policy import get_sgt_rules
from transforms.helpers.segments import _vlans_from_activations
from transforms.helpers.vxlan import _l2_from_activations

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TEMPLATE_ROOT = Path(__file__).parents[2] / "templates" / "configs"


def _load_template(subdir: str, platform: str) -> jinja2.Template:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(TEMPLATE_ROOT)),
        autoescape=False,
        keep_trailing_newline=True,
    )
    return env.get_template(f"{subdir}/{platform}.j2")


def _make_activation(
    *,
    vlan_id: int = 100,
    vni: int = 10100,
    customer_name: str = "web-frontend",
    sgt_name: str | None = None,
    sgt_group_id: int | None = None,
) -> dict:
    seg: dict = {
        "name": f"c001-{customer_name}-p",
        "customer_name": customer_name,
        "arp_suppression": True,
        "prefix": {"ip_namespace": {"name": "C001-PROD"}},
    }
    if sgt_name or sgt_group_id:
        seg["security_tag"] = {"name": sgt_name, "group_id": sgt_group_id}
    return {"vlan_id": vlan_id, "vni": vni, "segment": seg}


def _make_sgt_rule(
    src_name: str = "web-tier",
    src_sgt: int = 20,
    dst_name: str = "app-tier",
    dst_sgt: int = 30,
    action: str = "permit",
    log: bool = False,
) -> dict:
    return {
        "src_name": src_name,
        "src_sgt": src_sgt,
        "dst_name": dst_name,
        "dst_sgt": dst_sgt,
        "action": action,
        "log": log,
    }


def _tagged(seg_id: str, tag: str | None, group_id: int | None, **extra: object) -> dict:
    """A rule end (PolicyRuleSegmentFields, cleaned) with its tier tag."""
    return {
        "id": seg_id,
        "name": seg_id,
        "security_tag": {"name": tag, "group_id": group_id} if group_id else None,
        **extra,
    }


_WEB = _tagged("seg-web", "web-tier", 20, customer_name="web-frontend", environment="s")
_APP = _tagged("seg-app", "app-tier", 30, customer_name="app-backend")
_DB = _tagged("seg-db", "database", 50, customer_name="db")
_UNTAGGED = _tagged("seg-plain", None, None)


def _rule(src: dict, dst: dict | None, *, action: str = "permit", disabled: bool = False, log: bool = False) -> dict:
    return {
        "name": f"{src['id']}-to-{(dst or {}).get('id')}",
        "action": action,
        "disabled": disabled,
        "log": log,
        "source_segment": src,
        "destination_segment": dst,
    }


def _rules_activation(
    segment: dict, *, rules: list[dict] | None = None, inbound: list[dict] | None = None, enabled: bool = True
) -> dict:
    """Activation of ``segment`` with its own policy's rules and the rules into it."""
    seg = {
        **segment,
        "security_policy": {"name": f"{segment['id']}-egress", "enabled": enabled, "rules": rules or []},
        "inbound_rules": [{**rule, "policy": {"enabled": True}} for rule in inbound or []],
    }
    return {"vlan_id": 100, "vni": 10100, "segment": seg}


# ===========================================================================
# get_sgt_rules()
# ===========================================================================


class TestGetSgtRules:
    def test_none_returns_empty(self) -> None:
        assert get_sgt_rules(None) == []

    def test_empty_list_returns_empty(self) -> None:
        assert get_sgt_rules([]) == []

    def test_untagged_segment_returns_empty(self) -> None:
        assert get_sgt_rules([_make_activation(vlan_id=10)]) == []

    def test_permit_between_tagged_segments_is_a_contract(self) -> None:
        result = get_sgt_rules([_rules_activation(_WEB, rules=[_rule(_WEB, _APP, log=True)])])
        assert result == [
            {
                "src_name": "web-tier",
                "src_sgt": 20,
                "dst_name": "app-tier",
                "dst_sgt": 30,
                "action": "permit",
                "log": True,
                "src_customer": "web-frontend",
                "src_environment": "s",
            }
        ]

    def test_inbound_permit_is_a_contract_on_the_destination_leaf(self) -> None:
        """The egress VTEP enforces the contract, so a leaf carrying only the destination needs it."""
        result = get_sgt_rules([_rules_activation(_APP, inbound=[_rule(_WEB, _APP)])])
        assert [(r["src_sgt"], r["dst_sgt"]) for r in result] == [(20, 30)]
        assert result[0]["src_customer"] == "web-frontend"

    def test_deny_disabled_and_disabled_policy_rules_are_not_contracts(self) -> None:
        acts = [
            _rules_activation(_WEB, rules=[_rule(_WEB, _APP, action="deny"), _rule(_WEB, _DB, disabled=True)]),
            _rules_activation(_APP, rules=[_rule(_APP, _DB)], enabled=False),
        ]
        assert get_sgt_rules(acts) == []

    def test_an_untagged_end_is_not_a_contract(self) -> None:
        acts = [_rules_activation(_WEB, rules=[_rule(_WEB, _UNTAGGED), _rule(_WEB, None)])]
        assert get_sgt_rules(acts) == []

    def test_tag_pair_deduplicated_and_sorted(self) -> None:
        """Several rules (and both ends of one) between two tiers are one contract."""
        acts = [
            _rules_activation(_APP, rules=[_rule(_APP, _DB)], inbound=[_rule(_WEB, _APP)]),
            _rules_activation(_WEB, rules=[_rule(_WEB, _APP), _rule(_WEB, _APP, log=True)]),
        ]
        assert [(r["src_sgt"], r["dst_sgt"]) for r in get_sgt_rules(acts)] == [(20, 30), (30, 50)]

    def test_environment_none_when_absent(self) -> None:
        result = get_sgt_rules([_rules_activation(_APP, rules=[_rule(_APP, _DB)])])
        assert result[0]["src_environment"] is None


# ===========================================================================
# _vlans_from_activations() — sgt fields
# ===========================================================================


class TestVlansFromActivationsSgt:
    def test_sgt_fields_present_when_tagged(self) -> None:
        acts = [_make_activation(vlan_id=10, sgt_name="web-tier", sgt_group_id=20)]
        result = _vlans_from_activations(acts)
        assert result[0]["sgt"] == 20
        assert result[0]["sgt_name"] == "web-tier"

    def test_sgt_fields_none_when_untagged(self) -> None:
        acts = [_make_activation(vlan_id=10)]
        result = _vlans_from_activations(acts)
        assert result[0]["sgt"] is None
        assert result[0]["sgt_name"] is None

    def test_sgt_preserved_across_multiple_vlans(self) -> None:
        acts = [
            _make_activation(vlan_id=10, customer_name="web-frontend", sgt_name="web-tier", sgt_group_id=20),
            _make_activation(vlan_id=20, customer_name="app-backend", sgt_name="app-tier", sgt_group_id=30),
            _make_activation(vlan_id=30, customer_name="database", sgt_name="database", sgt_group_id=50),
        ]
        result = _vlans_from_activations(acts)
        sgts = {v["vlan_id"]: v["sgt"] for v in result}
        assert sgts == {10: 20, 20: 30, 30: 50}


# ===========================================================================
# _l2_from_activations() — sgt fields
# ===========================================================================


class TestL2FromActivationsSgt:
    def test_sgt_fields_present_when_tagged(self) -> None:
        acts = [_make_activation(vlan_id=10, vni=10010, sgt_name="web-tier", sgt_group_id=20)]
        result = _l2_from_activations(acts)
        assert result[0]["sgt"] == 20
        assert result[0]["sgt_name"] == "web-tier"

    def test_sgt_fields_none_when_untagged(self) -> None:
        acts = [_make_activation(vlan_id=10, vni=10010)]
        result = _l2_from_activations(acts)
        assert result[0]["sgt"] is None
        assert result[0]["sgt_name"] is None


# ===========================================================================
# Arista EOS template — SGT blocks
# ===========================================================================


class TestAristaEosSgtTemplate:
    def _render(self, vlans: list, sgt_rules: list) -> str:
        tpl = _load_template("leafs", "arista_eos")
        return tpl.render(
            hostname="DC1-LEAF-01",
            vlans=vlans,
            sgt_rules=sgt_rules,
            interfaces=[],
            loopback_name="Loopback0",
            acls=[],
            vxlan=None,
            vrf_gateways={},
            bgp=None,
            ospf=None,
            mlag=None,
        )

    def test_tag_without_rules_still_renders_profile(self) -> None:
        vlans = [
            {
                "vlan_id": 10,
                "name": "web",
                "sgt": 20,
                "sgt_name": "web-tier",
                "gateway_ip": None,
                "gateway_ipv6": None,
                "vrf": None,
                "isolation_mode": "normal",
                "arp_suppression": True,
            }
        ]
        output = self._render(vlans, [])
        assert "mac security profile SGT-20" in output
        assert "sgt-policy" not in output

    def test_sgt_block_rendered_when_rules_present(self) -> None:
        vlans = [
            {
                "vlan_id": 10,
                "name": "web-frontend",
                "sgt": 20,
                "sgt_name": "web-tier",
                "gateway_ip": None,
                "gateway_ipv6": None,
                "vrf": None,
                "isolation_mode": "normal",
                "arp_suppression": True,
            }
        ]
        rules = [_make_sgt_rule()]
        output = self._render(vlans, rules)
        assert "mac security" in output
        assert "security-group 20" in output
        assert "20-to-30" in output

    def test_untagged_vlans_skipped_in_sgt_block(self) -> None:
        vlans = [
            {
                "vlan_id": 10,
                "name": "untagged",
                "sgt": None,
                "sgt_name": None,
                "gateway_ip": None,
                "gateway_ipv6": None,
                "vrf": None,
                "isolation_mode": "normal",
                "arp_suppression": True,
            }
        ]
        rules = [_make_sgt_rule()]
        output = self._render(vlans, rules)
        assert "security-group" not in output


# ===========================================================================
# Cisco NX-OS template — CTS blocks
# ===========================================================================


class TestCiscoNxosSgtTemplate:
    def _render(self, vlans: list, sgt_rules: list) -> str:
        tpl = _load_template("leafs", "cisco_nxos")
        return tpl.render(
            name="DC1-LEAF-01",
            vlans=vlans,
            sgt_rules=sgt_rules,
            interfaces=[],
            loopback_name="loopback0",
            acls=[],
            vxlan=None,
            vrf_gateways={},
            bgp=[],
            ospf=[],
            mlag=None,
        )

    def test_tag_without_rules_still_renders_vlan_mapping(self) -> None:
        vlans = [{"vlan_id": 10, "name": "web", "sgt": 20, "sgt_name": "web-tier", "isolation_mode": "normal"}]
        output = self._render(vlans, [])
        assert "feature cts" in output
        assert "cts role-based sgt-map vlan 10 sgt 20" in output
        assert "cts role-based permissions" not in output

    def test_cts_block_rendered_when_rules_present(self) -> None:
        vlans = [{"vlan_id": 10, "name": "web-frontend", "sgt": 20, "sgt_name": "web-tier", "isolation_mode": "normal"}]
        rules = [_make_sgt_rule()]
        output = self._render(vlans, rules)
        assert "feature cts" in output
        assert "cts role-based sgt-map vlan 10 sgt 20" in output
        assert "cts role-based permissions from 20 to 30 permit" in output

    def test_deny_rule_renders_correctly(self) -> None:
        vlans = [{"vlan_id": 10, "name": "web", "sgt": 20, "sgt_name": "web-tier", "isolation_mode": "normal"}]
        rules = [_make_sgt_rule(action="deny")]
        output = self._render(vlans, rules)
        assert "cts role-based permissions from 20 to 30 deny" in output

    def test_untagged_vlans_skipped_in_cts_block(self) -> None:
        vlans = [{"vlan_id": 10, "name": "untagged", "sgt": None, "sgt_name": None, "isolation_mode": "normal"}]
        rules = [_make_sgt_rule()]
        output = self._render(vlans, rules)
        assert "cts role-based sgt-map" not in output
