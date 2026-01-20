"""Integration tests for repository management.

This module contains tests for:
1. Adding Git repository to Infrahub
2. Waiting for repository sync
3. Loading event/action definitions (requires generator definitions from repo)
4. Verifying repository content availability
"""

import logging
import shutil
import tempfile
from pathlib import Path
from typing import Any, cast

import pytest
from infrahub_sdk import InfrahubClient, InfrahubClientSync
from infrahub_sdk.testing.repository import GitRepo

from .conftest import PROJECT_DIRECTORY, TestInfrahubDockerWithClient
from .test_constants import REPO_SYNC_MAX_ATTEMPTS, REPO_SYNC_POLL_INTERVAL
from .test_helpers import wait_for_condition

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


class TestRepository(TestInfrahubDockerWithClient):
    """Test repository management."""

    @pytest.mark.order(6)
    @pytest.mark.dependency(name="add_repository", depends=["events_data"])
    @pytest.mark.asyncio
    async def test_01_add_repository(
        self,
        async_client_main: InfrahubClient,
        remote_repos_dir: str,
        workflow_state: dict[str, Any],
    ) -> None:
        """Add the demo repository to Infrahub."""
        logging.info("Starting test: test_01_add_repository")

        client = async_client_main

        # NOTE: GitRepo copies the source directory into the remote repo mount.
        # If the source tree contains a previous `.pytest-tmp/` (or other caches),
        # that directory may itself contain nested repo copies and cause path
        # recursion / "File name too long" errors on subsequent runs.
        ignore_patterns = shutil.ignore_patterns(
            ".git",
            ".pytest-tmp",
            ".pytest_cache",
            ".ruff_cache",
            ".mypy_cache",
            "__pycache__",
            ".venv",
            ".DS_Store",
        )

        with tempfile.TemporaryDirectory(prefix="infrahub-demo-src-") as tmpdir:
            src_directory = Path(tmpdir) / "repo"
            shutil.copytree(PROJECT_DIRECTORY, src_directory, ignore=ignore_patterns)

            git_repository = GitRepo(
                name="demo_repo",
                src_directory=src_directory,
                dst_directory=Path(remote_repos_dir),
            )

            # Create repository (idempotent: reuse if it already exists)
            existing_repo = await client.get(
                kind=git_repository.type.value,
                name__value=git_repository.name,
                raise_when_missing=False,
            )
            if existing_repo:
                workflow_state["repository_id"] = existing_repo.id
                logging.info(
                    "Repository %s already exists (ID: %s)",
                    git_repository.name,
                    existing_repo.id,
                )
            else:
                response = await git_repository.add_to_infrahub(client=client)
                assert response.get(f"{git_repository.type.value}Create", {}).get("ok"), (
                    f"Failed to add repository to Infrahub.\n"
                    f"  Repository name: {git_repository.name}\n"
                    f"  Source directory: {src_directory}\n"
                    f"  Response: {response}"
                )

    @pytest.mark.order(7)
    @pytest.mark.dependency(name="repository_sync", depends=["add_repository"])
    @pytest.mark.asyncio
    async def test_02_wait_for_repository_sync(
        self,
        async_client_main: InfrahubClient,
        workflow_state: dict[str, Any],
    ) -> None:
        """Wait for repository to sync."""
        logging.info("Starting test: test_02_wait_for_repository_sync")

        client = async_client_main

        # Use wait_for_condition helper for repository sync
        async def check_repo_sync() -> tuple[bool, Any]:
            repository = await client.get(
                kind="CoreRepository",
                name__value="demo_repo",
                raise_when_missing=False,
            )
            if not repository:
                return False, None
            sync_status = cast(
                str,
                repository.sync_status.value
                if hasattr(repository.sync_status, "value")
                else str(repository.sync_status),
            )
            synchronized = sync_status == "in-sync"
            has_error = "error" in sync_status

            if has_error:
                raise AssertionError(f"Repository sync failed with error status: {sync_status}")
            return synchronized, repository

        try:
            repository = await wait_for_condition(
                check_fn=check_repo_sync,
                max_attempts=REPO_SYNC_MAX_ATTEMPTS,
                poll_interval=REPO_SYNC_POLL_INTERVAL,
                description="repository sync",
            )
            workflow_state["repository_id"] = repository.id
            logging.info("Repository synchronized successfully (ID: %s)", repository.id)
        except TimeoutError as e:
            # Get final status for error message
            repository = await client.get(kind="CoreRepository", name__value="demo_repo")
            final_status = cast(
                str,
                repository.sync_status.value
                if hasattr(repository.sync_status, "value")
                else str(repository.sync_status),
            )
            raise AssertionError(
                f"Repository failed to sync within timeout.\n"
                f"  Final status: {final_status}\n"
                f"  Timeout: {REPO_SYNC_MAX_ATTEMPTS * REPO_SYNC_POLL_INTERVAL}s"
            ) from e

    @pytest.mark.order(8)
    @pytest.mark.dependency(name="events_data", depends=["repository_sync"])
    def test_03_load_events(self, client_main: InfrahubClientSync) -> None:
        """Load event/action definitions after repository sync.

        Events must be loaded after repository sync because they reference
        generator definitions that come from .infrahub.yml in the repository.
        """
        logging.info("Starting test: test_03_load_events")

        load_events = self.execute_command(
            "infrahubctl object load data/events/",
            address=client_main.config.address,
        )

        logging.info("Events load output: %s", load_events.stdout)
        assert load_events.returncode == 0, (
            f"Events data load failed.\n"
            f"  Return code: {load_events.returncode}\n"
            f"  stdout: {load_events.stdout}\n"
            f"  stderr: {load_events.stderr}"
        )
