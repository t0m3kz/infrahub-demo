"""Integration test — the LISP demo fabric (data/demos/31_lisp_fabric).

DC13 runs ``overlay_control_plane: lisp`` over an OSPF underlay: the border-leafs are the
LISP map-servers, the edge devices are ETRs, and there is no BGP anywhere in the fabric. The
scope is not part of 30_all (a lab-unverified platform would end up in test_65), so this module
loads it on its own branch and asserts what the LISP profile promises:

1. The staged load settles without a failed task.
2. The fabric has the expected device roles, an OSPF process on every device and no ManagedBGP.
3. The fabric's site key is attached and the tenant segments are active on the DC.
4. CheckLispFabric finds nothing to report (run in-process, like test_64's checks).
5. The border-leaf config renders ``router lisp`` with the ``site`` and no EVPN/VXLAN; the leaf
   and access-leaf configs carry the edge LISP config (``router lisp``, ITR/ETR and ``etr map-server``
   toward every border) over the OSPF underlay, with no EVPN/VXLAN.
6. Re-running add_dc changes no id and no count.

Every IOS-XE line the renders contain is unverified-no-lab (templates/configs/common/cisco_xe_lisp.j2),
and this module cannot change that: it asserts the shape of the render, not that a device accepts it.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import pytest
from infrahub_sdk import InfrahubClient, InfrahubClientSync

from checks.lisp_fabric import CheckLispFabric
from transforms.config.access_leaf import AccessLeaf
from transforms.config.border_leaf import BorderLeaf
from transforms.config.leaf import Leaf

from .conftest import TestInfrahubDockerWithClient
from .test_helpers import (
    compute_role_counts,
    diff_snapshots,
    fetch_dc_topology,
    fetch_object_counts,
    render_device_config,
    snapshot_topology,
)
from .workflow_helpers import run_generator, verify_no_failed_tasks, wait_for_tasks_completion

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

SCENARIO_NAME = "Scenario 31_lisp_fabric: LISP Overlay Fabric"

LISP_DATA = "data/demos/31_lisp_fabric"
LISP_BRANCH = "lisp-fabric-scenario"
LISP_DC = "DC13"

# (stage name, load paths under LISP_DATA). Same order as the README. The stages are separate loads
# because each needs what the previous one's generators create: the footprints name the DC, the
# segments name the footprints and draw their VNI from the DC's pool.
LISP_LOAD_STAGES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("design", ("00_design",)),
    ("fabric", ("01_fabric",)),
    ("footprint", ("02_footprint",)),
    ("segments", ("03_segments",)),
)
# The fabric stage generates a DC, two pods, two racks and their cabling: a few minutes at most.
LISP_STAGE_MAX_ATTEMPTS = 180
LISP_STAGE_POLL_INTERVAL = 10  # seconds
# Pods and racks are dispatched independently, so the queue can go quiet between waves.
LISP_STAGE_STABLE_ZERO = 10

# 2 pods x (2 spines, 2 leafs, 2 access-leafs) plus the DC-level pair of border-leafs.
LISP_EXPECTED_ROLE_COUNTS = {"border-leaf": 2, "spine": 4, "leaf": 4, "access-leaf": 4}
LISP_VTEP_ROLES = ("border-leaf", "leaf", "access-leaf")
# The two tenant footprints: PROD (L3 VNI 60000) and NON-PROD (60001), data/bootstrap/22_namespaces.yml.
LISP_EXPECTED_SEGMENT_DEPLOYMENTS = 2
LISP_EXPECTED_L3_INSTANCE_IDS = (60000, 60001)

# What a LISP fabric must not render: the EVPN control plane and its VXLAN data plane.
FORBIDDEN_EVPN_PATTERNS = (r"l2vpn evpn", r"\bnve1\b", r"^router bgp", r"address-family l2vpn")

DC_QUERY = """
query ($name: String!) {
  TopologyDataCenter(name__value: $name) {
    edges {
      node {
        id
        overlay_control_plane { value }
        routing_strategy { value }
        underlay_protocol { value }
        lisp_site_key { node { id name { value } } }
        segment_deployments(status__values: ["active", "provisioning"]) { edges { node { id vni { value } } } }
      }
    }
  }
}
"""


async def _fetch_dc(client: InfrahubClient, branch: str) -> dict[str, Any]:
    """The LISP DC's raw GraphQL node (DC_QUERY); fails the test when it is not on the branch."""
    result = await client.execute_graphql(query=DC_QUERY, variables={"name": LISP_DC}, branch_name=branch)
    edges = result["TopologyDataCenter"]["edges"]
    assert edges, f"{LISP_DC} not found on branch '{branch}'"
    return edges[0]["node"]


