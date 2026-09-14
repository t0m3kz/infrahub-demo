"""Unit tests for LB backend no-SNAT return-path PBR helpers in
transforms/helpers/loadbalancer_pbr.py.

Covers:
  - _flatten_deployment_lb_vips()   — device-scoped LB-VIP traversal, DC vs pod tier
  - get_lb_backend_pbr_rules()      — default-pass + explicit-redirect PBR rules per VLAN
"""

from transforms.helpers.loadbalancer_pbr import _flatten_deployment_lb_vips, get_lb_backend_pbr_rules

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_activation(
    *,
    vlan_id: int = 100,
    seg_id: str = "seg-1",
    customer_name: str = "web-frontend",
    environment: str | None = None,
) -> dict:
    seg: dict = {"id": seg_id, "customer_name": customer_name}
    if environment is not None:
        seg["environment"] = environment
    return {"vlan_id": vlan_id, "segment": seg}


def _make_vip(
    *,
    vip_id: str = "vip-1",
    snat_enabled: bool = False,
    backend_segment_id: str | None = "seg-1",
    customer_name: str | None = "web-frontend",
    environment: str | None = None,
    member_ips: list[str] | None = None,
) -> dict:
    backend_segment: dict = {}
    if backend_segment_id:
        backend_segment["id"] = backend_segment_id
        if customer_name is not None:
            backend_segment["customer_name"] = customer_name
        if environment is not None:
            backend_segment["environment"] = environment
    members = []
    for ip in member_ips or []:
        members.append({"pool_interfaces": [{"ip_address": {"address": f"{ip}/24"}}]})
    return {
        "id": vip_id,
        "snat_enabled": snat_enabled,
        "backend_segment": backend_segment,
        "members": members,
    }


def _make_lb_vip_entry(*, vip: dict | None = None, lb_ip: str = "10.2.10.5/24") -> dict:
    return {"vip": vip if vip is not None else _make_vip(), "ip_address": {"address": lb_ip}}


def _make_lb_device(*, entries: list[dict] | None = None) -> dict:
    """A device dict as returned by the LoadbalancerVipsOnDeploymentFields
    traversal — interfaces, each with an ip_address and interface_capabilities
    holding a LoadbalancerVIP typename cap."""
    interfaces = []
    for entry in entries or []:
        cap = dict(entry["vip"])
        cap["typename"] = "LoadbalancerVIP"
        interfaces.append({"ip_address": entry["ip_address"], "interface_capabilities": [cap]})
    return {"interfaces": interfaces}


# ===========================================================================
# _flatten_deployment_lb_vips()
# ===========================================================================


class TestFlattenDeploymentLbVips:
    def test_none_deployment_returns_empty(self) -> None:
        assert _flatten_deployment_lb_vips(None) == []

    def test_empty_deployment_returns_empty(self) -> None:
        assert _flatten_deployment_lb_vips({}) == []

    def test_dc_tier_device_vips_extracted(self) -> None:
        entry = _make_lb_vip_entry()
        deployment = {"lb_devices": [_make_lb_device(entries=[entry])]}
        result = _flatten_deployment_lb_vips(deployment)
        assert len(result) == 1
        assert result[0]["ip_address"]["address"] == "10.2.10.5/24"

    def test_pod_tier_device_vips_extracted_via_parent(self) -> None:
        entry = _make_lb_vip_entry()
        deployment = {"lb_devices": [], "parent": {"lb_devices": [_make_lb_device(entries=[entry])]}}
        result = _flatten_deployment_lb_vips(deployment)
        assert len(result) == 1

    def test_non_vip_capability_ignored(self) -> None:
        device = {
            "interfaces": [
                {"ip_address": {"address": "10.0.0.1/24"}, "interface_capabilities": [{"typename": "ManagedBGP"}]}
            ]
        }
        deployment = {"lb_devices": [device]}
        assert _flatten_deployment_lb_vips(deployment) == []

    def test_dedup_across_dc_and_pod_tier_paths(self) -> None:
        vip = _make_vip()
        vip["id"] = "vip-1"
        entry = _make_lb_vip_entry(vip=vip)
        deployment = {
            "lb_devices": [_make_lb_device(entries=[entry])],
            "parent": {"lb_devices": [_make_lb_device(entries=[entry])]},
        }
        result = _flatten_deployment_lb_vips(deployment)
        assert len(result) == 1

    def test_multiple_vips_across_devices(self) -> None:
        vip_a = _make_vip()
        vip_a["id"] = "vip-a"
        vip_b = _make_vip()
        vip_b["id"] = "vip-b"
        deployment = {
            "lb_devices": [
                _make_lb_device(entries=[_make_lb_vip_entry(vip=vip_a, lb_ip="10.2.10.5/24")]),
                _make_lb_device(entries=[_make_lb_vip_entry(vip=vip_b, lb_ip="10.2.20.5/24")]),
            ]
        }
        result = _flatten_deployment_lb_vips(deployment)
        assert {e["vip"]["id"] for e in result} == {"vip-a", "vip-b"}


