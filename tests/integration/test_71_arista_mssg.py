"""Integration test - the Arista EOS MSS-G demo fabric (data/demos/32_arista_mssg).

DC14 is an all-7280R3 fabric on EOS 4.36.2F, the one platform / device type / release the
``reference-arista-eos`` group-policy profile row accepts. The scope is not part of 30_all (it is a
separate scope with its own branch, never merged to main), so this module loads it on its own branch and
proves the whole path *generators -> group tags and contract rules -> transform -> rendered EOS text*:

1. The staged fabric load settles without a failed task; DC14 has its device roles and back-to-back MLAGs.
2. The tenant (servers, footprint, three VXLAN segments, hand-written policies, one application) loads in
   settled stages; every segment has a gateway prefix, a group tag and an active deployment; the servers are
   cabled to the tier the data intends.
3. CheckReferenceFabric says DC14 is a reference fabric (and reports no error first, so a silent no-render
   fails with the reason); CheckSegmentSecurity reports no error.
4. The segmentation matrix is a staged ``segment_security`` reference site whose prefixes are the live
   gateways and whose ``unsupported`` list holds the closed any-reply.
5. Leaf, access-leaf and border-leaf render ``router segment-security`` with the live prefixes and VRF, the
   expected ``from`` / ``fallback`` policies, the field-sets inside ``application traffic recognition``, the
   staged comment and the final ``shutdown``; a spine and a leaf that hosts nothing render none.
6. ``group_policy_enforce`` flips the render by exactly the staged comment and ``shutdown`` -> ``no shutdown``,
   and starts no generator.
7. One access-leaf on an older EOS removes the block from the whole DC; reverting brings it back.
8. Re-running add_vxlan_segment changes no tag, no id and no render.

What it does NOT prove: that EOS accepts or enforces any of the rendered lines. Custom policies, field-sets,
application traffic recognition and the fallback policy are documented by Arista's AVD only, and the 7280R3
port plans of the templates are approximate (see the scope README). The stored-artifact comparison against a
proposed change is left out: the proposed-change helpers only report artifact status, they do not fetch the
artifact content to compare with the in-process render.
"""

from __future__ import annotations

import difflib
import json
import logging
import re
from typing import Any

import pytest
from infrahub_sdk import InfrahubClient, InfrahubClientSync

from checks.reference_fabric import CheckReferenceFabric
from checks.segment_security import CheckSegmentSecurity
from transforms.config.access_leaf import AccessLeaf
from transforms.config.border_leaf import BorderLeaf
from transforms.config.leaf import Leaf
from transforms.config.spine import Spine
from transforms.topology.segmentation_matrix import SegmentationMatrix

from .conftest import TestInfrahubDockerWithClient
from .test_helpers import (
    compute_role_counts,
    diff_snapshots,
    fetch_dc_topology,
    fetch_endpoint_cabling,
    fetch_generator_runs,
    fetch_object_counts,
    render_device_config,
    snapshot_topology,
)
from .workflow_helpers import run_generator, verify_no_failed_tasks, wait_for_tasks_completion

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

SCENARIO_NAME = "Scenario 32_arista_mssg: Arista EOS Segment Security (MSS-G)"

MSSG_DATA = "data/demos/32_arista_mssg"
MSSG_BRANCH = "arista-mssg-scenario"
MSSG_DC = "DC14"

# (stage name, load paths under MSSG_DATA). Same order as the README. The stages are separate loads because
# each needs what the previous one's generators create: the servers need the switch ports, the footprint the
# DC, the segments the footprint and DC14's VNI pool, the policies the segments, the application the segments
# and the cabled servers.
MSSG_FABRIC_STAGES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("design", ("00_design",)),
    ("fabric", ("01_fabric",)),
)
MSSG_TENANT_STAGES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("compute", ("02_compute",)),
    ("footprint", ("03_footprint",)),
    ("segments", ("04_segments",)),
    ("policies", ("05_policies",)),
    ("application", ("06_application",)),
)
# The fabric stage generates a DC, two pods, four racks and their cabling: a few minutes at most.
MSSG_STAGE_MAX_ATTEMPTS = 180
MSSG_STAGE_POLL_INTERVAL = 10  # seconds
# Pods and racks are dispatched independently, so the queue can go quiet between waves.
MSSG_STAGE_STABLE_ZERO = 10

# Pod 1: 2 spines, row 1 = leaf pair + access-leaf pair, row 2 = leaf pair; pod 2: 2 spines; DC level: 2 border-leafs.
MSSG_EXPECTED_ROLE_COUNTS = {"border-leaf": 2, "spine": 4, "leaf": 4, "access-leaf": 2}
MSSG_VTEP_ROLES = ("border-leaf", "leaf", "access-leaf")
# Row 1 leaf pair, row 1 access-leaf pair, row 2 leaf pair, all back-to-back (the pod is mlag_create: back-to-back).
MSSG_EXPECTED_MLAG_PAIRS = 3

MSSG_ORG = "MSSG1"
SERVER_ROW1 = "dc14-pod1-server-1"  # row 1: its network rack has an access-leaf pair
SERVER_ROW2 = "dc14-pod1-server-2"  # row 2: its network rack has a leaf pair only
SEG_WEB = "mssg1-web-p"
SEG_APP = "mssg1-app-p"
SEG_DB = "mssg1-db-p"
SEGMENTS = (SEG_WEB, SEG_APP, SEG_DB)

# Policy name -> the hand-written rule names it holds (data/demos/32_arista_mssg/05_policies).
EXPECTED_POLICY_RULES: dict[str, set[str]] = {
    f"seg-{SEG_WEB}-egress": {"web-to-app-https", "web-to-app-service-range", "web-to-db-icmp"},
    f"seg-{SEG_APP}-egress": {"app-to-db-postgres"},
    f"seg-{SEG_DB}-egress": {"db-to-app-any"},
}

