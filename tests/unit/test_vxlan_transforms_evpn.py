"""Unit tests for EVPN selection, RD/RT derivation, anycast MAC and L3 VNI SVI VLANs."""

import logging

import pytest

from tests.unit.vxlan_transforms_helpers import _make_activation
from transforms.common import _fabric_anycast_mac, _fabric_rt_asn
from transforms.helpers.vxlan import (
    _DEFAULT_ANYCAST_GATEWAY_MAC,
    _collect_l3_vni_from_namespaces,
    _overlay_is_ebgp,
    _select_evpn_bgp_process,
    _warn_unencodable_vnis,
    get_vxlan_config,
)
from utils.limits import L3VNI_SVI_VLAN_BASE, L3VNI_SVI_VLAN_MAX

# ===========================================================================
# _select_evpn_bgp_process() — the EVPN discriminator
# ===========================================================================


class TestSelectEvpnBgpProcess:
    """The BGP process that carries EVPN is selected by `typename` then
    `process_role`.

    The original filter was `service_type == "bgp"`. ManagedBGP has no
    `service_type` attribute, so it matched nothing — which pinned
    `evpn.enabled` to False and left the route-target unset on every VTEP in the
    project while ruff, ty and the whole test suite stayed green.
    """

    def test_typename_is_the_discriminator_not_service_type(self) -> None:
        """A ManagedBGP with no `service_type` key must still be found."""
        caps = [{"typename": "ManagedBGP", "name": "bgp-overlay", "local_as": {"asn": 65001}}]
        assert _select_evpn_bgp_process(caps) is not None

    def test_service_type_bgp_alone_is_not_matched(self) -> None:
        """Guards the regression: `service_type` is not a real ManagedBGP field,
        so a capability carrying only that must not be mistaken for BGP."""
        caps = [{"service_type": "bgp", "name": "not-really-bgp"}]
        assert _select_evpn_bgp_process(caps) is None

    def test_non_bgp_capabilities_are_ignored(self) -> None:
        caps = [
            {"typename": "ManagedStandaloneVlanDomain", "name": "vlan-domain"},
            {"typename": "ManagedOSPF", "name": "ospf"},
        ]
        assert _select_evpn_bgp_process(caps) is None

    def test_empty_capabilities_returns_none(self) -> None:
        assert _select_evpn_bgp_process([]) is None

    def test_overlay_process_wins_over_underlay(self) -> None:
        """EVPN lives in the overlay process — picking the underlay one would
        derive the RD from the wrong router-id."""
        caps = [
            {"typename": "ManagedBGP", "name": "a-underlay", "process_role": "underlay"},
            {"typename": "ManagedBGP", "name": "z-overlay", "process_role": "overlay"},
        ]
        result = _select_evpn_bgp_process(caps)
        assert result is not None
        assert result["name"] == "z-overlay"

    def test_overlay_wins_regardless_of_capability_order(self) -> None:
        caps = [
            {"typename": "ManagedBGP", "name": "z-overlay", "process_role": "overlay"},
            {"typename": "ManagedBGP", "name": "a-underlay", "process_role": "underlay"},
        ]
        result = _select_evpn_bgp_process(caps)
        assert result is not None
        assert result["name"] == "z-overlay"

    def test_selection_is_deterministic_across_query_order(self) -> None:
        """Unsorted `[0]` made a device's route-distinguisher depend on the order
        the GraphQL backend happened to return capabilities in."""
        a = {"typename": "ManagedBGP", "name": "aaa", "local_as": {"asn": 1}}
        b = {"typename": "ManagedBGP", "name": "bbb", "local_as": {"asn": 2}}
        forward = _select_evpn_bgp_process([a, b])
        reverse = _select_evpn_bgp_process([b, a])
        assert forward == reverse
        assert forward is not None
        assert forward["name"] == "aaa"

    def test_falls_back_to_first_process_when_no_role_tagged(self) -> None:
        """ebgp-ebgp collapses underlay+overlay onto one ASN, and older data may
        predate process_role — a real VTEP must not silently lose EVPN."""
        caps = [{"typename": "ManagedBGP", "name": "bgp", "local_as": {"asn": 65001}}]
        result = _select_evpn_bgp_process(caps)
        assert result is not None
        assert result["name"] == "bgp"


