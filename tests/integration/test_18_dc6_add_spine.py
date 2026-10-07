"""Integration test - Scenario 5: Extend a DC6 Pod with a New Spine.

Coverage: Verifies that pointing DC6-1-POD-1's spine fabric_templates entry
at a bigger TopologyElement (2 -> 3 spines) triggers the pod generator
automatically (trigger-pod-update-fabric-templates -> run-pod-rack-cascade)
and creates exactly one additional POD-1 spine with cabling and routing,
without touching any other DC6 pod or any existing switch's underlay ASN.

Why a new element instead of bumping the existing one's quantity:
TopologyElement nodes are shared — ["2", "spine", "N9K-C9336C-FX2_SPINE"] is
referenced by several DC6 pods and other DCs, and its HFID contains the
quantity. Mutating it would silently resize every pod that uses it (on the
next run of their generators) and does not update the pod itself, so no pod
trigger fires. Swapping POD-1's relationship to a dedicated 3-spine element is
a real pod mutation, scoped to POD-1 only.

Prerequisites: DC6 merged (Scenario 1), pod added (Scenario 4).

Steps:
1.  Create branch, upsert a 3-spine element, and swap it into POD-1's fabric_templates
2.  Wait for event-triggered generators to complete and verify no failures
3.  Verify exactly one new POD-1 spine, other pods unchanged, ASNs stable
4.  Create proposed change
5.  Wait for validations, verify diff and artifacts
6.  Merge to main
7.  Verify in main
"""

import json
import logging
from typing import Any

import pytest
from infrahub_sdk import InfrahubClient, InfrahubClientSync

