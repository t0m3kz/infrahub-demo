#!/usr/bin/env python3
"""Fast model check: schema + a small topology on a throwaway branch, then L1/L3 traversal assertions.

  uv run invoke check-model [--case cloud_saas_model] [--update-golden] [--keep-branch] [--rebuild]

No pytest. Steps: stack on :8100 -> branch tv-<timestamp> -> schema load -> data load (ordered, sequential) ->
for each pair of the case: trace (bulk GraphQL reads, walk in memory), assert, compare with the golden file ->
delete the branch (also on failure). Cases live in tests/traversal/cases.yml.
"""

import argparse
import asyncio
import importlib.util
import os
import subprocess
import sys
import time
import urllib.request
from collections import Counter
from pathlib import Path
from types import ModuleType
from typing import Any

import yaml
from infrahub_sdk import Config, InfrahubClient

ROOT = Path(__file__).resolve().parents[2]
GOLDEN_DIR = Path(__file__).resolve().parent / "golden"
CASES_FILE = Path(__file__).resolve().parent / "cases.yml"
DEFAULT_TOKEN = "06438eb2-8019-4776-878c-0941b1f1d1ec"  # noqa: S105 - the local test stack's demo token


def say(text: str = "") -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


class Report:
    """PASS/FAIL rows, one per assertion."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, bool, str]] = []

    def add(self, name: str, ok: bool, detail: str = "") -> bool:
        self.rows.append((name, ok, detail))
        return ok

    @property
    def failed(self) -> int:
        return sum(1 for _, ok, _ in self.rows if not ok)

    def print(self) -> None:
        width = max((len(n) for n, _, _ in self.rows), default=10)
        say("")
        for name, ok, detail in self.rows:
            note = detail if (not ok or detail.startswith("note:")) and "\n" not in detail else ""
            say(f"  {'PASS' if ok else 'FAIL'}  {name.ljust(width)}  {note}")
            if not ok and "\n" in detail:
                say("        " + detail.replace("\n", "\n        "))
        say(f"\n  {len(self.rows) - self.failed} PASS, {self.failed} FAIL")


# ------------------------------------------------------------------ stack and branch


def stack_up(address: str) -> bool:
    try:
        with urllib.request.urlopen(f"{address}/api/info", timeout=5) as r:  # noqa: S310 - local address
            return r.status == 200
    except OSError:
        return False


def run(cmd: list[str], env: dict[str, str], cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, env={**os.environ, **env}, cwd=cwd, capture_output=True, text=True, check=False)  # noqa: S603


def start_stack(port: int, rebuild: bool) -> None:
    say(
        f"Stos testowy na :{port} {'przebudowuje sie' if rebuild else 'nie dziala, startuje'}: jednorazowo ok. 5 minut."
    )
    if rebuild:
        subprocess.run(["uv", "run", "invoke", "clean-testcontainers"], cwd=ROOT, check=False)  # noqa: S603, S607
    env = {
        "INFRAHUB_TESTING_ENABLE_INTEGRATION": "1",
        "INFRAHUB_TESTING_KEEP_STACK": "1",
        "INFRAHUB_TESTING_SERVER_PORT": str(port),
    }
    cmd = ["uv", "run", "invoke", "test-integration", "--server-port", str(port)]
    cmd += ["--tests", "tests/integration/test_01_setup.py"]
    proc = subprocess.run(cmd, env={**os.environ, **env}, cwd=ROOT, check=False)  # noqa: S603
    if proc.returncode != 0:
        sys.exit("start stosu nie powiodl sie (test_01_setup); zobacz wyjscie powyzej")


def tail(text: str, lines: int = 8) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


# ------------------------------------------------------------------ tracer, kinds, golden


def load_tracer(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"tracer_{path.parent.name}", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def render_golden(result: dict[str, Any]) -> str:
    """A deterministic text of the structured trace (sorted, so the API's row order does not matter)."""
    lines = ["# L1 legs"]
    lines += sorted("  " + " | ".join(leg["hops"]) for leg in result["l1"])
    lines.append("# L3 legs")
    lines += sorted("  " + " | ".join(leg["hops"]) for leg in result["l3"])
    lines.append("# peering interfaces")
    lines += sorted(f"  {p['name']} [{p['scope']}] on {p['transport']} -> {p['lands']}" for p in result["peerings"])
    lines.append("# missing")
    lines += sorted("  " + m for m in result["missing"]) or ["  (none)"]
    return "\n".join(lines) + "\n"


def diff_text(expected: str, actual: str) -> str:
    import difflib

    return "\n".join(
        list(difflib.unified_diff(expected.splitlines(), actual.splitlines(), "golden", "actual", lineterm="", n=0))[
            :20
        ]
    )


def first_broken_hop(leg_hops: list[str]) -> str:
    return leg_hops[-1] if leg_hops else "(no hop)"


def check_pair(rep: Report, case: dict, pair: dict, tracer: ModuleType, d: dict, update_golden: bool) -> None:
    dc, cloud = pair["dc_segment"], pair["cloud_segment"]
    tag = f"{dc} -> {cloud}"
    empty = [k for k in pair.get("non_empty", []) if not d.get(k)]
    rep.add(f"[{tag}] every object kind of the model non-empty", not empty, f"empty: {empty}")
    result = tracer.main_walk(d, dc, cloud, quiet=True)
    missing = result["missing"]
    rep.add(f"[{tag}] no hop marked missing", not missing, "\n".join(missing))
    for layer, expected_key, field in (("l1", "l1_legs", "carrier_type"), ("l3", "l3_legs", "session_types")):
        legs = result[layer]
        rep.add(f"[{tag}] {layer.upper()} reaches the cloud segment", bool(legs), "no leg reaches it")
        counted: Counter[str] = Counter()
        for leg in legs:
            for kind in leg[field] if isinstance(leg[field], list) else [leg[field]]:
                counted[kind] += 1
        want = pair.get(expected_key) or {}
        if want:
            rep.add(
                f"[{tag}] {layer.upper()} legs per {'carrier' if layer == 'l1' else 'session circuit'} type",
                dict(counted) == want,
                f"expected {want}, got {dict(counted)}",
            )
    # reverse: from the cloud segment's virtual network, every peering that lands in it is reached from the DC segment
    csg = next((s for s in d["csg"] if s["name"]["value"] == cloud), None)
    vname = ((csg or {}).get("virtual_network") or {}).get("node", {}).get("name", {}).get("value")
    reverse = {p["name"] for p in result["peerings"] if vname and f"virtual network {vname}" in (p["lands"] or "")}
    forward = {leg["peering"] for leg in result["l1"]} | {leg["peering"] for leg in result["l3"]}
    rep.add(
        f"[{cloud} -> {dc}] reverse: peerings landing in {vname} are the ones the DC reaches",
        bool(reverse) and reverse == forward,
        f"only from the cloud side: {sorted(reverse - forward)}; only from the DC side: {sorted(forward - reverse)}",
    )
    golden = GOLDEN_DIR / pair["golden"]
    text = render_golden(result)
    if update_golden:
        golden.write_text(text)
        rep.add(f"[{tag}] golden written", True, f"note: {golden.relative_to(ROOT)}")
    elif not golden.exists():
        rep.add(f"[{tag}] golden", False, f"{golden.relative_to(ROOT)} missing: run with --update-golden")
    else:
        rep.add(f"[{tag}] golden {pair['golden']}", golden.read_text() == text, diff_text(golden.read_text(), text))


# ------------------------------------------------------------------ main flow


async def sentinel_present(client: InfrahubClient, branch: str, sentinel: dict) -> bool:
    kind, name = sentinel["kind"], sentinel["name"]
    r = await client.execute_graphql(query=f'{{ {kind}(name__value: "{name}") {{ count }} }}', branch_name=branch)
    return r[kind]["count"] > 0


async def run_case(
    case: dict, args: argparse.Namespace, client: InfrahubClient, env: dict[str, str], rep: Report
) -> None:
    branch = f"tv-{time.strftime('%m%d-%H%M%S')}"
    t0 = time.monotonic()

    def lap(label: str) -> None:
        say(f"  [{time.monotonic() - t0:6.1f}s] {label}")

    say(f"\n== case {case['name']} on branch {branch}")
    await client.branch.create(branch_name=branch, sync_with_git=False, description="traversal check, throwaway")
    lap("branch created")
    try:
        schema = run(["uv", "run", "infrahubctl", "schema", "load", case["schema"], "--branch", branch], env)
        out = schema.stdout + schema.stderr
        ok = schema.returncode == 0
        hint = ""
        if not ok and ("instance" in out.lower() or "kind" in out.lower()):
            hint = "\n(refusal caused by data in another branch? the stack must be rebuilt: --rebuild)"
        rep.add(f"[{case['name']}] schema load on {branch}", ok, tail(out) + hint)
        lap(f"schema loaded (exit {schema.returncode})")
        if not ok:
            return
        present = await sentinel_present(client, branch, case["sentinel"])
        if present and not args.force_load:
            say("  UWAGA: dane przykladowe sa juz na main (galaz je dziedziczy): ladowanie pominiete, bo zdublowaloby")
            say("         zagniezdzone segment_deployments. Nie sprawdzono, czy pliki danych sie laduja;")
            say("         pelny test ladowania: --rebuild (czysty stos bez danych przykladowych).")
            rep.add(f"[{case['name']}] data load", True, "note: skipped, inherited from main")
        else:
            for data_dir in case["data"]:
                load = run(["uv", "run", "infrahubctl", "object", "load", data_dir, "--branch", branch], env)
                rep.add(f"[{case['name']}] data load {data_dir}", load.returncode == 0, tail(load.stdout + load.stderr))
                lap(f"data loaded {data_dir} (exit {load.returncode})")
                if load.returncode != 0:
                    return
        tracer = load_tracer(ROOT / case["tracer"])
        d = await tracer.load(client, branch)
        lap("graph read")
        for pair in case["pairs"]:
            check_pair(rep, case, pair, tracer, d, args.update_golden)
        lap("asserted")
    finally:
        if args.keep_branch:
            say(f"  branch {branch} kept (--keep-branch); delete: infrahubctl branch delete {branch}")
        else:
            try:
                await client.branch.delete(branch_name=branch)
                lap("branch deleted")
            except Exception as exc:  # noqa: BLE001
                # the server can answer 500 (flow-run read race) after it has deleted the branch: ask before warning
                names = {b for b in await client.branch.all()}
                if branch in names:
                    say(f"  UWAGA: nie udalo sie usunac galezi {branch}: {exc}")
                else:
                    lap("branch deleted (server answered with an error after the delete)")


async def amain(args: argparse.Namespace) -> int:
    started = time.monotonic()
    port = args.server_port
    address = f"http://localhost:{port}"
    token = os.environ.get("INFRAHUB_API_TOKEN", DEFAULT_TOKEN)
    env = {"INFRAHUB_ADDRESS": address, "INFRAHUB_API_TOKEN": token}
    cases = yaml.safe_load(CASES_FILE.read_text())["cases"]
    if args.case:
        cases = [c for c in cases if c["name"] == args.case]
        if not cases:
            sys.exit(f"no such case: {args.case}")
    if args.rebuild or not stack_up(address):
        start_stack(port, args.rebuild)
        if not stack_up(address):
            sys.exit(f"stos nie odpowiada na {address}")
    client = InfrahubClient(config=Config(address=address, api_token=token))
    rep = Report()
    try:
        for case in cases:
            await run_case(case, args, client, env, rep)
    finally:
        rep.print()
        say(f"\nczas calkowity: {time.monotonic() - started:.1f}s")
    return 1 if rep.failed else 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--case", default="", help="name of one case from cases.yml (default: all)")
    ap.add_argument("--update-golden", action="store_true", help="write the golden files instead of comparing")
    ap.add_argument("--keep-branch", action="store_true", help="do not delete the tv-* branch (debugging)")
    ap.add_argument("--rebuild", action="store_true", help="clean the test containers and start a fresh stack")
    ap.add_argument("--force-load", action="store_true", help="load the data even if it is already on main")
    ap.add_argument("--server-port", type=int, default=8100)
    sys.exit(asyncio.run(amain(ap.parse_args())))


if __name__ == "__main__":
    main()