# ===========================================================================
# get_vxlan_config() — EVPN enablement and RD/RT derivation
# ===========================================================================


def _bgp_cap(
    *,
    name: str = "bgp-overlay",
    asn: int = 4245880500,
    router_id: str = "10.0.0.7/32",
    process_role: str | None = "overlay",
) -> dict:
    cap: dict = {
        "typename": "ManagedBGP",
        "name": name,
        "local_as": {"asn": asn},
        "router_id": {"address": router_id},
    }
    if process_role is not None:
        cap["process_role"] = process_role
    return cap


class TestGetVxlanConfigEvpn:
    def _data(self, capabilities: list | None = None) -> dict:
        return {
            "name": "dc1-leaf-1",
            "interfaces": [{"name": "Loopback0", "ip_addresses": [{"address": "10.0.0.1/32"}]}],
            "capabilities": capabilities if capabilities is not None else [],
        }

    def test_evpn_disabled_without_a_bgp_process(self) -> None:
        """No BGP process means no fabric ASN, so there is no RT to derive.

        The per-mapping `route_target` must be None, NOT the literal string "auto": templates skip
        the line when the route-target is unset, and `rd auto` / `route-target ... auto`
        — while valid NX-OS/EOS — is a syntax error in FRR, which is what the
        Dell SONiC templates emit.

        `rd_format` still resolves, because the RD is per-VTEP and the VTEP's own
        loopback identifies it uniquely even with no BGP process to read a
        router-id from. It is unused in practice since `enabled` is False and the
        templates gate the whole EVPN block on that.
        """
        acts = [_make_activation(vlan_id=100, vni=10100)]
        result = get_vxlan_config(self._data(), "arista_eos", device_role="leaf", activations=acts)
        assert result is not None
        assert result["evpn"]["enabled"] is False
        assert result["l2_vni_mappings"][0]["route_target"] is None
        assert result["evpn"]["rd_format"] == "10.0.0.1:{vni}"

    def test_evpn_enabled_with_a_bgp_process(self) -> None:
        """The case that was unreachable before: a VTEP with BGP renders EVPN."""
        acts = [_make_activation(vlan_id=100, vni=10100)]
        result = get_vxlan_config(self._data([_bgp_cap()]), "arista_eos", device_role="leaf", activations=acts)
        assert result is not None
        assert result["evpn"]["enabled"] is True

    def test_rd_uses_overlay_router_id_not_vtep_ip(self) -> None:
        """RD is per-VTEP by design — it exists to distinguish the same
        segment's routes per advertising VTEP."""
        acts = [_make_activation(vlan_id=100, vni=10100)]
        result = get_vxlan_config(
            self._data([_bgp_cap(router_id="10.0.0.7/32")]),
            "arista_eos",
            device_role="leaf",
            activations=acts,
        )
        assert result is not None
        assert result["evpn"]["rd_format"] == "10.0.0.7:{vni}"

    def test_rt_uses_fabric_asn_not_device_local_asn(self) -> None:
        """The fabric-breaking case. Under ebgp-ebgp each VTEP has its own ASN,
        so an RT derived from local_as makes two leaves advertise the same
        segment under different route-targets and neither imports the other's
        routes — the segment silently fails to forward between racks.
        """
        acts = [_make_activation(vlan_id=100, vni=10100)]
        result = get_vxlan_config(
            self._data([_bgp_cap(asn=4245880501)]),
            "arista_eos",
            device_role="leaf",
            activations=acts,
            fabric_rt_asn=4245880999,
        )
        assert result is not None
        assert result["l2_vni_mappings"][0]["route_target"] == "4245880999:10100"

    def test_two_vteps_with_different_local_asns_derive_the_same_rt(self) -> None:
        """The property that actually matters: same fabric RT ASN in, same RT out."""
        acts = [_make_activation(vlan_id=100, vni=10100)]
        leaf1 = get_vxlan_config(
            self._data([_bgp_cap(asn=4245880501, router_id="10.0.0.1/32")]),
            "arista_eos",
            device_role="leaf",
            activations=acts,
            fabric_rt_asn=4245880999,
        )
        leaf2 = get_vxlan_config(
            self._data([_bgp_cap(asn=4245880502, router_id="10.0.0.2/32")]),
            "arista_eos",
            device_role="leaf",
            activations=acts,
            fabric_rt_asn=4245880999,
        )
        assert leaf1 is not None and leaf2 is not None
        assert leaf1["l2_vni_mappings"][0]["route_target"] == "4245880999:10100"
        assert leaf1["l2_vni_mappings"][0]["route_target"] == leaf2["l2_vni_mappings"][0]["route_target"]
        # ...while the RD stays per-VTEP, which is its whole purpose.
        assert leaf1["evpn"]["rd_format"] != leaf2["evpn"]["rd_format"]

    def test_rt_falls_back_to_overlay_asn_when_fabric_asn_absent(self) -> None:
        """ebgp-ibgp/ospf-ibgp share one overlay ASN fabric-wide, so they stay
        correct even before evpn_rt_as is populated."""
        acts = [_make_activation(vlan_id=100, vni=10100)]
        result = get_vxlan_config(
            self._data([_bgp_cap(asn=4245880999)]),
            "arista_eos",
            device_role="leaf",
            activations=acts,
            fabric_rt_asn=None,
        )
        assert result is not None
        assert result["l2_vni_mappings"][0]["route_target"] == "4245880999:10100"

    def test_rd_and_rt_derived_from_the_overlay_process(self) -> None:
        caps = [
            _bgp_cap(name="a-underlay", asn=4245880501, router_id="10.255.0.1/32", process_role="underlay"),
            _bgp_cap(name="z-overlay", asn=4245880999, router_id="10.0.0.7/32", process_role="overlay"),
        ]
        acts = [_make_activation(vlan_id=100, vni=10100)]
        result = get_vxlan_config(self._data(caps), "arista_eos", device_role="leaf", activations=acts)
        assert result is not None
        assert result["evpn"]["rd_format"] == "10.0.0.7:{vni}"
        assert result["l2_vni_mappings"][0]["route_target"] == "4245880999:10100"