OLDER_IMAGE = "arista_eos-4.32.1F"
RESTORED_IMAGE = "arista_eos-4.36.2F"

SEGMENTS_QUERY = """
query {{
  ManagedVxlanSegment(name__values: {names}) {{
    edges {{
      node {{
        id
        name {{ value }}
        security_tag {{ node {{ id name {{ value }} group_id {{ value }} }} }}
        gateway {{
          node {{
            address {{ value }}
            ip_prefix {{ node {{ prefix {{ value }} ip_namespace {{ node {{ name {{ value }} }} }} }} }}
          }}
        }}
        segment_deployments(status__values: ["active", "provisioning"]) {{
          edges {{ node {{ id vni {{ value }} deployment {{ node {{ name {{ value }} }} }} }} }}
        }}
      }}
    }}
  }}
}}
"""

POLICIES_QUERY = """
query {{
  SecurityPolicy(name__values: {names}) {{
    edges {{
      node {{
        name {{ value }}
        default_action {{ value }}
        rules {{ edges {{ node {{ index {{ value }} name {{ value }} action {{ value }} }} }} }}
      }}
    }}
  }}
}}
"""

MLAG_QUERY = "query { ManagedMLAG { edges { node { name { value } virtual_peer_link { value } } } } }"

DEVICE_QUERY = """
query {{
  DcimPhysicalDevice(name__value: {name}) {{
    edges {{ node {{ id }} }}
  }}
}}
"""

# A render's section lines, matched whole (the section is flush-left, its stanzas indented by 3).
MSSG_SECTION = "router segment-security"
STAGED_COMMENT_PREFIX = f"! data center {MSSG_DC} stages group policy (group_policy_enforce is false)"

# What a clean MSS-G render of this scope's tenant has, per segment of the VRF: its own `from` (intra-segment
# permit) and these `from` lines under the DESTINATION segment. A value of None means "a custom MSSG-POL-*
# policy"; a string is the exact built-in policy.
FORWARD_ALL = "policy-forward-all"
EXPECTED_FROMS: dict[str, dict[str, str | None]] = {
    SEG_APP: {SEG_APP: FORWARD_ALL, SEG_DB: FORWARD_ALL, SEG_WEB: None},
    SEG_DB: {SEG_DB: FORWARD_ALL, SEG_APP: None, SEG_WEB: None},
    SEG_WEB: {SEG_WEB: FORWARD_ALL, SEG_APP: None, SEG_DB: None},
}


# ---------------------------------------------------------------------------
# Fetch helpers
# ---------------------------------------------------------------------------


async def _fetch_dc_id(client: InfrahubClient, branch: str) -> str:
    """The id of DC14; fails the test when it is not on the branch."""
    result = await client.execute_graphql(
        query=f"query {{ TopologyDataCenter(name__value: {json.dumps(MSSG_DC)}) {{ edges {{ node {{ id }} }} }} }}",
        branch_name=branch,
    )
    edges = result["TopologyDataCenter"]["edges"]
    assert edges, f"{MSSG_DC} not found on branch '{branch}'"
    return str(edges[0]["node"]["id"])


async def _fetch_segments(client: InfrahubClient, branch: str) -> dict[str, dict[str, Any]]:
    """Segment name -> id, gateway prefix / address, VRF (namespace), tag and active deployments, read from the stack."""
    result = await client.execute_graphql(
        query=SEGMENTS_QUERY.format(names=json.dumps(list(SEGMENTS))), branch_name=branch
    )
    segments: dict[str, dict[str, Any]] = {}
    for edge in result["ManagedVxlanSegment"]["edges"]:
        node = edge["node"]
        gateway = (node.get("gateway") or {}).get("node") or {}
        prefix = (gateway.get("ip_prefix") or {}).get("node") or {}
        tag = (node.get("security_tag") or {}).get("node") or {}
        segments[str(node["name"]["value"])] = {
            "id": str(node["id"]),
            "gateway": (gateway.get("address") or {}).get("value"),
            "prefix": (prefix.get("prefix") or {}).get("value"),
            "vrf": (((prefix.get("ip_namespace") or {}).get("node")) or {}).get("name", {}).get("value"),
            "tag_id": tag.get("id"),
            "tag_name": (tag.get("name") or {}).get("value"),
            "group_id": (tag.get("group_id") or {}).get("value"),
            "deployments": sorted(
                (
                    {
                        "vni": (dep["node"].get("vni") or {}).get("value"),
                        "dc": ((dep["node"].get("deployment") or {}).get("node") or {}).get("name", {}).get("value"),
                    }
                    for dep in node["segment_deployments"]["edges"]
                ),
                key=lambda dep: (str(dep["dc"]), int(dep["vni"] or 0)),
            ),
        }
    return segments


def _live_vrf(segments: dict[str, dict[str, Any]]) -> str:
    """The one VRF (IPAM namespace) the three segments sit in; fails the test when they differ."""
    vrfs = {str(segment["vrf"]) for segment in segments.values()}
    assert len(vrfs) == 1 and "None" not in vrfs, f"the segments are expected in one tenant VRF, got {sorted(vrfs)}"
    return vrfs.pop()


def _devices_of_role(devices: list[dict[str, Any]], role: str) -> list[str]:
    """Sorted names of the devices with ``role`` (fetch_dc_topology's device dicts)."""
    return sorted(str(device["name"]) for device in devices if device.get("role") == role)


async def _fetch_hosting_switches(client: InfrahubClient, branch: str) -> dict[str, set[str]]:
    """Server name -> the names of the switches its NICs are cabled to."""
    hosts = await fetch_endpoint_cabling(client=client, branch=branch)
    return {
        str(host["name"]): {str(link["peer_device"]) for link in host["links"]}
        for host in hosts
        if host["name"] in (SERVER_ROW1, SERVER_ROW2)
    }


