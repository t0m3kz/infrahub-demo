#!/usr/bin/env python3
"""Fast model check: schema + a small topology on a throwaway branch, then L1/L3 traversal assertions.

  uv run invoke check-model [--case full_topology] [--update-golden] [--keep-branch] [--rebuild]

No pytest. Steps: stack on :8100 -> branch tv-<timestamp> -> schema load -> data load (ordered, sequential) ->
for each pair of the case: the server chains (utils/path_trace.py), assert, compare with the golden file ->
delete the branch (also on failure). Cases live in tests/traversal/cases.yml.
"""

import argparse
import asyncio
import difflib
import itertools
import os
import re
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml
from infrahub_sdk import Config, InfrahubClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tests.traversal import (  # noqa: E402 - needs ROOT on the path (the script runs from its own directory)
    chain_rows,
    graph_read,
    load_topology,
    walks,
)
from utils import path_trace  # noqa: E402
from utils.path_trace import Leg  # noqa: E402

GOLDEN_DIR = Path(__file__).resolve().parent / "golden"
CASES_FILE = Path(__file__).resolve().parent / "cases.yml"
PAIR_MAX_PATHS = 30  # the server's cost grows with the cap (L3: 20 -> 19 s, 64 -> 77 s); a pair may ask for up to 100
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


# ------------------------------------------------------------------ chains, golden


def diff_text(expected: str, actual: str) -> str:
    return "\n".join(
        list(difflib.unified_diff(expected.splitlines(), actual.splitlines(), "golden", "actual", lineterm="", n=0))[
            :20
        ]
    )


def check_golden(rep: Report, tag: str, name: str, text: str, update_golden: bool) -> None:
    """Compare a text with its golden file (or write it with --update-golden)."""
    golden = GOLDEN_DIR / name
    if update_golden:
        golden.write_text(text)
        rep.add(f"[{tag}] golden written", True, f"note: {golden.relative_to(ROOT)}")
    elif not golden.exists():
        rep.add(f"[{tag}] golden", False, f"{golden.relative_to(ROOT)} missing: run with --update-golden")
    else:
        rep.add(f"[{tag}] golden {name}", golden.read_text() == text, diff_text(golden.read_text(), text))


async def traced(
    rep: Report,
    tag: str,
    client: InfrahubClient,
    branch: str,
    src: str,
    dst: str,
    max_paths: int = PAIR_MAX_PATHS,
    **kwargs: Any,
) -> list[Leg] | None:
    """The server's chains between two nodes; an end that does not resolve is a failed row, not a crash."""
    try:
        return await path_trace.trace(client, src, dst, branch=branch, max_paths=max_paths, **kwargs)
    except path_trace.ResolveError as exc:
        rep.add(f"[{tag}] ends resolve", False, str(exc))
        return None


async def check_pair(
    rep: Report, case: dict, pair: dict, client: InfrahubClient, branch: str, d: dict, update_golden: bool
) -> None:
    """One pair through the server's tracer: the plain trace, one trace per transport it must be reached through, the
    reverse direction, the circuit types per transport, then the golden (the sorted equal-cost chains)."""
    src, dst = pair["src"], pair["dst"]
    tag = f"{src} -> {dst}"
    layers = tuple(pair.get("layers") or ("l1", "l3"))
    cap = pair.get("max_paths", PAIR_MAX_PATHS)  # a pair from a tenant segment crosses the whole pod: its set is larger
    plain = await traced(rep, tag, client, branch, src, dst, cap, layers=layers)
    if plain is None:
        return
    if pair.get("expect_unreachable"):
        for row in chain_rows.unreachable_rows(tag, plain):
            rep.add(*row)
        return
    kinds = pair.get("far_end_kinds", [])
    for row in chain_rows.reach_rows(tag, plain):
        rep.add(*row)
    kind_of = chain_rows.circuit_types(d)
    sections = [("plain", plain)]
    if pair.get("circuit_types"):  # the hops of the shortest chains between two sites: carriers in l1, sessions in l3
        for row in chain_rows.type_rows(f"{tag} plain", plain, pair["circuit_types"], kind_of):
            rep.add(*row)
    for transport, want in (pair.get("through") or {}).items():
        legs = await traced(
            rep, f"{tag} via {transport}", client, branch, src, dst, cap, via=transport, layers=tuple(want)
        )
        if legs is None:
            continue
        sections.append((f"via {transport}", legs))
        vtag = f"{tag} via {transport}"
        rows = [
            *chain_rows.reach_rows(vtag, legs),
            *chain_rows.transport_rows(vtag, kinds, transport),
            *chain_rows.type_rows(vtag, legs, want, kind_of),
        ]
        for row in rows:
            rep.add(*row)
    backward = await traced(rep, f"{dst} -> {src}", client, branch, dst, src, cap, layers=layers)
    if backward is not None:
        for row in chain_rows.reverse_rows(f"{dst} -> {src}", plain, backward):
            rep.add(*row)
    for row in walks.transport_rows(d, tag, pair.get("transport_kinds") or {}):
        rep.add(*row)
    check_golden(rep, tag, pair["golden"], chain_rows.golden_text(src, dst, sections), update_golden)


