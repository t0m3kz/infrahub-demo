"""Offline guards for the Arista MSS-G demo scope (data/demos/32_arista_mssg) and its integration test.

tests/integration/test_71_arista_mssg.py can only run against a live stack, so everything it assumes about the data
and about the render text is checked here first, without one:

- the scope's references resolve (templates, design elements, software image, racks, servers, segments, policies);
- every VTEP the DC declares passes the reference-fabric gate (utils.group_policy.fabric_reason) and the port plans
  satisfy the cabling rules the generators enforce (one uplink per spine, speed matches, mlag-peer ports);
- the real Leaf transform, fed the scope's hand-written policies (through the smoke fixture of a reference-fabric EOS
  leaf), renders what test_71's ``_mssg_errors`` expects, staged and enforced, and ``_mssg_errors`` catches the
  mistakes it exists for.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from generators.pod_config import POD_LAYOUTS
from tests.integration import test_71_arista_mssg as scenario
from transforms.config.leaf import Leaf
from utils.group_policy import fabric_reason, profiles_by_platform, release_key

SCOPE = "data/demos/32_arista_mssg"
SMOKE_FIXTURE = "tests/smoke/configs/leaf_arista_eos_gpo_reference_staged/input.json"
PROFILE_ROW = "reference-arista-eos"
VTEP_ROLES = ("border-leaf", "leaf", "access-leaf")


def _documents(path: Path) -> list[dict[str, Any]]:
    return [document for document in yaml.safe_load_all(path.read_text()) if document]


def _spec_data(path: Path) -> list[dict[str, Any]]:
    """The ``spec.data`` entries of every document in an object file."""
    return [entry for document in _documents(path) for entry in document["spec"].get("data") or []]


def _scope_data(root_dir: Path, relative: str) -> list[dict[str, Any]]:
    return _spec_data(root_dir / SCOPE / relative)


@pytest.fixture(scope="module")
def templates(root_dir: Path) -> dict[str, dict[str, Any]]:
    """Template name -> entry, the scope's device templates plus the bootstrap ones."""
    found: dict[str, dict[str, Any]] = {}
    for path in [*sorted((root_dir / "data/bootstrap").glob("10_physical_devices_templates_*.y*ml"))]:
        found.update({entry["template_name"]: entry for entry in _spec_data(path)})
    scope = {entry["template_name"]: entry for entry in _scope_data(root_dir, "00_design/01_device_templates.yml")}
    assert scope, "the scope declares no device template"
    return {**found, **scope}


@pytest.fixture(scope="module")
def topology(root_dir: Path) -> dict[str, Any]:
    """DC14, the one TopologyDataCenter of the scope."""
    (dc,) = _scope_data(root_dir, "01_fabric/02_topology.yml")
    return dc


def _interfaces(template: dict[str, Any]) -> list[dict[str, Any]]:
    return template["interfaces"]["data"]


def _count(template: dict[str, Any], role: str) -> int:
    """Interfaces of ``role`` a template creates (``[a-b]`` ranges expanded)."""
    total = 0
    for interface in _interfaces(template):
        if interface["role"] != role:
            continue
        name = interface["name"]
        if "[" in name:
            low, high = name[name.index("[") + 1 : name.index("]")].split("-")
            total += int(high) - int(low) + 1
        else:
            total += 1
    return total


def _types(template: dict[str, Any], role: str) -> set[str]:
    return {interface["interface_type"] for interface in _interfaces(template) if interface["role"] == role}


def _racks(topology: dict[str, Any]) -> Iterator[dict[str, Any]]:
    for pod in topology["children"]["data"]:
        yield from (pod.get("racks") or {}).get("data") or []


def _vtep_templates(topology: dict[str, Any]) -> dict[str, str]:
    """Role -> template name of every VTEP tier of DC14."""
    found: dict[str, str] = {}
    for _quantity, role, template in topology["fabric_templates"]:
        if role in VTEP_ROLES:
            found[role] = template
    for rack in _racks(topology):
        for _quantity, role, template in rack.get("fabric_templates") or []:
            if role in VTEP_ROLES:
                found[role] = template
    return found