async def _set_attributes(client: InfrahubClient, branch: str, kind: str, node_id: str, data: dict[str, Any]) -> None:
    """Upsert ``data`` onto an existing node by id (the generators' own partial-update pattern)."""
    node = await client.create(kind=kind, data={"id": node_id, **data}, branch=branch)
    await node.save(allow_upsert=True)


async def _set_enforce(client: InfrahubClient, branch: str, dc_id: str, enforce: bool) -> None:
    """Flip DC14's staging switch ``group_policy_enforce``."""
    await _set_attributes(client, branch, "TopologyDataCenter", dc_id, {"group_policy_enforce": enforce})


async def _set_software_image(client: InfrahubClient, branch: str, device: str, image: str) -> None:
    """Point ``device`` at the software image named ``image``."""
    result = await client.execute_graphql(query=DEVICE_QUERY.format(name=json.dumps(device)), branch_name=branch)
    node = result["DcimPhysicalDevice"]["edges"][0]["node"]
    images = await client.execute_graphql(
        query=f"query {{ DcimSoftwareImage(name__value: {json.dumps(image)}) {{ edges {{ node {{ id }} }} }} }}",
        branch_name=branch,
    )
    image_edges = images["DcimSoftwareImage"]["edges"]
    assert image_edges, f"software image '{image}' is not on branch '{branch}'"
    await _set_attributes(
        client, branch, "DcimPhysicalDevice", str(node["id"]), {"software_image": {"id": image_edges[0]["node"]["id"]}}
    )


# ---------------------------------------------------------------------------
# Render helpers
# ---------------------------------------------------------------------------

# role -> (query, transform class) of the in-process render, the same pairs test_70 uses.
RENDERERS: dict[str, tuple[str, type[Any]]] = {
    "leaf": ("leaf_config", Leaf),
    "access-leaf": ("leaf_config", AccessLeaf),
    "border-leaf": ("border_leaf_config", BorderLeaf),
    "spine": ("spine_config", Spine),
}


async def _render(client: InfrahubClient, branch: str, role: str, device: str) -> str:
    """The in-process render of ``device`` (a device of ``role``)."""
    query, transform_cls = RENDERERS[role]
    return await render_device_config(client, branch, device, query=query, transform_cls=transform_cls)


def _config_lines(rendered: str) -> list[str]:
    """The lines of ``rendered`` that are configuration, not ``!`` comments."""
    return [line for line in rendered.splitlines() if line.strip() and not line.lstrip().startswith("!")]


def _section(rendered: str) -> list[str]:
    """The lines of the ``router segment-security`` section: the header up to the closing ``!`` (empty: absent)."""
    lines = rendered.splitlines()
    if MSSG_SECTION not in lines:
        return []
    start = lines.index(MSSG_SECTION)
    end = next((index for index in range(start + 1, len(lines)) if lines[index] == "!"), len(lines))
    return lines[start:end]


def _parse_section(section: list[str]) -> dict[str, Any]:
    """The policies, per-VRF segments (definition, ``from`` lines, fallback) and shutdown state of the section."""
    parsed: dict[str, Any] = {"policies": {}, "vrfs": {}, "shutdown": None}
    policy: str | None = None
    vrf: str | None = None
    segment: dict[str, Any] | None = None
    for line in section[1:]:
        if match := re.fullmatch(r"   policy (\S+)", line):
            policy, vrf, segment = match.group(1), None, None
            parsed["policies"][policy] = []
        elif policy and (match := re.fullmatch(r"      (\d+) application (\S+) action (.+)", line)):
            parsed["policies"][policy].append(match.group(2))
        elif match := re.fullmatch(r"   vrf (\S+)", line):
            policy, vrf, segment = None, match.group(1), None
            parsed["vrfs"][vrf] = {}
        elif vrf and (match := re.fullmatch(r"      segment (\S+)", line)):
            segment = {"match": None, "froms": {}, "fallback": None}
            parsed["vrfs"][vrf][match.group(1)] = segment
        elif segment is not None and (match := re.fullmatch(r"            match prefix-ipv\d (\S+)", line)):
            segment["match"] = match.group(1)
        elif segment is not None and (match := re.fullmatch(r"            from (\S+) policy (\S+)", line)):
            segment["froms"][match.group(1)] = match.group(2)
        elif segment is not None and (match := re.fullmatch(r"            fallback policy (\S+)", line)):
            segment["fallback"] = match.group(1)
        elif line in ("   shutdown", "   no shutdown"):
            parsed["shutdown"] = line.strip()
            policy = vrf = segment = None
    return parsed


def _recognition_block(rendered: str) -> list[str]:
    """The lines of ``application traffic recognition`` up to ``router segment-security`` (empty: absent)."""
    lines = rendered.splitlines()
    if "application traffic recognition" not in lines or MSSG_SECTION not in lines:
        return []
    return lines[lines.index("application traffic recognition") : lines.index(MSSG_SECTION)]


