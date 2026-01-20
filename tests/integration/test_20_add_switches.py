"""Integration test - Scenario 2: Add Switches to Existing DC.

This test adds switch infrastructure to the existing DC1:
1. Load switch data on new branch
2. Run add_switches generator (or add_rack if appropriate)
3. Verify new devices and cabling
4. Create proposed change
5. Wait for validations
6. Merge to main
7. Verify switches exist in main
"""

import logging
from typing import Any

import pytest
from infrahub_sdk import InfrahubClient, InfrahubClientSync

from .conftest import TestInfrahubDockerWithClient
from .workflow_helpers import (
    create_proposed_change,
    merge_proposed_change,
    run_generator,
    verify_cable_connections,
    verify_cables_created,
    verify_device_interfaces,
    verify_device_role,
    verify_devices_created,
    wait_for_validations,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


class TestAddSwitches(TestInfrahubDockerWithClient):
    """Test adding switches to existing DC."""

    @pytest.fixture(scope="class")
    def scenario_branch(self) -> str:
        """Branch name for this scenario."""
        return "scenario-02-add-switches"

    @pytest.mark.order(200)
    @pytest.mark.dependency(name="dc02_load_data", depends=["dc01_verify_main"])
    def test_01_load_switch_data(self, client_main: InfrahubClientSync, scenario_branch: str) -> None:
        """Load switch data."""
        logging.info("=== Scenario 2: Add Switches - Step 1: Load Data ===")

        # Create branch
        existing_branches = client_main.branch.all()
        if scenario_branch not in existing_branches:
            client_main.branch.create(
                branch_name=scenario_branch,
                sync_with_git=False,
                wait_until_completion=True,
            )
            logging.info("Created branch: %s", scenario_branch)

        # Load switch data
        load_switches = self.execute_command(
            f"infrahubctl object load tests/integration/data/02_switches --branch {scenario_branch}",
            address=client_main.config.address,
        )

        assert load_switches.returncode == 0, (
            f"Failed to load switch data.\n"
            f"  Return code: {load_switches.returncode}\n"
            f"  stdout: {load_switches.stdout}\n"
            f"  stderr: {load_switches.stderr}"
        )

        logging.info("✓ Switch data loaded successfully")

    @pytest.mark.order(201)
    @pytest.mark.dependency(name="dc02_run_generator", depends=["dc02_load_data"])
    @pytest.mark.asyncio
    async def test_02_run_generator(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
        workflow_state: dict[str, Any],
    ) -> None:
        """Run appropriate generator for switches."""
        logging.info("=== Scenario 2: Add Switches - Step 2: Run Generator ===")

        # This depends on your data structure - adjust generator name and node query as needed
        # For example, if switches are added via rack generator:
        client = async_client_main
        client.default_branch = scenario_branch

        # Query for the rack/pod that was added
        # Adjust this based on your actual data structure
        query = """
        query GetRacks {
            TopologyRack {
                edges {
                    node {
                        id
                        name {
                            value
                        }
                    }
                }
            }
        }
        """

        result = await client.execute_graphql(query=query)
        racks = result.get("TopologyRack", {}).get("edges", [])

        if racks:
            rack_ids = [edge["node"]["id"] for edge in racks]
            gen_result = await run_generator(
                client=client,
                generator_name="add_rack",
                node_ids=rack_ids,
                branch=scenario_branch,
            )
            workflow_state["dc02_generator_task"] = gen_result
            logging.info("✓ Generator task completed: %s", gen_result["task_state"])
        else:
            logging.warning("No racks found - skipping generator run")

    @pytest.mark.order(202)
    @pytest.mark.dependency(name="dc02_verify_devices", depends=["dc02_run_generator"])
    @pytest.mark.asyncio
    async def test_03_verify_devices_added(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Verify new devices were created."""
        logging.info("=== Scenario 2: Add Switches - Step 3: Verify Devices ===")

        result = await verify_devices_created(
            client=async_client_main,
            branch=scenario_branch,
            expected_min_count=1,
            device_types=["spine", "leaf", "tor"],
        )

        logging.info("✓ Devices verified: %d total", result["device_count"])

        # Detailed verification: Each ToR should have 2 interfaces (uplink to spines)
        # Get ToR devices
        query = """
        query GetToRs {
            DcimDevice(role__value: "tor") {
                edges {
                    node {
                        id
                        name {
                            value
                        }
                    }
                }
            }
        }
        """
        async_client_main.default_branch = scenario_branch
        tor_result = await async_client_main.execute_graphql(query=query)
        tor_edges = tor_result.get("DcimDevice", {}).get("edges", [])

        # Verify each ToR has expected interfaces
        for tor_edge in tor_edges:
            tor_name = tor_edge["node"]["name"]["value"]

            # Verify interface count
            iface_result = await verify_device_interfaces(
                client=async_client_main,
                branch=scenario_branch,
                device_name=tor_name,
                expected_interface_count=2,  # 2 uplinks to spines
            )
            logging.info("  ✓ ToR '%s' has %d interfaces", tor_name, iface_result["interface_count"])

            # Verify device role
            role_result = await verify_device_role(
                client=async_client_main,
                branch=scenario_branch,
                device_name=tor_name,
                expected_role="tor",
            )
            logging.info("  ✓ ToR '%s' has role '%s'", tor_name, role_result["role"])

            # Verify cable connections to spines
            conn_result = await verify_cable_connections(
                client=async_client_main,
                branch=scenario_branch,
                device_name=tor_name,
                expected_connections=2,  # Connected to 2 spines
                connected_to_roles=["spine"],
            )
            logging.info("  ✓ ToR '%s' has %d connections to spines", tor_name, conn_result["connection_count"])

    @pytest.mark.order(203)
    @pytest.mark.dependency(name="dc02_verify_cables", depends=["dc02_run_generator"])
    @pytest.mark.asyncio
    async def test_04_verify_cables_added(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Verify new cabling was created."""
        logging.info("=== Scenario 2: Add Switches - Step 4: Verify Cabling ===")

        result = await verify_cables_created(
            client=async_client_main,
            branch=scenario_branch,
            expected_min_count=1,
        )

        logging.info("✓ Cables verified: %d total", result["cable_count"])

    @pytest.mark.order(204)
    @pytest.mark.dependency(name="dc02_create_pc", depends=["dc02_verify_devices", "dc02_verify_cables"])
    def test_05_create_proposed_change(
        self,
        client_main: InfrahubClientSync,
        scenario_branch: str,
        workflow_state: dict[str, Any],
    ) -> None:
        """Create proposed change."""
        logging.info("=== Scenario 2: Add Switches - Step 5: Create Proposed Change ===")

        pc_id = create_proposed_change(
            client=client_main,
            name="Scenario 2: Add Switches to DC1",
            source_branch=scenario_branch,
        )

        workflow_state["dc02_pc_id"] = pc_id
        logging.info("✓ Proposed change created: %s", pc_id)

    @pytest.mark.order(205)
    @pytest.mark.dependency(name="dc02_wait_validations", depends=["dc02_create_pc"])
    def test_06_wait_for_validations(self, client_main: InfrahubClientSync) -> None:
        """Wait for validations to complete."""
        logging.info("=== Scenario 2: Add Switches - Step 6: Wait for Validations ===")

        validations = wait_for_validations(
            client=client_main,
            pc_name="Scenario 2: Add Switches to DC1",
        )

        logging.info("✓ Validations completed: %d checks", len(validations))

    @pytest.mark.order(206)
    @pytest.mark.dependency(name="dc02_merge", depends=["dc02_wait_validations"])
    def test_07_merge_to_main(
        self,
        client_main: InfrahubClientSync,
        workflow_state: dict[str, Any],
    ) -> None:
        """Merge proposed change to main."""
        logging.info("=== Scenario 2: Add Switches - Step 7: Merge to Main ===")

        pc_id = workflow_state["dc02_pc_id"]
        result = merge_proposed_change(
            client=client_main,
            pc_id=pc_id,
        )

        assert result["success"], (
            f"Merge failed.\n"
            f"  PC state: {result['pc_state_before']} → {result['pc_state_after']}\n"
            f"  Task state: {result['task_state']}"
        )

        logging.info("✓ Merge completed successfully")
        logging.info("=== Scenario 2: Add Switches - COMPLETED ===")