async def check_pairs(
    rep: Report,
    case: dict,
    pairs: list[dict],
    client: InfrahubClient,
    branch: str,
    d: dict,
    update_golden: bool,
    jobs: int,
    lap: Callable[[str], None],
) -> None:
    """The pairs, `jobs` of them at a time. A pair writes its rows into a report of its own and the reports are merged in
    the order of cases.yml, so the output does not depend on which trace finishes first."""
    gate = asyncio.Semaphore(max(1, jobs))

    async def one(pair: dict) -> Report:
        part = Report()
        async with gate:
            await check_pair(part, case, pair, client, branch, d, update_golden)
        lap(f"pair {pair['src']} -> {pair['dst']}")
        return part

    for part in await asyncio.gather(*(one(pair) for pair in pairs)):
        rep.rows.extend(part.rows)


REACH_ATTEMPTS = 3  # a far-end trace whose search ran out of budget is asked again, up to this many times


def reach_jobs(cfg: dict) -> list[tuple[str, str, str, str | None]]:
    """The traces of the far-end matrix: every pair of `l3` ends in L3, and each `l1` pair (with its waypoint, when the
    tracer's depth limit needs one) in L1."""
    jobs: list[tuple[str, str, str, str | None]] = [
        ("l3", a, b, None) for a, b in itertools.combinations(cfg.get("l3") or [], 2)
    ]
    jobs += [("l1", p["src"], p["dst"], p.get("via")) for p in cfg.get("l1") or []]
    return jobs


async def check_reachability(
    rep: Report, cfg: dict, client: InfrahubClient, branch: str, jobs: int, lap: Callable[[str], None]
) -> None:
    """The far ends reach each other: ONE shortest path per pair and layer, no ECMP set and no golden (those are the
    pairs'). The ends are where a user, a server or a workload sits (an access point, a VM, a cloud segment, THE
    INTERNET), so a broken hop anywhere between them fails a row, whichever box it is on."""
    gate = asyncio.Semaphore(max(1, jobs))

    async def one(layer: str, src: str, dst: str, via: str | None) -> Report:
        part = Report()
        title = f"reach {layer.upper()}: {src} -> {dst}" + (f" via {via}" if via else "")
        async with gate:
            for _attempt in range(REACH_ATTEMPTS):
                legs = await traced(part, title, client, branch, src, dst, 1, layers=(layer,), via=via)
                # a leg with no path is only a failure when the search ended: the server also stops when its budget runs
                # out (a busy stack), which says nothing about the path, so that one is asked again
                if legs is None or not any(not leg.paths and leg.truncated_at for leg in legs):
                    break
        if legs is not None:
            missing = [f"{leg.src.display_label} -> {leg.dst.display_label}" for leg in legs if not leg.paths]
            budget = [str(leg.truncated_at) for leg in legs if not leg.paths and leg.truncated_at]
            note = f"no path for {missing}" + (f" (budget ran out at depth {budget})" if budget else "")
            part.add(title, bool(legs) and not missing, note)
        return part

    work = reach_jobs(cfg)
    for part in await asyncio.gather(*(one(*job) for job in work)):
        rep.rows.extend(part.rows)
    lap(f"reachability ({len(work)} traces)")