# ===========================================================================
# VTEP_ROLES (utils/fabric_roles.py) — role spellings must match the schema
# ===========================================================================


class TestVtepRoleSpellings:
    def _data(self) -> dict:
        return {
            "name": "dev",
            "interfaces": [{"name": "Loopback0", "ip_addresses": [{"address": "10.0.0.1/32"}]}],
            "capabilities": [_bgp_cap()],
        }

    def test_hyphenated_border_leaf_is_a_vtep(self) -> None:
        acts = [_make_activation(vlan_id=100, vni=10100)]
        assert get_vxlan_config(self._data(), "arista_eos", device_role="border-leaf", activations=acts) is not None

    def test_underscored_border_leaf_is_not_accepted(self) -> None:
        """The schema role is `border-leaf`. Tolerating `border_leaf` hid the
        fact that transforms/config/border_leaf.py was passing the underscore
        form, which bgp.py's leaf-RR check could never match either.
        """
        acts = [_make_activation(vlan_id=100, vni=10100)]
        assert get_vxlan_config(self._data(), "arista_eos", device_role="border_leaf", activations=acts) is None

    def test_l2_leaf_is_not_a_vtep(self) -> None:
        """schemas/base/dcim.yml describes l2-leaf as explicitly having no
        VXLAN/overlay BGP — it trunks VLANs up to a leaf."""
        acts = [_make_activation(vlan_id=100, vni=10100)]
        assert get_vxlan_config(self._data(), "arista_eos", device_role="l2-leaf", activations=acts) is None

    def test_access_leaf_is_a_vtep(self) -> None:
        """...whereas access-leaf is described as a routed VTEP."""
        acts = [_make_activation(vlan_id=100, vni=10100)]
        assert get_vxlan_config(self._data(), "arista_eos", device_role="access-leaf", activations=acts) is not None


# ===========================================================================
# RD/RT encodability guard
# ===========================================================================