from .conftest import TestInfrahubDockerWithClient
from .test_constants import SCENARIO_DC_NAME, SCENARIO_POD_1, SCENARIO_UNDERLAY_ROLES
from .test_helpers import (
    fetch_artifacts,
    fetch_proposed_change_diff,
    fetch_underlay_asn_drift,
    snapshot_dc_device_counts_by_role,
    snapshot_underlay_asn_by_roles,
)
from .workflow_helpers import (
    create_and_validate_proposed_change,
    merge_proposed_change,
    verify_no_failed_tasks,
    wait_for_tasks_completion,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

SCENARIO_NAME = "Scenario 5: Add Spine to Pod"
BRANCH_NAME = "dc6-add-spine"

# M_MIDDLE caps a pod at 3 spines (generators/pod_config.py), so POD-1 can grow by one.
SPINES_ADDED = 1

QUERY_POD_FABRIC_TEMPLATES = """
query GetPodFabricTemplates($pod_name: String!) {
    TopologyPod(name__value: $pod_name) {
        edges {
            node {
                id
                fabric_templates {
                    edges {
                        node {
                            id
                            role { value }
                            quantity { value }
                            template { node { id } }
                        }
                    }
                }
            }
        }
    }
}
"""

MUTATION_UPSERT_ELEMENT = """
mutation UpsertSpineElement($quantity: BigInt!, $template_id: String!) {
    TopologyElementUpsert(
        data: {
            quantity: { value: $quantity }
            role: { value: "spine" }
            template: { id: $template_id }
        }
    ) { ok object { id } }
}
"""


def _assert_pod_spine_delta(before: dict[str, list[str]], after: dict[str, list[str]]) -> None:
    """Assert POD-1 gained exactly SPINES_ADDED spines and every other DC6 pod is unchanged."""
    pod1_before = before.get(SCENARIO_POD_1, [])
    pod1_after = after.get(SCENARIO_POD_1, [])
    assert len(pod1_after) == len(pod1_before) + SPINES_ADDED, (
        f"Expected exactly {SPINES_ADDED} new {SCENARIO_POD_1} spine(s) after swapping its spine element.\n"
        f"  before: {pod1_before}\n"
        f"  after: {pod1_after}"
    )
    assert set(pod1_before) <= set(pod1_after), (
        f"Existing {SCENARIO_POD_1} spines disappeared: {sorted(set(pod1_before) - set(pod1_after))}"
    )

    changed = {
        pod: (spines, after.get(pod, []))
        for pod, spines in before.items()
        if pod != SCENARIO_POD_1 and after.get(pod, []) != spines
    }
    assert not changed, (
        f"Spines of other {SCENARIO_DC_NAME} pods changed while updating {SCENARIO_POD_1}:\n"
        + "\n".join(f"  - {pod}: {old} -> {new}" for pod, (old, new) in sorted(changed.items()))
    )


class TestDC6AddSpine(TestInfrahubDockerWithClient):
    """Test extending an existing DC6 pod with an additional spine."""

    @pytest.fixture(scope="class")
    def scenario_branch(self) -> str:
        return BRANCH_NAME

    @pytest.mark.order(230)
    @pytest.mark.dependency(scope="session", name="dc6_add_spine_load", depends=["dc6_add_pod_merge"])
    @pytest.mark.asyncio
    async def test_01_update_pod_spine_count(
        self,
        async_client_main: InfrahubClient,
        client_main: InfrahubClientSync,
        workflow_state: dict[str, Any],
        scenario_branch: str,
    ) -> None:
        """Create branch and point POD-1's spine fabric_templates entry at a 3-spine element."""
        logging.info("=== %s - Step 1: Update Pod Spine Count ===", SCENARIO_NAME)

        # Create branch
        existing = client_main.branch.all()
        if scenario_branch not in existing:
            client_main.branch.create(
                branch_name=scenario_branch,
                sync_with_git=False,
                wait_until_completion=True,
            )
            logging.info("Created branch: %s", scenario_branch)

        before = await self._snapshot_spines_by_pod(async_client_main, scenario_branch, SCENARIO_DC_NAME)
        assert before.get(SCENARIO_POD_1), f"{SCENARIO_POD_1} has no spines before the update: {before}"
        workflow_state["dc6_add_spine_before"] = before
        workflow_state["dc6_add_spine_counts_before"] = await snapshot_dc_device_counts_by_role(
            client=async_client_main,
            branch=scenario_branch,
            dc_name=SCENARIO_DC_NAME,
            roles=["spine"],
        )
        workflow_state["dc6_add_spine_asn_baseline"] = await snapshot_underlay_asn_by_roles(
            client=async_client_main,
            branch=scenario_branch,
            dc_name=SCENARIO_DC_NAME,
            roles=SCENARIO_UNDERLAY_ROLES,
        )
        logging.info("Baseline spines before update: %s", {pod: len(spines) for pod, spines in before.items()})

        # Find POD-1 and its spine-role fabric_templates (TopologyElement) entry
        async_client_main.default_branch = scenario_branch
        result = await async_client_main.execute_graphql(
            query=QUERY_POD_FABRIC_TEMPLATES,
            variables={"pod_name": SCENARIO_POD_1},
        )
        pods = result["TopologyPod"]["edges"]
        assert pods, f"{SCENARIO_POD_1} not found on branch '{scenario_branch}'"
        pod = pods[0]["node"]

        elements = [edge["node"] for edge in pod["fabric_templates"]["edges"]]
        spine_entries = [element for element in elements if element["role"]["value"] == "spine"]
        assert len(spine_entries) == 1, (
            f"Expected exactly 1 spine fabric_templates entry on {SCENARIO_POD_1}, found {len(spine_entries)}"
        )
        spine_element = spine_entries[0]
        current_spines = int(spine_element["quantity"]["value"])
        new_spine_count = current_spines + SPINES_ADDED

        # Upsert a dedicated element (the existing one is shared with other pods)
        upsert = await async_client_main.execute_graphql(
            query=MUTATION_UPSERT_ELEMENT,
            variables={"quantity": new_spine_count, "template_id": spine_element["template"]["node"]["id"]},
        )
        new_element_id = upsert["TopologyElementUpsert"]["object"]["id"]

        # Swap it into POD-1, keeping every non-spine entry as-is. Updating the
        # pod's fabric_templates relationship is what fires the cascade trigger.
        fabric_template_ids = [element["id"] for element in elements if element["id"] != spine_element["id"]]
        fabric_template_ids.append(new_element_id)
        related = ", ".join(f"{{ id: {json.dumps(element_id)} }}" for element_id in fabric_template_ids)
        mutation = f"""
        mutation UpdatePodFabricTemplates($pod_id: String!) {{
            TopologyPodUpdate(data: {{ id: $pod_id, fabric_templates: [{related}] }}) {{ ok }}
        }}
        """
        update = await async_client_main.execute_graphql(query=mutation, variables={"pod_id": pod["id"]})
        assert update["TopologyPodUpdate"]["ok"], f"Failed to update {SCENARIO_POD_1} fabric_templates: {update}"

        logging.info("Updated %s spines: %d -> %d", SCENARIO_POD_1, current_spines, new_spine_count)

    @pytest.mark.order(231)
    @pytest.mark.dependency(scope="session", name="dc6_add_spine_wait_tasks", depends=["dc6_add_spine_load"])
    @pytest.mark.asyncio
    async def test_02_wait_for_tasks(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Wait for event-triggered generators to complete."""
        logging.info("=== %s - Step 2: Wait for Tasks ===", SCENARIO_NAME)

        await wait_for_tasks_completion(async_client_main, scenario_branch)

        logging.info("All tasks completed")

    @pytest.mark.order(232)
    @pytest.mark.dependency(scope="session", name="dc6_add_spine_no_failures", depends=["dc6_add_spine_wait_tasks"])
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

    @pytest.mark.order(233)
    @pytest.mark.dependency(scope="session", name="dc6_add_spine_verify_devices", depends=["dc6_add_spine_no_failures"])
    @pytest.mark.asyncio
    async def test_03_verify_devices_created(
        self,
        async_client_main: InfrahubClient,
        workflow_state: dict[str, Any],
        scenario_branch: str,
    ) -> None:
        """Verify exactly one new POD-1 spine, no other pod touched, and every existing ASN kept."""
        logging.info("=== %s - Step 3: Verify Devices ===", SCENARIO_NAME)

        after = await self._snapshot_spines_by_pod(async_client_main, scenario_branch, SCENARIO_DC_NAME)
        _assert_pod_spine_delta(workflow_state["dc6_add_spine_before"], after)

        counts_before = workflow_state["dc6_add_spine_counts_before"]
        counts_after = await snapshot_dc_device_counts_by_role(
            client=async_client_main,
            branch=scenario_branch,
            dc_name=SCENARIO_DC_NAME,
            roles=["spine"],
        )
        assert counts_after["spine"] == counts_before["spine"] + SPINES_ADDED, (
            f"Expected {SCENARIO_DC_NAME} spine count {counts_before['spine']} + {SPINES_ADDED}, "
            f"got {counts_after['spine']}"
        )

        errors = await fetch_underlay_asn_drift(
            client=async_client_main,
            branch=scenario_branch,
            dc_name=SCENARIO_DC_NAME,
            baseline=workflow_state["dc6_add_spine_asn_baseline"],
        )
        assert not errors, f"Underlay ASN stability check failed on branch '{scenario_branch}':\n" + "\n".join(
            f"  - {e}" for e in errors
        )

        logging.info("%s spines after update: %s", SCENARIO_POD_1, after.get(SCENARIO_POD_1, []))

    @pytest.mark.order(234)
    @pytest.mark.dependency(
        scope="session",
        name="dc6_add_spine_create_pc",
        depends=["dc6_add_spine_verify_devices", "dc6_add_spine_no_failures"],
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
        workflow_state["dc6_add_spine_pc_id"] = pc_id
        workflow_state["dc6_add_spine_validations"] = pc_result["validations"]
        logging.info("Proposed change created: %s", pc_id)

    @pytest.mark.order(235)
    @pytest.mark.dependency(scope="session", name="dc6_add_spine_validate", depends=["dc6_add_spine_create_pc"])
    def test_05_wait_for_validations(self, workflow_state: dict[str, Any]) -> None:
        """Wait for validations."""
        logging.info("=== %s - Step 5: Wait for Validations ===", SCENARIO_NAME)

        validations = workflow_state["dc6_add_spine_validations"]
        logging.info("Validations completed: %d checks", len(validations))

    @pytest.mark.order(235)
    @pytest.mark.dependency(scope="session", name="dc6_add_spine_verify_diff", depends=["dc6_add_spine_validate"])
    @pytest.mark.asyncio
    async def test_05b_verify_proposed_change_diff(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Verify the proposed change diff contains the new spine and the pod update."""
        logging.info("=== %s - Step 5b: Verify PC Diff ===", SCENARIO_NAME)

        result = await fetch_proposed_change_diff(client=async_client_main, branch=scenario_branch)

        expected_counts = {
            "DcimPhysicalDevice": {"added": SPINES_ADDED},
            "TopologyPod": {"updated": 1},
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

    @pytest.mark.order(235)
    @pytest.mark.dependency(scope="session", name="dc6_add_spine_verify_artifacts", depends=["dc6_add_spine_validate"])
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

    @pytest.mark.order(236)
    @pytest.mark.dependency(scope="session", name="dc6_add_spine_merge", depends=["dc6_add_spine_verify_diff"])
    def test_06_merge(
        self,
        client_main: InfrahubClientSync,
        workflow_state: dict[str, Any],
    ) -> None:
        """Merge to main."""
        logging.info("=== %s - Step 6: Merge ===", SCENARIO_NAME)

        result = merge_proposed_change(
            client=client_main,
            pc_id=workflow_state["dc6_add_spine_pc_id"],
        )

        assert result["success"], (
            f"Merge failed.\n"
            f"  PC state: {result['pc_state_before']} -> {result['pc_state_after']}\n"
            f"  Task state: {result['task_state']}"
        )

        logging.info("Merge completed successfully")

    @pytest.mark.order(237)
    @pytest.mark.dependency(scope="session", name="dc6_add_spine_verify_main", depends=["dc6_add_spine_merge"])
    @pytest.mark.asyncio
    async def test_07_verify_in_main(
        self,
        async_client_main: InfrahubClient,
        workflow_state: dict[str, Any],
    ) -> None:
        """Verify the extra POD-1 spine is in main and no other pod changed."""
        logging.info("=== %s - Step 7: Verify in Main ===", SCENARIO_NAME)

        after = await self._snapshot_spines_by_pod(async_client_main, "main", SCENARIO_DC_NAME)
        _assert_pod_spine_delta(workflow_state["dc6_add_spine_before"], after)

        logging.info("Spines in main: %s", {pod: len(spines) for pod, spines in after.items()})
        logging.info("=== %s - COMPLETED ===", SCENARIO_NAME)