def select_pairs(pairs: list[dict], pattern: str) -> list[dict]:
    """The pairs whose `src dst golden` text matches the regular expression (all of them for an empty pattern)."""
    if not pattern:
        return pairs
    return [p for p in pairs if re.search(pattern, f"{p['src']} {p['dst']} {p.get('golden', '')}")]


async def check_pod(rep: Report, cfg: dict, client: InfrahubClient, branch: str, d: dict) -> None:
    """The pod's paths as the server returns them: the shortest server -> edge set (the direct uplink), leaf -> spine ->
    border leaf for every (leaf, border leaf) pair (first leg of a `--via` trace), both leaves -> edge in L3."""
    role = walks.role_of(d)
    kind = "DcimPhysicalDevice:"
    direct = await traced(rep, "pod", client, branch, cfg["server"], cfg["edge"], layers=("l1",))
    via: dict[tuple[str, str], list] = {}
    for leaf in cfg["leaves"]:
        for border in cfg["border_leaves"]:
            legs = await traced(
                rep, "pod", client, branch, kind + leaf, kind + cfg["edge"], layers=("l1",), via=kind + border
            )
            via[(leaf, border)] = [p for p in legs[0].paths] if legs else []
    l3: dict[str, list] = {}
    for leaf in cfg["leaves"]:
        legs = await traced(rep, "pod", client, branch, kind + leaf, kind + cfg["edge"], layers=("l3",))
        l3[leaf] = [p for g in legs or [] for p in g.paths]
    if direct is not None:
        for name, ok, detail in walks.pod_path_rows(cfg, role, [p for g in direct for p in g.equal_cost], via, l3):
            rep.add(f"{name} [{cfg['edge']}]", ok, detail)


async def check_internet(rep: Report, cfg: dict, client: InfrahubClient, branch: str, update_golden: bool) -> None:
    """L1 from the edge port of each ISP to the public side's port of each service, through THE INTERNET."""
    legs: dict[tuple[str, str], Leg] = {}
    for isp in cfg["isps"]:
        for side in cfg["public_sides"]:
            start = f"DcimPhysicalInterface:{cfg['edge']}/{isp['access_port']}"
            goal = f"DcimPhysicalInterface:{side['transport']}/{side['port']}"
            got = await traced(rep, "internet", client, branch, start, goal, layers=("l1",))
            if got:
                legs[(isp["isp_device"], side["endpoint"])] = got[0]
    if len(legs) == len(cfg["isps"]) * len(cfg["public_sides"]):
        for row in chain_rows.internet_rows(cfg, legs):
            rep.add(*row)
        check_golden(rep, "internet", cfg["golden"], chain_rows.internet_golden(cfg, legs), update_golden)


async def check_sites(rep: Report, cfg: dict, client: InfrahubClient, branch: str) -> None:
    """L1 from each site (DC, colocation, office) to THE INTERNET: each reaches it through its own access carrier."""
    legs: dict[str, Leg] = {}
    for site in cfg["sites"]:
        got = await traced(rep, f"site {site['site']}", client, branch, site["node"], cfg["internet"], layers=("l1",))
        if got:
            legs[site["site"]] = got[0]
    for row in chain_rows.site_internet_rows(cfg, legs):
        rep.add(*row)


# ------------------------------------------------------------------ main flow


