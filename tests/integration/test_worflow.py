# import logging
# import time

# import pytest

# from infrahub_sdk.graphql import Mutation
# from infrahub_sdk.task.models import TaskState
# from infrahub_sdk.testing.repository import GitRepo

# from .conftest import PROJECT_DIRECTORY, TestInfrahubDockerWithClient

# logging.basicConfig(
#     level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
# )

# DATA_GENERATORS = [
#     "bootstrap.py",
# ]


# class TestDemoflow(TestInfrahubDockerWithClient):
#     @pytest.fixture(scope="class")
#     def default_branch(self) -> str:
#         return "test-demo"

#     def test_schema_load(self, client_main):
#         logging.info("Starting test: test_schema_load")
#         # Load schema files into main
#         logging.info("Invoking schema load command")

#         load_schemas = self.execute_command(
#             "infrahubctl schema load  schemas --wait 60",
#             address=client_main.config.address,
#         )

#         logging.info("Command output: %s", load_schemas.stdout)
#         assert load_schemas.returncode == 0, (
#             f"Schema load failed: {load_schemas.stdout}"
#         )

#     def test_load_data(self, client_main):
#         for data_generator in DATA_GENERATORS:
#             load_data = self.execute_command(
#                 f"infrahubctl run bootstrap/{data_generator}",
#                 address=client_main.config.address,
#             )
#             assert load_data.returncode == 0

#     async def test_add_repository(self, async_client_main, remote_repos_dir):
#         client = async_client_main
#         src_directory = PROJECT_DIRECTORY
#         git_repository = GitRepo(
#             name="demo_repo",
#             src_directory=src_directory,
#             dst_directory=remote_repos_dir,
#         )

#         response = await git_repository.add_to_infrahub(client=client)
#         assert response.get(f"{git_repository.type.value}Create", {}).get("ok")

#         repos = await client.all(kind=git_repository.type)
#         assert repos

#         synchronized = False
#         max_attempts, attempts = 60, 0

#         while not synchronized and attempts < max_attempts:
#             repository = await client.get(
#                 kind=git_repository.type.value, name__value="demo_repo"
#             )
#             synchronized = repository.sync_status.value == "in-sync"
#             error = "error" in repository.sync_status.value
#             if synchronized or error:
#                 break
#             attempts += 1
#             time.sleep(10)

#         assert synchronized

# def test_generate_topology(self, client_main):
#     generate_topology = self.execute_command(
#         "infrahubctl run bootstrap/generate_topology.py topology=fra05-pod1",
#         address=client_main.config.address,
#     )
#     assert generate_topology.returncode == 0

# def test_create_branch(self, client_main, default_branch):
#     client_main.branch.create(default_branch)

# def test_create_services(self, client, default_branch):
#     l2_service = client.create(
#         kind="TopologyLayer2NetworkService",
#         name="cust01",
#         status="provisioning",
#         topology={"hfid": "fra05-pod1"},
#         member_of_groups=[{"id": "network_services"}],
#     )
#     l2_service.save(allow_upsert=True)

#     l3_service = client.create(
#         kind="TopologyLayer3NetworkService",
#         name="cust02",
#         status="provisioning",
#         topology={"hfid": "fra05-pod1"},
#         member_of_groups=[{"id": "network_services"}],
#     )
#     l3_service.save(allow_upsert=True)

# def test_generator(self, client, default_branch):
#     definition = client.get(
#         "CoreGeneratorDefinition", name__value="generate_network_services"
#     )
#     mutation = Mutation(
#         mutation="CoreGeneratorDefinitionRun",
#         input_data={
#             "data": {
#                 "id": definition.id,
#             },
#             "wait_until_completion": False,
#         },
#         query={"ok": None, "task": {"id": None}},
#     )

#     response = client.execute_graphql(query=mutation.render())

#     task = client.task.wait_for_completion(
#         id=response["CoreGeneratorDefinitionRun"]["task"]["id"], timeout=1800
#     )

#     assert task.state == TaskState.COMPLETED, (
#         f"Task {task.id} - generator generate_network_services did not complete successfully"
#     )

# def test_create_diff(self, client_main, default_branch):
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
#         f"Task {task.id} - generate diff for {default_branch} did not complete successfully"
#     )

# def test_proposed_change(self, client_main, default_branch):
#     pc_mutation_create = Mutation(
#         mutation="CoreProposedChangeCreate",
#         input_data={
#             "data": {
#                 "name": {"value": "Test Merge PC"},
#                 "source_branch": {"value": default_branch},
#                 "destination_branch": {"value": "main"},
#             }
#         },
#         query={"ok": None, "object": {"id": None}},
#     )
#     response_pc = client_main.execute_graphql(query=pc_mutation_create.render())

#     pc_id = response_pc["CoreProposedChangeCreate"]["object"]["id"]

#     max_attempts = 15
#     attempts = 0
#     validation_results = []
#     validations_completed = False

#     while not validations_completed and attempts < max_attempts:
#         pc = client_main.get(
#             "CoreProposedChange",
#             name__value="Test Merge PC",
#             include=["validations"],
#             exclude=["reviewers", "approved_by", "created_by"],
#             prefetch_relationships=True,
#             populate_store=True,
#         )

#         validations_completed = all(
#             (
#                 validation.peer.state.value == "completed"
#                 for validation in pc.validations.peers
#             )
#         )

#         if validations_completed:
#             validation_results = [
#                 validation.peer for validation in pc.validations.peers
#             ]
#             break

#         attempts += 1
#         time.sleep(60)

#     assert validations_completed, (
#         "Not all proposed change validations managed to complete in time"
#     )

#     assert all(
#         (result.conclusion.value == "succes" for result in validation_results)
#     ), "Not all validations have succeeded!"

#     mutation = Mutation(
#         mutation="CoreProposedChangeMerge",
#         input_data={
#             "data": {
#                 "id": pc_id,
#             },
#             "wait_until_completion": False,
#         },
#         query={"ok": None, "task": {"id": None}},
#     )

#     response = client_main.execute_graphql(query=mutation.render())
#     task = client_main.task.wait_for_completion(
#         id=response["CoreProposedChangeMerge"]["task"]["id"], timeout=600
#     )

#     assert task.state == TaskState.COMPLETED, (
#         f"Task {task.id} - merge proposed change did not complete succesfully"
#     )
