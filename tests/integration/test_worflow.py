"""Integration smoke workflow.

This revives the previously-disabled workflow test with a minimal, reliable set
of assertions:
- load schemas (base + extensions)
- load menu
- load bootstrap objects
- verify a known bootstrap object exists

The test is intentionally opt-in. Enable with either:
- env var: INFRAHUB_RUN_INTEGRATION=1
- pytest flag: --run-integration
"""

from __future__ import annotations

import logging

import pytest
from infrahub_sdk import InfrahubClientSync

from .conftest import TestInfrahubDockerWithClient


pytestmark = pytest.mark.integration


logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


class TestWorkflowSmoke(TestInfrahubDockerWithClient):
	"""Smoke test: bootstrap the demo into a fresh Infrahub."""

	def test_01_load_schemas(self, client_main: InfrahubClientSync) -> None:
		"""Load schemas (base + extensions)."""

		for cmd in (
			"uv run infrahubctl schema load schemas/base --branch main --wait 120",
			"uv run infrahubctl schema load schemas/extensions --branch main --wait 120",
		):
			result = self.execute_command(cmd, address=client_main.config.address)
			logging.info("Schema load stdout: %s", result.stdout)
			logging.info("Schema load stderr: %s", result.stderr)
			assert result.returncode == 0, (
				f"Schema load failed for command: {cmd}\n{result.stdout}\n{result.stderr}"
			)

	def test_02_load_menu(self, client_main: InfrahubClientSync) -> None:
		"""Load menu definitions."""

		cmd = "uv run infrahubctl menu load menu --branch main"
		result = self.execute_command(cmd, address=client_main.config.address)
		logging.info("Menu load stdout: %s", result.stdout)
		logging.info("Menu load stderr: %s", result.stderr)
		assert result.returncode == 0, f"Menu load failed:\n{result.stdout}\n{result.stderr}"

	def test_03_load_bootstrap(self, client_main: InfrahubClientSync) -> None:
		"""Load bootstrap objects (groups/locations/etc)."""

		cmd = "uv run infrahubctl object load data/bootstrap/ --branch main"
		result = self.execute_command(cmd, address=client_main.config.address)
		logging.info("Bootstrap load stdout: %s", result.stdout)
		logging.info("Bootstrap load stderr: %s", result.stderr)
		assert result.returncode == 0, (
			f"Bootstrap load failed:\n{result.stdout}\n{result.stderr}"
		)

	def test_04_verify_bootstrap_group(self, client_main: InfrahubClientSync) -> None:
		"""Verify a known bootstrap object exists."""

		group = client_main.get("CoreStandardGroup", name__value="leafs")
		assert group is not None
		assert group.name.value == "leafs"


# # pyright: reportAttributeAccessIssue=false
# """Integration test for the DC-3 demo workflow.

# This test validates the complete workflow from the README:
# 1. Load schemas
# 2. Load bootstrap data
# 3. Add repository
# 4. Create branch
# 5. Load DC-3 design data
# 6. Run DC generator
# 7. Create proposed change
# 8. Validate and merge

# Note: Pylance warnings about .value attributes are suppressed at file level.
# The Infrahub SDK uses dynamic attribute generation at runtime.
# """

# import logging

# import pytest
# from infrahub_sdk import InfrahubClientSync

# from .conftest import TestInfrahubDockerWithClient

# logging.basicConfig(
#     level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
# )


# class TestDCWorkflow(TestInfrahubDockerWithClient):
#     """Test the complete DC-3 workflow from the demo."""

#     @pytest.fixture(scope="class")
#     def default_branch(self) -> str:
#         """Default branch for testing."""
#         return "add-dc"

#     def test_01_schema_load(self, client_main: InfrahubClientSync) -> None:
#         """Load all schemas into Infrahub."""
#         logging.info("Starting test: test_01_schema_load")

#         load_schemas = self.execute_command(
#             "infrahubctl schema load schemas --wait 60",
#             address=client_main.config.address,
#         )

#         logging.info("Schema load output: %s", load_schemas.stdout)
#         logging.info("Schema load stderr: %s", load_schemas.stderr)

