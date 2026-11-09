"""Pytest smoke tests for config transforms.

Each configs/{name}/input.json + output.txt pair becomes one parametrized test.
The directory name prefix determines which transform class to use.

Usage:
    uv run pytest tests/smoke/ -v
    uv run pytest tests/smoke/ -k arista_eos  # filter by platform
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tests.smoke.fixture_builders.common import _make_interface, _render, make_transform, run_transform
from tests.smoke.fixture_builders.firewall import build_firewall_data
from tests.smoke.fixture_builders.vrf import ZONE_CATALOG, routing_zone, with_vrf_objects
from tests.smoke.fixture_builders.zone_migration import legacy_zone_names, migrate_zone_owners
from transforms.common import BaseDeviceTransform
from transforms.config.access_leaf import AccessLeaf
from transforms.config.access_switch import AccessSwitch
from transforms.config.border_leaf import BorderLeaf
from transforms.config.border_spine import BorderSpine
from transforms.config.distribution_switch import DistributionSwitch
from transforms.config.edge import Edge
from transforms.config.firewall import Firewall
from transforms.config.l2_leaf import L2Leaf
from transforms.config.leaf import Leaf
from transforms.config.loadbalancer import LoadBalancer
from transforms.config.spine import Spine
from transforms.config.super_spine import SuperSpine
from transforms.config.tor import ToR
from transforms.helpers.firewall import get_zone_policies

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _raw_vrf(interface: dict) -> dict | None:
    """The raw ManagedVRF of an interface: the one among its ``zone_capabilities`` (None when it is in none)."""
    edges = (interface.get("zone_capabilities") or {}).get("edges") or []
    return next((edge["node"] for edge in edges if edge["node"].get("__typename") == "ManagedVRF"), None)


CONFIGS_DIR = Path(__file__).resolve().parent / "configs"

# Sorted longest-first so "super_spine" matches before "spine", "border_leaf" before "leaf",
# "l2_leaf"/"access_leaf" before "leaf"
_PREFIX_TO_CLASS: list[tuple[str, type]] = sorted(
    [
        ("super_spine", SuperSpine),
        ("border_leaf", BorderLeaf),
        ("border_spine", BorderSpine),
        ("loadbalancer", LoadBalancer),
        ("firewall", Firewall),
        ("spine", Spine),
        ("l2_leaf", L2Leaf),
        ("access_leaf", AccessLeaf),
        ("access_switch", AccessSwitch),
        ("distribution_switch", DistributionSwitch),
        ("leaf", Leaf),
        ("tor", ToR),
        ("edge", Edge),
    ],
    key=lambda t: len(t[0]),
    reverse=True,
)


def _fixture_params() -> list:
    if not CONFIGS_DIR.exists():
        return []
    params = []
    for d in sorted(CONFIGS_DIR.iterdir()):
        if not d.is_dir():
            continue
        input_file = d / "input.json"
        output_file = d / "output.txt"
        if not input_file.exists() or not output_file.exists():
            continue
        for prefix, cls in _PREFIX_TO_CLASS:
            if d.name.startswith(prefix + "_"):
                params.append(pytest.param(cls, d, id=d.name))
                break
    return params


@pytest.mark.parametrize("transform_cls,fixture_dir", _fixture_params())
def test_config_transform_matches_fixture(transform_cls: type, fixture_dir: Path) -> None:
    """Transform output must exactly match the saved fixture."""
    input_data = json.loads((fixture_dir / "input.json").read_text())
    is_sonic = "_sonic_" in fixture_dir.name or "_dell_sonic_" in fixture_dir.name
    expected = (fixture_dir / "output.txt").read_text()
    actual = run_transform(transform_cls, input_data)
    if is_sonic:
        configdb = json.loads(actual)
        assert isinstance(configdb["DEVICE_METADATA"], dict)
        assert "NTP_SERVER" in configdb
        assert "SYSLOG_SERVER" in configdb
        assert not any(name.startswith("PortChannel") for name in configdb["INTERFACE"])
        if fixture_dir.name.endswith("_mlag"):
            domain = configdb["MCLAG_DOMAIN"]["1"]
            assert domain["source_ip"] == "10.254.0.0"
            assert domain["peer_ip"] == "10.254.0.1"
            assert domain["peer_link"] == "PortChannel100"
            # The control SVI is merged into the shared VLAN tables, not
            # rendered as a routed INTERFACE.
            assert configdb["VLAN"]["Vlan4094"]["vlanid"] == "4094"
            assert "Vlan4094|10.254.0.0/31" in configdb["VLAN_INTERFACE"]
            assert "Vlan4094|PortChannel100" in configdb["VLAN_MEMBER"]
            assert "Vlan4094" not in configdb["INTERFACE"]
            assert "1|PortChannel101" in configdb["MCLAG_INTERFACE"]
            assert "PortChannel100|Ethernet33" in configdb["PORTCHANNEL_MEMBER"]
            assert "PortChannel101|Ethernet10" in configdb["PORTCHANNEL_MEMBER"]
        # Every VLAN_MEMBER names a VLAN the device defines; a port carrying VLANs in the
        # input (a host port) is a member of each, tagged on a trunk, untagged on an access port.
        members = configdb.get("VLAN_MEMBER", {})
        assert all(key.split("|")[0] in configdb["VLAN"] for key in members)
        assert all(entry["tagging_mode"] in {"tagged", "untagged"} for entry in members.values())
        if fixture_dir.name.endswith(("_sonic_ebgp_ibgp", "_sonic_ospf_ibgp")) and not fixture_dir.name.startswith(
            ("spine_", "super_spine_", "border_spine_", "border_leaf_")
        ):
            assert members, "a SONiC fixture whose host port carries VLANs must render VLAN_MEMBER"
    assert actual == expected


@pytest.mark.parametrize("name", ZONE_CATALOG)
def test_routing_zone_fixture_uses_catalog(name: str) -> None:
    """A standard zone overrides historical VNI metadata without consulting IPAM."""
    vrf_type, l3_vni = ZONE_CATALOG[name]
    zone = routing_zone(name, l3_vni=50001)
    assert zone["name"] == {"value": name}
    assert zone["vrf_type"] == {"value": vrf_type}
    assert zone["l3_vni"] == {"value": l3_vni}


def test_zone_owner_migration_preserves_ipam_and_underlay() -> None:
    """Only an explicit legacy zone owner migrates; underlay/global IPAM is untouched."""
    data = {
        "interfaces": [
            {"name": {"value": "Loopback0"}, "ip_namespace": {"node": {"name": {"value": "default"}}}},
            {"security_zone": {"node": {"name": {"value": "VRF_A"}, "l3_vni": {"value": 50001}}}},
        ]
    }
    migrated = migrate_zone_owners(data)
    assert migrated["interfaces"][0] == data["interfaces"][0]
    assert migrated["interfaces"][1]["vrf"]["node"]["security_zone"]["node"] == routing_zone("PROD")
    assert "security_zone" not in migrated["interfaces"][1]
    assert legacy_zone_names(data) == {"VRF_A"}
    assert legacy_zone_names(migrated) == set()
    assert migrate_zone_owners(migrated) == migrated


@pytest.mark.parametrize("name", ["default", *ZONE_CATALOG])
def test_standalone_interface_fixture_owns_requested_zone(name: str) -> None:
    """A requested routing zone is explicit on the interface VRF, never its IPAM namespace."""
    interface = _make_interface(name="Ethernet1", device_name="leaf-01", ip_address="192.0.2.0/31", ns_name=name)
    assert interface["ip_address"]["node"]["ip_namespace"]["node"]["name"] == {"value": "default"}
    if name == "default":
        assert _raw_vrf(interface) is None
    else:
        vrf = _raw_vrf(interface)
        assert vrf is not None
        assert vrf["security_zone"]["node"] == routing_zone(name)


@pytest.mark.parametrize("platform", ["paloalto_panos", "checkpoint_gaia"])
def test_firewall_builder_uses_direct_subinterface_zone(platform: str) -> None:
    """A firewall builder must not hide zone authority behind a nonexistent virtual-interface segment edge."""
    data = build_firewall_data(device_name="dc1-fw-01", platform=platform)
    interfaces = data["DcimPhysicalDevice"]["edges"][0]["node"]["interfaces"]["edges"]
    assert data["SecurityZone"] == {"edges": []}
    for edge, name in zip(interfaces, ("PROD", "NONPROD", "INTERNET"), strict=True):
        interface = edge["node"]
        assert interface["name"]["value"].startswith("ethernet1/1.")
        assert "segments" not in interface
        zone = interface["security_zone"]["node"]
        assert "network_segments" not in zone
        assert all(zone[key] == value for key, value in routing_zone(name).items())
        assert "capabilities" not in interface or not any(
            edge["node"].get("__typename") == "ManagedFirewallContext" for edge in interface["capabilities"]["edges"]
        )
    device = data["DcimPhysicalDevice"]["edges"][0]["node"]
    context = device["shared_capabilities"]["edges"][0]["node"]
    assert context["__typename"] == "ManagedFirewallContext"
    assert context["vlan_id"] == {"value": None}
    assert len(context["served_deployments"]["edges"]) == 2


@pytest.mark.parametrize("platform", ["paloalto_panos", "checkpoint_gaia"])
def test_standalone_firewall_context_preserves_ordered_rules(platform: str) -> None:
    """The scoped context keeps the original rule order, endpoints, actions and single closing deny."""
    data = build_firewall_data(device_name="dc1-fw-01", platform=platform)
    instance = make_transform(Firewall)
    device, _, _ = instance.prepared_device(data)
    _, contexts, root, by_context = instance.collect_policies(device)
    assert root == []
    assert len(contexts) == 1
    assert contexts[0]["legs"] == []
    policies = get_zone_policies(by_context[contexts[0]["id"]])
    rules = [rule for policy in policies for rule in policy["rules"]]
    assert [rule["name"] for rule in rules] == ["allow-https", "allow-ssh", "allow-any", "implicit-deny-all"]
    assert [(rule["src_zone"], rule["dst_zone"]) for rule in rules[:3]] == [
        ("NONPROD", "PROD"),
        ("NONPROD", "PROD"),
        ("PROD", "NONPROD"),
    ]
    assert [rule["port_start"] for rule in rules[:3]] == [443, 22, None]
    assert [rule["action"] for rule in rules] == ["permit", "permit", "permit", "deny"]
    assert [rule["log"] for rule in rules] == [True, True, False, True]
    assert rules[0]["security_profile"] == "strict-av"


@pytest.mark.parametrize(
    "fixture_name",
    [
        "firewall_checkpoint_gaia_with_ha",
        "firewall_checkpoint_gaia_with_policy",
        "firewall_paloalto_panos_internet_permit",
        "firewall_paloalto_panos_with_ha",
        "firewall_paloalto_panos_with_policy",
    ],
)
def test_standalone_firewall_fixture_keeps_authored_interfaces_and_routes_at_the_root(
    fixture_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The policy-only context (no VLAN, so no legs) owns no interface: the authored /30 interfaces and their return
    routes are the firewall's own. A context owns the legs derived from its VLAN (utils/service_contexts.py), nothing
    else is stored on an interface to bind it."""
    data = json.loads((CONFIGS_DIR / fixture_name / "input.json").read_text())
    instance = make_transform(Firewall)
    render = MagicMock(wraps=instance._render)
    monkeypatch.setattr(instance, "_render", render)
    actual = _render(instance, data)
    config = render.call_args.args[1]
    assert len(config["contexts"]) == 1
    context = config["contexts"][0]
    assert context["legs"] == []
    assert context["routes"] == []
    expected = [
        {"destination": "10.0.1.0/24", "nexthop": "10.0.1.2", "interface": "ethernet1/1.10", "vrf": "PROD"},
    ]
    if not fixture_name.endswith("internet_permit"):
        expected.extend(
            [
                {
                    "destination": "10.0.10.0/24",
                    "nexthop": "10.0.1.2",
                    "interface": "ethernet1/1.10",
                    "vrf": "PROD",
                },
                {
                    "destination": "10.0.2.0/24",
                    "nexthop": "10.0.2.2",
                    "interface": "ethernet1/1.20",
                    "vrf": "NONPROD",
                },
            ]
        )
    routes = [
        {field: route[field] for field in ("destination", "nexthop", "interface", "vrf")}
        for route in config["static_routes"]
    ]
    assert routes == sorted(expected, key=lambda route: (route["vrf"], route["destination"]))
    names = [interface["name"] for interface in config["fw_interfaces"]]
    assert config["default_vr_interfaces"] == names
    for interface in config["fw_interfaces"]:
        assert interface["context_name"] is None
        assert interface["ip_address"]["address"] in {"10.0.1.1/30", "10.0.2.1/30", "10.0.3.1/30"}
    scope = context["name"]
    for name in names:
        assert f"{scope} import network interface {name}" not in actual
        assert f"{scope} interface {name}" not in actual