class TestUnencodableVniWarning:
    """A type-1 RD is `IPv4:2-byte` and a type-2 RT with a 4-byte ASN is
    `ASN:2-byte`. Every ASN in this project is a 4-byte private ASN, so a VNI
    above 65535 cannot be encoded into either and the device rejects the line.
    """

    def test_no_warning_for_encodable_vnis(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING):
            _warn_unencodable_vnis([{"vni": 65535}], [{"l3_vni": 50001}], device_name="leaf-1")
        assert caplog.text == ""

    def test_warns_for_oversized_l2_vni(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING):
            _warn_unencodable_vnis([{"vni": 100100}], [], device_name="leaf-1")
        assert "100100" in caplog.text
        assert "leaf-1" in caplog.text

    def test_warns_for_oversized_l3_vni(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING):
            _warn_unencodable_vnis([], [{"l3_vni": 16777000}], device_name="leaf-1")
        assert "16777000" in caplog.text

    def test_missing_vni_values_do_not_crash(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING):
            _warn_unencodable_vnis([{"vni": None}, {}], [{"l3_vni": None}], device_name="leaf-1")
        assert caplog.text == ""

    def test_warns_when_an_l2_vni_collides_with_an_l3_vni(self, caplog: pytest.LogCaptureFixture) -> None:
        """The VNI space is flat, not one namespace per VNI type.

        An L2 segment allocated on top of a VRF's L3 VNI makes the device reject
        the `member vni ... associate` line. This is the symptom of an L2 pool
        range reaching a namespace's fixed l3_vni, which is why the site L2
        pool stops at 39999 and the stretched pool at 49999.
        """
        with caplog.at_level(logging.WARNING):
            _warn_unencodable_vnis([{"vni": 50001}], [{"l3_vni": 50001}], device_name="leaf-1")
        assert "50001" in caplog.text
        assert "both an L2 VNI and an L3 VNI" in caplog.text

    def test_no_collision_warning_for_disjoint_ranges(self, caplog: pytest.LogCaptureFixture) -> None:
        """The capped L2 range (10001-49999) never meets the L3 range."""
        with caplog.at_level(logging.WARNING):
            _warn_unencodable_vnis(
                [{"vni": 10001}, {"vni": 49999}],
                [{"l3_vni": 50001}, {"l3_vni": 59999}],
                device_name="leaf-1",
            )
        assert caplog.text == ""

    def test_collision_and_oversize_are_reported_independently(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING):
            _warn_unencodable_vnis(
                [{"vni": 50001}, {"vni": 70000}],
                [{"l3_vni": 50001}],
                device_name="leaf-1",
            )
        assert "70000" in caplog.text
        assert "16-bit" in caplog.text
        assert "both an L2 VNI and an L3 VNI" in caplog.text


# ===========================================================================
# _fabric_rt_asn() — reaching the fabric AS from a device's deployment
# ===========================================================================


class TestFabricRtAsn:
    """Only TopologyDataCenter and TopologyColocationMetro inherit
    TopologySegmentHosting. A border-leaf's deployment IS the DC, but a leaf's
    deployment is the pod — one hop below it.
    """

    def test_reads_from_the_deployment_directly(self) -> None:
        """DC-level tiers: border-leaf, border-spine."""
        assert _fabric_rt_asn({"evpn_rt_as": {"asn": 4245880999}}) == 4245880999

    def test_reads_through_the_parent_hop(self) -> None:
        """Pod-level tiers: leaf, tor, access-leaf."""
        deployment = {"parent": {"evpn_rt_as": {"asn": 4245880999}}}
        assert _fabric_rt_asn(deployment) == 4245880999

    def test_direct_value_wins_over_parent(self) -> None:
        deployment = {"evpn_rt_as": {"asn": 1}, "parent": {"evpn_rt_as": {"asn": 2}}}
        assert _fabric_rt_asn(deployment) == 1

    def test_returns_none_when_unset(self) -> None:
        assert _fabric_rt_asn({"evpn_rt_as": None}) is None
        assert _fabric_rt_asn({}) is None

    def test_returns_none_for_missing_or_malformed_deployment(self) -> None:
        assert _fabric_rt_asn(None) is None
        assert _fabric_rt_asn("not-a-dict") is None

    def test_non_integer_asn_is_rejected(self) -> None:
        assert _fabric_rt_asn({"evpn_rt_as": {"asn": "4245880999"}}) is None


