"""Integration tests for merge verification.

This module contains tests for:
1. Verifying DC1 exists in main branch after merge
2. Verifying devices exist in main branch
3. Final integrity checks
"""

import asyncio
import logging
from typing import cast

import pytest
from infrahub_sdk import InfrahubClient

from generators.protocols import DcimDevice, TopologyDataCenter

from .conftest import TestInfrahubDockerWithClient
from .test_constants import MERGE_PROPAGATION_DELAY

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


class TestVerification(TestInfrahubDockerWithClient):
    """Test merge verification."""

    @pytest.mark.order(19)
    @pytest.mark.dependency(name="verify_merge_to_main", depends=["merge_proposed_change"])
    @pytest.mark.asyncio
    async def test_01_verify_dc_in_main(self, async_client_main: InfrahubClient, default_branch: str) -> None:
        """Verify that DC1 exists in main branch after merge."""
        logging.info("Starting test: test_01_verify_dc_in_main")

        client = async_client_main
        client.default_branch = "main"

        # Add a delay to allow for data propagation after merge
        # Infrahub needs time to propagate merged data across the system
        logging.info(
            "Waiting %d seconds for merge data propagation...",
            MERGE_PROPAGATION_DELAY,
        )
        await asyncio.sleep(MERGE_PROPAGATION_DELAY)

        # Try to get DC1 from main branch
        dc_main = None
        diagnostic_info: list[str] = []

        try:
            dc_main = await client.get(
                kind=TopologyDataCenter,
                name__value="DC1",
                raise_when_missing=False,
            )
        except Exception as e:
            logging.error("Error querying for DC1 in main: %s", str(e))
            diagnostic_info.append(f"Query error: {e}")

        # If DC1 not found, gather diagnostic information
        if not dc_main:
            logging.info("DC1 not found in main, gathering diagnostics...")

            all_dcs = await client.all(kind=TopologyDataCenter)
            dc_names = [dc.name.value if hasattr(dc, "name") else str(dc) for dc in all_dcs]
            logging.info("Datacenters in main branch: %s", dc_names)
            diagnostic_info.append(f"Available datacenters: {dc_names}")

            # Also check the proposed changes to see if merge actually succeeded
            proposed_changes = await client.all(kind="CoreProposedChange")
            logging.info("Total proposed changes: %d", len(proposed_changes))

            for pc in proposed_changes:
                if hasattr(pc, "name"):
                    pc_name = cast(str, pc.name.value if hasattr(pc.name, "value") else str(pc.name))
                    if "DC1" in pc_name:
                        pc_state = pc.state.value if hasattr(pc.state, "value") else pc.state
                        logging.info("Found PC '%s' with state: %s", pc_name, pc_state)
                        diagnostic_info.append(f"PC '{pc_name}' state: {pc_state}")

        assert dc_main, (
            f"DC1 not found in main branch after merge.\n"
            f"  Source branch: {default_branch}\n"
            f"  The merge may have failed.\n"
            f"  Diagnostics:\n    " + "\n    ".join(diagnostic_info)
        )
        logging.info("DC1 verified in main branch")

    @pytest.mark.order(20)
    @pytest.mark.dependency(name="verify_devices_in_main", depends=["verify_merge_to_main"])
    @pytest.mark.asyncio
    async def test_02_verify_devices_in_main(self, async_client_main: InfrahubClient, default_branch: str) -> None:
        """Verify devices exist in main branch."""
        logging.info("Starting test: test_02_verify_devices_in_main")

        client = async_client_main
        client.default_branch = "main"

        # Verify devices exist in main
        devices_main = await client.all(kind=DcimDevice)
        assert devices_main, (
            f"No devices found in main branch after merge.\n"
            f"  DC1 was found, but no DcimDevice objects exist.\n"
            f"  Source branch: {default_branch}"
        )
        logging.info("Found %d devices in main branch", len(devices_main))

        # Log device summary
        device_names = [device.name.value for device in devices_main]
        spine_count = sum(1 for name in device_names if "spine" in name.lower())
        leaf_count = sum(1 for name in device_names if "leaf" in name.lower())
        logging.info("Device summary in main: %d spines, %d leafs", spine_count, leaf_count)
        logging.info("All devices: %s", ", ".join(device_names))