def test_virtual_firewall_management_route_requires_both_native_authorities(monkeypatch: pytest.MonkeyPatch) -> None:
    """The unowned native management interface routes only its explicitly queried zone prefixes."""
    data = json.loads((CONFIGS_DIR / "firewall_checkpoint_gaia_virtual_dedicated" / "input.json").read_text())
    device = data["DcimDevice"]["edges"][0]["node"]
    management = next(edge["node"] for edge in device["interfaces"]["edges"] if edge["node"]["name"]["value"] == "eth0")
    vrf = _raw_vrf(management)
    assert vrf is not None
    assert vrf["security_zone"]["node"] == routing_zone("MANAGEMENT")
    assert management["ip_address"]["node"]["address"] == {"value": "10.254.1.11/24"}
    assert management["ip_address"]["node"]["ip_namespace"]["node"]["name"] == {"value": "default"}
    assert management["capabilities"] == {"edges": []}
    expected = [
        {
            "vrf": "MANAGEMENT",
            "destination": "10.250.0.0/24",
            "nexthop": "10.254.1.1",
            "interface": "eth0",
            "zone": "MANAGEMENT",
        }
    ]
    policy_tables = []
    for missing_authority in (None, "interface_zone", "queried_cidrs"):
        probe = deepcopy(data)
        if missing_authority == "interface_zone":
            interfaces = probe["DcimDevice"]["edges"][0]["node"]["interfaces"]["edges"]
            next(edge["node"] for edge in interfaces if edge["node"]["name"]["value"] == "eth0")[
                "zone_capabilities"
            ] = {"edges": []}
        elif missing_authority == "queried_cidrs":
            probe["SecurityZone"]["edges"] = []
        instance = make_transform(Firewall)
        render = MagicMock(wraps=instance._render)
        monkeypatch.setattr(instance, "_render", render)
        _render(instance, probe)
        config = render.call_args.args[1]
        assert config["static_routes"] == (expected if missing_authority is None else [])
        policy_tables.append([context["policies"] for context in config["contexts"]])
    assert policy_tables[0] == policy_tables[1] == policy_tables[2]


@pytest.mark.parametrize("transform_cls,fixture_dir", _fixture_params())
def test_config_fixture_raw_zone_and_svi_contract(transform_cls: type, fixture_dir: Path) -> None:
    """Persisted inputs already own catalog zones and device-scoped SVIs; the render hook adds nothing."""
    data = json.loads((fixture_dir / "input.json").read_text())
    assert not legacy_zone_names(data)
    assert migrate_zone_owners(data) == data
    instance = make_transform(transform_cls)
    if isinstance(instance, BaseDeviceTransform):
        assert with_vrf_objects(instance, data) == data
    pending = [data]
    while pending:
        node = pending.pop()
        if isinstance(node, dict):
            if node.get("__typename") == "DcimVirtualInterface":
                assert not (node.get("segments") or {}).get("edges")
            if namespace := (node.get("ip_namespace") or {}).get("node"):
                assert namespace["name"] == {"value": "default"}
            pending.extend(node.values())
        elif isinstance(node, list):
            pending.extend(node)
