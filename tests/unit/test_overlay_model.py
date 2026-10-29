"""Unit tests for the control-plane-neutral overlay model (transforms/helpers/overlay.py).

Covers:
  - neutrality guard         — no BGP/EVPN state in any model field or in the builder signature
  - build_overlay_node()     — role gate, instances, locator, default-exit flag, anycast gateway
  - evpn_vxlan_context()     — get_vxlan_config is exactly the composition of builder and renderer
  - purity                   — neither the inputs nor the model are mutated; output lists stay lists
  - control-plane registry   — OVERLAY_RENDERERS / OVERLAY_CONFIG_KEYS pins, the two renderer layers never import each other
"""

from __future__ import annotations

import ast
import copy
import dataclasses
import inspect
import re
from pathlib import Path
from typing import Any

import pytest

from transforms.helpers import lisp, overlay
from transforms.helpers import vxlan as vxlan_module
from transforms.helpers.overlay import (
    OVERLAY_CONFIG_KEYS,
    OVERLAY_RENDERERS,
    AnycastGateway,
    L2Instance,
    L3Instance,
    Locator,
    OverlayNode,
    build_overlay_node,
    select_locator,
)
from transforms.helpers.vrf import device_vrfs
from transforms.helpers.vxlan import (
    _DEFAULT_ANYCAST_GATEWAY_MAC,
    EvpnInputs,
    _is_bgw,
    build_evpn_inputs,
    evpn_vxlan_context,
    get_vxlan_config,
)

_MODEL_CLASSES = (Locator, AnycastGateway, L2Instance, L3Instance, OverlayNode)
# A name token that ties a field or parameter to BGP/EVPN. Matched per underscore-separated word,
# so `address` or `arp_suppression` are fine but `rt_anchor` / `local_asn` / `rd_format` are not.
_CONTROL_PLANE_TOKENS = {"rt", "rd", "asn", "as", "evpn", "bgp", "capabilities", "capability", "peering", "peerings"}
_INTERNET_NS = {"name": "INTERNET", "l3_vni": 60002, "namespace_type": "internet"}
_PROD_NS = {"name": "PROD", "l3_vni": 50001, "namespace_type": "prod"}


def _tokens(name: str) -> set[str]:
    return set(re.split(r"_+", name.lower()))


def _segment(
    *,
    name: str = "web",
    gateway: str | None = None,
    namespace: dict[str, Any] | None = None,
    stretched_over: list[tuple[str, int]] | None = None,
) -> dict[str, Any]:
    seg: dict[str, Any] = {"name": f"Owner - prod - {name}", "customer_name": name, "arp_suppression": True}
    if gateway or namespace:
        gw: dict[str, Any] = {"ip_prefix": {"ip_namespace": namespace or {"name": "default"}}}
        if gateway:
            gw["address"] = gateway
        seg["gateway"] = gw
    if stretched_over:
        seg["id"] = f"seg-{name}"
        seg["stretch_scope"] = "metro"
        seg["segment_deployments"] = [
            {"vni": 1, "deployment": {"id": dep, "evpn_rt_as": {"asn": asn}}} for dep, asn in stretched_over
        ]
    return seg


def _activation(vlan_id: int, vni: int | None, segment: dict[str, Any]) -> dict[str, Any]:
    return {"vlan_id": vlan_id, "vni": vni, "status": "active", "segment": segment}


def _bgp_cap(asn: int = 4245880501, router_id: str = "10.0.0.7/32", role: str = "overlay") -> dict[str, Any]:
    return {
        "typename": "ManagedBGP",
        "name": "bgp-overlay",
        "process_role": role,
        "local_as": {"asn": asn},
        "router_id": {"address": router_id},
    }


def _vrf(namespace: dict[str, Any]) -> dict[str, Any]:
    """A ManagedVRF membership of the device (``device.vrfs``), as the cleaned config query returns it."""
    return {"namespace": namespace}


def _leaf(capabilities: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "name": "dc1-leaf-1",
        "interfaces": [
            {"name": "Loopback0", "role": "loopback", "ip_address": {"address": "10.0.0.1/32"}},
            {"name": "Ethernet1", "role": "uplink"},
        ],
        "capabilities": [_bgp_cap()] if capabilities is None else capabilities,
        "vrfs": [_vrf(_PROD_NS)] if capabilities is None else [],
    }


