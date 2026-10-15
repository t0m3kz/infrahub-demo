"""Unit tests for the group contracts (transforms/helpers/group_policy.py) and the
segmentation_matrix artifact (transforms/topology/segmentation_matrix.py)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from transforms.helpers.group_policy import REPLIES, get_group_contracts
from transforms.topology.segmentation_matrix import SegmentationMatrix

TAGS = [{"id": "t-web", "group_id": 17}, {"id": "t-cache", "group_id": 18}, {"id": "t-api", "group_id": 19}]


def _rule(
    name: str,
    src: str | None,
    dst: str | None,
    *,
    protocol: str = "tcp",
    port_start: int | None = 6379,
    port_end: int | None = None,
    action: str = "permit",
    disabled: bool = False,
    dst_segment: str = "seg-m",
    index: int = 100,
) -> dict[str, Any]:
    """A cleaned rule of the segment's own policy."""
    return {
        "id": f"id-{name}",
        "index": index,
        "name": name,
        "action": action,
        "protocol": protocol,
        "port_start": port_start,
        "port_end": port_end,
        "disabled": disabled,
        "destination_segment": {"id": dst_segment},
        "source_security_tag": {"id": src} if src else None,
        "destination_security_tag": {"id": dst} if dst else None,
    }


def _segment(*rules: dict[str, Any], enabled: bool = True) -> dict[str, Any]:
    return {
        "id": "seg-m",
        "name": "c005-web-frontend-local-dc10-p",
        "isolation_mode": "microsegmented",
        "security_policy": {"name": "seg-egress", "enabled": enabled, "default_action": "deny", "rules": list(rules)},
    }


def _pairs(rows: list[dict[str, Any]]) -> list[tuple[int, int, str]]:
    return [(row["src_tag"], row["dst_tag"], row["action"]) for row in rows]


class TestGroupContracts:
    """AC4: permit rows with ports, deny-all for un-ruled pairs, any collapses, disabled absent, sorted."""

    def test_permit_row_carries_the_rule_ports(self) -> None:
        rows = get_group_contracts(_segment(_rule("web-to-cache", "t-web", "t-cache")), TAGS[:2])
        assert rows == [
            {
                "segment": "c005-web-frontend-local-dc10-p",
                "src_tag": 17,
                "dst_tag": 18,
                "action": "permit",
                "entries": [{"protocol": "tcp", "port_start": 6379, "port_end": 6379}],
                "rules": ["web-to-cache"],
                "replies": REPLIES,
            }
        ]

    def test_un_ruled_pairs_get_deny_all_rows(self) -> None:
        rows = get_group_contracts(_segment(_rule("web-to-cache", "t-web", "t-cache")), TAGS)
        assert _pairs(rows) == [(17, 18, "permit"), (17, 19, "deny"), (18, 19, "deny")]
        deny = rows[1]
        assert (deny["entries"], deny["rules"]) == ([], [])

    def test_reverse_rule_counts_for_the_unordered_pair(self) -> None:
        """A rule either way removes the pair's deny-all row; rows stay forward."""
        rows = get_group_contracts(_segment(_rule("cache-to-web", "t-cache", "t-web")), TAGS[:2])
        assert _pairs(rows) == [(18, 17, "permit")]

    def test_any_collapses_the_row(self) -> None:
        rows = get_group_contracts(
            _segment(
                _rule("tcp", "t-web", "t-cache", port_start=443),
                _rule("all", "t-web", "t-cache", protocol="any", port_start=None, index=110),
            ),
            TAGS[:2],
        )
        assert rows[0]["entries"] == [{"protocol": "any", "port_start": None, "port_end": None}]
        assert rows[0]["rules"] == ["all", "tcp"]

    def test_entries_are_sorted_and_de_duplicated(self) -> None:
        rows = get_group_contracts(
            _segment(
                _rule("b", "t-web", "t-cache", port_start=8443, index=110),
                _rule("a", "t-web", "t-cache", port_start=443, index=100),
                _rule("a2", "t-web", "t-cache", port_start=443, index=120),
                _rule("r", "t-web", "t-cache", protocol="udp", port_start=30000, port_end=30010, index=130),
            ),
            TAGS[:2],
        )
        assert rows[0]["entries"] == [
            {"protocol": "tcp", "port_start": 443, "port_end": 443},
            {"protocol": "tcp", "port_start": 8443, "port_end": 8443},
            {"protocol": "udp", "port_start": 30000, "port_end": 30010},
        ]

    def test_disabled_rules_and_policies_are_absent(self) -> None:
        disabled = get_group_contracts(_segment(_rule("off", "t-web", "t-cache", disabled=True)), TAGS[:2])
        assert _pairs(disabled) == [(17, 18, "deny")]
        policy_off = get_group_contracts(_segment(_rule("r", "t-web", "t-cache"), enabled=False), TAGS[:2])
        assert _pairs(policy_off) == [(17, 18, "deny")]

    @pytest.mark.parametrize(
        "rule",
        [
            _rule("untagged", None, "t-cache"),
            _rule("same-tag", "t-web", "t-web"),
            _rule("foreign-tag", "t-web", "t-other"),
            _rule("cross-segment", "t-web", "t-cache", dst_segment="seg-other"),
            _rule("deny", "t-web", "t-cache", action="deny"),
        ],
    )
    def test_rules_that_are_no_contract_are_ignored(self, rule: dict[str, Any]) -> None:
        """Only a permit inside the segment between two of its own, different tags is a contract."""
        assert _pairs(get_group_contracts(_segment(rule), TAGS[:2])) == [(17, 18, "deny")]

    def test_same_tag_is_never_a_row(self) -> None:
        """Instances of one component share a tag and are permitted (no intra-group isolation)."""
        assert get_group_contracts(_segment(), TAGS[:1]) == []