async def assert_case(
    case: dict,
    client: InfrahubClient,
    branch: str,
    rep: Report,
    update_golden: bool = False,
    lap: Callable[[str], None] = lambda _label: None,
    pair_filter: str = "",
    jobs: int = 1,
    reach_only: bool = False,
) -> None:
    """Read the data of the branch, ask the server for the chains and run every assertion into the report.

    `pair_filter` (a regular expression over `src dst golden`) runs only the pairs it matches and skips the pods, THE
    INTERNET and the sites, which are traced too: the checks that read only the graph still run. `jobs` traces that many
    pairs at once."""
    d = await graph_read.load(client, branch)
    lap("graph read")
    for row in walks.inventory_rows(d, case["non_empty"]):
        rep.add(*row)
    pairs = [] if reach_only else select_pairs(case["pairs"], pair_filter)
    if not pairs and not reach_only:
        rep.add(f"[{case['name']}] pairs matching '{pair_filter}'", False, "no pair of cases.yml matches")
    # the traced parts (pairs, pods, THE INTERNET, sites) are independent: they run together, each into a report of its
    # own, and the rows are put back in the order below
    parts = {name: Report() for name in ("pairs", "pods", "internet", "sites", "reach")}
    full = not pair_filter and not reach_only

    async def trace_pods() -> None:
        for pod in case.get("pods") or []:
            for name, ok, detail in walks.pod_rows(d, pod):
                parts["pods"].add(f"{name} [{pod['edge']}]", ok, detail)
            await check_pod(parts["pods"], pod, client, branch, d)
            lap(f"pod {pod['server']}")

    async def trace_internet() -> None:
        for row in walks.internet_rows(d, case["internet"]):
            parts["internet"].add(*row)
        await check_internet(parts["internet"], case["internet"], client, branch, update_golden)
        lap("internet")

    async def trace_sites() -> None:
        await check_sites(parts["sites"], case["sites"], client, branch)
        lap("sites")

    work = [] if reach_only else [check_pairs(parts["pairs"], case, pairs, client, branch, d, update_golden, jobs, lap)]
    if case.get("reachability") and not pair_filter:
        work.append(check_reachability(parts["reach"], case["reachability"], client, branch, jobs, lap))
    if full:
        work.append(trace_pods())
        if case.get("internet"):
            work.append(trace_internet())
        if case.get("sites"):
            work.append(trace_sites())
    await asyncio.gather(*work)
    rep.rows.extend(parts["pairs"].rows)
    rep.rows.extend(parts["pods"].rows)
    if case.get("transport_sub_interfaces"):
        for row in [*walks.sub_interface_rows(d), *walks.tunnel_underlay_rows(d)]:
            rep.add(*row)
    rep.rows.extend(parts["internet"].rows)
    if not full and case.get("internet"):
        for row in walks.internet_rows(d, case["internet"]):
            rep.add(*row)
    if case.get("layout"):
        for row in walks.layout_rows(d, case["layout"]):
            rep.add(*row)
    rep.rows.extend(parts["sites"].rows)
    rep.rows.extend(parts["reach"].rows)
    check_golden(rep, "inventory", case["inventory_golden"], "\n".join(walks.inventory_lines(d)) + "\n", update_golden)
    declared = walks.declared_rides_on([s for p in case["data"] for s in walks.yaml_documents(ROOT / p)])
    for row in walks.rides_on_rows(d, declared):
        rep.add(*row)
    lap("asserted")


def load_cases(name: str = "") -> list[dict]:
    """The cases of cases.yml (one by name when given; exits when there is no such case)."""
    cases = yaml.safe_load(CASES_FILE.read_text())["cases"]
    if name:
        cases = [c for c in cases if c["name"] == name]
        if not cases:
            sys.exit(f"no such case: {name}")
    return cases


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
        try:
            done = await load_topology.apply(client, branch)
        except RuntimeError as exc:
            rep.add(f"[{case['name']}] data load {case['data']}", False, str(exc))
            return
        rep.add(f"[{case['name']}] data load {case['data']}", True, f"note: {done}")
        lap(f"data loaded: {done}")
        await assert_case(case, client, branch, rep, args.update_golden, lap, args.pair, args.jobs, args.reach)
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
    cases = load_cases(args.case)
    if args.rebuild or not stack_up(address):
        start_stack(port, args.rebuild)
        if not stack_up(address):
            sys.exit(f"stos nie odpowiada na {address}")
    client = InfrahubClient(config=Config(address=address, api_token=token, timeout=path_trace.REQUEST_TIMEOUT))
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
    ap.add_argument("--pair", default="", help="regular expression: only the pairs whose 'src dst golden' matches")
    ap.add_argument(
        "--reach", action="store_true", help="only the far-end reachability matrix: shortest paths, a minute or two"
    )
    ap.add_argument("--jobs", type=int, default=2, help="pairs traced at once (default 2; 1 is the old sequential run)")
    ap.add_argument("--update-golden", action="store_true", help="write the golden files instead of comparing")
    ap.add_argument("--keep-branch", action="store_true", help="do not delete the tv-* branch (debugging)")
    ap.add_argument("--rebuild", action="store_true", help="clean the test containers and start a fresh stack")
    ap.add_argument("--server-port", type=int, default=8100)
    sys.exit(asyncio.run(amain(ap.parse_args())))


if __name__ == "__main__":
    main()