# ===========================================================================
# _fabric_anycast_mac() — the fabric's second must-agree-everywhere constant
# ===========================================================================


class TestFabricAnycastMac:
    """Same deployment/parent walk as _fabric_rt_asn, for the same reason: a
    leaf's deployment is the pod, a border-leaf's IS the DC.
    """

    def test_reads_from_the_deployment_directly(self) -> None:
        assert _fabric_anycast_mac({"evpn_anycast_gateway_mac": "00:00:5e:00:01:01"}) == "00:00:5e:00:01:01"

    def test_reads_through_the_parent_hop(self) -> None:
        deployment = {"parent": {"evpn_anycast_gateway_mac": "00:00:5e:00:01:01"}}
        assert _fabric_anycast_mac(deployment) == "00:00:5e:00:01:01"

    def test_direct_value_wins_over_parent(self) -> None:
        deployment = {
            "evpn_anycast_gateway_mac": "00:00:5e:00:01:01",
            "parent": {"evpn_anycast_gateway_mac": "00:00:5e:00:01:02"},
        }
        assert _fabric_anycast_mac(deployment) == "00:00:5e:00:01:01"

    def test_whitespace_only_is_treated_as_unset(self) -> None:
        """An empty string would otherwise render `anycast-gateway-mac` with no value."""
        assert _fabric_anycast_mac({"evpn_anycast_gateway_mac": "   "}) is None

    def test_value_is_stripped(self) -> None:
        assert _fabric_anycast_mac({"evpn_anycast_gateway_mac": " 00:00:5e:00:01:01 "}) == "00:00:5e:00:01:01"

    def test_returns_none_when_unset_or_malformed(self) -> None:
        assert _fabric_anycast_mac({"evpn_anycast_gateway_mac": None}) is None
        assert _fabric_anycast_mac({}) is None
        assert _fabric_anycast_mac(None) is None
        assert _fabric_anycast_mac("not-a-dict") is None


class TestAnycastMacPlumbing:
    """The MAC has to reach the template from the fabric, not be invented per device."""

    def _data(self) -> dict:
        return {
            "name": "dc1-leaf-1",
            "interfaces": [{"name": "Loopback0", "ip_addresses": [{"address": "10.0.0.1/32"}]}],
            "capabilities": [_bgp_cap()],
        }

    def test_fabric_mac_overrides_the_default(self) -> None:
        acts = [_make_activation(vlan_id=100, vni=10100, gateway_ip="10.1.1.1/24")]
        result = get_vxlan_config(
            self._data(),
            "arista_eos",
            device_role="leaf",
            activations=acts,
            fabric_anycast_mac="00:00:5e:00:01:01",
        )
        assert result is not None
        assert result["anycast_gateway"]["mac"] == "00:00:5e:00:01:01"

    def test_falls_back_to_the_documented_default(self) -> None:
        acts = [_make_activation(vlan_id=100, vni=10100, gateway_ip="10.1.1.1/24")]
        result = get_vxlan_config(self._data(), "arista_eos", device_role="leaf", activations=acts)
        assert result is not None
        assert result["anycast_gateway"]["mac"] == _DEFAULT_ANYCAST_GATEWAY_MAC

    def test_two_leaves_in_one_fabric_get_the_same_mac(self) -> None:
        """The property that matters: a host ARPs for its gateway once and keeps
        that MAC as its flows land on a different leaf. Two different MACs
        blackhole traffic on the leaf that did not answer.
        """
        acts = [_make_activation(vlan_id=100, vni=10100, gateway_ip="10.1.1.1/24")]
        leaf1 = get_vxlan_config(
            {**self._data(), "capabilities": [_bgp_cap(asn=4245880501, router_id="10.0.0.1/32")]},
            "arista_eos",
            device_role="leaf",
            activations=acts,
            fabric_anycast_mac="00:00:5e:00:01:01",
        )
        leaf2 = get_vxlan_config(
            {**self._data(), "capabilities": [_bgp_cap(asn=4245880502, router_id="10.0.0.2/32")]},
            "cisco_nxos",
            device_role="leaf",
            activations=acts,
            fabric_anycast_mac="00:00:5e:00:01:01",
        )
        assert leaf1 is not None and leaf2 is not None
        assert leaf1["anycast_gateway"]["mac"] == leaf2["anycast_gateway"]["mac"]