# ---------------------------------------------------------------------------
# The artifact
# ---------------------------------------------------------------------------


def _v(value: Any) -> dict[str, Any]:
    return {"value": value}


def _device(name: str, role: str, device_type: str, version: str, memory_gb: int = 32) -> dict[str, Any]:
    """One raw DeviceGroupPolicyFields edge."""
    return {
        "node": {
            "name": _v(name),
            "role": _v(role),
            "platform": {"node": {"name": _v("cisco_nxos")}},
            "device_type": {"node": {"name": _v(device_type), "memory_gb": _v(memory_gb)}},
            "software_image": {"node": {"version": _v(version)}},
        }
    }


NXOS_PROFILE = {
    "node": {
        "name": _v("reference-cisco-nxos"),
        "group_policy_kind": _v("vxlan_gpo"),
        "min_release": _v("10.6(1)F"),
        "min_memory_gb": _v(24),
        "platform": {"node": {"name": _v("cisco_nxos")}},
        "device_types": {
            "edges": [{"node": {"name": _v("N9K-C93180YC-FX3")}}, {"node": {"name": _v("N9K-C9364C-GX")}}]
        },
    }
}


def _payload(*, border_version: str = "10.6(3)F", access_leaf: bool = True) -> dict[str, Any]:
    """A raw segmentation_matrix response: one microsegmented segment with two tagged components.

    The border leaf (GX) and leaf (FX3) are reference hardware; the optional access leaf is EX.
    """
    rule = {
        "node": {
            "id": "rule-1",
            "index": _v(100),
            "name": _v("web-to-cache-tcp-6379"),
            "action": _v("permit"),
            "protocol": _v("tcp"),
            "port_start": _v(6379),
            "port_end": _v(None),
            "disabled": _v(False),
            "apply_on_switch": _v(True),
            "destination_segment": {"node": {"id": "seg-m"}},
            "source_security_tag": {"node": {"id": "t-web"}},
            "destination_security_tag": {"node": {"id": "t-cache"}},
        }
    }
    segment = {
        "id": "seg-m",
        "__typename": "ManagedVxlanSegment",
        "name": _v("c005-web-frontend-local-dc10-p"),
        "isolation_mode": _v("microsegmented"),
        "gateway": {"node": {"ip_prefix": {"node": {"ip_namespace": {"node": {"name": _v("PROD")}}}}}},
        "inline_service": {"node": None},
        "security_policy": {
            "node": {
                "name": _v("seg-egress"),
                "enabled": _v(True),
                "default_action": _v("deny"),
                "rules": {"edges": [rule]},
            }
        },
    }
    pod_devices = [_device("lf-01", "leaf", "N9K-C93180YC-FX3", "10.6(1)F")]
    if access_leaf:
        pod_devices.append(_device("al-01", "access-leaf", "N9K-C93180YC-EX", "10.3(3)", memory_gb=24))
    tag_component = lambda fqdn: {"node": {"fqdn": _v(fqdn), "network_segment": {"node": {"id": "seg-m"}}}}  # noqa: E731
    return {
        "TopologyDataCenter": {
            "edges": [
                {
                    "node": {
                        "id": "dc10",
                        "name": _v("DC10"),
                        "devices": {"edges": [_device("bl-01", "border-leaf", "N9K-C9364C-GX", border_version)]},
                        "segment_deployments": {
                            "edges": [
                                {"node": {"segment": {"node": segment}}},
                                {"node": {"segment": {"node": segment}}},
                            ]
                        },
                    }
                }
            ]
        },
        "TopologyPod": {"edges": [{"node": {"devices": {"edges": pod_devices}}}]},
        "DcimGroupPolicyProfile": {"edges": [NXOS_PROFILE]},
        "SecurityTag": {
            "edges": [
                {
                    "node": {
                        "id": "t-cache",
                        "name": _v("cache.example"),
                        "group_id": _v(18),
                        "components": {"edges": [tag_component("cache.example")]},
                    }
                },
                {
                    "node": {
                        "id": "t-web",
                        "name": _v("web.example"),
                        "group_id": _v(17),
                        "components": {"edges": [tag_component("web.example")]},
                    }
                },
                {
                    "node": {
                        "id": "t-orphan",
                        "name": _v("orphan.example"),
                        "group_id": _v(30),
                        "components": {"edges": []},
                    }
                },
            ]
        },
    }