#         # Check that schemas loaded successfully (even if returncode is non-zero due to warnings)
#         assert (
#             "loaded successfully" in load_schemas.stdout or load_schemas.returncode == 0
#         ), f"Schema load failed: {load_schemas.stdout}\n{load_schemas.stderr}"

#     def test_02_load_menu(self, client_main: InfrahubClientSync) -> None:
#         """Load menu definitions."""
#         logging.info("Starting test: test_02_load_menu")

#         load_menu = self.execute_command(
#             "infrahubctl menu load menus/menu-full.yml",
#             address=client_main.config.address,
#         )

#         logging.info("Menu load output: %s", load_menu.stdout)
#         assert load_menu.returncode == 0, (
#             f"Menu load failed: {load_menu.stdout}\n{load_menu.stderr}"
#         )

#     def test_03_load_bootstrap_data(self, client_main: InfrahubClientSync) -> None:
#         """Load bootstrap data."""
#         logging.info("Starting test: test_03_load_bootstrap_data")

#         load_data = self.execute_command(
#             "infrahubctl object load objects/bootstrap",
#             address=client_main.config.address,
#         )

#         logging.info("Bootstrap data load output: %s", load_data.stdout)
#         assert load_data.returncode == 0, (
#             f"Bootstrap data load failed: {load_data.stdout}\n{load_data.stderr}"
#         )

# def test_04_load_security_data(self, client_main: InfrahubClientSync) -> None:
#     """Load security data (optional but good for completeness)."""
#     logging.info("Starting test: test_04_load_security_data")

#     load_security = self.execute_command(
#         "infrahubctl object load objects/security/",
#         address=client_main.config.address,
#     )

#     logging.info("Security data load output: %s", load_security.stdout)
#     assert load_security.returncode == 0, (
#         f"Security data load failed: {load_security.stdout}\n{load_security.stderr}"
#     )

# async def test_05_add_repository(
#     self, async_client_main: InfrahubClient, remote_repos_dir: str
# ) -> None:
#     """Add the demo repository to Infrahub."""
#     logging.info("Starting test: test_05_add_repository")

#     client = async_client_main
#     src_directory = PROJECT_DIRECTORY
#     git_repository = GitRepo(
#         name="demo_repo",
#         src_directory=src_directory,
#         dst_directory=remote_repos_dir,
#     )

#     response = await git_repository.add_to_infrahub(client=client)
#     assert response.get(f"{git_repository.type.value}Create", {}).get("ok"), (
#         f"Failed to add repository: {response}"
#     )

#     repos = await client.all(kind=git_repository.type)
#     assert repos, "No repositories found after adding"

#     # Wait for repository to sync
#     synchronized = False
#     max_attempts, attempts = 60, 0

#     while not synchronized and attempts < max_attempts:
#         repository = await client.get(
#             kind=git_repository.type.value, name__value="demo_repo"
#         )
#         synchronized = repository.sync_status.value == "in-sync"
#         error = "error" in repository.sync_status.value

#         if synchronized or error:
#             break

#         attempts += 1
#         logging.info(
#             "Waiting for repository sync... attempt %d/%d (status: %s)",
#             attempts,
#             max_attempts,
#             repository.sync_status.value,
#         )
#         time.sleep(10)

#     assert synchronized, (
#         f"Repository failed to sync. Status: {repository.sync_status.value}"
#     )
#     logging.info("Repository synchronized successfully")

# def test_06_create_branch(
#     self, client_main: InfrahubClientSync, default_branch: str
# ) -> None:
#     """Create a new branch for the DC-3 deployment."""
#     logging.info(
#         "Starting test: test_06_create_branch - branch: %s", default_branch
#     )

#     # Check if branch already exists
#     existing_branches = client_main.branch.all()
#     if default_branch in existing_branches:
#         logging.info("Branch %s already exists", default_branch)
#     else:
#         client_main.branch.create(default_branch, wait_until_completion=True)
#         logging.info("Created branch: %s", default_branch)

# def test_07_load_dc3_design(
#     self, client_main: InfrahubClientSync, default_branch: str
# ) -> None:
#     """Load DC-3 design data onto the branch."""
#     logging.info("Starting test: test_07_load_dc3_design")

