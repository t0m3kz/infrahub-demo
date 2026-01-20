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

from .conftest import TestInfrahubDockerWithClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


class TestSetup(TestInfrahubDockerWithClient):
    """Test schema and bootstrap data loading."""

    @pytest.mark.order(1)
    @pytest.mark.dependency(name="schema_load")
    def test_01_load_base_schemas(self, client_main: InfrahubClientSync) -> None:
        """Load base schemas into Infrahub."""
        logging.info("Starting test: test_01_load_base_schemas")

        load_base = self.execute_command(
            "infrahubctl schema load schemas/base --wait 60",
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
    @pytest.mark.dependency(name="schema_extensions", depends=["schema_load"])
    def test_02_load_extension_schemas(self, client_main: InfrahubClientSync) -> None:
        """Load extension schemas into Infrahub."""
        logging.info("Starting test: test_02_load_extension_schemas")

        load_extensions = self.execute_command(
            "infrahubctl schema load schemas/extensions --wait 60",
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
    @pytest.mark.dependency(name="menu_load", depends=["schema_extensions"])
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
    @pytest.mark.dependency(name="bootstrap_data", depends=["schema_extensions"])
    def test_04_load_bootstrap_data(self, client_main: InfrahubClientSync) -> None:
        """Load bootstrap data."""
        logging.info("Starting test: test_04_load_bootstrap_data")

        load_data = self.execute_command(
            "infrahubctl object load data/bootstrap/",
            address=client_main.config.address,
        )

        logging.info("Bootstrap data load output: %s", load_data.stdout)
        assert load_data.returncode == 0, (
            f"Bootstrap data load failed.\n"
            f"  Return code: {load_data.returncode}\n"
            f"  stdout: {load_data.stdout}\n"
            f"  stderr: {load_data.stderr}"
        )
