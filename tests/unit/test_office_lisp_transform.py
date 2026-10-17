"""Unit tests for the render side of an office LISP site (access-switch and distribution-switch on cisco_xe).

An office switch is a LISP device by role: there is no ``overlay_control_plane`` flag on an office, so
``transforms/common.py:_runs_lisp`` gates on ``utils/fabric_roles.OFFICE_LISP_ROLES`` while a data center's VTEPs
keep the flag. The renderer is the DC one (transforms/helpers/lisp.py) retargeted: map-servers are the
distribution-switches of the office, the site is the office, the key is the office's lisp_site_key.

Covers:
  - the role gate           — office roles always LISP, a DC VTEP only with the flag, other roles never
  - the rendered CLI        — access-switch is an edge, distribution-switch the co-located border with the office site
  - refusals                — no map-server / key / office name, other platform, a segment outside a VRF with an
                              L3 instance-id (also on a data center: the fix of the global-table SVI), IPv6 gateway
  - the registration        — .infrahub.yml artifacts, transforms, queries and the target groups of the roles
  - the role sets           — the office roles stay out of the DC-only sets
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml
from graphql import parse
from graphql.language import FragmentDefinitionNode, FragmentSpreadNode

from generators.devices import ROLE_GROUP_NAMES
from tests.integration.demo_data import attribute_names, relationship_names
from tests.smoke.test_configs_smoke import _run_transform
from transforms.common import BaseDeviceTransform, _runs_lisp
from transforms.config.access_switch import AccessSwitch
from transforms.config.border_leaf import BorderLeaf
from transforms.config.distribution_switch import DistributionSwitch
from transforms.helpers.lisp import EidRecord, LispInputs, build_lisp_inputs, get_lisp_config
from transforms.helpers.overlay import build_overlay_node
from utils.fabric_roles import (
    BORDER_ROLES,
    CAMPUS_FABRIC_ROLES,
    DC_VTEP_ROLES,
    LISP_BORDER_ROLES,
    OFFICE_LISP_ROLES,
    VTEP_ROLES,
)

ROOT = Path(__file__).resolve().parents[2]
CONFIGS_DIR = ROOT / "tests" / "smoke" / "configs"
ACCESS_INPUT = "access_switch_cisco_xe_lisp_ospf"
DISTRIBUTION_INPUT = "distribution_switch_cisco_xe_lisp_ospf"
DC_BORDER_INPUT = "border_leaf_cisco_xe_lisp_ospf_ibgp"
MARKER = "unverified-no-lab:"
OFFICE_SWITCHES = [(AccessSwitch, ACCESS_INPUT), (DistributionSwitch, DISTRIBUTION_INPUT)]

_OFFICE = "C005-P"
_MAP_SERVERS: list[dict[str, Any]] = [
    {"name": "ds-c005-p02", "interfaces": [{"ip_address": {"address": "10.250.5.2/32"}}]},
    {"name": "ds-c005-p01", "interfaces": [{"ip_address": {"address": "10.250.5.1/32"}}]},
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _raw(directory: str) -> dict[str, Any]:
    """The raw GraphQL input of a smoke golden."""
    return json.loads((CONFIGS_DIR / directory / "input.json").read_text())


def _node(data: dict[str, Any]) -> dict[str, Any]:
    return data["DcimDevice"]["edges"][0]["node"]


def _deployment(data: dict[str, Any]) -> dict[str, Any]:
    return _node(data)["deployment"]["node"]


def _seg200_namespaces(data: dict[str, Any]) -> list[dict[str, Any]]:
    """The ip_namespace nodes of seg-200 everywhere it appears in the input (host port and activations)."""
    segments: list[dict[str, Any]] = []
    for iface in _node(data)["interfaces"]["edges"]:
        segments += [
            edge["node"] for edge in iface["node"]["interface_capabilities"]["edges"] if "gateway" in edge["node"]
        ]
    segments += [act["node"]["segment"]["node"] for act in _deployment(data)["segment_deployments"]["edges"]]
    return [
        seg["gateway"]["node"]["ip_prefix"]["node"]["ip_namespace"]["node"]
        for seg in segments
        if seg["name"]["value"] == "seg-200"
    ]


def _lisp_lines(text: str) -> list[str]:
    """The lines of the ``router lisp`` block of a rendered config (up to the next column-0 ``!``)."""
    lines = text.splitlines()
    start = lines.index("router lisp")
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "!")
    return lines[start:end]


def _inputs(**overrides: Any) -> LispInputs:
    values: dict[str, Any] = {
        "map_servers": ("10.250.5.1", "10.250.5.2"),
        "site_name": _OFFICE,
        "site_key": "0ff1c3-k3y",
        "l3_records": (EidRecord(instance_id=50001, vrf="VRF_A", prefix="10.100.0.0/24"),),
        "l2_instance_ids": (10100,),
    }
    values.update(overrides)
    return LispInputs(**values)


def _activation(
    vlan_id: int = 100, vni: int | None = 10100, gateway: str = "10.100.0.1/24", namespace: dict | None = None
) -> dict[str, Any]:
    segment: dict[str, Any] = {
        "name": f"seg-{vlan_id}",
        "customer_name": f"seg-{vlan_id}",
        "gateway": {
            "address": gateway,
            "ip_prefix": {"ip_namespace": namespace if namespace is not None else {"name": "VRF_A", "l3_vni": 50001}},
        },
    }
    return {"vlan_id": vlan_id, "vni": vni, "status": "active", "segment": segment}


def _device() -> dict[str, Any]:
    return {
        "name": "as-c005-p01",
        "interfaces": [{"name": "Loopback0", "role": "loopback", "ip_address": {"address": "10.0.0.1/32"}}],
    }


# ---------------------------------------------------------------------------
# The role gate and the role sets
# ---------------------------------------------------------------------------


class TestRoleGate:
    @pytest.mark.parametrize("role", sorted(OFFICE_LISP_ROLES))
    @pytest.mark.parametrize("deployment", [None, {}, {"overlay_control_plane": "bgp_evpn"}, {"name": _OFFICE}])
    def test_an_office_role_is_lisp_whatever_the_deployment_says(self, role: str, deployment: Any) -> None:
        """An office has no overlay flag: the role alone selects the LISP overlay."""
        assert _runs_lisp(role, deployment) is True

    @pytest.mark.parametrize("role", sorted(DC_VTEP_ROLES))
    def test_a_dc_vtep_is_lisp_only_with_the_fabric_flag(self, role: str) -> None:
        assert _runs_lisp(role, {"overlay_control_plane": "lisp"}) is True
        assert _runs_lisp(role, {"overlay_control_plane": "bgp_evpn"}) is False
        assert _runs_lisp(role, {"parent": {"overlay_control_plane": "lisp"}}) is True
        assert _runs_lisp(role, None) is False

    @pytest.mark.parametrize("role", ["spine", "super-spine", "hyper-spine", "l2-leaf", "edge", "firewall"])
    def test_other_roles_never_run_lisp(self, role: str) -> None:
        assert _runs_lisp(role, {"overlay_control_plane": "lisp"}) is False


class TestRoleSets:
    def test_the_office_roles_are_the_campus_and_the_lisp_border_roles(self) -> None:
        assert OFFICE_LISP_ROLES == CAMPUS_FABRIC_ROLES | LISP_BORDER_ROLES == {"access-switch", "distribution-switch"}

    def test_the_office_roles_stay_out_of_the_dc_only_sets(self) -> None:
        """DC_VTEP_ROLES / BORDER_ROLES drive the DC SVI refusals, the VXLAN rendering and the group policy."""
        for role in OFFICE_LISP_ROLES:
            assert role not in DC_VTEP_ROLES
            assert role not in BORDER_ROLES
            assert role not in VTEP_ROLES

    @pytest.mark.parametrize("role", sorted(OFFICE_LISP_ROLES))
    def test_the_overlay_model_terminates_an_overlay_on_the_office_roles(self, role: str) -> None:
        node = build_overlay_node(_device()["interfaces"], role, [_activation()], [], "00:1c:73:00:dc:01")

        assert node is not None
        assert [l2.instance_id for l2 in node.l2] == [10100]

    @pytest.mark.parametrize("role", ["spine", "l2-leaf", "firewall"])
    def test_the_overlay_model_still_terminates_nothing_on_other_roles(self, role: str) -> None:
        assert build_overlay_node(_device()["interfaces"], role, [_activation()], [], "00:1c:73:00:dc:01") is None

    @pytest.mark.parametrize("role", sorted(OFFICE_LISP_ROLES))
    def test_the_office_roles_get_the_cisco_xe_context(self, role: str) -> None:
        """The segment ACLs and VRF statics are rendered on them like on a leaf (they must not vanish)."""
        transform = BaseDeviceTransform.__new__(BaseDeviceTransform)
        transform.device_role = role
        config = {"vlans": [], "acls": [], "vrf_routes": [], "lisp": {"l3_instances": []}}

        assert set(transform._cisco_xe_context("cisco_xe", config)) == {"xe_acls", "xe_vrf_routes"}

    def test_the_other_roles_get_no_cisco_xe_context(self) -> None:
        transform = BaseDeviceTransform.__new__(BaseDeviceTransform)
        transform.device_role = "spine"

        assert transform._cisco_xe_context("cisco_xe", {}) == {}

    @pytest.mark.parametrize("role", ["leaf", "access-leaf", "tor", "border-leaf"])
    def test_a_dc_role_keeps_its_place_in_the_cisco_xe_set(self, role: str) -> None:
        assert role in BaseDeviceTransform._CISCO_XE_FABRIC_ROLES


# ---------------------------------------------------------------------------
# The renderer inputs: border role set, office reads
# ---------------------------------------------------------------------------


class TestBuildLispInputs:
    def test_a_distribution_switch_reads_the_office_as_the_site(self) -> None:
        deployment = {
            "name": _OFFICE,
            "lisp_map_servers": _MAP_SERVERS,
            "lisp_site_key": {"password": "k"},
            "segment_deployments": [
                {
                    "vni": 10100,
                    "segment": {
                        "gateway": {
                            "ip_prefix": {"prefix": "10.100.0.0/24", "ip_namespace": {"name": "VRF_A", "l3_vni": 50001}}
                        }
                    },
                }
            ],
        }

        inputs = build_lisp_inputs(deployment, "distribution-switch")

        assert inputs == LispInputs(
            map_servers=("10.250.5.1", "10.250.5.2"),
            site_name=_OFFICE,
            site_key="k",
            l3_records=(EidRecord(50001, "VRF_A", "10.100.0.0/24"),),
            l2_instance_ids=(10100,),
        )

    def test_an_access_switch_reads_the_map_servers_and_the_key_only(self) -> None:
        deployment = {"name": _OFFICE, "lisp_map_servers": _MAP_SERVERS, "lisp_site_key": {"password": "k"}}

        inputs = build_lisp_inputs(deployment, "access-switch")

        assert inputs.map_servers == ("10.250.5.1", "10.250.5.2")
        assert inputs.site_key == "k"
        assert inputs.site_name is None
        assert inputs.l3_records == () and inputs.l2_instance_ids == ()


class TestLispContext:
    def test_a_distribution_switch_is_a_border_with_the_site(self) -> None:
        config = get_lisp_config(_device(), "distribution-switch", [], _inputs())

        assert config["role"] == "border"
        assert config["site"] is not None and config["site"]["name"] == _OFFICE
        assert config["l2_instances"] == [] and config["l3_instances"] == []

    def test_an_access_switch_is_an_edge_without_a_site(self) -> None:
        config = get_lisp_config(_device(), "access-switch", [_activation()], _inputs())

        assert config["role"] == "edge"
        assert config["site"] is None
        assert config["map_servers"] == ["10.250.5.1", "10.250.5.2"]
        assert config["l3_instances"] == [{"instance_id": 50001, "vrf": "VRF_A", "prefixes": ["10.100.0.0/24"]}]
        assert config["l2_instances"] == [{"instance_id": 10100, "vlan_id": 100, "name": "seg-100"}]


# ---------------------------------------------------------------------------
# Rendered CLI
# ---------------------------------------------------------------------------


class TestRenderedAccessSwitch:
    def test_it_is_the_lisp_edge_of_the_office_without_evpn_or_bgp(self) -> None:
        text = _run_transform(AccessSwitch, _raw(ACCESS_INPUT))
        lisp = _lisp_lines(text)

        assert text.splitlines()[2] == "! Cisco IOS-XE (Catalyst 9000) LISP access-switch"
        assert "  map-server" not in lisp and "  map-resolver" not in lisp
        assert not any(line.startswith(" site ") for line in lisp)
        assert not any(re.search(pattern, text, re.MULTILINE) for pattern in ("l2vpn evpn", r"\bnve1\b", "^router bgp"))
        assert "router ospf 1" in text

    def test_it_registers_with_every_distribution_switch_sorted_by_name(self) -> None:
        text = _run_transform(AccessSwitch, _raw(ACCESS_INPUT))

        assert "  itr map-resolver 10.250.5.1\n  itr map-resolver 10.250.5.2\n" in text
        assert text.count("  etr map-server 10.250.5.1 key 0ff1c3-k3y") == 2  # service ipv4 and service ethernet
        assert text.count("  etr map-server 10.250.5.2 proxy-reply") == 2

    def test_its_tenant_svis_sit_in_a_vrf_with_the_mobility_binding(self) -> None:
        text = _run_transform(AccessSwitch, _raw(ACCESS_INPUT))

        for vlan in (100, 200):
            svi = text.split(f"interface Vlan{vlan}\n")[1].split("\n!\n")[0]
            assert " vrf forwarding VRF_A" in svi
            assert " lisp mobility VRF_A-IPV4" in svi

    def test_the_mobility_bindings_follow_the_section_marker(self) -> None:
        lines = _run_transform(AccessSwitch, _raw(ACCESS_INPUT)).splitlines()
        section = lines.index("! Gateway SVIs")
        first = next(i for i in range(section, len(lines)) if lines[i].strip().startswith("lisp mobility"))

        assert any(MARKER in line and "lisp mobility" in line for line in lines[section:first])
        assert sum(line.strip().startswith("lisp mobility") for line in lines) == 2

    def test_the_golden_input_has_no_overlay_flag(self) -> None:
        """An office is LISP by role: the input carries no flag and no EVPN route-target ASN."""
        deployment = _deployment(_raw(ACCESS_INPUT))

        assert "overlay_control_plane" not in deployment and "evpn_rt_as" not in deployment

    def test_a_flag_on_the_deployment_does_not_change_the_render(self) -> None:
        plain = _run_transform(AccessSwitch, _raw(ACCESS_INPUT))
        data = _raw(ACCESS_INPUT)
        _deployment(data)["overlay_control_plane"] = {"value": "bgp_evpn"}

        assert _run_transform(AccessSwitch, data) == plain


class TestRenderedDistributionSwitch:
    def test_it_is_the_co_located_map_server_and_resolver(self) -> None:
        text = _run_transform(DistributionSwitch, _raw(DISTRIBUTION_INPUT))
        lisp = _lisp_lines(text)

        assert text.splitlines()[2] == "! Cisco IOS-XE (Catalyst 9000) LISP distribution-switch"
        assert lisp.count("  map-server") == 2  # service ipv4 and service ethernet
        assert lisp.count("  map-resolver") == 2
        assert "  itr map-resolver 10.250.5.1" in lisp and "  itr map-resolver 10.250.5.2" in lisp

    def test_the_site_is_the_office_with_its_key_and_the_eid_records(self) -> None:
        text = _run_transform(DistributionSwitch, _raw(DISTRIBUTION_INPUT))

        assert (
            f" site {_OFFICE}\n  authentication-key 0ff1c3-k3y\n"
            "  eid-record instance-id 50001 10.100.0.0/24 accept-more-specifics\n"
            "  eid-record instance-id 10100 any-mac\n  exit-site\n"
        ) in text

    @pytest.mark.parametrize(("cls", "directory"), OFFICE_SWITCHES)
    def test_every_lisp_stanza_sits_under_an_unverified_marker(self, cls: type, directory: str) -> None:
        lines = _run_transform(cls, _raw(directory)).splitlines()
        stanza = re.compile(r"^\s*(locator-table|service (ipv4|ethernet)|instance-id \d+|site \S+)")
        checked = 0
        for index, line in enumerate(lines):
            if line == "router lisp" or stanza.match(line) and not line.startswith("  "):
                assert MARKER in lines[index - 1], line
                checked += 1
        assert checked >= 4


class TestDataCenterIsUnchanged:
    def test_a_dc_border_leaf_still_needs_the_fabric_flag(self) -> None:
        """Without overlay_control_plane = lisp a border-leaf takes the EVPN path (here refused: the input has no
        BGP); an office role never does."""
        data = _raw(DC_BORDER_INPUT)
        del _deployment(data)["overlay_control_plane"]

        with pytest.raises(ValueError, match="VXLAN without an EVPN BGP process"):
            _run_transform(BorderLeaf, data)

    def test_a_dc_border_leaf_still_uses_its_own_name_as_the_site(self) -> None:
        text = _run_transform(BorderLeaf, _raw(DC_BORDER_INPUT))

        assert " site DC-1\n" in text


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


class TestRefusals:
    @pytest.mark.parametrize(("cls", "directory"), OFFICE_SWITCHES)
    def test_an_office_without_a_distribution_switch_is_refused(self, cls: type, directory: str) -> None:
        """No map-server: the office has no distribution-switch yet."""
        data = _raw(directory)
        _deployment(data)["lisp_map_servers"] = {"edges": []}

        with pytest.raises(ValueError, match=r"the fabric has no map-server \(border-leaf or distribution-switch\)"):
            _run_transform(cls, data)

    @pytest.mark.parametrize(("cls", "directory"), OFFICE_SWITCHES)
    def test_an_office_without_a_site_key_is_refused(self, cls: type, directory: str) -> None:
        data = _raw(directory)
        _deployment(data)["lisp_site_key"] = {"node": None}

        with pytest.raises(ValueError, match="the fabric has no LISP site key"):
            _run_transform(cls, data)

    def test_a_distribution_switch_without_an_office_name_is_refused(self) -> None:
        data = _raw(DISTRIBUTION_INPUT)
        _deployment(data)["name"] = {"value": None}

        with pytest.raises(ValueError, match="the fabric has no name for the LISP site"):
            _run_transform(DistributionSwitch, data)

    def test_a_map_server_without_a_loopback_is_refused(self) -> None:
        data = _raw(ACCESS_INPUT)
        _deployment(data)["lisp_map_servers"]["edges"][0]["node"]["interfaces"] = {"edges": []}

        with pytest.raises(ValueError, match="LISP map-server ds-c005-p02 has no loopback address"):
            _run_transform(AccessSwitch, data)

    @pytest.mark.parametrize("platform", ["cisco_nxos", "arista_eos", "nokia_sros"])
    @pytest.mark.parametrize(
        ("cls", "directory", "role"),
        [
            (AccessSwitch, ACCESS_INPUT, "access-switch"),
            (DistributionSwitch, DISTRIBUTION_INPUT, "distribution-switch"),
        ],
    )
    def test_another_platform_is_refused_before_the_missing_template_can_fail(
        self, platform: str, cls: type, directory: str, role: str
    ) -> None:
        data = _raw(directory)
        _node(data)["platform"]["node"]["netmiko_device_type"]["value"] = platform

        with pytest.raises(ValueError) as error:
            _run_transform(cls, data)

        assert str(error.value) == f"{platform} {role} cannot render the LISP overlay; refusing incomplete config"

    def test_pbr_is_still_refused_on_an_office_switch(self) -> None:
        with patch("transforms.common._combine_leaf_pbr_rules", return_value=[{"vlan_id": 100}]):
            with pytest.raises(ValueError, match=r"cisco_xe access-switch cannot render: leaf_pbr_rules"):
                _run_transform(AccessSwitch, _raw(ACCESS_INPUT))


class TestSegmentsOutsideAVrf:
    """A gateway SVI outside a VRF with an L3 instance-id would land in the global table next to the EID tables."""

    def test_a_vlan_only_segment_in_the_default_namespace_is_refused(self) -> None:
        data = _raw(ACCESS_INPUT)
        for namespace in _seg200_namespaces(data):
            namespace["name"]["value"] = "default"
            namespace["l3_vni"]["value"] = None

        with pytest.raises(ValueError, match=r"the gateway of VLAN 200 is in no VRF with an L3 instance-id"):
            _run_transform(AccessSwitch, data)

    def test_a_segment_in_a_vrf_without_an_l3_vni_is_refused(self) -> None:
        data = _raw(ACCESS_INPUT)
        for namespace in _seg200_namespaces(data):
            namespace["l3_vni"]["value"] = None

        with pytest.raises(ValueError, match=r"the gateway of VLAN 200 is in no VRF with an L3 instance-id"):
            _run_transform(AccessSwitch, data)

    def test_a_vni_segment_in_the_default_namespace_is_refused(self) -> None:
        activation = _activation(namespace={"name": "default"})

        with pytest.raises(ValueError, match=r"the gateway of VLAN 100 is in no VRF with an L3 instance-id"):
            get_lisp_config(_device(), "access-switch", [activation], _inputs())

    @pytest.mark.parametrize("vni", [10100, None])
    def test_an_ipv6_gateway_is_refused_with_or_without_a_vni(self, vni: int | None) -> None:
        activation = _activation(vni=vni, gateway="2001:db8::1/64")

        with pytest.raises(ValueError, match=r"VLAN 100 has an IPv6 gateway \(service ipv6 is not rendered\)"):
            get_lisp_config(_device(), "access-switch", [activation], _inputs())

    def test_a_vlan_only_gateway_in_a_vrf_is_accepted(self) -> None:
        config = get_lisp_config(_device(), "access-switch", [_activation(vni=None)], _inputs())

        assert config["l2_instances"] == []
        assert [l3["vrf"] for l3 in config["l3_instances"]] == ["VRF_A"]

    def test_a_gateway_less_segment_needs_no_vrf(self) -> None:
        activation = _activation(vni=10200)
        activation["segment"]["gateway"] = None

        config = get_lisp_config(_device(), "access-switch", [activation], _inputs())

        assert config["l2_instances"][0]["instance_id"] == 10200 and config["l3_instances"] == []

    def test_the_fix_applies_to_a_data_center_too(self) -> None:
        """The DC border-leaf golden used to render seg-200 in the global table; it is refused now, so the golden
        input puts seg-200 in VRF_A."""
        data = _raw(DC_BORDER_INPUT)
        assert {ns["name"]["value"] for ns in _seg200_namespaces(data)} == {"VRF_A"}
        for namespace in _seg200_namespaces(data):
            namespace["name"]["value"] = "default"
            namespace["l3_vni"]["value"] = None

        with pytest.raises(
            ValueError, match=r"cisco_xe border-leaf cannot render the LISP overlay: the gateway of VLAN 200"
        ):
            _run_transform(BorderLeaf, data)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def registry() -> dict[str, Any]:
    return yaml.safe_load((ROOT / ".infrahub.yml").read_text())


@pytest.fixture(scope="module")
def group_names() -> set[str]:
    names: set[str] = set()
    for document in yaml.safe_load_all((ROOT / "data" / "bootstrap" / "00_groups.yml").read_text()):
        names |= {entry["name"] for entry in (document or {}).get("spec", {}).get("data") or []}
    return names


OFFICE_TRANSFORMS = [
    ("access-switch", AccessSwitch, "access_switch", "access_switch_config", "access_switches"),
    (
        "distribution-switch",
        DistributionSwitch,
        "distribution_switch",
        "distribution_switch_config",
        "distribution_switches",
    ),
]


@pytest.mark.parametrize(("role", "cls", "transform", "query", "subdir"), OFFICE_TRANSFORMS)
class TestRegistration:
    def test_the_transform_is_a_device_transform_of_its_role(
        self, role: str, cls: type[BaseDeviceTransform], transform: str, query: str, subdir: str
    ) -> None:
        assert (cls.device_role, cls.query, cls.template_subdir) == (role, query, subdir)

    def test_the_transform_is_registered(
        self, registry: dict, role: str, cls: type, transform: str, query: str, subdir: str
    ) -> None:
        entry = next(t for t in registry["python_transforms"] if t["name"] == transform)

        assert entry["class_name"] == cls.__name__
        assert entry["file_path"] == f"transforms/config/{transform}.py"
        assert (ROOT / entry["file_path"]).is_file()

    def test_the_query_is_registered_and_exists(
        self, registry: dict, role: str, cls: type, transform: str, query: str, subdir: str
    ) -> None:
        entry = next(q for q in registry["queries"] if q["name"] == query)

        assert entry["file_path"] == f"queries/config/{transform}.gql"
        text = (ROOT / entry["file_path"]).read_text()
        assert f"query {query}(" in text
        assert "$device: String!" in text

    def test_the_artifact_targets_the_role_group(
        self, registry: dict, group_names: set[str], role: str, cls: type, transform: str, query: str, subdir: str
    ) -> None:
        artifact = next(a for a in registry["artifact_definitions"] if a["transformation"] == transform)

        assert artifact["artifact_name"] == role
        assert artifact["parameters"] == {"device": "name__value"}
        assert artifact["targets"] == ROLE_GROUP_NAMES[role]
        assert artifact["targets"] in group_names

    def test_the_cisco_xe_template_reuses_the_leaf_template(
        self, role: str, cls: type, transform: str, query: str, subdir: str
    ) -> None:
        template = (ROOT / "templates" / "configs" / subdir / "cisco_xe.j2").read_text()

        assert "{%- include 'leafs/cisco_xe.j2' -%}" in template
        assert not [line for line in template.splitlines() if line and not line.startswith(("{", "    "))]


# ---------------------------------------------------------------------------
# Queries (offline: syntax, spreads and the office schema fields; a live stack confirms the rest)
# ---------------------------------------------------------------------------


def _fragment_definitions() -> dict[str, FragmentDefinitionNode]:
    defined: dict[str, FragmentDefinitionNode] = {}
    for path in sorted((ROOT / "queries" / "fragments").glob("*.gql")):
        for item in parse(path.read_text()).definitions:
            if isinstance(item, FragmentDefinitionNode):
                defined[item.name.value] = item
    return defined


def _spreads(node: Any) -> set[str]:
    if isinstance(node, FragmentSpreadNode):
        return {node.name.value}
    selection_set = getattr(node, "selection_set", None)
    found: set[str] = set()
    for child in selection_set.selections if selection_set else ():
        found |= _spreads(child)
    return found


class TestQueries:
    @pytest.mark.parametrize("query", ["access_switch", "distribution_switch"])
    def test_every_spread_resolves_against_the_fragments_directory(self, query: str) -> None:
        defined = _fragment_definitions()
        pending = _spreads(parse((ROOT / "queries" / "config" / f"{query}.gql").read_text()).definitions[0])
        seen: set[str] = set()
        while pending:
            name = pending.pop()
            assert name in defined, f"{query} spreads {name}, defined in no queries/fragments/ file"
            seen.add(name)
            pending |= _spreads(defined[name]) - seen

    def test_the_lisp_fabric_check_query_parses_and_selects_the_office_inputs(self) -> None:
        text = (ROOT / "queries" / "validation" / "lisp_fabric.gql").read_text()
        parse(text)
        office = text.split("TopologyCustomerOffice {")[1]

        for selection in (
            "fabric_templates",
            "locations",
            "lisp_site_key",
            "devices",
            "capabilities",
            "segment_deployments",
            "l3_vni",
        ):
            assert selection in office

    def test_the_office_schema_has_every_field_the_check_and_the_config_queries_read(self) -> None:
        """name, the LISP key and the segment activations come from TopologySegmentHosting; the buildings, the
        opt-in and the devices from the footprint itself."""
        names = relationship_names("TopologyCustomerOffice")
        assert {"fabric_templates", "locations", "lisp_site_key", "devices", "segment_deployments"} <= names
        assert {"name", "evpn_anycast_gateway_mac", "overlay_control_plane"} <= attribute_names(
            "TopologyCustomerOffice"
        )