#     load_dc3 = self.execute_command(
#         f"infrahubctl object load objects/dc/dc-arista-s.yml --branch {default_branch}",
#         address=client_main.config.address,
#     )

#     logging.info("DC-3 design load output: %s", load_dc3.stdout)
#     assert load_dc3.returncode == 0, (
#         f"DC-3 design load failed: {load_dc3.stdout}\n{load_dc3.stderr}"
#     )

# async def test_08_verify_dc3_created(
#     self, async_client_main: InfrahubClient, default_branch: str
# ) -> None:
#     """Verify that DC-3 topology object was created."""
#     logging.info("Starting test: test_08_verify_dc3_created")

#     client = async_client_main
#     client.default_branch = default_branch

#     # Wait a moment for data to be fully committed
#     time.sleep(5)

#     # Query for DC-3
#     dc3 = await client.get(
#         kind="TopologyDataCenter",
#         name__value="DC-3",
#         populate_store=True,
#     )

#     assert dc3, "DC-3 topology not found"
#     assert dc3.name.value == "DC-3", f"Expected DC-3, got {dc3.name.value}"
#     logging.info("DC-3 topology verified: %s", dc3.name.value)

# async def test_09_run_generator(
#     self, async_client_main: InfrahubClient, default_branch: str
# ) -> None:
#     """Run the create_dc generator for DC-3."""
#     logging.info("Starting test: test_09_run_generator")

#     client = async_client_main

#     # Check for available generator definitions
#     all_generators = await client.all("CoreGeneratorDefinition", branch="main")
#     logging.info(
#         "Available generator definitions: %s",
#         [g.name.value if hasattr(g, "name") else str(g) for g in all_generators],
#     )

#     # Wait for generator definition to be available (loaded from repository)
#     definition = None
#     max_attempts, attempts = 10, 0

#     while not definition and attempts < max_attempts:
#         try:
#             definition = await client.get(
#                 "CoreGeneratorDefinition",
#                 name__value="create_dc",
#                 branch="main",
#             )
#             if definition:
#                 break
#         except Exception as e:
#             logging.info(
#                 "Waiting for generator definition... attempt %d/%d (%s)",
#                 attempts + 1,
#                 max_attempts,
#                 str(e),
#             )

#         attempts += 1
#         time.sleep(10)

#     assert definition, (
#         "Generator definition 'create_dc' not available after waiting. "
#         "Repository may not have synced properly in test_05."
#     )

#     logging.info("Found generator: %s", definition.name.value)

#     # Switch to target branch for running the generator
#     client.default_branch = default_branch

#     # Get the DC-3 topology object to pass its ID to the generator
#     dc3 = await client.get(
#         kind="TopologyDataCenter",
#         name__value="DC-3",
#         populate_store=True,
#     )

#     assert dc3, "DC-3 topology not found before running generator"
#     logging.info("Found DC-3 topology with ID: %s", dc3.id)

#     # Run the generator with the correct format
#     # The nodes field should contain a list of node IDs to process
#     mutation = Mutation(
#         mutation="CoreGeneratorDefinitionRun",
#         input_data={
#             "data": {
#                 "id": definition.id,
#                 "nodes": [dc3.id],  # List of node IDs to run the generator on
#             },
#             "wait_until_completion": False,
#         },
#         query={"ok": None, "task": {"id": None}},
#     )

#     response = await client.execute_graphql(query=mutation.render())
#     task_id = response["CoreGeneratorDefinitionRun"]["task"]["id"]

#     logging.info("Generator task started: %s", task_id)

#     # Wait for generator to complete (can take a while for DC generation)
#     task = await client.task.wait_for_completion(id=task_id, timeout=1800)

#     # The generator task can fail due to post-processing issues (like GraphQL
#     # query group updates) even if devices were created successfully.
#     # So we check if the task completed OR if devices exist (test_10 will verify).
#     if task.state != TaskState.COMPLETED:
#         logging.warning(
#             "Generator task %s finished with state %s, "
#             "but will verify devices were created in next test",
#             task.id,
#             task.state,
#         )
#     else:
#         logging.info("Generator completed successfully")