def _leaf_activations() -> list[dict[str, Any]]:
    return [
        _activation(100, 10100, _segment(name="web", gateway="10.0.100.1/24", namespace=_PROD_NS)),
        _activation(200, 10200, _segment(name="l2only")),
        _activation(250, None, _segment(name="traditional")),
        _activation(
            300,
            10300,
            _segment(
                name="stretched",
                gateway="10.0.30.1/24",
                namespace=_PROD_NS,
                stretched_over=[("dc-10", 65010), ("colo-fr", 65100)],
            ),
        ),
        # a second record for VLAN 100 never overrides the first
        _activation(100, 19999, _segment(name="dup")),
    ]


def _transit_peering() -> dict[str, Any]:
    return {
        "name": "TRANSIT",
        "session_type": "EBGP",
        "peering_role": "regular",
        "capabilities": [
            {
                "name": "Ethernet1/14",
                "device": {"name": "eg-fr201"},
                "ip_address": {"address": "203.0.113.0/31", "ip_namespace": _INTERNET_NS},
            }
        ],
    }


def _bgw_edge() -> dict[str, Any]:
    return {
        "name": "eg-fr201",
        "interfaces": [
            {"name": "loopback0", "role": "loopback", "ip_address": {"address": "10.0.0.2/32"}},
            {"name": "loopback1", "role": "loopback-vtep", "ip_address": {"address": "fd00:100::1/128"}},
            {"name": "Ethernet1/1", "role": "uplink"},
            {"name": "loopback100", "role": "multisite-vip", "ip_address": {"address": "fd00:1ff::1/128"}},
        ],
        "capabilities": [
            {"typename": "ManagedBGP", "name": "bgp", "peerings": [_transit_peering()]},
        ],
        "vrfs": [_vrf(_INTERNET_NS), _vrf(_PROD_NS)],
    }


# (id, data, role, activations, fabric_rt_asn, fabric_anycast_mac)
_SCENARIOS: list[tuple[str, dict[str, Any], str, list[dict[str, Any]] | None, int | None, str | None]] = [
    ("leaf-fabric-asn", _leaf(), "leaf", _leaf_activations(), 65100, "00:00:22:22:33:33"),
    ("leaf-no-fabric-asn", _leaf(), "border-leaf", _leaf_activations(), None, None),
    ("leaf-no-bgp", _leaf(capabilities=[]), "leaf", _leaf_activations(), None, None),
    ("bgw-internet-only", _bgw_edge(), "edge", None, 65100, None),
    ("bgw-with-segments", _bgw_edge(), "edge", _leaf_activations(), 65100, None),
]


def _compose(
    data: dict[str, Any],
    platform: str,
    role: str,
    activations: list[dict[str, Any]] | None,
    fabric_rt_asn: int | None,
    mac: str | None,
) -> dict | None:
    """The documented composition get_vxlan_config is a thin wrapper around."""
    vrfs = device_vrfs(data)
    exits = (
        frozenset(ns["name"] for ns in vrfs if ns["namespace_type"] == "internet")
        if _is_bgw(data, fabric_rt_asn)
        else frozenset()
    )
    node = build_overlay_node(
        data["interfaces"], role, activations or [], vrfs, exits, mac or _DEFAULT_ANYCAST_GATEWAY_MAC
    )
    if node is None:
        return None
    return evpn_vxlan_context(node, build_evpn_inputs(data, activations, fabric_rt_asn), platform)


