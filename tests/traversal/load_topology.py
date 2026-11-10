"""Load the full topology (data/demos/00_full_topology) in one `infrahubctl object load` run.

  INFRAHUB_ADDRESS=http://localhost:8100 INFRAHUB_API_TOKEN=... uv run python tests/traversal/load_topology.py [--branch main]

The SDK loads a directory recursively, files sorted by name, one after the other. The numbered roots
00_common, 10_topology, 20_C900, 21_C901 and 90_bindings provide the dependency order. Loading is idempotent:
every object is upserted by its human-friendly id, including segment realizations and VLAN rows.

Used by this script and `tests/integration/test_03_full_topology.py`. No Python lives under data/ (tests/unit/test_no_python_in_data.py).
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


def object_load(address: str, token: str, branch: str) -> subprocess.CompletedProcess[str]:
    """`infrahubctl object load` of the whole directory (output captured)."""
    env = {**os.environ, "INFRAHUB_ADDRESS": address, "INFRAHUB_API_TOKEN": token}
    cmd = ["uv", "run", "infrahubctl", "object", "load", str(TOPOLOGY), "--branch", branch]
    return subprocess.run(cmd, env=env, cwd=ROOT, capture_output=True, text=True, check=False)  # noqa: S603


async def apply(client: InfrahubClient, branch: str) -> str:
    """Load the topology onto the branch; returns what was done. Raises on a failed load."""
    token = str(client.config.api_token)
    loaded = object_load(client.config.address, token, branch)
    if loaded.returncode != 0:
        tail = "\n".join((loaded.stdout + loaded.stderr).replace(token, "[REDACTED]").strip().splitlines()[-12:])
        raise RuntimeError(f"infrahubctl object load failed (exit {loaded.returncode}):\n{tail}")
    return "objects loaded"


async def main(branch: str) -> None:
    client = InfrahubClient(
        config=Config(
            address=os.environ.get("INFRAHUB_ADDRESS", "http://localhost:8100"),
            api_token=os.environ["INFRAHUB_API_TOKEN"],
        )
    )
    sys.stdout.write(await apply(client, branch) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--branch", default="main")
    args = parser.parse_args()
    asyncio.run(main(args.branch))