# async def test_10_verify_devices_created(
#     self, async_client_main: InfrahubClient, default_branch: str
# ) -> None:
#     """Verify that devices were created by the generator."""
#     logging.info("Starting test: test_10_verify_devices_created")

#     client = async_client_main
#     client.default_branch = default_branch

#     # Query for devices
#     devices = await client.all(kind="DcimDevice")

#     assert devices, (
#         "No devices found after generator run. "
#         "Generator in test_09 may not have completed successfully."
#     )

#     logging.info("Found %d devices after generator run", len(devices))

#     # Check for specific device types (spine, leaf, etc.)
#     device_names = [device.name.value for device in devices]
#     logging.info("Created devices: %s", ", ".join(device_names))

# def test_11_create_diff(
#     self, client_main: InfrahubClientSync, default_branch: str
# ) -> None:
#     """Create a diff for the branch."""
#     logging.info("Starting test: test_11_create_diff")

#     mutation = Mutation(
#         mutation="DiffUpdate",
#         input_data={
#             "data": {
#                 "name": f"diff-for-{default_branch}",
#                 "branch": default_branch,
#                 "wait_for_completion": False,
#             }
#         },
#         query={"ok": None, "task": {"id": None}},
#     )

#     response = client_main.execute_graphql(query=mutation.render())
#     task = client_main.task.wait_for_completion(
#         id=response["DiffUpdate"]["task"]["id"], timeout=600
#     )

#     assert task.state == TaskState.COMPLETED, (
#         f"Task {task.id} - diff creation for {default_branch} did not complete successfully"
#     )
#     logging.info("Diff created successfully")

# def test_12_create_proposed_change(
#     self, client_main: InfrahubClientSync, default_branch: str
# ) -> None:
#     """Create a proposed change to merge the branch."""
#     logging.info("Starting test: test_12_create_proposed_change")

#     pc_mutation_create = Mutation(
#         mutation="CoreProposedChangeCreate",
#         input_data={
#             "data": {
#                 "name": {"value": f"Add DC-3 - Test {default_branch}"},
#                 "source_branch": {"value": default_branch},
#                 "destination_branch": {"value": "main"},
#             }
#         },
#         query={"ok": None, "object": {"id": None}},
#     )

#     response_pc = client_main.execute_graphql(query=pc_mutation_create.render())
#     pc_id = response_pc["CoreProposedChangeCreate"]["object"]["id"]

#     logging.info("Proposed change created with ID: %s", pc_id)

#     # Wait for validations to complete
#     max_attempts = 30
#     attempts = 0
#     validation_results = []
#     validations_completed = False

#     while not validations_completed and attempts < max_attempts:
#         pc = client_main.get(
#             "CoreProposedChange",
#             name__value=f"Add DC-3 - Test {default_branch}",
#             include=["validations"],
#             exclude=["reviewers", "approved_by", "created_by"],
#             prefetch_relationships=True,
#             populate_store=True,
#         )

#         if pc.validations.peers:
#             validations_completed = all(
#                 (
#                     validation.peer.state.value == "completed"
#                     for validation in pc.validations.peers
#                 )
#             )

#             if validations_completed:
#                 validation_results = [
#                     validation.peer for validation in pc.validations.peers
#                 ]
#                 break

#         attempts += 1
#         logging.info(
#             "Waiting for validations to complete... attempt %d/%d",
#             attempts,
#             max_attempts,
#         )
#         time.sleep(30)

#     assert validations_completed, (
#         "Not all proposed change validations completed in time"
#     )

#     # Check validation results
#     failed_validations = [
#         result
#         for result in validation_results
#         if hasattr(result, "conclusion") and result.conclusion.value != "success"
#     ]

#     if failed_validations:
#         for result in failed_validations:
#             name = result.name.value if hasattr(result, "name") else str(result.id)
#             conclusion = (
#                 result.conclusion.value
#                 if hasattr(result, "conclusion")
#                 else "unknown"
#             )
#             logging.error(
#                 "Validation failed: %s - %s",
#                 name,
#                 conclusion,
#             )

