import os
import subprocess
from pathlib import Path

import pytest
from infrahub_sdk import Config, InfrahubClient, InfrahubClientSync
from infrahub_testcontainers.helpers import TestInfrahubDocker

TEST_DIRECTORY = Path(__file__).parent
PROJECT_DIRECTORY = TEST_DIRECTORY.parent.parent


class TestInfrahubDockerWithClient(TestInfrahubDocker):
    @pytest.fixture(scope="class")
    def async_client_main(self, infrahub_port: int) -> InfrahubClient:
        client = InfrahubClient(
            config=Config(
                address=f"http://localhost:{infrahub_port}",
            )  # noqa: S106
        )
        return client

    @pytest.fixture(scope="class")
    def client_main(self, infrahub_port: int) -> InfrahubClientSync:
        client = InfrahubClientSync(
            config=Config(
                address=f"http://localhost:{infrahub_port}",
            )  # noqa: S106
        )

        return client

    @pytest.fixture(scope="class")
    def client(self, infrahub_port: int, default_branch: str) -> InfrahubClientSync:
        client = InfrahubClientSync(
            config=Config(
                address=f"http://localhost:{infrahub_port}",
            )  # noqa: S106
        )
        if default_branch not in client.branch.all():
            client.branch.create(default_branch)
        if client.default_branch != default_branch:
            client.default_branch = default_branch

        return client

    # @pytest.fixture(scope="class")
    # def infrahubctl(self, client_main: InfrahubClientSync):
    #     # Set the INFRAHUB_ADDRESS environment variable to match the testcontainers address
    #     return CliRunner(env={"INFRAHUB_ADDRESS": client_main.config.address})

    @staticmethod
    def execute_command(command: str, address: str) -> str:
        env = os.environ.copy()
        env["INFRAHUB_ADDRESS"] = address
        # env["INFRAHUB_API_TOKEN"] = "06438eb2-8019-4776-878c-0941b1f1d1ec"
        env["INFRAHUB_MAX_CONCURRENT_EXECUTION"] = "10"
        result = subprocess.run(  # noqa: S602
            command, shell=True, capture_output=True, text=True, env=env, check=False
        )
        return result