# ---------------------------------------------------------------------------
# The data
# ---------------------------------------------------------------------------


class TestReferenceGate:
    """One VTEP outside the profile row silently removes the MSS-G block from the whole DC."""

    def test_every_vtep_passes_the_reference_fabric_gate(
        self, root_dir: Path, templates: dict[str, dict[str, Any]], topology: dict[str, Any]
    ) -> None:
        """Each VTEP tier's platform, device type and release are accepted by the reference-arista-eos row."""
        row = next(
            entry
            for entry in _spec_data(root_dir / "data/bootstrap/19b_group_policy_profile.yml")
            if entry["name"] == PROFILE_ROW
        )
        profiles, problems = profiles_by_platform(
            [
                {
                    "name": row["name"],
                    "group_policy_kind": row["group_policy_kind"],
                    "min_release": row["min_release"],
                    "platform": {"name": row["platform"]},
                    "device_types": [{"name": name} for name in row["device_types"]],
                }
            ]
        )
        assert not problems and profiles, problems

        images = {
            entry["name"]: entry["version"]
            for entry in _scope_data(root_dir, "00_design/00_software_image.yml")
            + _spec_data(root_dir / "data/bootstrap/05b_software_images.yml")
        }
        tiers = _vtep_templates(topology)
        assert sorted(tiers) == sorted(VTEP_ROLES), f"VTEP tiers declared: {tiers}"
        for role, template_name in tiers.items():
            template = templates[template_name]
            device = {
                "name": template_name,
                "role": role,
                "platform": {"name": template["platform"]},
                "device_type": {"name": template["device_type"]},
                "software_image": {"version": images[template["software_image"]]},
            }
            assert fabric_reason(device, profiles) is None, (
                f"{role} template {template_name}: {fabric_reason(device, profiles)}"
            )

    def test_the_older_image_the_negative_gate_uses_is_below_the_floor(self, root_dir: Path) -> None:
        """arista_eos-4.32.1F exists in bootstrap and sorts below the profile's min_release."""
        images = {
            entry["name"]: entry["version"] for entry in _spec_data(root_dir / "data/bootstrap/05b_software_images.yml")
        }
        restored = {
            entry["name"]: entry["version"] for entry in _scope_data(root_dir, "00_design/00_software_image.yml")
        }
        assert release_key(images[scenario.OLDER_IMAGE]) < release_key(restored[scenario.RESTORED_IMAGE])


