"""Create the VLAN-domain row of dc90-leaf-02 for the segment c900-demo-web-p (vlan 100, as on dc90-leaf-01).

Run after the object files by tests/traversal/check_model.py (case key `after_load`), or by hand on a branch:

  INFRAHUB_ADDRESS=http://localhost:8100 INFRAHUB_API_TOKEN=... uv run python data/demos/cloud_saas_model_pod/vlan_row.py main

Idempotent: a row that exists is left alone.
"""

import asyncio
import os
import sys

from infrahub_sdk import Config, InfrahubClient

SEGMENT, DEPLOYMENT, DEVICE, SVI, VLAN_ID = "c900-demo-web-p", "DC90", "dc90-leaf-02", "Vlan100", 100


async def apply(client: InfrahubClient, branch: str) -> str:
    """Add the row; returns what was done."""
    deployments = await client.filters(
        kind="ManagedSegmentDeployment", branch=branch, segment__name__value=SEGMENT, deployment__name__value=DEPLOYMENT
    )
    if len(deployments) != 1:
        raise RuntimeError(f"expected one deployment of {SEGMENT} at {DEPLOYMENT}, found {len(deployments)}")
    rows = await client.filters(
        kind="ManagedVlanDomainSegment",
        branch=branch,
        device__name__value=DEVICE,
        segment_deployment__ids=[deployments[0].id],
    )
    if rows:
        return "row exists"
    svi = await client.get(kind="DcimVirtualInterface", branch=branch, device__name__value=DEVICE, name__value=SVI)
    row = await client.create(
        kind="ManagedVlanDomainSegment",
        branch=branch,
        vlan_id=VLAN_ID,
        segment_deployment=deployments[0].id,
        device=(await client.get(kind="DcimPhysicalDevice", branch=branch, name__value=DEVICE)).id,
        svi=svi.id,
    )
    await row.save()
    return "row created"


async def main(branch: str) -> None:
    client = InfrahubClient(
        config=Config(
            address=os.environ.get("INFRAHUB_ADDRESS", "http://localhost:8100"),
            api_token=os.environ["INFRAHUB_API_TOKEN"],
        )
    )
    sys.stdout.write(await apply(client, branch) + "\n")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "main"))
