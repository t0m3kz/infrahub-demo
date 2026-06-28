"""Unit tests for SecurityTag / SGT helpers.

Covers:
  - _get_sgt_rules()              — extract rules from extra_roots
  - sgt/sgt_name in VLAN dicts   — _vlans_from_activations with security_tag
  - sgt/sgt_name in L2 VNI dicts — _l2_from_activations with security_tag
  - Arista EOS template           — mac security sgt-policy blocks rendered
  - Cisco NX-OS template          — cts role-based permissions blocks rendered
"""

from pathlib import Path

import jinja2

from transforms.common import _get_sgt_rules
from transforms.helpers.segments import _vlans_from_activations
from transforms.helpers.vxlan import _l2_from_activations


# Convenience: build a sgt_rules list directly from activations for template tests
def _rules_from_acts(acts: list) -> list:
    return _get_sgt_rules(acts)


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


def _make_activation_with_rules(
    *,
    vlan_id: int = 100,
    vni: int = 10100,
    customer_name: str = "web-frontend",
    sgt_name: str | None = None,
    sgt_group_id: int | None = None,
    rules: list[dict] | None = None,
) -> dict:
    """Build activation with security_tag.rules_as_source populated."""
    seg: dict = {
        "name": f"c001-{customer_name}-p",
        "customer_name": customer_name,
        "arp_suppression": True,
        "prefix": {"ip_namespace": {"name": "C001-PROD"}},
    }
    if sgt_name or sgt_group_id:
        seg["security_tag"] = {
            "name": sgt_name,
            "group_id": sgt_group_id,
            "rules_as_source": [
                {
                    "action": r["action"],
                    "log": r.get("log", False),
                    "destination_tag": {"name": r["dst_name"], "group_id": r["dst_sgt"]},
                }
                for r in (rules or [])
            ],
        }
    return {"vlan_id": vlan_id, "vni": vni, "segment": seg}


# ===========================================================================
# _get_sgt_rules()
# ===========================================================================


class TestGetSgtRules:
    def test_none_returns_empty(self) -> None:
        assert _get_sgt_rules(None) == []

    def test_empty_list_returns_empty(self) -> None:
        assert _get_sgt_rules([]) == []

    def test_untagged_segment_returns_empty(self) -> None:
        acts = [_make_activation(vlan_id=10)]
        assert _get_sgt_rules(acts) == []

    def test_basic_rule_extracted(self) -> None:
        acts = [
            _make_activation_with_rules(
                sgt_name="web-tier",
                sgt_group_id=20,
                rules=[_make_sgt_rule()],
            )
        ]
        result = _get_sgt_rules(acts)
        assert len(result) == 1
        r = result[0]
        assert r["src_name"] == "web-tier"
        assert r["src_sgt"] == 20
        assert r["dst_name"] == "app-tier"
        assert r["dst_sgt"] == 30
        assert r["action"] == "permit"

    def test_multiple_segments_rules_merged(self) -> None:
        acts = [
            _make_activation_with_rules(
                vlan_id=10,
                customer_name="web-frontend",
                sgt_name="web-tier",
                sgt_group_id=20,
                rules=[_make_sgt_rule(src_name="web-tier", src_sgt=20, dst_name="app-tier", dst_sgt=30)],
            ),
            _make_activation_with_rules(
                vlan_id=20,
                customer_name="app-backend",
                sgt_name="app-tier",
                sgt_group_id=30,
                rules=[_make_sgt_rule(src_name="app-tier", src_sgt=30, dst_name="database", dst_sgt=50)],
            ),
        ]
        result = _get_sgt_rules(acts)
        assert len(result) == 2
        assert result[1]["src_name"] == "app-tier"
        assert result[1]["dst_sgt"] == 50

    def test_duplicate_rules_deduplicated(self) -> None:
        rule = _make_sgt_rule()
        acts = [
            _make_activation_with_rules(vlan_id=10, sgt_name="web-tier", sgt_group_id=20, rules=[rule]),
            _make_activation_with_rules(
                vlan_id=11, customer_name="web2", sgt_name="web-tier", sgt_group_id=20, rules=[rule]
            ),
        ]
        result = _get_sgt_rules(acts)
        assert len(result) == 1

    def test_rule_missing_dst_group_id_skipped(self) -> None:
        acts = [
            _make_activation_with_rules(
                sgt_name="web-tier",
                sgt_group_id=20,
                rules=[{"action": "permit", "dst_name": "app-tier", "dst_sgt": None}],
            )
        ]
        assert _get_sgt_rules(acts) == []

    def test_deny_action_preserved(self) -> None:
        acts = [
            _make_activation_with_rules(
                sgt_name="web-tier",
                sgt_group_id=20,
                rules=[_make_sgt_rule(action="deny")],
            )
        ]
        result = _get_sgt_rules(acts)
        assert result[0]["action"] == "deny"


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
            acls=[],
            vxlan=None,
            vrf_gateways={},
            bgp=None,
            ospf=None,
            mlag=None,
        )

    def test_no_sgt_rules_no_sgt_block(self) -> None:
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
        assert "mac security" not in output
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
            acls=[],
            vxlan=None,
            vrf_gateways={},
            bgp=[],
            ospf=[],
            mlag=None,
        )

    def test_no_sgt_rules_no_cts_block(self) -> None:
        vlans = [{"vlan_id": 10, "name": "web", "sgt": 20, "sgt_name": "web-tier", "isolation_mode": "normal"}]
        output = self._render(vlans, [])
        assert "feature cts" not in output
        assert "cts role-based" not in output

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