class TestReferences:
    """Everything the object files name exists, by the HFID the loader would resolve."""

    def test_templates_elements_and_images_resolve(
        self, root_dir: Path, templates: dict[str, dict[str, Any]], topology: dict[str, Any]
    ) -> None:
        elements = {
            (str(entry["quantity"]), entry["role"], entry["template"])
            for entry in _scope_data(root_dir, "00_design/02_design_elements.yml")
            + _spec_data(root_dir / "data/bootstrap/14_design_elements.yml")
        }
        images = {
            entry["name"]
            for entry in _scope_data(root_dir, "00_design/00_software_image.yml")
            + _spec_data(root_dir / "data/bootstrap/05b_software_images.yml")
        }
        device_types = {entry["name"] for entry in _spec_data(root_dir / "data/bootstrap/06_device_types.yml")}
        declared = [tuple(item) for item in topology["fabric_templates"]]
        for pod in topology["children"]["data"]:
            declared += [tuple(item) for item in pod["fabric_templates"]]
        declared += [tuple(item) for rack in _racks(topology) for item in rack.get("fabric_templates") or []]
        assert declared
        for item in declared:
            assert item in elements, f"fabric_templates entry {item} is no TopologyElement"
            assert item[2] in templates, f"template {item[2]} is not declared"
        for name, template in templates.items():
            if name in {entry for _q, _r, entry in declared}:
                assert template["software_image"] in images, f"{name}: image {template['software_image']} unknown"
                assert template["device_type"] in device_types, f"{name}: device type {template['device_type']} unknown"

    def test_racks_servers_segments_policies_and_application_agree(
        self, root_dir: Path, topology: dict[str, Any]
    ) -> None:
        racks = {rack["shortname"] for rack in _racks(topology)}
        servers = {server["name"]: server for server in _scope_data(root_dir, "02_compute/01_servers.yml")}
        assert set(servers) == {scenario.SERVER_ROW1, scenario.SERVER_ROW2}
        assert {server["rack"] for server in servers.values()} <= racks
        # Row 1's server is in a row whose network rack has an access-leaf, row 2's in one with leafs only.
        by_row = {rack["row_index"]: rack for rack in _racks(topology) if rack.get("rack_type") == "network"}
        assert {role for _q, role, _t in by_row[1]["fabric_templates"]} == {"leaf", "access-leaf"}
        assert {role for _q, role, _t in by_row[2]["fabric_templates"]} == {"leaf"}
        row_of = {rack["shortname"]: rack["row_index"] for rack in _racks(topology)}
        assert row_of[servers[scenario.SERVER_ROW1]["rack"]] == 1
        assert row_of[servers[scenario.SERVER_ROW2]["rack"]] == 2

        (footprint,) = _scope_data(root_dir, "03_footprint/01_deployments.yml")
        footprint_name = f"{footprint['owner']}-{footprint['environment'].upper()}-{footprint['parent']}"
        segment_entries = _scope_data(root_dir, "04_segments/01_segments.yml")
        names = {f"{s['owner'].lower()}-{s['customer_name']}-{s['environment']}" for s in segment_entries}
        assert names == set(scenario.SEGMENTS)
        assert all(s["customer_deployments"] == [footprint_name] for s in segment_entries)
        assert footprint["parent"] == scenario.MSSG_DC == topology["name"]

        policies = _scope_data(root_dir, "05_policies/01_security_policies.yml")
        assert {p["segment"] for p in policies} == names
        found: dict[str, set[str]] = {}
        for policy in policies:
            found[policy["name"]] = {rule["name"] for rule in policy["rules"]["data"]}
            for rule in policy["rules"]["data"]:
                assert rule["source_segment"] == policy["segment"], f"rule {rule['name']} lives in the wrong policy"
                assert rule["destination_segment"] in names and rule["index"] < 100
        assert found == scenario.EXPECTED_POLICY_RULES

        (application,) = _scope_data(root_dir, "06_application/01_application.yml")
        components = application["children"]["data"]
        assert {c["network_segment"] for c in components} == names
        assert {instance for c in components for instance in c["instances"]} == set(servers)
        assert all(server["owner"] == application["owner"] == scenario.MSSG_ORG for server in servers.values())

    def test_two_pods_hold_the_two_border_leafs(self, topology: dict[str, Any]) -> None:
        """An S_MIDDLE pod takes one border-leaf (max_border_leafs_per_pod), so two need two pods."""
        wanted = next(int(q) for q, role, _t in topology["fabric_templates"] if role == "border-leaf")
        capacity = sum(POD_LAYOUTS[pod["layout"]]["max_border_leafs_per_pod"] for pod in topology["children"]["data"])
        assert capacity >= wanted, f"{wanted} border-leafs but the pods offer room for {capacity}"
        pod_one = topology["children"]["data"][0]
        assert pod_one["mlag_create"] == "back-to-back"
        assert "group_policy_enforce" not in topology, "the scope must load staged"
        assert topology["underlay_protocol"] == "ipv4"