def _mssg_errors(device: str, rendered: str, segments: dict[str, dict[str, Any]], *, enforced: bool) -> list[str]:
    """Everything the MSS-G render of a VTEP carrying the tenant VRF must contain (empty list: it does)."""
    errors: list[str] = []
    vrf = _live_vrf(segments)
    section = _section(rendered)
    if not section:
        return [f"{device}: no '{MSSG_SECTION}' (is DC14 a reference fabric, and does the device carry VRF {vrf}?)"]
    parsed = _parse_section(section)

    # The prefix of each segment, from the live gateway, as a global match-list.
    for name, segment in segments.items():
        if not re.search(
            rf"^match-list input prefix-ipv4 {re.escape(name)}\n   match prefix-ipv4 {re.escape(str(segment['prefix']))}$",
            rendered,
            re.MULTILINE,
        ):
            errors.append(f"{device}: no match-list '{name}' for the live prefix {segment['prefix']}")

    # The VRF and the segments in it, defined by their match-list.
    tenant = parsed["vrfs"].get(vrf)
    if tenant is None:
        return [*errors, f"{device}: no 'vrf {vrf}' in the section (VRFs: {sorted(parsed['vrfs'])})"]
    for name in segments:
        found = tenant.get(name)
        if found is None:
            errors.append(f"{device}: no 'segment {name}' under vrf {vrf}")
            continue
        if found["match"] != name:
            errors.append(f"{device}: segment {name} is defined by match-list {found['match']!r}")
        for source, expected in EXPECTED_FROMS[name].items():
            policy = found["froms"].get(source)
            if policy is None:
                errors.append(f"{device}: segment {name} has no 'from {source}'")
            elif expected is not None and policy != expected:
                errors.append(f"{device}: segment {name} 'from {source}' is {policy}, expected {expected}")
            elif expected is None and not policy.startswith("MSSG-POL-"):
                errors.append(f"{device}: segment {name} 'from {source}' is {policy}, expected a custom MSSG-POL-*")
        if not found["fallback"]:
            errors.append(f"{device}: segment {name} has no 'fallback policy'")

    # The closed any-reply of db -> app, as a comment (never a rendered permit).
    if not re.search(
        rf"^! not expressible \(left closed\): {re.escape(SEG_DB)} -> {re.escape(SEG_APP)}: "
        r"the reply of a permit-all flow",
        rendered,
        re.MULTILINE,
    ):
        errors.append(
            f"{device}: no 'not expressible (left closed)' line for the permit-all reply {SEG_DB} -> {SEG_APP}"
        )

    # Self-consistency: every policy / application / field-set a line names is defined once.
    block = _recognition_block(rendered)
    defined_apps = set(re.findall(r"^   application l4 (\S+)$", "\n".join(block), re.MULTILINE))
    used_field_sets = set(re.findall(r"port field-set (\S+)$", "\n".join(block), re.MULTILINE))
    defined_field_sets = set(re.findall(r"^   field-set l4-port (\S+)$", "\n".join(block), re.MULTILINE))
    if not block:
        errors.append(f"{device}: no 'application traffic recognition' block before the section")
    if not defined_field_sets or not used_field_sets <= defined_field_sets:
        errors.append(f"{device}: field-sets {sorted(defined_field_sets)} do not cover {sorted(used_field_sets)}")
    # field-set l4-port belongs INSIDE application traffic recognition, indented (flush-left it ends the block).
    if re.search(r"^field-set l4-port", rendered, re.MULTILINE):
        errors.append(f"{device}: a flush-left 'field-set l4-port' sits outside application traffic recognition")
    if not re.search(r"^      443, 8000-8010$", "\n".join(block), re.MULTILINE):
        errors.append(f"{device}: no field-set with the ports '443, 8000-8010' in application traffic recognition")
    for policy, names in parsed["policies"].items():
        missing = sorted(app for app in names if app != "app-match-all" and app not in defined_apps)
        if missing:
            errors.append(f"{device}: policy {policy} names undefined application(s) {missing}")
    referenced = {
        policy
        for segment in tenant.values()
        for policy in [*segment["froms"].values(), segment["fallback"]]
        if policy and policy.startswith("MSSG-POL-")
    }
    if referenced - set(parsed["policies"]):
        errors.append(f"{device}: undefined polic(ies) referenced: {sorted(referenced - set(parsed['policies']))}")

    # Never rendered: the box-wide default-drop removal (it is only ever named in a comment).
    if any("policy-drop-all default" in line for line in _config_lines(rendered)):
        errors.append(f"{device}: renders 'no segment policy policy-drop-all default'")

    # The staging switch: a comment and the LAST line of the section.
    staged = [line for line in rendered.splitlines() if line.startswith(STAGED_COMMENT_PREFIX)]
    if enforced:
        if staged or parsed["shutdown"] != "no shutdown":
            errors.append(f"{device}: enforcing DC renders staged={bool(staged)} shutdown={parsed['shutdown']!r}")
    else:
        if len(staged) != 1:
            errors.append(f"{device}: expected one staged comment line, found {len(staged)}")
        if parsed["shutdown"] != "shutdown":
            errors.append(f"{device}: staged DC renders {parsed['shutdown']!r}, expected 'shutdown'")
    if section[-1] not in ("   shutdown", "   no shutdown"):
        errors.append(f"{device}: the section does not end with the feature switch (last line {section[-1]!r})")
    return errors


def _changed_lines(before: str, after: str) -> tuple[list[str], list[str]]:
    """(removed, added) lines between two renders."""
    removed: list[str] = []
    added: list[str] = []
    # The first two entries are the (empty) file headers.
    for line in list(difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=0))[2:]:
        if line.startswith("@@"):
            continue
        (removed if line.startswith("-") else added).append(line[1:])
    return removed, added


# ---------------------------------------------------------------------------
# The scenario
# ---------------------------------------------------------------------------