# ===========================================================================
# get_lb_backend_pbr_rules()
# ===========================================================================


class TestGetLbBackendPbrRules:
    def test_none_activations_returns_empty(self) -> None:
        assert get_lb_backend_pbr_rules(None, [_make_lb_vip_entry()]) == []

    def test_none_lb_vips_returns_empty(self) -> None:
        assert get_lb_backend_pbr_rules([_make_activation()], None) == []

    def test_snat_enabled_vip_produces_no_rule(self) -> None:
        """snat_enabled=true is the common case — no return-path PBR needed."""
        activations = [_make_activation()]
        entry = _make_lb_vip_entry(vip=_make_vip(snat_enabled=True, member_ips=["10.2.10.10"]))
        assert get_lb_backend_pbr_rules(activations, [entry]) == []

    def test_no_backend_segment_produces_no_rule(self) -> None:
        activations = [_make_activation()]
        entry = _make_lb_vip_entry(vip=_make_vip(backend_segment_id=None, member_ips=["10.2.10.10"]))
        assert get_lb_backend_pbr_rules(activations, [entry]) == []

    def test_no_pool_members_produces_no_rule(self) -> None:
        activations = [_make_activation()]
        entry = _make_lb_vip_entry(vip=_make_vip(member_ips=[]))
        assert get_lb_backend_pbr_rules(activations, [entry]) == []

    def test_basic_rule_produced(self) -> None:
        activations = [_make_activation(vlan_id=100, seg_id="seg-1")]
        entry = _make_lb_vip_entry(vip=_make_vip(member_ips=["10.2.10.10", "10.2.10.11"]), lb_ip="10.2.10.5/24")
        result = get_lb_backend_pbr_rules(activations, [entry])
        assert len(result) == 1
        rule = result[0]
        assert rule["vlan_id"] == 100
        assert rule["backend_ips"] == ["10.2.10.10", "10.2.10.11"]
        assert rule["lb_nexthop"] == "10.2.10.5"

    def test_customer_and_environment_passed_through(self) -> None:
        activations = [_make_activation(vlan_id=100, seg_id="seg-1")]
        entry = _make_lb_vip_entry(
            vip=_make_vip(customer_name="web-frontend", environment="p", member_ips=["10.2.10.10"])
        )
        result = get_lb_backend_pbr_rules(activations, [entry])
        assert result[0]["customer_name"] == "web-frontend"
        assert result[0]["environment"] == "p"

    def test_segment_without_matching_vip_produces_no_rule(self) -> None:
        """A VLAN whose segment doesn't match any no-SNAT VIP's backend_segment
        falls through to normal fabric routing — no rule at all."""
        activations = [_make_activation(vlan_id=200, seg_id="seg-other")]
        entry = _make_lb_vip_entry(vip=_make_vip(backend_segment_id="seg-1", member_ips=["10.2.10.10"]))
        assert get_lb_backend_pbr_rules(activations, [entry]) == []

    def test_multiple_vlans_produce_multiple_rules_sorted(self) -> None:
        activations = [
            _make_activation(vlan_id=200, seg_id="seg-2", customer_name="app-backend"),
            _make_activation(vlan_id=100, seg_id="seg-1", customer_name="web-frontend"),
        ]
        entries = [
            _make_lb_vip_entry(vip=_make_vip(backend_segment_id="seg-1", member_ips=["10.2.10.10"])),
            _make_lb_vip_entry(vip=_make_vip(backend_segment_id="seg-2", member_ips=["10.2.20.10"])),
        ]
        result = get_lb_backend_pbr_rules(activations, entries)
        assert [r["vlan_id"] for r in result] == [100, 200]