def _render(payload: dict[str, Any]) -> dict[str, Any]:
    """Run the transform on a raw response."""
    transform = SegmentationMatrix.__new__(SegmentationMatrix)
    return asyncio.run(transform.transform(payload))


class TestSegmentationMatrix:
    """AC5: the DC's reference verdict names the EX access leaf; tags, segments and contracts are listed sorted."""

    def test_site_names_the_ex_access_leaf(self) -> None:
        """One VTEP outside the profile makes the DC not a reference fabric."""
        result = _render(_payload())
        assert result["deployment"] == "DC10"
        assert result["site"] == {
            "reference": False,
            "kind": "vxlan_gpo",
            "reason": "access-leaf al-01 N9K-C93180YC-EX: device type not in profile reference-cisco-nxos",
        }

    def test_site_names_every_distinct_cause(self) -> None:
        """The EX access leaf and the border leaf's 10.3(3) release are both named; VTEPs sorted by name."""
        result = _render(_payload(border_version="10.3(3)"))
        assert result["site"]["reason"] == (
            "access-leaf al-01 N9K-C93180YC-EX: device type not in profile reference-cisco-nxos; "
            "border-leaf bl-01 N9K-C9364C-GX: release 10.3(3) < 10.6(1)F"
        )
        assert result["vteps"] == [
            {
                "name": "al-01",
                "role": "access-leaf",
                "device_type": "N9K-C93180YC-EX",
                "release": "10.3(3)",
                "reason": "device type not in profile reference-cisco-nxos",
            },
            {
                "name": "bl-01",
                "role": "border-leaf",
                "device_type": "N9K-C9364C-GX",
                "release": "10.3(3)",
                "reason": "release 10.3(3) < 10.6(1)F",
            },
            {
                "name": "lf-01",
                "role": "leaf",
                "device_type": "N9K-C93180YC-FX3",
                "release": "10.6(1)F",
                "reason": None,
            },
        ]

    def test_reference_site(self) -> None:
        """Every VTEP in the profile: reference, no reason."""
        result = _render(_payload(access_leaf=False))
        assert result["site"] == {"reference": True, "kind": "vxlan_gpo", "reason": None}
        assert [vtep["reason"] for vtep in result["vteps"]] == [None, None]

    def test_non_vtep_devices_are_not_listed(self) -> None:
        """A pod's spine is no VTEP: neither listed nor counted against the site."""
        payload = _payload(access_leaf=False)
        payload["TopologyPod"]["edges"][0]["node"]["devices"]["edges"].append(
            _device("sp-01", "spine", "N9K-C9336C-FX2", "10.3(3)")
        )
        result = _render(payload)
        assert [vtep["name"] for vtep in result["vteps"]] == ["bl-01", "lf-01"]
        assert result["site"]["reference"] is True

    def test_without_a_profile_row_nothing_is_reference(self) -> None:
        """Fail closed: no row for the platform, no kind, every VTEP named."""
        payload = _payload(access_leaf=False)
        payload["DcimGroupPolicyProfile"]["edges"] = []
        result = _render(payload)
        assert result["site"] == {
            "reference": False,
            "kind": None,
            "reason": "border-leaf bl-01 N9K-C9364C-GX: no group-policy profile for platform cisco_nxos (L0 only) "
            "(and 1 more)",
        }

    def test_tags_segments_and_contracts(self) -> None:
        result = _render(_payload())
        assert result["tags"] == [
            {
                "name": "web.example",
                "group_id": 17,
                "component": "web.example",
                "segment": "c005-web-frontend-local-dc10-p",
                "vrf": "PROD",
            },
            {
                "name": "cache.example",
                "group_id": 18,
                "component": "cache.example",
                "segment": "c005-web-frontend-local-dc10-p",
                "vrf": "PROD",
            },
        ]
        assert result["segments"] == [
            {
                "name": "c005-web-frontend-local-dc10-p",
                "isolation_mode": "microsegmented",
                "gbp_required": True,
                "leaf_svi_acl": True,
                "inline_firewall": None,
            }
        ]
        assert [(row["src_tag"], row["dst_tag"], row["action"]) for row in result["contracts"]] == [(17, 18, "permit")]

    def test_segment_with_one_tag_has_no_contracts(self) -> None:
        payload = _payload()
        payload["SecurityTag"]["edges"] = payload["SecurityTag"]["edges"][:1]
        result = _render(payload)
        assert result["segments"][0]["gbp_required"] is False
        assert result["contracts"] == []

    def test_re_render_is_byte_identical(self) -> None:
        assert json.dumps(_render(_payload()), sort_keys=True) == json.dumps(_render(_payload()), sort_keys=True)

    def test_missing_data_center_is_refused(self) -> None:
        with pytest.raises(ValueError, match="no TopologyDataCenter"):
            _render({"TopologyDataCenter": {"edges": []}})

    def test_registered_as_a_dc_artifact(self, root_dir: Path) -> None:
        """Artifact definition, python transform and query, like the cable matrix."""
        import yaml

        config = yaml.safe_load((root_dir / ".infrahub.yml").read_text())
        artifact = next(
            item for item in config["artifact_definitions"] if item["transformation"] == "segmentation_matrix"
        )
        assert (artifact["targets"], artifact["content_type"], artifact["parameters"]) == (
            "topologies_dc",
            "application/json",
            {"id": "id"},
        )
        assert any(item["name"] == "segmentation_matrix" for item in config["python_transforms"])
        assert any(item["name"] == SegmentationMatrix.query for item in config["queries"])