class TestAristaMssg(TestInfrahubDockerWithClient):
    """Load the MSS-G demo on its own branch and verify the fabric, the tags and the EOS renders."""

    @pytest.fixture(scope="class")
    def scenario_branch(self) -> str:
        return MSSG_BRANCH

    @pytest.fixture(scope="class")
    def renders(self) -> dict[str, str]:
        """Device name -> its baseline (staged) render, filled by test_08 and read by the later steps."""
        return {}

    def _load_stage(self, client_main: InfrahubClientSync, branch: str, stage: tuple[str, tuple[str, ...]]) -> None:
        """Load one stage with a single ``infrahubctl object load`` call (the loader already sorts its files)."""
        stage_name, paths = stage
        full_paths = " ".join(f"{MSSG_DATA}/{path}" for path in paths)
        logging.info("--- stage '%s': loading %s ---", stage_name, full_paths)
        load_result = self.execute_command(
            f"infrahubctl object load {full_paths} --branch {branch}",
            address=client_main.config.address,
            concurrent_execution=10,
            pagination_size=200,
        )
        assert load_result.returncode == 0, (
            f"Stage '{stage_name}' of the MSS-G load failed.\n"
            f"  Paths: {full_paths}\n"
            f"  Return code: {load_result.returncode}\n"
            f"  stdout: {load_result.stdout}\n"
            f"  stderr: {load_result.stderr}"
        )

    async def _load_and_settle(
        self,
        async_client: InfrahubClient,
        client_main: InfrahubClientSync,
        branch: str,
        stages: tuple[tuple[str, tuple[str, ...]], ...],
    ) -> None:
        """Load the stages one by one, letting each stage's generators settle before the next."""
        for stage in stages:
            self._load_stage(client_main, branch, stage)
            await wait_for_tasks_completion(
                async_client,
                branch,
                initial_delay=10,
                max_wait_attempts=MSSG_STAGE_MAX_ATTEMPTS,
                poll_interval=MSSG_STAGE_POLL_INTERVAL,
                stable_zero_count=MSSG_STAGE_STABLE_ZERO,
            )
            await verify_no_failed_tasks(client=async_client, branch=branch)
            logging.info("--- stage '%s': settled, no failed tasks ---", stage[0])

    @pytest.mark.order(430)
    @pytest.mark.dependency(scope="session", name="mssg_branch", depends=["triggers_active"])
    def test_01_create_branch(self, client_main: InfrahubClientSync, scenario_branch: str) -> None:
        """Create the branch the MSS-G scope loads onto (demo data never goes into main)."""
        logging.info("=== %s - Step 1: Create Branch ===", SCENARIO_NAME)

        if scenario_branch not in client_main.branch.all():
            client_main.branch.create(branch_name=scenario_branch, sync_with_git=False, wait_until_completion=True)
            logging.info("Created branch: %s", scenario_branch)
        else:
            logging.info("Branch already exists: %s", scenario_branch)

    @pytest.mark.order(431)
    @pytest.mark.dependency(scope="session", name="mssg_fabric_load", depends=["mssg_branch"])
    @pytest.mark.asyncio
    async def test_02_load_fabric(
        self, async_client_main: InfrahubClient, client_main: InfrahubClientSync, scenario_branch: str
    ) -> None:
        """Load the design and the fabric, letting the DC's generators settle without a failed task."""
        logging.info("=== %s - Step 2: Load the Fabric ===", SCENARIO_NAME)

        await self._load_and_settle(async_client_main, client_main, scenario_branch, MSSG_FABRIC_STAGES)

    @pytest.mark.order(432)
    @pytest.mark.dependency(scope="session", name="mssg_fabric", depends=["mssg_fabric_load"])
    @pytest.mark.asyncio
    async def test_03_fabric_shape(self, async_client_main: InfrahubClient, scenario_branch: str) -> None:
        """DC14 has its device roles and three back-to-back MLAG pairs (never virtual)."""
        logging.info("=== %s - Step 3: Fabric Shape ===", SCENARIO_NAME)

        topology = await fetch_dc_topology(client=async_client_main, branch=scenario_branch, dc_name=MSSG_DC)
        counts = compute_role_counts(topology["devices"])
        found = {role: counts.get(role, 0) for role in MSSG_EXPECTED_ROLE_COUNTS}
        assert found == MSSG_EXPECTED_ROLE_COUNTS, f"{MSSG_DC} roles {found}, expected {MSSG_EXPECTED_ROLE_COUNTS}"

        result = await async_client_main.execute_graphql(query=MLAG_QUERY, branch_name=scenario_branch)
        pairs = [edge["node"] for edge in result["ManagedMLAG"]["edges"]]
        assert len(pairs) == MSSG_EXPECTED_MLAG_PAIRS, (
            f"{len(pairs)} MLAG domain(s) {[p['name']['value'] for p in pairs]}, expected {MSSG_EXPECTED_MLAG_PAIRS}"
        )
        virtual = [p["name"]["value"] for p in pairs if p["virtual_peer_link"]["value"]]
        assert not virtual, f"virtual MLAG peer link(s) {virtual}: arista_eos_mlag.j2 renders back-to-back only"

    @pytest.mark.order(433)
    @pytest.mark.dependency(scope="session", name="mssg_tenant_load", depends=["mssg_fabric"])
    @pytest.mark.asyncio
    async def test_04_load_tenant(
        self, async_client_main: InfrahubClient, client_main: InfrahubClientSync, scenario_branch: str
    ) -> None:
        """Load servers, footprint, segments, policies and the application, each stage settled."""
        logging.info("=== %s - Step 4: Load the Tenant ===", SCENARIO_NAME)

        await self._load_and_settle(async_client_main, client_main, scenario_branch, MSSG_TENANT_STAGES)

    @pytest.mark.order(434)
    @pytest.mark.dependency(scope="session", name="mssg_tenant", depends=["mssg_tenant_load"])
    @pytest.mark.asyncio
    async def test_05_tenant_state(self, async_client_main: InfrahubClient, scenario_branch: str) -> None:
        """Servers sit on the intended tier; every segment has gateway, tag and deployment; the policies are intact."""
        logging.info("=== %s - Step 5: Tenant State ===", SCENARIO_NAME)

        await verify_no_failed_tasks(client=async_client_main, branch=scenario_branch)
        topology = await fetch_dc_topology(client=async_client_main, branch=scenario_branch, dc_name=MSSG_DC)

        # add_endpoint cables a server to the access-leaf pair of its row when it has one, else to the leaf pair.
        hosting = await _fetch_hosting_switches(async_client_main, scenario_branch)
        access, leafs = (_devices_of_role(topology["devices"], role) for role in ("access-leaf", "leaf"))
        assert hosting.get(SERVER_ROW1), f"{SERVER_ROW1} is not cabled to any switch: {hosting}"
        assert hosting.get(SERVER_ROW2), f"{SERVER_ROW2} is not cabled to any switch: {hosting}"
        assert hosting[SERVER_ROW1] <= set(access) and len(hosting[SERVER_ROW1]) == 2, (
            f"{SERVER_ROW1} is cabled to {sorted(hosting[SERVER_ROW1])}, expected the two access-leafs {access}"
        )
        assert hosting[SERVER_ROW2] <= set(leafs) and len(hosting[SERVER_ROW2]) == 2, (
            f"{SERVER_ROW2} is cabled to {sorted(hosting[SERVER_ROW2])}, expected a leaf pair of {leafs}"
        )

        segments = await _fetch_segments(async_client_main, scenario_branch)
        assert sorted(segments) == sorted(SEGMENTS), f"segments on the branch: {sorted(segments)}"
        _live_vrf(segments)
        prefixes = [segment["prefix"] for segment in segments.values()]
        assert all(prefixes) and len(set(prefixes)) == len(SEGMENTS), f"gateway prefixes: {prefixes}"
        for name, segment in segments.items():
            assert segment["gateway"], f"{name} has no gateway"
            assert segment["tag_name"] == name, (
                f"{name}: group tag {segment['tag_name']!r}, expected one named after it"
            )
            assert segment["group_id"], f"{name}: its group tag has no group_id"
            assert [dep["dc"] for dep in segment["deployments"]] == [MSSG_DC], (
                f"{name}: active deployments {segment['deployments']}, expected exactly one in {MSSG_DC}"
            )
            assert all(dep["vni"] for dep in segment["deployments"]), f"{name}: a deployment has no VNI"
        group_ids = [segment["group_id"] for segment in segments.values()]
        assert len(set(group_ids)) == len(group_ids), f"group tags share a group_id: {group_ids}"

        # The contract is exactly the hand-written rules: the application declares no dependency, so the
        # application generator must have written none (a rule it added would change every row below).
        result = await async_client_main.execute_graphql(
            query=POLICIES_QUERY.format(names=json.dumps(sorted(EXPECTED_POLICY_RULES))), branch_name=scenario_branch
        )
        found = {
            edge["node"]["name"]["value"]: {rule["node"]["name"]["value"] for rule in edge["node"]["rules"]["edges"]}
            for edge in result["SecurityPolicy"]["edges"]
        }
        assert found == EXPECTED_POLICY_RULES, f"policy rules {found}, expected {EXPECTED_POLICY_RULES}"

    @pytest.mark.order(435)
    @pytest.mark.dependency(scope="session", name="mssg_checks", depends=["mssg_tenant"])
    @pytest.mark.asyncio
    async def test_06_checks(self, async_client_main: InfrahubClient, scenario_branch: str) -> None:
        """CheckReferenceFabric (no error first, then the verdict) and CheckSegmentSecurity (no error), run in-process."""
        logging.info("=== %s - Step 6: Checks ===", SCENARIO_NAME)

        topology = await fetch_dc_topology(client=async_client_main, branch=scenario_branch, dc_name=MSSG_DC)
        vteps = sum(len(_devices_of_role(topology["devices"], role)) for role in MSSG_VTEP_ROLES)

        reference = CheckReferenceFabric(branch=scenario_branch, client=async_client_main, params={})
        passed = await reference.run()
        errors = [str(entry.get("message") or "") for entry in reference.logs if entry.get("level") == "ERROR"]
        messages = [str(entry.get("message") or "") for entry in reference.logs]
        # Errors first: a fabric the gate rejects says why in an INFO line, so print every line on failure.
        assert passed and not errors, "CheckReferenceFabric failed:\n" + "\n".join(f"  - {e}" for e in errors)
        verdict = f"{MSSG_DC} is a reference fabric (segment_security, {vteps} VTEPs)"
        assert verdict in messages, f"expected '{verdict}', the check said:\n" + "\n".join(f"  - {m}" for m in messages)

        security = CheckSegmentSecurity(branch=scenario_branch, client=async_client_main, params={})
        passed = await security.run()
        errors = [str(entry.get("message") or "") for entry in security.logs if entry.get("level") == "ERROR"]
        assert passed and not errors, "CheckSegmentSecurity failed:\n" + "\n".join(f"  - {e}" for e in errors)

    @pytest.mark.order(436)
    @pytest.mark.dependency(scope="session", name="mssg_matrix", depends=["mssg_checks"])
    @pytest.mark.asyncio
    async def test_07_segmentation_matrix(self, async_client_main: InfrahubClient, scenario_branch: str) -> None:
        """The matrix is a staged segment_security reference site with the live prefixes and the closed any-reply."""
        logging.info("=== %s - Step 7: Segmentation Matrix ===", SCENARIO_NAME)

        dc_id = await _fetch_dc_id(async_client_main, scenario_branch)
        raw = await async_client_main.query_gql_query(
            name="segmentation_matrix", branch_name=scenario_branch, variables={"id": dc_id}
        )
        transform = SegmentationMatrix.__new__(SegmentationMatrix)
        matrix = await transform.transform(raw.get("data") or raw)

        site = matrix["site"]
        assert site["kind"] == "segment_security", f"site kind {site['kind']!r}"
        assert site["reference"] is True, f"{MSSG_DC} is not a reference site: {site['reason']}"
        assert site["enforce"] is False, "a fresh DC14 must stage (group_policy_enforce false)"
        assert all(vtep["reason"] is None for vtep in matrix["vteps"]), (
            f"VTEPs outside the profile row: {[v for v in matrix['vteps'] if v['reason']]}"
        )

        live = await _fetch_segments(async_client_main, scenario_branch)
        selectors = {item["segment"]: item for item in matrix["classification"]["selectors"]}
        assert sorted(selectors) == sorted(SEGMENTS), f"classified segments: {sorted(selectors)}"
        assert matrix["classification"]["unclassified"] == [], "a routed tenant segment has no group tag"
        for name, segment in live.items():
            assert selectors[name]["prefix"] == segment["prefix"], (
                f"{name}: the matrix classifies by {selectors[name]['prefix']}, the gateway prefix is {segment['prefix']}"
            )

        view = matrix["kinds"]["segment_security"]
        by_name = {item["name"]: item for item in view["segments"]}
        for name, segment in live.items():
            assert by_name[name]["prefix"] == segment["prefix"], f"{name}: view prefix {by_name[name]['prefix']}"
        closed = [
            item
            for item in view["unsupported"]
            if item["src"] == SEG_DB and item["dst"] == SEG_APP and "reply of a permit-all flow" in item["reason"]
        ]
        assert closed, f"the db -> app any-reply is not in `unsupported`: {view['unsupported']}"
        assert closed[0]["rules"] == ["db-to-app-any"], f"closed rule(s): {closed[0]['rules']}"

    @pytest.mark.order(437)
    @pytest.mark.dependency(scope="session", name="mssg_render", depends=["mssg_matrix"])
    @pytest.mark.asyncio
    async def test_08_renders(
        self, async_client_main: InfrahubClient, scenario_branch: str, renders: dict[str, str]
    ) -> None:
        """Every VTEP that carries the tenant VRF renders the staged MSS-G block; nothing else does."""
        logging.info("=== %s - Step 8: Renders ===", SCENARIO_NAME)

        topology = await fetch_dc_topology(client=async_client_main, branch=scenario_branch, dc_name=MSSG_DC)
        segments = await _fetch_segments(async_client_main, scenario_branch)
        hosting = await _fetch_hosting_switches(async_client_main, scenario_branch)
        carriers = hosting[SERVER_ROW1] | hosting[SERVER_ROW2]

        errors: list[str] = []
        for role in ("leaf", "access-leaf", "border-leaf"):
            devices = _devices_of_role(topology["devices"], role)
            assert devices, f"no {role} in {MSSG_DC}"
            for device in devices:
                rendered = await _render(async_client_main, scenario_branch, role, device)
                renders[device] = rendered
                # A leaf or access-leaf carries the tenant VRF only through a segment tagged on its own ports; a
                # border-leaf through the firewall legs of the tenant's context.
                if role == "border-leaf" or device in carriers:
                    errors += _mssg_errors(device, rendered, segments, enforced=False)
                elif _section(rendered):
                    errors.append(f"{device} ({role}) hosts no segment but renders '{MSSG_SECTION}'")
        assert not errors, "MSS-G renders:\n" + "\n".join(f"  - {e}" for e in errors)

        # Negative control: a spine is no VTEP and renders no segment security.
        for spine in _devices_of_role(topology["devices"], "spine"):
            rendered = await _render(async_client_main, scenario_branch, "spine", spine)
            assert MSSG_SECTION not in rendered.splitlines(), f"spine {spine} renders '{MSSG_SECTION}'"

    @pytest.mark.order(438)
    @pytest.mark.dependency(scope="session", name="mssg_enforce", depends=["mssg_render"])
    @pytest.mark.asyncio
    async def test_09_enforce_flips_one_line(
        self, async_client_main: InfrahubClient, scenario_branch: str, renders: dict[str, str]
    ) -> None:
        """group_policy_enforce starts no generator and changes each render by the staged comment and the switch only."""
        logging.info("=== %s - Step 9: Enforce ===", SCENARIO_NAME)

        dc_id = await _fetch_dc_id(async_client_main, scenario_branch)
        segments = await _fetch_segments(async_client_main, scenario_branch)
        topology = await fetch_dc_topology(client=async_client_main, branch=scenario_branch, dc_name=MSSG_DC)
        roles = {device["name"]: device["role"] for device in topology["devices"]}
        rendering = sorted(name for name, rendered in renders.items() if _section(rendered))
        assert rendering, "no device rendered the MSS-G block in the previous step"

        runs_before = await fetch_generator_runs(async_client_main, scenario_branch)
        await _set_enforce(async_client_main, scenario_branch, dc_id, True)
        try:
            await wait_for_tasks_completion(async_client_main, scenario_branch, initial_delay=15)
            runs_after = await fetch_generator_runs(async_client_main, scenario_branch)
            assert runs_after == runs_before, f"enforcing started generator(s): {runs_after} vs {runs_before}"

            errors: list[str] = []
            for device in rendering:
                enforced = await _render(async_client_main, scenario_branch, str(roles[device]), device)
                removed, added = _changed_lines(renders[device], enforced)
                if (
                    len(removed) != 2
                    or not removed[0].startswith(STAGED_COMMENT_PREFIX)
                    or removed[1] != "   shutdown"
                    or added != ["   no shutdown"]
                ):
                    errors.append(f"{device}: enforcing changed {removed} -> {added}")
                errors += _mssg_errors(device, enforced, segments, enforced=True)
            assert not errors, "enforced renders:\n" + "\n".join(f"  - {e}" for e in errors)

            raw = await async_client_main.query_gql_query(
                name="segmentation_matrix", branch_name=scenario_branch, variables={"id": dc_id}
            )
            matrix = await SegmentationMatrix.__new__(SegmentationMatrix).transform(raw.get("data") or raw)
            assert matrix["site"]["enforce"] is True, "the matrix does not show the DC as enforcing"
        finally:
            await _set_enforce(async_client_main, scenario_branch, dc_id, False)
            await wait_for_tasks_completion(async_client_main, scenario_branch, initial_delay=5)

        restored = await _render(async_client_main, scenario_branch, str(roles[rendering[0]]), rendering[0])
        assert restored == renders[rendering[0]], f"{rendering[0]}: staging again did not restore the staged render"

    @pytest.mark.order(439)
    @pytest.mark.dependency(scope="session", name="mssg_gate", depends=["mssg_enforce"])
    @pytest.mark.asyncio
    async def test_10_one_old_vtep_removes_the_block(
        self, async_client_main: InfrahubClient, scenario_branch: str, renders: dict[str, str]
    ) -> None:
        """One access-leaf below the profile's min_release makes DC14 no reference fabric: no block anywhere."""
        logging.info("=== %s - Step 10: Negative Gate ===", SCENARIO_NAME)

        topology = await fetch_dc_topology(client=async_client_main, branch=scenario_branch, dc_name=MSSG_DC)
        roles = {device["name"]: device["role"] for device in topology["devices"]}
        rendering = sorted(name for name, rendered in renders.items() if _section(rendered))
        victim = _devices_of_role(topology["devices"], "access-leaf")[0]
        bystander = next(name for name in rendering if name != victim and roles[name] == "leaf")

        await _set_software_image(async_client_main, scenario_branch, victim, OLDER_IMAGE)
        try:
            await wait_for_tasks_completion(async_client_main, scenario_branch, initial_delay=5)
            check = CheckReferenceFabric(branch=scenario_branch, client=async_client_main, params={})
            await check.run()
            messages = [str(entry.get("message") or "") for entry in check.logs]
            rejected = [m for m in messages if m.startswith(f"{MSSG_DC} is not a reference fabric")]
            assert rejected, "CheckReferenceFabric still accepts DC14:\n" + "\n".join(f"  - {m}" for m in messages)
            assert victim in rejected[0] and "release 4.32.1F < 4.36.2F" in rejected[0], (
                f"the verdict does not name {victim} and its release: {rejected[0]}"
            )

            for device in (victim, bystander):
                rendered = await _render(async_client_main, scenario_branch, str(roles[device]), device)
                assert not _section(rendered) and "Segment security (Arista MSS-G)" not in rendered, (
                    f"{device} still renders MSS-G although DC14 is not a reference fabric"
                )
        finally:
            await _set_software_image(async_client_main, scenario_branch, victim, RESTORED_IMAGE)
            await wait_for_tasks_completion(async_client_main, scenario_branch, initial_delay=5)

        check = CheckReferenceFabric(branch=scenario_branch, client=async_client_main, params={})
        await check.run()
        messages = [str(entry.get("message") or "") for entry in check.logs]
        assert any(m.startswith(f"{MSSG_DC} is a reference fabric") for m in messages), (
            "DC14 is not a reference fabric again after the image was restored:\n"
            + "\n".join(f"  - {m}" for m in messages)
        )
        again = await _render(async_client_main, scenario_branch, str(roles[bystander]), bystander)
        assert again == renders[bystander], f"{bystander}: the restored render differs from the baseline"

    @pytest.mark.order(440)
    @pytest.mark.dependency(scope="session", name="mssg_rerun", depends=["mssg_gate"])
    @pytest.mark.asyncio
    async def test_11_rerunning_add_vxlan_segment_changes_nothing(
        self, async_client_main: InfrahubClient, scenario_branch: str, renders: dict[str, str]
    ) -> None:
        """A second add_vxlan_segment run keeps every tag, prefix and id, and every render byte-identical."""
        logging.info("=== %s - Step 11: Rerun add_vxlan_segment ===", SCENARIO_NAME)

        await wait_for_tasks_completion(async_client_main, scenario_branch)
        segments_before = await _fetch_segments(async_client_main, scenario_branch)
        before, labels = await snapshot_topology(async_client_main, scenario_branch)
        for kind in ("DcimDevice", "DcimInterface", "IpamPrefix", "ManagedSegmentDeployment", "SecurityPolicy"):
            assert before[kind], f"no {kind} on the branch to compare"
        tags_before = await fetch_object_counts(async_client_main, scenario_branch, ["SecurityTag"])

        outcome = await run_generator(
            client=async_client_main,
            generator_name="add_vxlan_segment",
            node_ids=[segment["id"] for segment in segments_before.values()],
            branch=scenario_branch,
        )
        assert outcome["success"], f"add_vxlan_segment rerun failed: {outcome}"
        await wait_for_tasks_completion(async_client_main, scenario_branch)
        await verify_no_failed_tasks(client=async_client_main, branch=scenario_branch)

        segments_after = await _fetch_segments(async_client_main, scenario_branch)
        assert segments_after == segments_before, f"segments changed on a rerun:\n{segments_before}\n{segments_after}"
        assert await fetch_object_counts(async_client_main, scenario_branch, ["SecurityTag"]) == tags_before

        after, labels_after = await snapshot_topology(async_client_main, scenario_branch)
        changes = diff_snapshots(before, after, {**labels_after, **labels})
        assert not changes, f"{len(changes)} object(s) changed on an add_vxlan_segment rerun:\n  " + "\n  ".join(
            changes[:50]
        )

        topology = await fetch_dc_topology(client=async_client_main, branch=scenario_branch, dc_name=MSSG_DC)
        roles = {device["name"]: device["role"] for device in topology["devices"]}
        drifted: list[str] = []
        for device, baseline in sorted(renders.items()):
            if await _render(async_client_main, scenario_branch, str(roles[device]), device) != baseline:
                drifted.append(device)
        assert not drifted, f"render(s) differ after the rerun: {drifted}"