# ===========================================================================
# _overlay_is_ebgp() and the RT fallback it guards
# ===========================================================================


def _bgp_cap_with_peerings(session_types: list[str], **kwargs) -> dict:
    cap = _bgp_cap(**kwargs)
    cap["peerings"] = [{"session_type": st} for st in session_types]
    return cap


class TestOverlayIsEbgp:
    """Decided by session type, never by TTL: an eBGP overlay peered over
    directly-connected links would defeat a TTL-based guess.
    """

    def test_all_ebgp_peerings_means_a_per_device_asn(self) -> None:
        assert _overlay_is_ebgp(_bgp_cap_with_peerings(["EBGP"])) is True

    def test_session_type_matching_is_case_insensitive(self) -> None:
        assert _overlay_is_ebgp(_bgp_cap_with_peerings(["ebgp"])) is True

    def test_ebgp_variants_are_recognised_by_prefix(self) -> None:
        """EBGP_MULTIHOP and friends are still eBGP."""
        assert _overlay_is_ebgp(_bgp_cap_with_peerings(["EBGP_MULTIHOP"])) is True

    def test_any_ibgp_peering_means_the_asn_is_shared(self) -> None:
        assert _overlay_is_ebgp(_bgp_cap_with_peerings(["IBGP"])) is False

    def test_mixed_sessions_count_as_shared(self) -> None:
        """One iBGP peering is enough to prove the ASN is not unique to this device."""
        assert _overlay_is_ebgp(_bgp_cap_with_peerings(["EBGP", "IBGP"])) is False

    def test_no_process_or_no_peerings_is_not_ebgp(self) -> None:
        assert _overlay_is_ebgp(None) is False
        assert _overlay_is_ebgp(_bgp_cap()) is False
        assert _overlay_is_ebgp(_bgp_cap_with_peerings([])) is False


class TestRtFallbackUnderEbgpOverlayIsLoudlyWrong:
    """The fallback `rt_asn = local_as` is correct under an iBGP overlay and
    fabric-breaking under an eBGP one. Nothing in the rendered config looks
    wrong — sessions come up, routes are advertised, imports never match — so
    the only way an operator finds out is if this is said out loud.
    """

    def _data(self, capabilities: list) -> dict:
        return {
            "name": "dc1-leaf-1",
            "interfaces": [{"name": "Loopback0", "ip_addresses": [{"address": "10.0.0.1/32"}]}],
            "capabilities": capabilities,
        }

    def test_errors_when_ebgp_overlay_has_no_fabric_asn(self, caplog: pytest.LogCaptureFixture) -> None:
        acts = [_make_activation(vlan_id=100, vni=10100)]
        with caplog.at_level(logging.ERROR):
            get_vxlan_config(
                self._data([_bgp_cap_with_peerings(["EBGP"], asn=4245880501)]),
                "arista_eos",
                device_role="leaf",
                activations=acts,
                fabric_rt_asn=None,
            )
        assert "evpn_rt_as" in caplog.text
        assert "dc1-leaf-1" in caplog.text
        assert "4245880501" in caplog.text

    def test_silent_when_the_fabric_asn_is_populated(self, caplog: pytest.LogCaptureFixture) -> None:
        acts = [_make_activation(vlan_id=100, vni=10100)]
        with caplog.at_level(logging.ERROR):
            get_vxlan_config(
                self._data([_bgp_cap_with_peerings(["EBGP"], asn=4245880501)]),
                "arista_eos",
                device_role="leaf",
                activations=acts,
                fabric_rt_asn=4245880999,
            )
        assert caplog.text == ""

    def test_silent_for_an_ibgp_overlay_without_a_fabric_asn(self, caplog: pytest.LogCaptureFixture) -> None:
        """ebgp-ibgp/ospf-ibgp share one overlay ASN fabric-wide, so the
        fallback lands on the same value on every VTEP."""
        acts = [_make_activation(vlan_id=100, vni=10100)]
        with caplog.at_level(logging.ERROR):
            get_vxlan_config(
                self._data([_bgp_cap_with_peerings(["IBGP"], asn=4245880999)]),
                "arista_eos",
                device_role="leaf",
                activations=acts,
                fabric_rt_asn=None,
            )
        assert caplog.text == ""