#     # Note: We're not asserting all validations pass because some might fail
#     # in test environments. The important part is they complete.
#     logging.info("Validations completed. Results:")
#     for result in validation_results:
#         name = result.name.value if hasattr(result, "name") else str(result.id)
#         conclusion = (
#             result.conclusion.value if hasattr(result, "conclusion") else "unknown"
#         )
#         logging.info("  - %s: %s", name, conclusion)

# def test_13_merge_proposed_change(
#     self, client_main: InfrahubClientSync, default_branch: str
# ) -> None:
#     """Merge the proposed change."""
#     logging.info("Starting test: test_13_merge_proposed_change")

#     # Get the proposed change
#     pc = client_main.get(
#         "CoreProposedChange",
#         name__value=f"Add DC-3 - Test {default_branch}",
#     )

#     logging.info(
#         "Proposed change state: %s",
#         pc.state.value if hasattr(pc.state, "value") else pc.state,
#     )

#     # Merge the proposed change
#     mutation = Mutation(
#         mutation="CoreProposedChangeMerge",
#         input_data={
#             "data": {
#                 "id": pc.id,
#             },
#             "wait_until_completion": False,
#         },
#         query={"ok": None, "task": {"id": None}},
#     )

#     response = client_main.execute_graphql(query=mutation.render())
#     task = client_main.task.wait_for_completion(
#         id=response["CoreProposedChangeMerge"]["task"]["id"], timeout=600
#     )

#     logging.info(
#         "Merge task %s finished with state: %s",
#         task.id,
#         task.state,
#     )

#     # Log task logs if available
#     if hasattr(task, "logs") and task.logs:
#         logging.info("Merge task logs:")
#         for log_entry in task.logs[:10]:  # Show first 10 log entries
#             logging.info("  %s", log_entry)

#     # The merge task can fail due to check failures even if the merge completes.
#     # test_14 will verify that the data actually made it to main.
#     if task.state != TaskState.COMPLETED:
#         logging.warning(
#             "Merge task %s finished with state %s, "
#             "but will verify merge succeeded in next test",
#             task.id,
#             task.state,
#         )
#     else:
#         logging.info("Proposed change merged successfully")

# async def test_14_verify_merge_to_main(
#     self, async_client_main: InfrahubClient
# ) -> None:
#     """Verify that DC-3 and devices exist in main branch."""
#     logging.info("Starting test: test_14_verify_merge_to_main")

#     client = async_client_main
#     client.default_branch = "main"

#     # Add a small delay to allow for data propagation after merge
#     logging.info("Waiting 5 seconds for data propagation...")
#     time.sleep(5)

#     # Try to get DC-3 from main branch
#     try:
#         dc3_main = await client.get(
#             kind="TopologyDataCenter",
#             name__value="DC-3",
#             raise_when_missing=False,
#         )
#     except Exception as e:
#         logging.error("Error querying for DC-3 in main: %s", str(e))
#         dc3_main = None

#     # If DC-3 not found, query all datacenters to see what's there
#     if not dc3_main:
#         logging.info("DC-3 not found in main, querying all datacenters...")
#         all_dcs = await client.all(kind="TopologyDataCenter")
#         dc_names = [
#             dc.name.value if hasattr(dc, "name") else str(dc) for dc in all_dcs
#         ]
#         logging.info("Datacenters in main branch: %s", dc_names)

#         # Also check the proposed changes to see if merge actually succeeded
#         proposed_changes = await client.all(kind="CoreProposedChange")
#         logging.info("Total proposed changes: %d", len(proposed_changes))
#         for pc in proposed_changes:
#             if hasattr(pc, "name") and "DC-3" in pc.name.value:
#                 pc_state = (
#                     pc.state.value if hasattr(pc.state, "value") else pc.state
#                 )
#                 logging.info(
#                     "Found PC '%s' with state: %s", pc.name.value, pc_state
#                 )

#     assert dc3_main, (
#         "DC-3 not found in main branch after merge. "
#         "The merge in test_13 may have failed. Check logs above for merge task state."
#     )
#     logging.info("DC-3 verified in main branch")

#     # Verify devices exist in main
#     devices_main = await client.all(kind="DcimDevice")
#     assert devices_main, "No devices found in main branch after merge"
#     logging.info("Found %d devices in main branch", len(devices_main))
