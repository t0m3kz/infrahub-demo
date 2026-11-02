"""Load the full topology (data/demos/00_full_topology) in one `infrahubctl object load` run, then the step no object file can do.

  INFRAHUB_ADDRESS=http://localhost:8100 INFRAHUB_API_TOKEN=... uv run python tests/traversal/load_topology.py [--branch main] [--force]

The SDK loads a directory recursively, files sorted by name, one after the other, so the numbered folders and files of
the sample are the dependency order. One step is left for after the load: the VLAN-domain row of dc90-leaf-02 for the
segment c900-demo-web-p (VLAN 100, as on dc90-leaf-01), because an object file cannot name the segment's deployment the
row hangs under (ManagedSegmentDeployment has no human friendly id, and nesting it a second time breaks the
deployment's uniqueness). Idempotent as a whole: when the sentinel segment is already on the branch (nested
`segment_deployments` are not idempotent on a second load) the object load is skipped and only the row step runs (a row
that exists is left alone).

Used by this script, `tests/traversal/check_model.py` (`invoke check-model`) and
`tests/integration/test_03_full_topology.py`. No Python lives under data/ (tests/unit/test_no_python_in_data.py).
"""

import argparse
import asyncio
import os
import subprocess
import sys
from pathlib import Path

from infrahub_sdk import Config, InfrahubClient

ROOT = Path(__file__).resolve().parents[2]
TOPOLOGY = ROOT / "data" / "demos" / "00_full_topology"
SENTINEL = {"kind": "ManagedVxlanSegment", "name": "c900-demo-web-p"}
SEGMENT, DEPLOYMENT, DEVICE, SVI, VLAN_ID = "c900-demo-web-p", "DC90", "dc90-leaf-02", "Vlan100", 100


def object_load(address: str, token: str, branch: str) -> subprocess.CompletedProcess[str]:
    """`infrahubctl object load` of the whole directory (output captured)."""
    env = {**os.environ, "INFRAHUB_ADDRESS": address, "INFRAHUB_API_TOKEN": token}
    cmd = ["uv", "run", "infrahubctl", "object", "load", str(TOPOLOGY), "--branch", branch]
    return subprocess.run(cmd, env=env, cwd=ROOT, capture_output=True, text=True, check=False)  # noqa: S603


async def is_loaded(client: InfrahubClient, branch: str) -> bool:
    """The sentinel segment exists on the branch."""
    kind, name = SENTINEL["kind"], SENTINEL["name"]
    result = await client.execute_graphql(query=f'{{ {kind}(name__value: "{name}") {{ count }} }}', branch_name=branch)
    return result[kind]["count"] > 0


async def add_vlan_row(client: InfrahubClient, branch: str) -> str:
    """Create the VLAN-domain row of dc90-leaf-02 for the sample segment; returns what was done."""
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


async def apply(client: InfrahubClient, branch: str, force: bool = False) -> str:
    """Load the topology onto the branch and run the after-load step; returns what was done. Raises on a failed load."""
    notes = []
    if await is_loaded(client, branch) and not force:
        notes.append("object load skipped, the sentinel is already on the branch")
    else:
        loaded = object_load(client.config.address, str(client.config.api_token), branch)
        if loaded.returncode != 0:
            tail = "\n".join((loaded.stdout + loaded.stderr).strip().splitlines()[-12:])
            raise RuntimeError(f"infrahubctl object load failed (exit {loaded.returncode}):\n{tail}")
        notes.append("objects loaded")
    notes.append(await add_vlan_row(client, branch))
    return "; ".join(notes)


async def main(branch: str, force: bool) -> None:
    client = InfrahubClient(
        config=Config(
            address=os.environ.get("INFRAHUB_ADDRESS", "http://localhost:8100"),
            api_token=os.environ["INFRAHUB_API_TOKEN"],
        )
    )
    sys.stdout.write(await apply(client, branch, force) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--branch", default="main")
    parser.add_argument("--force", action="store_true", help="load the objects even if the sentinel is on the branch")
    args = parser.parse_args()
    asyncio.run(main(args.branch, args.force))