class TestPortPlans:
    """What the cabling generators enforce, read off the templates (tests/unit/test_uplink_capacity.py's rules)."""

    def test_leaf_and_access_cable_up_at_matching_speeds(
        self, templates: dict[str, dict[str, Any]], topology: dict[str, Any]
    ) -> None:
        tiers = _vtep_templates(topology)
        leaf, access, border = (templates[tiers[role]] for role in ("leaf", "access-leaf", "border-leaf"))
        spine = templates[topology["children"]["data"][0]["fabric_templates"][0][2]]
        spines = int(topology["children"]["data"][0]["fabric_templates"][0][0])
        server = {"25gbase-x-sfp28"}

        assert _count(leaf, "uplink") >= spines and _count(border, "uplink") >= spines
        assert _types(leaf, "uplink") == _types(border, "uplink") == _types(spine, "downlink")
        # Two access-leafs per rack, each cabled to both leafs of the pair: a leaf needs one downlink for each.
        assert _count(leaf, "downlink") >= 2 and _count(access, "uplink") >= 2
        assert _types(access, "uplink") == _types(leaf, "downlink")
        # Back-to-back MLAG needs mlag-peer ports on every tier the pod pairs (generators/devices.py).
        assert _count(leaf, "mlag-peer") >= 2 and _count(access, "mlag-peer") >= 2
        # Servers dual-home on 25G customer ports.
        assert _types(leaf, "customer") == _types(access, "customer") == server
        assert _count(access, "customer") >= 2 and _count(leaf, "customer") >= 2
        # A firewall pair cables to the border-leaf's firewall ports.
        assert _count(border, "firewall") >= 2

    def test_no_template_names_one_port_twice(
        self, templates: dict[str, dict[str, Any]], topology: dict[str, Any]
    ) -> None:
        for name in set(_vtep_templates(topology).values()):
            names = [interface["name"] for interface in _interfaces(templates[name])]
            assert len(names) == len(set(names)), f"{name}: duplicate interface entry"
            template_names = [interface["template_name"] for interface in _interfaces(templates[name])]
            assert len(template_names) == len(set(template_names)), f"{name}: duplicate template_name"


# ---------------------------------------------------------------------------
# The render
# ---------------------------------------------------------------------------

LIVE_PREFIXES = {
    scenario.SEG_WEB: "10.10.1.0/24",
    scenario.SEG_APP: "10.10.2.0/24",
    scenario.SEG_DB: "10.10.3.0/24",
}
LIVE = {name: {"prefix": prefix, "vrf": "PROD"} for name, prefix in LIVE_PREFIXES.items()}
# The smoke fixture's NONPROD web/app/db segments and DC10 become this scope's tenant (same shape of flows).
RENAMES = (
    ("web-np", scenario.SEG_WEB),
    ("app-np", scenario.SEG_APP),
    ("db-np", scenario.SEG_DB),
    ("NONPROD-ZONE", "PROD-ZONE"),
    ("NONPROD", "PROD"),
    ("non_prod", "prod"),
    ("C010", scenario.MSSG_ORG),
    ("DC10", scenario.MSSG_DC),
)
DROPPED_SEGMENTS = {"seg-pay-p", "seg-portal-p", "seg-internet"}


