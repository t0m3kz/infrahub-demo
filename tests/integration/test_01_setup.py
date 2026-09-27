"""Integration tests for schema and bootstrap data loading.

This module contains tests for:
1. Loading base schemas
2. Loading extension schemas
3. Loading menu definitions
4. Loading bootstrap data
"""

import logging

import pytest
from infrahub_sdk import InfrahubClientSync

from utils.bootstrap_order import bootstrap_load_tiers

from .conftest import PROJECT_DIRECTORY, TestInfrahubDockerWithClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


class TestSetup(TestInfrahubDockerWithClient):
    """Test schema and bootstrap data loading."""

    @pytest.mark.order(1)
    @pytest.mark.dependency(scope="session", name="schema_load")
    def test_01_load_base_schemas(self, client_main: InfrahubClientSync) -> None:
        """Load base schemas into Infrahub."""
        logging.info("Starting test: test_01_load_base_schemas")

        load_base = self.execute_command(
            "infrahubctl schema load schemas/base",
            address=client_main.config.address,
        )

        logging.info("Base schema load output: %s", load_base.stdout)
        logging.info("Base schema load stderr: %s", load_base.stderr)

        assert "loaded successfully" in load_base.stdout or load_base.returncode == 0, (
            f"Base schema load failed.\n"
            f"  Return code: {load_base.returncode}\n"
            f"  stdout: {load_base.stdout}\n"
            f"  stderr: {load_base.stderr}"
        )

    @pytest.mark.order(2)
    @pytest.mark.dependency(scope="session", name="schema_extensions", depends=["schema_load"])
    def test_02_load_extension_schemas(self, client_main: InfrahubClientSync) -> None:
        """Load extension schemas into Infrahub."""
        logging.info("Starting test: test_02_load_extension_schemas")

        load_extensions = self.execute_command(
            "infrahubctl schema load schemas/extensions",
            address=client_main.config.address,
        )

        logging.info("Extensions schema load output: %s", load_extensions.stdout)
        logging.info("Extensions schema load stderr: %s", load_extensions.stderr)

        assert "loaded successfully" in load_extensions.stdout or load_extensions.returncode == 0, (
            f"Extensions schema load failed.\n"
            f"  Return code: {load_extensions.returncode}\n"
            f"  stdout: {load_extensions.stdout}\n"
            f"  stderr: {load_extensions.stderr}"
        )

    @pytest.mark.order(3)
    @pytest.mark.dependency(scope="session", name="menu_load", depends=["schema_extensions"])
    def test_03_load_menu(self, client_main: InfrahubClientSync) -> None:
        """Load menu definitions."""
        logging.info("Starting test: test_03_load_menu")

        load_menu = self.execute_command(
            "infrahubctl menu load menu/menu.yml",
            address=client_main.config.address,
        )

        logging.info("Menu load output: %s", load_menu.stdout)
        assert load_menu.returncode == 0, (
            f"Menu load failed.\n"
            f"  Return code: {load_menu.returncode}\n"
            f"  stdout: {load_menu.stdout}\n"
            f"  stderr: {load_menu.stderr}"
        )

    @pytest.mark.order(4)
    @pytest.mark.dependency(scope="session", name="bootstrap_data", depends=["schema_extensions"])
    def test_04_load_bootstrap_data(self, client_main: InfrahubClientSync) -> None:
        """Load bootstrap data one dependency tier at a time.

        The bootstrap directory is layered by filename prefix, and a higher
        prefix may reference a lower one by HFID: device templates in 09_*/10_*
        reference an owner (02_), platform (05_), software_image (05b_) and
        device_type (06_); software images reference a platform; device types
        reference a manufacturer (04_). An HFID lookup only succeeds once the
        referenced node is visible to a query, which is not guaranteed the
        moment its own upsert returns, so loading the whole directory as one
        concurrent run races referencing objects against their dependencies and
        fails partway with "Unable to find the node arista_eos / DcimPlatform in
        the database" — intermittently, which is worse than slowly.

        utils.bootstrap_order turns the prefix convention into a barrier: one
        invocation per tier, each finishing before the next starts. This
        generalises what used to be a hand-rolled pre-load of 02_providers.yml
        alone, which fixed the owner references and left every other
        cross-file reference racing.

        Concurrency stays at the default WITHIN a tier. Tiers fix cross-file
        ordering; they do not touch the nested-object race one level deeper,
        where each 09_*/10_* template's interfaces reference a parent created
        moments earlier in the same file. Raising concurrency to 30 put 30 such
        groups in flight at once and was observed to fail with "Unable to find
        the node <id> / TemplateDcimDevice in the database" partway through the
        Edgecore SONiC spine templates.
        """
        logging.info("Starting test: test_04_load_bootstrap_data")

        tiers = bootstrap_load_tiers(PROJECT_DIRECTORY / "data" / "bootstrap")
        assert tiers, "no bootstrap object files found — the load would silently do nothing"

        for index, tier in enumerate(tiers, start=1):
            paths = " ".join(str(path.relative_to(PROJECT_DIRECTORY)) for path in tier)
            logging.info("Loading bootstrap tier %d/%d: %s", index, len(tiers), ", ".join(p.name for p in tier))

            load_tier = self.execute_command(
                f"infrahubctl object load {paths}",
                address=client_main.config.address,
                pagination_size=200,
            )

            assert load_tier.returncode == 0, (
                f"Bootstrap load failed at tier {index}/{len(tiers)} ({paths}).\n"
                f"  Return code: {load_tier.returncode}\n"
                f"  stdout: {load_tier.stdout}\n"
                f"  stderr: {load_tier.stderr}"
            )
