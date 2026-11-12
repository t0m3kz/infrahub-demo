"""Integration test - Scenario 7: Add Endpoints to DC6."""

import logging
from pathlib import Path
from typing import Any

import pytest
import yaml
from infrahub_sdk import InfrahubClient, InfrahubClientSync

from .branch_helpers import fetch_artifacts, fetch_proposed_change_diff
from .conftest import TestInfrahubDockerWithClient
from .dc_topology_helpers import snapshot_dc_device_counts_by_role
from .endpoint_app_helpers import fetch_endpoint_cabling
from .segment_helpers import fetch_segment_ports, fetch_segment_vlans
from .suite_constants import (
    DEMO_SERVERS_DATA,
    SCENARIO_DC_NAME,
    SCENARIO_ENDPOINT_APP_DATA,
    SCENARIO_ENDPOINT_COMPONENT_FQDN,
    SCENARIO_ENDPOINT_SEGMENT,
    SCENARIO_ENDPOINT_SERVER,
)
from .workflow_helpers import (
    create_and_validate_proposed_change,
    merge_proposed_change,
    run_generator,
    verify_no_failed_tasks,
    wait_for_tasks_completion,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

SCENARIO_NAME = "Scenario 7: Add Endpoints"
BRANCH_NAME = "dc6-add-endpoints"

# The segment by name: its ports are read from the ports (segment_helpers.fetch_segment_ports).
SEGMENT_ID_QUERY = """
query ($name: String!) {
  ManagedVxlanSegment(name__value: $name) {
    edges { node { id } }
  }
}
"""


def _port_name(port: Any) -> str:
    """The name of an SDK interface node (its attributes are dynamic)."""
    return str(getattr(port, "name").value)


def _peers(node: Any, relationship: str) -> list[Any]:
    """The peers of an SDK node's relationship, loaded with ``include=[relationship]``."""
    return list(getattr(node, relationship).peers)


def _load_server_names(root_dir: Path) -> list[str]:
    """Return the names of every DcimPhysicalDevice declared under DEMO_SERVERS_DATA."""
    names: list[str] = []
    for path in sorted((root_dir / DEMO_SERVERS_DATA).rglob("*.yml")):
        for document in yaml.safe_load_all(path.read_text(encoding="utf-8")):
            spec = (document or {}).get("spec") or {}
            if spec.get("kind") == "DcimPhysicalDevice":
                names.extend(str(item["name"]) for item in spec.get("data") or [])
    return names


async def _assert_endpoint_growth(
    client: InfrahubClient,
    branch: str,
    baseline: int,
    expected_growth: int,
) -> None:
    """Assert DC6 gained at least expected_growth endpoint devices over baseline."""
    counts = await snapshot_dc_device_counts_by_role(
        client=client,
        branch=branch,
        dc_name=SCENARIO_DC_NAME,
        roles=["endpoint"],
    )
    assert counts["endpoint"] >= baseline + expected_growth, (
        f"Expected {SCENARIO_DC_NAME} endpoint count >= {baseline} + {expected_growth}, "
        f"got {counts['endpoint']} on branch '{branch}'"
    )
    logging.info("%s endpoints on branch '%s': %d -> %d", SCENARIO_DC_NAME, branch, baseline, counts["endpoint"])


class TestDC6AddEndpoints(TestInfrahubDockerWithClient):
    """Test adding endpoint servers to DC6 racks."""

    @pytest.fixture(scope="class")
    def scenario_branch(self) -> str:
        return BRANCH_NAME

    @pytest.fixture(scope="class")
    def server_names(self, root_dir: Path) -> list[str]:
        names = _load_server_names(root_dir)
        assert names, f"No DcimPhysicalDevice found under {DEMO_SERVERS_DATA}"
        return names

    # ------------------------------------------------------------------
    # Step 0: Snapshot baseline
    # ------------------------------------------------------------------

    @pytest.mark.order(250)
    @pytest.mark.dependency(scope="session", name="dc6_add_ep_snapshot", depends=["dc6_seg_merge"])
    @pytest.mark.asyncio
    async def test_00_snapshot_baseline(
        self,
        async_client_main: InfrahubClient,
        workflow_state: dict[str, Any],
    ) -> None:
        """Snapshot the DC6 endpoint count on main before loading servers."""
        logging.info("=== %s - Step 0: Snapshot Baseline ===", SCENARIO_NAME)

        counts = await snapshot_dc_device_counts_by_role(
            client=async_client_main,
            branch="main",
            dc_name=SCENARIO_DC_NAME,
            roles=["endpoint"],
        )
        workflow_state["dc6_add_ep_baseline"] = counts["endpoint"]
        logging.info("Baseline %s endpoints: %d", SCENARIO_DC_NAME, counts["endpoint"])

    # ------------------------------------------------------------------
    # Step 1: Load data
    # ------------------------------------------------------------------

    @pytest.mark.order(250)
    @pytest.mark.dependency(scope="session", name="dc6_add_ep_load", depends=["dc6_add_ep_snapshot"])
    def test_01_load_endpoint_data(
        self,
        client_main: InfrahubClientSync,
        scenario_branch: str,
    ) -> None:
        """Create branch and load endpoint data (device type, compute racks, servers)."""
        logging.info("=== %s - Step 1: Load Data ===", SCENARIO_NAME)

        existing_branches = client_main.branch.all()
        if scenario_branch not in existing_branches:
            client_main.branch.create(
                branch_name=scenario_branch,
                sync_with_git=False,
                wait_until_completion=True,
            )
            logging.info("Created branch: %s", scenario_branch)

        load_result = self.execute_command(
            f"infrahubctl object load {DEMO_SERVERS_DATA} --branch {scenario_branch}",
            address=client_main.config.address,
        )

        assert load_result.returncode == 0, (
            f"Failed to load endpoint data.\n"
            f"  Return code: {load_result.returncode}\n"
            f"  stdout: {load_result.stdout}\n"
            f"  stderr: {load_result.stderr}"
        )

        logging.info("Endpoint data loaded successfully")

    # ------------------------------------------------------------------
    # Step 2: Wait for event-triggered generators
    # ------------------------------------------------------------------

    @pytest.mark.order(251)
    @pytest.mark.dependency(scope="session", name="dc6_add_ep_wait_tasks", depends=["dc6_add_ep_load"])
    @pytest.mark.asyncio
    async def test_02_wait_for_tasks(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Wait for event-triggered endpoint generators to complete."""
        logging.info("=== %s - Step 2: Wait for Tasks ===", SCENARIO_NAME)

        await wait_for_tasks_completion(async_client_main, scenario_branch)

        logging.info("All tasks completed")

    @pytest.mark.order(252)
    @pytest.mark.dependency(scope="session", name="dc6_add_ep_no_failures", depends=["dc6_add_ep_wait_tasks"])
    @pytest.mark.asyncio
    async def test_02b_verify_no_failed_tasks(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Verify no tasks failed during generator execution."""
        logging.info("=== %s - Step 2b: Verify No Failed Tasks ===", SCENARIO_NAME)

        await verify_no_failed_tasks(
            client=async_client_main,
            branch=scenario_branch,
        )

        logging.info("No failed tasks found")

    # ------------------------------------------------------------------
    # Step 3: Verify endpoint devices and cabling
    # ------------------------------------------------------------------

    @pytest.mark.order(253)
    @pytest.mark.dependency(scope="session", name="dc6_add_ep_verify_devices", depends=["dc6_add_ep_no_failures"])
    @pytest.mark.asyncio
    async def test_03_verify_devices_created(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
        server_names: list[str],
        workflow_state: dict[str, Any],
    ) -> None:
        """Verify DC6 gained every loaded server and each one is cabled to the fabric."""
        logging.info("=== %s - Step 3: Verify Devices ===", SCENARIO_NAME)

        await _assert_endpoint_growth(
            async_client_main,
            scenario_branch,
            baseline=workflow_state["dc6_add_ep_baseline"],
            expected_growth=len(server_names),
        )

        hosts = {host["name"]: host for host in await fetch_endpoint_cabling(async_client_main, scenario_branch)}
        missing = sorted(name for name in server_names if name not in hosts)
        uncabled = sorted(name for name in server_names if name in hosts and not hosts[name]["links"])
        assert not missing and not uncabled, (
            f"Endpoint connectivity check failed on branch '{scenario_branch}'.\n"
            f"  Servers not found: {missing}\n"
            f"  Servers without any uplink cable: {uncabled}"
        )

        logging.info("All %d servers present and cabled", len(server_names))

    # ------------------------------------------------------------------
    # Step 3b-3d: AppComponent on a cabled server -> segment realized
    # ------------------------------------------------------------------

    @pytest.mark.order(253)
    @pytest.mark.dependency(scope="session", name="dc6_add_ep_load_app", depends=["dc6_add_ep_verify_devices"])
    @pytest.mark.asyncio
    async def test_03b_load_app_component(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Load an application whose one component sits on a DC6 local segment."""
        logging.info("=== %s - Step 3b: Load AppComponent ===", SCENARIO_NAME)

        load_result = self.execute_command(
            f"infrahubctl object load {SCENARIO_ENDPOINT_APP_DATA} --branch {scenario_branch}",
            address=async_client_main.config.address,
        )
        assert load_result.returncode == 0, (
            f"Failed to load application data.\n"
            f"  Return code: {load_result.returncode}\n"
            f"  stdout: {load_result.stdout}\n"
            f"  stderr: {load_result.stderr}"
        )
        # The AppApplication created-trigger dispatches add_app_application.
        await wait_for_tasks_completion(async_client_main, scenario_branch)
        logging.info("Application data loaded")

    @pytest.mark.order(253)
    @pytest.mark.dependency(scope="session", name="dc6_add_ep_component_segment", depends=["dc6_add_ep_load_app"])
    @pytest.mark.asyncio
    async def test_03c_run_component_segment_generator(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Run add_app_component_segment over the component."""
        logging.info("=== %s - Step 3c: Run add_app_component_segment ===", SCENARIO_NAME)

        component = await async_client_main.get(
            kind="AppComponent", fqdn__value=SCENARIO_ENDPOINT_COMPONENT_FQDN, branch=scenario_branch
        )
        outcome = await run_generator(
            client=async_client_main,
            generator_name="add_app_component_segment",
            node_ids=[component.id],
            branch=scenario_branch,
        )
        assert outcome["success"], f"add_app_component_segment failed: {outcome}"
        await wait_for_tasks_completion(async_client_main, scenario_branch)
        await verify_no_failed_tasks(client=async_client_main, branch=scenario_branch)

    @pytest.mark.order(253)
    @pytest.mark.dependency(
        scope="session", name="dc6_add_ep_verify_segment_vlan", depends=["dc6_add_ep_component_segment"]
    )
    @pytest.mark.asyncio
    async def test_03d_verify_segment_realized_on_server_switches(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """The segment gets a local VLAN ID on the server's switch VLAN domain(s)"""
        logging.info("=== %s - Step 3d: Verify VLAN Domain Segments ===", SCENARIO_NAME)

        result = await async_client_main.execute_graphql(
            query=SEGMENT_ID_QUERY, variables={"name": SCENARIO_ENDPOINT_SEGMENT}, branch_name=scenario_branch
        )
        segments = result["ManagedVxlanSegment"]["edges"]
        assert segments, f"Segment '{SCENARIO_ENDPOINT_SEGMENT}' not found on '{scenario_branch}'"
        segment = segments[0]["node"]

        segment_vlan_result = await fetch_segment_vlans(
            client=async_client_main, branch=scenario_branch, expected_count=1, segment_id=segment["id"]
        )
        records = segment_vlan_result["records"]
        assert records, (
            f"No ManagedSegmentVlan for '{SCENARIO_ENDPOINT_SEGMENT}' although an AppComponent "
            f"instance ({SCENARIO_ENDPOINT_SERVER}) is cabled to the fabric"
        )
        without_vlan = [r for r in records if r["vlan_id"] is None]
        assert not without_vlan, f"VLAN domain segment(s) without a VLAN ID: {without_vlan}"

        hosts = {host["name"]: host for host in await fetch_endpoint_cabling(async_client_main, scenario_branch)}
        cabled_switches = {link["peer_device"] for link in hosts[SCENARIO_ENDPOINT_SERVER]["links"]}
        tagged = [
            (port["device"], port["name"])
            for port in await fetch_segment_ports(async_client_main, scenario_branch, segment["id"])
        ]
        tagged_switches = {device for device, _ in tagged}
        assert tagged, f"No switch interface carries '{SCENARIO_ENDPOINT_SEGMENT}'"
        assert tagged_switches == cabled_switches, (
            f"'{SCENARIO_ENDPOINT_SEGMENT}' is on interfaces of {sorted(tagged_switches)}, expected exactly the "
            f"switches {SCENARIO_ENDPOINT_SERVER} is cabled to: {sorted(cabled_switches)} (tagged: {sorted(tagged)})"
        )
        logging.info(
            "Segment %s: %d row(s), tagged %s",
            SCENARIO_ENDPOINT_SEGMENT,
            len(records),
            sorted(tagged),
        )

    @pytest.mark.order(253)
    @pytest.mark.dependency(
        scope="session", name="dc6_add_ep_assigned_port", depends=["dc6_add_ep_verify_segment_vlan"]
    )
    @pytest.mark.asyncio
    async def test_03e_a_port_assigned_by_a_person_is_realized_and_withdrawn(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """A port a person assigns to the segment's deployment (the request) becomes a member of its switch's row."""
        logging.info("=== %s - Step 3e: A port assigned by a person is realized and withdrawn ===", SCENARIO_NAME)

        client = async_client_main
        client.default_branch = scenario_branch
        result = await client.execute_graphql(
            query=SEGMENT_ID_QUERY, variables={"name": SCENARIO_ENDPOINT_SEGMENT}, branch_name=scenario_branch
        )
        segment_id = result["ManagedVxlanSegment"]["edges"][0]["node"]["id"]

        generated = await fetch_segment_ports(client, scenario_branch, segment_id)
        assert generated, f"'{SCENARIO_ENDPOINT_SEGMENT}' has no generated port to compare with"

        hosts = {host["name"]: host for host in await fetch_endpoint_cabling(client, scenario_branch)}
        switch = sorted({link["peer_device"] for link in hosts[SCENARIO_ENDPOINT_SERVER]["links"]})[0]
        candidates = await client.filters(
            kind="DcimPhysicalInterface",
            device__name__value=switch,
            role__value="customer",
            include=["cable", "capabilities"],
            branch=scenario_branch,
        )
        free = sorted(
            (port for port in candidates if not getattr(port.cable, "id", None) and not _peers(port, "capabilities")),
            key=_port_name,
        )
        assert free, f"{switch} has no uncabled customer port left to assign by hand"
        port = free[0]
        # The deployment of the switch's row of the segment
        row_id, deployment_id = next((p["row_id"], p["deployment_id"]) for p in generated if p["device"] == switch)

        async def _reconcile() -> list[dict[str, Any]]:
            outcome = await run_generator(
                client=client, generator_name="add_vxlan_segment", node_ids=[segment_id], branch=scenario_branch
            )
            assert outcome["success"], f"add_vxlan_segment failed: {outcome}"
            await wait_for_tasks_completion(client, scenario_branch)
            await verify_no_failed_tasks(client=client, branch=scenario_branch)
            return await fetch_segment_ports(client, scenario_branch, segment_id)

        await port.add_relationships(relation_to_update="capabilities", related_nodes=[deployment_id])
        after = await _reconcile()
        assert {(p["id"], p["row_id"]) for p in after} == {(p["id"], p["row_id"]) for p in generated} | {
            (port.id, row_id)
        }, f"the assigned port {switch}:{_port_name(port)} is not a member of its switch's row: {after}"

        # Back to the generated set: the assignment is withdrawn and the reconcile removes the membership.
        await port.remove_relationships(relation_to_update="capabilities", related_nodes=[deployment_id])
        final = await _reconcile()
        assert {p["id"] for p in final} == {p["id"] for p in generated}, (
            f"the reconcile kept a port nobody asks for: {final}"
        )

    # ------------------------------------------------------------------
    # Step 4-6: Proposed change, validations, merge
    # ------------------------------------------------------------------

    @pytest.mark.order(254)
    @pytest.mark.dependency(
        scope="session",
        name="dc6_add_ep_create_pc",
        depends=["dc6_add_ep_verify_segment_vlan"],
    )
    def test_04_create_proposed_change(
        self,
        client_main: InfrahubClientSync,
        scenario_branch: str,
        workflow_state: dict[str, Any],
    ) -> None:
        """Create proposed change."""
        logging.info("=== %s - Step 4: Create Proposed Change ===", SCENARIO_NAME)

        pc_result = create_and_validate_proposed_change(
            client=client_main,
            name=SCENARIO_NAME,
            source_branch=scenario_branch,
        )
        pc_id = pc_result["pc_id"]
        workflow_state["dc6_add_ep_pc_id"] = pc_id
        workflow_state["dc6_add_ep_validations"] = pc_result["validations"]
        logging.info("Proposed change created: %s", pc_id)

    @pytest.mark.order(255)
    @pytest.mark.dependency(scope="session", name="dc6_add_ep_validate", depends=["dc6_add_ep_create_pc"])
    def test_05_wait_for_validations(self, workflow_state: dict[str, Any]) -> None:
        """Wait for validations."""
        logging.info("=== %s - Step 5: Wait for Validations ===", SCENARIO_NAME)

        validations = workflow_state["dc6_add_ep_validations"]
        logging.info("Validations completed: %d checks", len(validations))

    @pytest.mark.order(255)
    @pytest.mark.dependency(scope="session", name="dc6_add_ep_verify_diff", depends=["dc6_add_ep_validate"])
    @pytest.mark.asyncio
    async def test_05b_verify_proposed_change_diff(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
        server_names: list[str],
    ) -> None:
        """Verify the proposed change diff contains every server and their cables."""
        logging.info("=== %s - Step 5b: Verify PC Diff ===", SCENARIO_NAME)

        result = await fetch_proposed_change_diff(client=async_client_main, branch=scenario_branch)

        expected_counts = {
            "DcimPhysicalDevice": {"added": len(server_names)},
            "DcimCable": {"added": len(server_names)},
        }
        errors = []
        for kind, action_counts in expected_counts.items():
            for action, expected in action_counts.items():
                actual = result["by_kind"].get(kind, {}).get(action, 0)
                if actual < expected:
                    errors.append(f"{kind}.{action}: expected >= {expected}, got {actual}")
        assert not errors, f"DiffTree verification failed for branch '{scenario_branch}':\n" + "\n".join(
            f"  - {e}" for e in errors
        )

        logging.info("Diff verified: %d nodes changed", result["node_count"])

    @pytest.mark.order(255)
    @pytest.mark.dependency(scope="session", name="dc6_add_ep_verify_artifacts", depends=["dc6_add_ep_validate"])
    @pytest.mark.asyncio
    async def test_05c_verify_artifacts(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Verify artifacts generated in the proposed change."""
        logging.info("=== %s - Step 5c: Verify Artifacts ===", SCENARIO_NAME)

        result = await fetch_artifacts(client=async_client_main, branch=scenario_branch)
        failed = [f"'{art['name']}' for {art['object']}: {art['status']}" for art in result["failed"]]
        assert not failed, "Artifacts not ready:\n" + "\n".join(f"  - {line}" for line in failed)

        logging.info("Artifacts verified: %d total", result["total"])

    @pytest.mark.order(256)
    @pytest.mark.dependency(scope="session", name="dc6_add_ep_merge", depends=["dc6_add_ep_verify_diff"])
    def test_06_merge(
        self,
        client_main: InfrahubClientSync,
        workflow_state: dict[str, Any],
    ) -> None:
        """Merge to main."""
        logging.info("=== %s - Step 6: Merge ===", SCENARIO_NAME)

        result = merge_proposed_change(
            client=client_main,
            pc_id=workflow_state["dc6_add_ep_pc_id"],
        )

        assert result["success"], (
            f"Merge failed.\n"
            f"  PC state: {result['pc_state_before']} -> {result['pc_state_after']}\n"
            f"  Task state: {result['task_state']}"
        )

        logging.info("Merge completed successfully")

    # ------------------------------------------------------------------
    # Step 7: Verify in main
    # ------------------------------------------------------------------

    @pytest.mark.order(257)
    @pytest.mark.dependency(scope="session", name="dc6_add_ep_verify_main", depends=["dc6_add_ep_merge"])
    @pytest.mark.asyncio
    async def test_07_verify_in_main(
        self,
        async_client_main: InfrahubClient,
        server_names: list[str],
        workflow_state: dict[str, Any],
    ) -> None:
        """Verify the DC6 endpoint growth is in main after merge."""
        logging.info("=== %s - Step 7: Verify in Main ===", SCENARIO_NAME)

        await _assert_endpoint_growth(
            async_client_main,
            "main",
            baseline=workflow_state["dc6_add_ep_baseline"],
            expected_growth=len(server_names),
        )

        logging.info("=== %s - COMPLETED ===", SCENARIO_NAME)