def _tenant_input(root_dir: Path, *, enforce: bool = False) -> dict[str, Any]:
    """The smoke fixture of a reference-fabric EOS leaf, re-pointed at this scope's segments and hand-written rules."""
    text = (root_dir / SMOKE_FIXTURE).read_text()
    for old, new in RENAMES:
        text = text.replace(old, new)
    data = json.loads(text)
    segments = data["ManagedNetworkSegment"]["edges"] = [
        edge for edge in data["ManagedNetworkSegment"]["edges"] if edge["node"]["id"] not in DROPPED_SEGMENTS
    ]
    data["SecurityTag"]["edges"] = []
    for interface in data["DcimDevice"]["edges"][0]["node"]["interfaces"]["edges"]:
        capabilities = interface["node"]["interface_capabilities"]
        capabilities["edges"] = [c for c in capabilities["edges"] if c["node"]["id"] not in DROPPED_SEGMENTS]

    policies = {p["segment"]: p for p in _spec_data(root_dir / SCOPE / "05_policies/01_security_policies.yml")}
    for edge in segments:
        node = edge["node"]
        node["owner"] = {"node": {"org_id": {"value": scenario.MSSG_ORG}}}
        node["stretch_scope"] = {"value": "local"}
        node["security_policy"]["node"]["rules"]["edges"] = [
            {
                "node": {
                    "id": f"rule-{rule['name']}",
                    "index": {"value": rule["index"]},
                    "name": {"value": rule["name"]},
                    "action": {"value": rule["action"]},
                    "protocol": {"value": rule["protocol"]},
                    "port_start": {"value": rule.get("port_start")},
                    "port_end": {"value": rule.get("port_end")},
                    "disabled": {"value": False},
                    "apply_on_switch": {"value": False},
                    "source_segment": {"node": None},
                    "destination_segment": {"node": {"id": f"seg-{rule['destination_segment']}"}},
                    "source_security_tag": {"node": None},
                    "destination_security_tag": {"node": None},
                }
            }
            for rule in policies[node["name"]["value"]]["rules"]["data"]
        ]
    for edge in data["TopologyDataCenter"]["edges"]:
        if edge["node"]["name"]["value"] == scenario.MSSG_DC:
            edge["node"]["group_policy_enforce"] = {"value": enforce}
    return data


def _render_leaf(root_dir: Path, data: dict[str, Any]) -> str:
    client = MagicMock()
    client.clone.return_value = client
    client.schema = MagicMock()
    client.schema.get = AsyncMock(return_value=MagicMock())
    client.execute_graphql = AsyncMock(side_effect=Exception("no server"))
    transform = Leaf(client=client, infrahub_node=MagicMock(), root_directory=str(root_dir))
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(transform.transform(data))
    finally:
        loop.close()


@pytest.fixture(scope="module")
def staged(root_dir: Path) -> str:
    return _render_leaf(root_dir, _tenant_input(root_dir))


@pytest.fixture(scope="module")
def enforced(root_dir: Path) -> str:
    return _render_leaf(root_dir, _tenant_input(root_dir, enforce=True))


class TestRenderExpectations:
    """test_71's assertions hold on a real render of this scope's policies, so a live failure is the stack's, not theirs."""

    def test_staged_render_satisfies_the_integration_assertions(self, staged: str) -> None:
        assert scenario._mssg_errors("leaf", staged, LIVE, enforced=False) == []

    def test_enforced_render_satisfies_the_integration_assertions(self, enforced: str) -> None:
        assert scenario._mssg_errors("leaf", enforced, LIVE, enforced=True) == []

    def test_enforcing_changes_the_staged_comment_and_the_switch_only(self, staged: str, enforced: str) -> None:
        removed, added = scenario._changed_lines(staged, enforced)
        assert len(removed) == 2 and removed[0].startswith(scenario.STAGED_COMMENT_PREFIX)
        assert removed[1] == "   shutdown" and added == ["   no shutdown"]

    def test_the_section_lists_the_contract_of_the_data(self, staged: str) -> None:
        parsed = scenario._parse_section(scenario._section(staged))
        tenant = parsed["vrfs"]["PROD"]
        assert sorted(tenant) == sorted(scenario.SEGMENTS)
        assert tenant[scenario.SEG_APP]["froms"][scenario.SEG_DB] == scenario.FORWARD_ALL
        assert tenant[scenario.SEG_DB]["froms"][scenario.SEG_APP].startswith("MSSG-POL-")
        assert tenant[scenario.SEG_DB]["froms"][scenario.SEG_APP] != scenario.FORWARD_ALL
        assert all(segment["fallback"] for segment in tenant.values())
        assert parsed["shutdown"] == "shutdown"

    def test_a_device_that_hosts_no_segment_renders_no_section(self, root_dir: Path) -> None:
        data = _tenant_input(root_dir)
        for interface in data["DcimDevice"]["edges"][0]["node"]["interfaces"]["edges"]:
            interface["node"]["interface_capabilities"]["edges"] = []
        assert scenario._section(_render_leaf(root_dir, data)) == []

    def test_one_vtep_below_the_release_floor_removes_the_section(self, root_dir: Path) -> None:
        data = _tenant_input(root_dir)
        victims = [
            edge["node"]
            for dc in data["TopologyDataCenter"]["edges"]
            if dc["node"]["name"]["value"] == scenario.MSSG_DC
            for edge in dc["node"]["devices"]["edges"]
        ]
        assert victims, "the fixture's DC has no VTEP to age"
        victims[0]["software_image"] = {"node": {"version": {"value": "4.32.1F"}}}
        assert scenario._section(_render_leaf(root_dir, data)) == []