class TestNeutralityGuard:
    @pytest.mark.parametrize("cls", _MODEL_CLASSES, ids=lambda c: c.__name__)
    def test_model_fields_carry_no_control_plane_state(self, cls: Any) -> None:
        """A field named after RD/RT/ASN/EVPN/BGP would make the model EVPN-shaped again."""
        offenders = [f.name for f in dataclasses.fields(cls) if _tokens(f.name) & _CONTROL_PLANE_TOKENS]
        assert offenders == []

    def test_model_classes_are_frozen(self) -> None:
        for cls in _MODEL_CLASSES:
            assert getattr(cls, "__dataclass_params__").frozen, cls.__name__  # noqa: B009

    def test_builder_signature_takes_no_bgp_capabilities(self) -> None:
        params = inspect.signature(build_overlay_node).parameters
        offenders = [name for name in params if _tokens(name) & _CONTROL_PLANE_TOKENS or name in {"data", "device"}]
        assert offenders == []

    def test_overlay_module_does_not_import_the_evpn_layer(self) -> None:
        tree = ast.parse(Path(overlay.__file__).read_text())
        imported = {
            node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module is not None
        } | {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        assert "transforms.helpers.vxlan" not in imported
        assert not {m for m in imported if "bgp" in m}


def _imported_modules(module: Any) -> set[str]:
    """Every module a source file imports (``import x`` and ``from x import y``)."""
    tree = ast.parse(Path(module.__file__).read_text())
    return {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module is not None} | {
        alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names
    }


class TestControlPlaneRegistry:
    def test_only_lisp_has_a_renderer_allow_list_and_it_is_cisco_xe(self) -> None:
        """bgp_evpn is the default and has no entry: every platform decides for itself whether it renders EVPN."""
        assert OVERLAY_RENDERERS == {"lisp": frozenset({"cisco_xe"})}

    def test_config_keys_name_the_template_context_key_of_each_control_plane(self) -> None:
        assert OVERLAY_CONFIG_KEYS == {"bgp_evpn": "vxlan", "lisp": "lisp"}

    def test_every_control_plane_with_a_renderer_has_a_config_key(self) -> None:
        assert set(OVERLAY_RENDERERS) <= set(OVERLAY_CONFIG_KEYS)

    def test_the_lisp_layer_never_imports_the_evpn_layer(self) -> None:
        imported = _imported_modules(lisp)
        assert "transforms.helpers.vxlan" not in imported
        assert not {m for m in imported if "bgp" in m}

    def test_the_evpn_layer_never_imports_the_lisp_layer(self) -> None:
        assert "transforms.helpers.lisp" not in _imported_modules(vxlan_module)

    def test_the_anycast_mac_default_lives_in_the_neutral_model_and_vxlan_re_exports_it(self) -> None:
        assert overlay._DEFAULT_ANYCAST_GATEWAY_MAC == "00:1c:73:00:dc:01"
        assert vxlan_module._DEFAULT_ANYCAST_GATEWAY_MAC is overlay._DEFAULT_ANYCAST_GATEWAY_MAC

    def test_the_overlay_model_has_no_lisp_fields(self) -> None:
        """EID prefixes, map-servers, site and key are the LISP layer's inputs, not model fields."""
        names = {f.name for cls in _MODEL_CLASSES for f in dataclasses.fields(cls)}
        assert not {n for n in names if _tokens(n) & {"lisp", "eid", "site", "key", "map", "flood"}}


class TestSelectLocator:
    def test_prefers_the_dedicated_vtep_loopback(self) -> None:
        assert select_locator(_bgw_edge()["interfaces"]) == Locator(interface="loopback1", address="fd00:100::1")

    def test_falls_back_to_loopback0(self) -> None:
        assert select_locator(_leaf()["interfaces"]) == Locator(interface="Loopback0", address="10.0.0.1")

    def test_no_loopback_is_an_empty_locator_and_does_not_mutate(self) -> None:
        interfaces = [{"name": "Ethernet1", "role": "uplink"}]
        before = copy.deepcopy(interfaces)

        assert select_locator(interfaces) == Locator(interface=None, address=None)
        assert interfaces == before


class TestBuildOverlayNode:
    def _build(self, **overrides: Any) -> OverlayNode | None:
        kwargs: dict[str, Any] = {
            "interfaces": _leaf()["interfaces"],
            "device_role": "leaf",
            "activations": _leaf_activations(),
            "vrfs": [_PROD_NS],
            "exits": frozenset(),
            "anycast_mac": "00:00:22:22:33:33",
        }
        kwargs.update(overrides)
        return build_overlay_node(**kwargs)

    def test_non_vtep_role_has_no_overlay(self) -> None:
        assert self._build(device_role="spine") is None

    def test_no_instances_means_no_overlay(self) -> None:
        assert self._build(activations=[]) is None

    def test_l2_instances_skip_vni_less_and_duplicate_vlans_and_sort_by_vlan(self) -> None:
        node = self._build()
        assert node is not None
        assert [(i.vlan_id, i.instance_id) for i in node.l2] == [(100, 10100), (200, 10200), (300, 10300)]
        first = node.l2[0]
        assert (first.name, first.vrf, first.l3_instance_id, first.gateway_ip) == (
            "web",
            "PROD",
            50001,
            "10.0.100.1/24",
        )
        assert [i.stretched for i in node.l2] == [False, False, True]

    def test_l3_instances_are_per_vrf_and_flag_the_default_exit(self) -> None:
        node = self._build(vrfs=[_PROD_NS, _INTERNET_NS], exits=frozenset({"INTERNET"}))
        assert node is not None
        assert [(i.vrf, i.instance_id, i.default_exit) for i in node.l3] == [
            ("INTERNET", 60002, True),
            ("PROD", 50001, False),
        ]

    def test_l3_svi_vlan_is_the_fixed_slot_of_the_namespace_type(self) -> None:
        """A VRF keeps its VLAN whatever else the device carries (3900 + TRANSIT_SLOT)."""
        both = self._build(vrfs=[_PROD_NS, _INTERNET_NS], exits=frozenset({"INTERNET"}))
        only_internet = self._build(activations=[], vrfs=[_INTERNET_NS], exits=frozenset({"INTERNET"}))
        assert both is not None and only_internet is not None
        assert {i.vrf: i.svi_vlan_id for i in both.l3} == {"INTERNET": 3902, "PROD": 3900}
        assert [i.svi_vlan_id for i in only_internet.l3] == [3902]

    def test_a_namespace_type_without_a_slot_has_no_svi_vlan(self, caplog: pytest.LogCaptureFixture) -> None:
        odd = {"name": "ODD", "l3_vni": 50009, "namespace_type": "lab"}
        activation = _activation(100, 10100, _segment(name="web", gateway="10.0.100.1/24", namespace=odd))
        node = self._build(activations=[activation], vrfs=[odd])
        assert node is not None
        assert [(i.vrf, i.svi_vlan_id) for i in node.l3] == [("ODD", None)]
        assert "no L3 VNI SVI slot" in caplog.text

    def test_default_exit_alone_is_enough_to_have_an_overlay(self) -> None:
        node = self._build(activations=[], vrfs=[_INTERNET_NS], exits=frozenset({"INTERNET"}))
        assert node is not None
        assert node.l2 == ()
        assert [i.vrf for i in node.l3] == ["INTERNET"]

    def test_an_instance_without_a_routed_activation_or_exit_stays_vrf_lite(self) -> None:
        """The realization rule: the INTERNET instance of an edge with an overlay process is not an L3 instance."""
        node = self._build(vrfs=[_PROD_NS, _INTERNET_NS])
        assert node is not None
        assert [i.vrf for i in node.l3] == ["PROD"]

    def test_an_activation_in_a_namespace_with_no_instance_is_not_an_l3_instance(self) -> None:
        """The device's VRF set is its instances only (the transform refuses the missing one before this)."""
        node = self._build(vrfs=[])
        assert node is not None
        assert node.l3 == ()

    def test_the_default_namespace_and_a_namespace_without_l3_vni_are_never_l3_instances(self) -> None:
        no_vni = {"name": "NOVNI", "l3_vni": None, "namespace_type": "prod"}
        default = {"name": "default", "l3_vni": 1, "namespace_type": "prod"}
        node = self._build(vrfs=[no_vni, default], exits=frozenset({"NOVNI", "default"}))
        assert node is not None
        assert node.l3 == ()

    def test_locator_prefers_the_dedicated_vtep_loopback(self) -> None:
        node = self._build(interfaces=_bgw_edge()["interfaces"], device_role="edge")
        assert node is not None
        assert node.locator == Locator(interface="loopback1", address="fd00:100::1")

    def test_locator_without_a_loopback_is_empty_not_defaulted(self) -> None:
        """Loopback0 is a vendor convention applied by the renderer, not by the neutral model."""
        node = self._build(interfaces=[{"name": "Ethernet1", "role": "uplink"}])
        assert node is not None
        assert node.locator == Locator(interface=None, address=None)

    def test_anycast_gateway_enabled_only_with_a_gateway_address(self) -> None:
        with_gw = self._build()
        no_gw = self._build(activations=[_activation(200, 10200, _segment(name="l2only"))])
        assert with_gw is not None and no_gw is not None
        assert with_gw.gateway == AnycastGateway(enabled=True, mac="00:00:22:22:33:33")
        assert no_gw.gateway.enabled is False


class TestEvpnComposition:
    @pytest.mark.parametrize("platform", ["arista_eos", "cisco_nxos", "dell_sonic", "unknown_platform"])
    @pytest.mark.parametrize("scenario", _SCENARIOS, ids=lambda s: s[0])
    def test_get_vxlan_config_equals_builder_plus_evpn_context(
        self,
        scenario: tuple[str, dict[str, Any], str, list[dict[str, Any]] | None, int | None, str | None],
        platform: str,
    ) -> None:
        _, data, role, activations, fabric_rt_asn, mac = scenario
        direct = get_vxlan_config(
            data,
            platform,
            device_role=role,
            activations=activations,
            fabric_rt_asn=fabric_rt_asn,
            fabric_anycast_mac=mac,
        )
        assert direct is not None
        assert direct == _compose(data, platform, role, activations, fabric_rt_asn, mac)

    def test_dropped_dead_keys_stay_dropped(self) -> None:
        result = get_vxlan_config(_leaf(), "arista_eos", activations=_leaf_activations(), fabric_rt_asn=65100)
        assert result is not None
        assert "flooding" not in result
        assert "rt_format" not in result["evpn"]
        assert all("rt_anchor_asn" not in m for m in result["l2_vni_mappings"])

    def test_rd_and_rt_are_added_by_the_evpn_layer(self) -> None:
        result = get_vxlan_config(_leaf(), "cisco_nxos", activations=_leaf_activations(), fabric_rt_asn=65100)
        assert result is not None
        assert result["evpn"] == {"enabled": True, "rd_format": "10.0.0.7:{vni}"}
        by_vlan = {m["vlan_id"]: m for m in result["l2_vni_mappings"]}
        assert by_vlan[100]["route_target"] == "65100:10100"
        # a stretched segment anchors on the lowest site ASN, not this fabric's
        assert by_vlan[300]["route_target"] == "65010:10300"
        assert by_vlan[300]["multisite"] is True
        (prod,) = result["l3_vni_mappings"]
        assert prod["route_target"] == "65100:50001"
        assert prod["import_route_targets"] == ["65010:50001"]
        assert result["anycast_gateway"] == {"enabled": True, "mac": _DEFAULT_ANYCAST_GATEWAY_MAC}

    def test_locator_falls_back_to_loopback0_in_the_evpn_layer(self) -> None:
        data = _leaf()
        data["interfaces"] = [{"name": "Ethernet1", "role": "uplink"}]
        result = get_vxlan_config(data, "arista_eos", activations=_leaf_activations(), fabric_rt_asn=65100)
        assert result is not None
        assert result["vtep"] == {"source_interface": "Loopback0", "ipv4": None, "udp_port": 4789}


class TestRenderedContextShape:
    """Flatten hazards: the templates concatenate, `| default` and test these values."""

    def _result(self, scenario_id: str) -> dict:
        _, data, role, activations, fabric_rt_asn, mac = next(s for s in _SCENARIOS if s[0] == scenario_id)
        result = get_vxlan_config(
            data,
            "cisco_nxos",
            device_role=role,
            activations=activations,
            fabric_rt_asn=fabric_rt_asn,
            fabric_anycast_mac=mac,
        )
        assert result is not None
        return result

    def test_mapping_collections_are_lists(self) -> None:
        result = self._result("bgw-with-segments")
        assert type(result["l2_vni_mappings"]) is list
        assert type(result["l3_vni_mappings"]) is list
        assert type(result["multisite"]["advertise_interfaces"]) is list
        assert type(result["multisite"]["dci_interfaces"]) is list
        assert type(result["multisite"]["fabric_interfaces"]) is list
        assert type(result["features"]) is list

    def test_import_route_targets_is_always_a_list(self) -> None:
        for scenario_id in ("leaf-fabric-asn", "bgw-with-segments", "bgw-internet-only"):
            for mapping in self._result(scenario_id)["l3_vni_mappings"]:
                assert type(mapping["import_route_targets"]) is list

    def test_namespace_type_is_present_only_on_the_internet_vrf(self) -> None:
        mappings = {m["vrf_name"]: m for m in self._result("bgw-with-segments")["l3_vni_mappings"]}
        assert mappings["INTERNET"]["namespace_type"] == "internet"
        assert "namespace_type" not in mappings["PROD"]

    def test_l3_mapping_keys_keep_their_order(self) -> None:
        mappings = {m["vrf_name"]: m for m in self._result("bgw-with-segments")["l3_vni_mappings"]}
        assert list(mappings["INTERNET"]) == [
            "vrf_name",
            "l3_vni",
            "namespace_type",
            "svi_vlan_id",
            "route_target",
            "import_route_targets",
        ]
        assert list(mappings["PROD"]) == ["vrf_name", "l3_vni", "svi_vlan_id", "route_target", "import_route_targets"]


class TestPurity:
    @pytest.mark.parametrize("scenario", _SCENARIOS, ids=lambda s: s[0])
    def test_inputs_are_not_mutated(
        self, scenario: tuple[str, dict[str, Any], str, list[dict[str, Any]] | None, int | None, str | None]
    ) -> None:
        _, data, role, activations, fabric_rt_asn, mac = scenario
        data_before, acts_before = copy.deepcopy(data), copy.deepcopy(activations)
        get_vxlan_config(
            data,
            "cisco_nxos",
            device_role=role,
            activations=activations,
            fabric_rt_asn=fabric_rt_asn,
            fabric_anycast_mac=mac,
        )
        assert data == data_before
        assert activations == acts_before

    def test_evpn_context_mutates_neither_the_model_nor_its_inputs_and_is_repeatable(self) -> None:
        data, activations = _leaf(), _leaf_activations()
        node = build_overlay_node(
            data["interfaces"], "leaf", activations, device_vrfs(data), frozenset(), _DEFAULT_ANYCAST_GATEWAY_MAC
        )
        inputs = build_evpn_inputs(data, activations, 65100)
        assert node is not None
        node_before, inputs_before = copy.deepcopy(node), copy.deepcopy(inputs)

        first = evpn_vxlan_context(node, inputs, "cisco_nxos")
        # the caller owns the returned dict: scribbling on it must not leak into the model or a later call
        first["l2_vni_mappings"][0]["vni"] = -1
        first["l3_vni_mappings"].clear()
        first["features"].append("junk")
        second = evpn_vxlan_context(node, inputs, "cisco_nxos")

        assert node == node_before
        assert inputs == inputs_before
        assert second == get_vxlan_config(data, "cisco_nxos", activations=activations, fabric_rt_asn=65100)
        assert second["l2_vni_mappings"][0]["vni"] == 10100

    def test_model_instances_cannot_be_reassigned(self) -> None:
        node = build_overlay_node(
            _leaf()["interfaces"], "leaf", _leaf_activations(), [_PROD_NS], frozenset(), "00:00:22:22:33:33"
        )
        assert node is not None
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(node, "role", "spine")  # noqa: B010
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(node.l2[0], "vlan_id", 1)  # noqa: B010

    def test_evpn_inputs_carry_only_the_devices_own_evpn_state(self) -> None:
        inputs = build_evpn_inputs(_leaf(), _leaf_activations(), 65100)
        assert isinstance(inputs, EvpnInputs)
        assert inputs.local_as == 4245880501
        assert inputs.evpn_enabled is True
        assert inputs.router_id == "10.0.0.7"
        assert inputs.rt_anchor_by_vlan == {100: None, 200: None, 300: 65010}
        assert inputs.remote_site_asns_by_vrf == {"PROD": {65010, 65100}}