def _devices_of_role(devices: list[dict[str, Any]], role: str) -> list[str]:
    """Sorted names of the devices with ``role`` (fetch_dc_topology's device dicts)."""
    return sorted(str(device["name"]) for device in devices if device.get("role") == role)


def _forbidden_found(rendered: str) -> list[str]:
    """The FORBIDDEN_EVPN_PATTERNS that match somewhere in ``rendered``."""
    return [pattern for pattern in FORBIDDEN_EVPN_PATTERNS if re.search(pattern, rendered, re.MULTILINE)]


class TestLispFabric(TestInfrahubDockerWithClient):
    """Load the LISP demo on its own branch and verify the fabric it builds."""

    @pytest.fixture(scope="class")
    def scenario_branch(self) -> str:
        return LISP_BRANCH

    @pytest.mark.order(420)
    @pytest.mark.dependency(scope="session", name="lisp_branch", depends=["triggers_active"])
    def test_01_create_branch(self, client_main: InfrahubClientSync, scenario_branch: str) -> None:
        """Create the branch the LISP scope loads onto (demo data never goes into main)."""
        logging.info("=== %s - Step 1: Create Branch ===", SCENARIO_NAME)

        if scenario_branch not in client_main.branch.all():
            client_main.branch.create(branch_name=scenario_branch, sync_with_git=False, wait_until_completion=True)
            logging.info("Created branch: %s", scenario_branch)
        else:
            logging.info("Branch already exists: %s", scenario_branch)

    @pytest.mark.order(421)
    @pytest.mark.dependency(scope="session", name="lisp_load", depends=["lisp_branch"])
    @pytest.mark.asyncio
    async def test_02_load_data_in_stages(
        self,
        async_client_main: InfrahubClient,
        client_main: InfrahubClientSync,
        scenario_branch: str,
    ) -> None:
        """Load the scope stage by stage, letting each stage's generators settle first."""
        logging.info("=== %s - Step 2: Staged Load ===", SCENARIO_NAME)

        for stage_name, paths in LISP_LOAD_STAGES:
            full_paths = " ".join(f"{LISP_DATA}/{path}" for path in paths)
            logging.info("--- stage '%s': loading %s ---", stage_name, full_paths)

            load_result = self.execute_command(
                f"infrahubctl object load {full_paths} --branch {scenario_branch}",
                address=client_main.config.address,
                concurrent_execution=10,
                pagination_size=200,
            )
            assert load_result.returncode == 0, (
                f"Stage '{stage_name}' of the LISP load failed.\n"
                f"  Paths: {full_paths}\n"
                f"  Return code: {load_result.returncode}\n"
                f"  stdout: {load_result.stdout}\n"
                f"  stderr: {load_result.stderr}"
            )

            await wait_for_tasks_completion(
                async_client_main,
                scenario_branch,
                initial_delay=10,
                max_wait_attempts=LISP_STAGE_MAX_ATTEMPTS,
                poll_interval=LISP_STAGE_POLL_INTERVAL,
                stable_zero_count=LISP_STAGE_STABLE_ZERO,
            )
            await verify_no_failed_tasks(client=async_client_main, branch=scenario_branch)
            logging.info("--- stage '%s': settled, no failed tasks ---", stage_name)

    @pytest.mark.order(422)
    @pytest.mark.dependency(scope="session", name="lisp_no_failures", depends=["lisp_load"])
    @pytest.mark.asyncio
    async def test_03_verify_no_failed_tasks(self, async_client_main: InfrahubClient, scenario_branch: str) -> None:
        """Final branch-wide failure sweep once everything has settled."""
        logging.info("=== %s - Step 3: Verify No Failed Tasks ===", SCENARIO_NAME)

        await verify_no_failed_tasks(client=async_client_main, branch=scenario_branch)

    @pytest.mark.order(423)
    @pytest.mark.dependency(scope="session", name="lisp_routing", depends=["lisp_no_failures"])
    @pytest.mark.asyncio
    async def test_04_fabric_is_ospf_only(self, async_client_main: InfrahubClient, scenario_branch: str) -> None:
        """The DC has its expected roles, an OSPF process on every device, and no BGP at all."""
        logging.info("=== %s - Step 4: OSPF-only Fabric ===", SCENARIO_NAME)

        topology = await fetch_dc_topology(client=async_client_main, branch=scenario_branch, dc_name=LISP_DC)
        devices = topology["devices"]
        counts = compute_role_counts(devices)
        assert counts == LISP_EXPECTED_ROLE_COUNTS, f"{LISP_DC} roles {counts}, expected {LISP_EXPECTED_ROLE_COUNTS}"

        without_ospf = sorted(
            str(device["name"])
            for device in devices
            if not any(cap.get("typename") == "ManagedOSPF" for cap in device.get("capabilities") or [])
        )
        assert not without_ospf, f"devices without an OSPF process: {without_ospf}"

        with_bgp = sorted(
            str(device["name"])
            for device in devices
            if any(cap.get("typename") == "ManagedBGP" for cap in device.get("capabilities") or [])
        )
        assert not with_bgp, f"LISP fabric devices with a ManagedBGP process: {with_bgp}"

        # The branch holds this scope only, so any ManagedBGP, peering or EVPN address family
        # anywhere is a process the LISP planner should not have created.
        counts_by_kind = await fetch_object_counts(
            client=async_client_main, branch=scenario_branch, kinds=["ManagedBGP", "ManagedBGPPeering"]
        )
        assert counts_by_kind == {"ManagedBGP": 0, "ManagedBGPPeering": 0}, (
            f"the LISP branch holds BGP objects: {counts_by_kind}"
        )

    @pytest.mark.order(424)
    @pytest.mark.dependency(scope="session", name="lisp_dc_state", depends=["lisp_no_failures"])
    @pytest.mark.asyncio
    async def test_05_site_key_and_segments(self, async_client_main: InfrahubClient, scenario_branch: str) -> None:
        """The DC is lisp/ospf-ibgp/ipv4, its site key is attached, and both tenant segments are active on it."""
        logging.info("=== %s - Step 5: Site Key and Segments ===", SCENARIO_NAME)

        dc = await _fetch_dc(async_client_main, scenario_branch)
        assert dc["overlay_control_plane"]["value"] == "lisp"
        assert dc["routing_strategy"]["value"] == "ospf-ibgp"
        assert dc["underlay_protocol"]["value"] == "ipv4"

        site_key = (dc.get("lisp_site_key") or {}).get("node")
        assert site_key, f"{LISP_DC} has no lisp_site_key: the generator did not create or attach the site key"
        logging.info("Site key: %s (%s)", site_key["name"]["value"], site_key["id"])

        deployments = dc["segment_deployments"]["edges"]
        assert len(deployments) == LISP_EXPECTED_SEGMENT_DEPLOYMENTS, (
            f"{LISP_DC} has {len(deployments)} active segment deployment(s), expected {LISP_EXPECTED_SEGMENT_DEPLOYMENTS}"
        )
        assert all(edge["node"]["vni"]["value"] for edge in deployments), "a segment deployment has no VNI"

    @pytest.mark.order(425)
    @pytest.mark.dependency(scope="session", name="lisp_check", depends=["lisp_dc_state"])
    @pytest.mark.asyncio
    async def test_06_lisp_fabric_check_is_silent(
        self, async_client_main: InfrahubClient, scenario_branch: str
    ) -> None:
        """CheckLispFabric (targetless, run in-process) reports no error for DC13."""
        logging.info("=== %s - Step 6: CheckLispFabric ===", SCENARIO_NAME)

        check = CheckLispFabric(branch=scenario_branch, client=async_client_main, params={})
        passed = await check.run()

        errors = [str(entry.get("message") or "") for entry in check.errors]
        assert passed and not errors, f"CheckLispFabric failed on branch '{scenario_branch}':\n" + "\n".join(
            f"  - {e}" for e in errors
        )

    @pytest.mark.order(426)
    @pytest.mark.dependency(scope="session", name="lisp_border_render", depends=["lisp_dc_state"])
    @pytest.mark.asyncio
    async def test_07_border_leafs_render_the_lisp_site(
        self, async_client_main: InfrahubClient, scenario_branch: str
    ) -> None:
        """Each border-leaf renders the co-located map-server/resolver and the DC's ``site`` with its EID records,
        over the OSPF underlay, with no EVPN, VXLAN or BGP."""
        logging.info("=== %s - Step 7: Border-leaf Renders ===", SCENARIO_NAME)

        topology = await fetch_dc_topology(client=async_client_main, branch=scenario_branch, dc_name=LISP_DC)
        borders = _devices_of_role(topology["devices"], "border-leaf")
        assert len(borders) == LISP_EXPECTED_ROLE_COUNTS["border-leaf"], f"border-leafs: {borders}"

        errors: list[str] = []
        for device in borders:
            rendered = await render_device_config(
                async_client_main, scenario_branch, device, query="border_leaf_config", transform_cls=BorderLeaf
            )
            expected = {
                "router lisp": re.search(r"^router lisp$", rendered, re.MULTILINE),
                f"site {LISP_DC}": re.search(rf"^\s+site {LISP_DC}$", rendered, re.MULTILINE),
                "map-server": re.search(r"^\s+map-server$", rendered, re.MULTILINE),
                "authentication-key": re.search(r"^\s+authentication-key \S+$", rendered, re.MULTILINE),
                "OSPF underlay": re.search(r"^router ospf ", rendered, re.MULTILINE),
            }
            errors += [f"{device}: no '{what}'" for what, found in expected.items() if not found]
            for instance_id in LISP_EXPECTED_L3_INSTANCE_IDS:
                if not re.search(
                    rf"^\s+eid-record instance-id {instance_id} \S+ accept-more-specifics$", rendered, re.M
                ):
                    errors.append(f"{device}: no L3 eid-record for instance-id {instance_id}")
            errors += [f"{device}: renders '{pattern}'" for pattern in _forbidden_found(rendered)]
        assert not errors, "border-leaf renders:\n" + "\n".join(f"  - {e}" for e in errors)

    @pytest.mark.order(427)
    @pytest.mark.dependency(scope="session", name="lisp_edge_render", depends=["lisp_dc_state"])
    @pytest.mark.asyncio
    async def test_08_edge_devices_render_edge_lisp_without_evpn(
        self, async_client_main: InfrahubClient, scenario_branch: str
    ) -> None:
        """Every leaf and access-leaf renders (the transform does not refuse), carries the OSPF underlay and the edge
        LISP config (``router lisp``, ``etr map-server`` toward every border) and renders no EVPN, VXLAN or BGP.

        The scope hosts no segment (a cisco_xe leaf can render a hosted segment and its SVI ACL, but this test does
        not exercise it, see the scope README), so the edge LISP config has no instance-id yet: hosts would register
        later.
        """
        logging.info("=== %s - Step 8: Leaf and Access-leaf Renders ===", SCENARIO_NAME)

        topology = await fetch_dc_topology(client=async_client_main, branch=scenario_branch, dc_name=LISP_DC)
        errors: list[str] = []
        for role, transform_cls in (("leaf", Leaf), ("access-leaf", AccessLeaf)):
            devices = _devices_of_role(topology["devices"], role)
            assert devices, f"no {role} in {LISP_DC}"
            for device in devices:
                rendered = await render_device_config(
                    async_client_main, scenario_branch, device, query="leaf_config", transform_cls=transform_cls
                )
                if not re.search(r"^router ospf ", rendered, re.MULTILINE):
                    errors.append(f"{device} ({role}): no OSPF underlay")
                if not re.search(r"^router lisp$", rendered, re.MULTILINE):
                    errors.append(f"{device} ({role}): no 'router lisp'")
                if not re.search(r"^\s+etr map-server \S+ key \S+$", rendered, re.MULTILINE):
                    errors.append(f"{device} ({role}): no 'etr map-server ... key'")
                if re.search(r"^\s+map-server$", rendered, re.MULTILINE):
                    errors.append(f"{device} ({role}): renders the map-server role (only borders are map-servers)")
                errors += [f"{device} ({role}): renders '{pattern}'" for pattern in _forbidden_found(rendered)]
        assert not errors, "edge renders:\n" + "\n".join(f"  - {e}" for e in errors)

    @pytest.mark.order(428)
    @pytest.mark.dependency(scope="session", name="lisp_rerun", depends=["lisp_border_render", "lisp_edge_render"])
    @pytest.mark.asyncio
    async def test_09_rerunning_add_dc_changes_nothing(
        self, async_client_main: InfrahubClient, scenario_branch: str
    ) -> None:
        """A second add_dc run keeps every id, address, pool and count, including the site key.

        test_65 does not cover this scope, so the rerun is asserted here with the same snapshot and diff.
        """
        logging.info("=== %s - Step 9: Rerun add_dc ===", SCENARIO_NAME)

        await wait_for_tasks_completion(async_client_main, scenario_branch)
        dc_before = await _fetch_dc(async_client_main, scenario_branch)
        before, labels = await snapshot_topology(async_client_main, scenario_branch)
        for kind in ("DcimDevice", "DcimInterface", "DcimCable", "IpamIPAddress", "RoutingPassword"):
            assert before[kind], f"no {kind} on the branch to compare"

        outcome = await run_generator(
            client=async_client_main, generator_name="add_dc", node_ids=[dc_before["id"]], branch=scenario_branch
        )
        assert outcome["success"], f"add_dc rerun failed: {outcome}"
        await wait_for_tasks_completion(async_client_main, scenario_branch)
        await verify_no_failed_tasks(client=async_client_main, branch=scenario_branch)

        after, labels_after = await snapshot_topology(async_client_main, scenario_branch)
        changes = diff_snapshots(before, after, {**labels_after, **labels})
        assert not changes, f"{len(changes)} object(s) changed on an add_dc rerun:\n  " + "\n  ".join(changes[:50])

        dc_after = await _fetch_dc(async_client_main, scenario_branch)
        key_before = dc_before["lisp_site_key"]["node"]["id"]
        key_after = (dc_after.get("lisp_site_key") or {}).get("node", {}).get("id")
        assert key_after == key_before, f"site key {key_before} became {key_after}"
        assert {kind: len(objects) for kind, objects in after.items()} == {
            kind: len(objects) for kind, objects in before.items()
        }