# ===========================================================================
# svi_vlan_id — the NX-OS-only local VLAN carrying each L3 VNI's transit SVI
# ===========================================================================


class TestL3VniSviVlanAssignment:
    """NX-OS cannot bind a VRF to an L3 VNI without an `ip forward` SVI, and
    that SVI needs a local VLAN. The number has local significance only — no two
    devices need to agree on it — so it is assigned by position rather than
    modelled or drawn from a pool.
    """

    @staticmethod
    def _ns(name: str, l3_vni: int) -> dict:
        return {"name": name, "l3_vni": l3_vni}

    def test_single_vrf_gets_the_base_vlan(self) -> None:
        mappings = _collect_l3_vni_from_namespaces([self._ns("production", 50001)])
        assert [m["svi_vlan_id"] for m in mappings] == [L3VNI_SVI_VLAN_BASE]

    def test_vlans_are_assigned_by_sorted_vrf_name(self) -> None:
        """Assignment must be stable for a given set of VRFs regardless of the
        order GraphQL returned the namespaces in, or a re-render churns the
        config with no data change."""
        forward = _collect_l3_vni_from_namespaces(
            [self._ns("production", 50001), self._ns("development", 50002), self._ns("staging", 50003)]
        )
        reversed_order = _collect_l3_vni_from_namespaces(
            [self._ns("staging", 50003), self._ns("production", 50001), self._ns("development", 50002)]
        )
        assert [(m["vrf_name"], m["svi_vlan_id"]) for m in forward] == [
            ("development", L3VNI_SVI_VLAN_BASE),
            ("production", L3VNI_SVI_VLAN_BASE + 1),
            ("staging", L3VNI_SVI_VLAN_BASE + 2),
        ]
        assert forward == reversed_order

    def test_band_is_above_the_customer_vlan_ceiling(self) -> None:
        """The customer ceiling sits below the firewall-context and transit
        bands, which sit below this band, so none can collide."""
        from utils.limits import CUSTOMER_VLAN_ID_MAX, FW_CONTEXT_VLAN_END

        assert CUSTOMER_VLAN_ID_MAX < FW_CONTEXT_VLAN_END + 600 < L3VNI_SVI_VLAN_BASE

    def test_band_is_below_the_nxos_reserved_range(self) -> None:
        """NX-OS reserves 3968-4094 for its own internal use."""
        assert L3VNI_SVI_VLAN_MAX < 3968

    def test_exhausted_band_yields_none_and_warns(self, caplog: pytest.LogCaptureFixture) -> None:
        """None, not an out-of-range VLAN: the template filters these out rather
        than emitting `interface VlanNone`, so the VRF simply gets no symmetric
        IRB on NX-OS instead of a config the device rejects.
        """
        count = L3VNI_SVI_VLAN_MAX - L3VNI_SVI_VLAN_BASE + 2
        namespaces = [self._ns(f"vrf-{index:04d}", 50001 + index) for index in range(count)]
        with caplog.at_level(logging.WARNING):
            mappings = _collect_l3_vni_from_namespaces(namespaces)

        assert len(mappings) == count
        assert mappings[-2]["svi_vlan_id"] == L3VNI_SVI_VLAN_MAX
        assert mappings[-1]["svi_vlan_id"] is None
        assert mappings[-1]["vrf_name"] in caplog.text

    def test_default_namespace_is_excluded(self) -> None:
        """The default namespace is the underlay — it has no tenant L3 VNI."""
        mappings = _collect_l3_vni_from_namespaces([{"name": "default", "l3_vni": 50001}])
        assert mappings == []

    def test_namespace_without_an_l3_vni_is_excluded(self) -> None:
        mappings = _collect_l3_vni_from_namespaces([{"name": "production", "l3_vni": None}])
        assert mappings == []