class TestRenderAssertionsCatchMistakes:
    """``_mssg_errors`` must fail on the faults it exists for (a live run cannot be used to find that out)."""

    def test_missing_section(self) -> None:
        assert any(
            "no 'router segment-security'" in e
            for e in scenario._mssg_errors("d", "hostname d\n", LIVE, enforced=False)
        )

    def test_wrong_live_prefix(self, staged: str) -> None:
        wrong = {**LIVE, scenario.SEG_WEB: {"prefix": "192.0.2.0/24", "vrf": "PROD"}}
        errors = scenario._mssg_errors("d", staged, wrong, enforced=False)
        assert any("192.0.2.0/24" in e for e in errors)

    def test_wrong_vrf(self, staged: str) -> None:
        other = {name: {**segment, "vrf": "NONPROD"} for name, segment in LIVE.items()}
        assert any("no 'vrf NONPROD'" in e for e in scenario._mssg_errors("d", staged, other, enforced=False))

    def test_forward_all_where_a_custom_policy_belongs(self, staged: str) -> None:
        parsed = scenario._parse_section(scenario._section(staged))
        policy = parsed["vrfs"]["PROD"][scenario.SEG_DB]["froms"][scenario.SEG_APP]
        broken = staged.replace(
            f"from {scenario.SEG_APP} policy {policy}", f"from {scenario.SEG_APP} policy policy-forward-all"
        )
        assert any("expected a custom MSSG-POL" in e for e in scenario._mssg_errors("d", broken, LIVE, enforced=False))

    def test_flush_left_field_set(self, staged: str) -> None:
        broken = staged.replace("   field-set l4-port", "field-set l4-port")
        errors = scenario._mssg_errors("d", broken, LIVE, enforced=False)
        assert any("flush-left" in e for e in errors)

    def test_missing_not_expressible_line(self, staged: str) -> None:
        broken = "\n".join(line for line in staged.splitlines() if "not expressible" not in line)
        assert any("not expressible" in e for e in scenario._mssg_errors("d", broken, LIVE, enforced=False))

    def test_default_drop_removal_rendered_as_config(self, staged: str) -> None:
        broken = staged.replace(
            "router segment-security\n", "router segment-security\n   no segment policy policy-drop-all default\n"
        )
        assert any("policy-drop-all default" in e for e in scenario._mssg_errors("d", broken, LIVE, enforced=False))

    def test_enforced_render_is_not_staged_and_vice_versa(self, staged: str, enforced: str) -> None:
        assert scenario._mssg_errors("d", enforced, LIVE, enforced=False)
        assert scenario._mssg_errors("d", staged, LIVE, enforced=True)

    def test_a_policy_naming_an_undefined_application(self, staged: str) -> None:
        parsed = scenario._parse_section(scenario._section(staged))
        name = next(iter(parsed["policies"]))
        broken = staged.replace(
            f"   policy {name}\n", f"   policy {name}\n      5 application MSSG-APP-ghost action forward stateless\n"
        )
        assert any("undefined application" in e for e in scenario._mssg_errors("d", broken, LIVE, enforced=False))

    def test_changed_lines_ignores_headers(self) -> None:
        assert scenario._changed_lines("a\nb\n", "a\nb\n") == ([], [])
        assert scenario._changed_lines("a\n--- b\n", "a\n+++ c\n") == (["--- b"], ["+++ c"])
