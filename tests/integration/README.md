# Integration Tests

*Where every generator proves it works before production gets the chance to prove otherwise.*

This directory contains integration tests that validate the complete Infrahub workflow for datacenter infrastructure management.

## Test Structure

- `test_01_setup.py` - Schema loading, menu, and bootstrap data
- `test_02_repository.py` - Git repository integration and sync (registers every query against the live schema)
- `test_04_kicks.py` - every suite kick of `kicks/`, one parametrized test each (`-k <kick>` picks one)

The deployments are **suite kicks**: YAML in `kicks/`, one file per kick, in the format of the demo
kicks of `tasks/kicks.yml` plus an `expect` block per stage. `test_04` runs each through `tasks.run_kick`
(the loop `uv run invoke kick` runs) with `kick_expect.StageChecker` as its `after_stage`:

| Kick | File | What it proves |
| --- | --- | --- |
| `dc6-suite` | `kicks/dc6.yml` | DC6 deployed, then grown change by change: 2 L2 leafs, a rack, a pod, a POD-1 spine, C001's segments, the servers, an application, a port assigned by hand and withdrawn |
| `full-topology-suite` | `kicks/full_topology.yml` | `data/demos/00_full_topology` as one proposed change that passes every check, never merged |

Each stage without `branch:` is one change: branch `<kick>-<stage>` from main, load, generators settle, no failed
task, the assertions on the branch, the proposed change and its validations (artifacts included), the merge, the
assertions on main. Every growth stage asserts that no existing BGP process changed its ASN (renumbering a fabric is
not a growth strategy) and that the diff removes nothing.

### Adding a suite kick

Add `kicks/<name>.yml` with one `kicks:` mapping. Names are unique across the folder and against the demo
kicks. A stage takes the demo keys (`name`, `load`, `generator`, `min_artifacts`, `merge`) plus:

- `load` paths are relative to the **repository root** (`data/demos/...`, `tests/integration/data/...`); demo
  kicks keep theirs relative to `data/demos`
- `run: <generator>` (also allowed in a demo kick): once the load's own triggers settled, the stage runs that
  generator definition on every member of its target group on the branch (`CoreGeneratorDefinitionRun`), as a
  person would by hand, then waits again
- `expect:` the assertions (refused in `tasks/kicks.yml`; the loader hands them over raw, `tasks` never reads
  them):

```yaml
expect:
  count:                  # a list; each item one `<Kind>: {filters}` and one of equals | min | max
    - DcimPhysicalDevice: {role: spine, deployment: DC6-1-POD-1}
      equals: 3
  unchanged:              # kind -> fields every object that existed before the stage keeps
    ManagedBGP: [local_as]
  added:                  # kind -> minimum number of objects of that kind the stage branch's diff adds
    DcimPhysicalDevice: 1
  removed: 0              # the diff removes at most this many objects in total
```

A filter key is a plain name: an attribute (`role`) filters on its value, a relationship (`deployment`) on its peer's
name; a key with `__` (`parent__name__value`) is passed as written, the escape hatch for a hierarchy parent or a peer
without a `name`. `unchanged` compares an attribute's value, a cardinality-one relationship's peer id or a
cardinality-many relationship's sorted peer ids; new objects are allowed, a gone one fails. `count` and `unchanged`
are checked on the branch and on main after the merge (`unchanged` against a snapshot taken right before the load);
`added` and `removed` read the branch's `DiffTreeSummary` once the diff is updated. A failure names the kick,
the stage, the phase and expected against actual. `tests/unit/test_kick_expect.py` holds the format and checks
that every field named exists on its kind in `schemas/`.

`uv run invoke kick <name>` runs a suite kick against a dev stack too, without its assertions.

### Shared Utilities

- `kick_expect.py` - parses and evaluates `expect` (the only assertion code of the suite)
- `workflow_helpers.py` - `object_loader()`, the load function `tasks.run_kick` runs against the test stack
- `wait_helpers.py` - `wait_for_condition()`, a polling utility (repository sync)
- `query_field_collisions.py` - the "Fields ... conflict" walker of the repository sync
- `suite_constants.py` - repository sync and trigger activation timings
- `demo_data.py` - reads the objects the demo files declare (nested ones included), renders computed names and
  counts the runs a load must dispatch; also used by the unit tests
- `conftest.py` - pytest fixtures and the base test class (`execute_command`)

The proposed change flow (`create_proposed_change()`, `update_diff()`, `wait_for_validations()`, `fetch_artifacts()`,
`merge_proposed_change()`) lives in `tasks/proposed_change.py`, shared with `invoke kick`.

## Test Data

```text
data/
├── 20_endpoints/              # C001's application on a DC6 server (dc6-suite, stage application)
├── 20_port_assign/            # A port assigned to c001-web-app-p in DC6 (stage port-assign)
├── 20_port_withdraw/          # The same assignment withdrawn (stage port-withdraw)
├── 20_segments/               # C001's DC6 footprint and three VXLAN segments (stage segments)
└── 60_app_catalogue/          # Application catalogue and deployment requests (read by a unit test)
```

`dc6-suite` loads its fabric, switch, rack, pod, spine and server data straight from `data/demos/`, so the demos
and the tests cannot drift apart. `20_segments` is test-local: `data/demos/07_customers` and `08_segments` depend on
the `30_all` customer organizations, so they cannot be loaded on their own.

## Running Tests

### Run All Integration Tests

```bash
uv run invoke test-integration
```

Run them through Invoke rather than calling `pytest` directly. `_run_integration_suite` in `tasks/__init__.py` sets two
things bare `pytest` does not, and each one fails in a way that does not look like its cause:

| Set by the task | What happens without it |
| --- | --- |
| `INFRAHUB_TESTING_ENABLE_INTEGRATION=1` | Every test is **silently skipped**. A run reporting `16 skipped` tested nothing, and exits 0. |
| `--basetemp ~/.pytest-tmp/infrahub-demo` | On Docker Desktop the stack dies ~20s in, before collection. See below. |

A green run that tested nothing is the most dangerous kind of green.

**The basetemp trap.** `InfrahubDockerCompose.init()` writes `docker-compose.yml` and `haproxy.cfg` into pytest's
basetemp, and the `infrahub-server-lb` service bind-mounts `./haproxy.cfg`. pytest's default basetemp is under
`/private/var/folders/...`, which Docker Desktop does not share with its VM by default. Docker answers an
unshareable bind source by creating an **empty directory** at the mount point, so HAProxy is handed a directory
where its config should be, prints its usage text and exits 1 — and `up --wait` then aborts the whole stack with
every other service healthy. The symptom is `Failed to start docker compose ... returned non-zero exit status 1`
plus `infrahub-server-lb-1 exited (1)`, which reads like a port conflict and is not one
(many an afternoon has gone to it). Confirm a path is
shareable with:

```bash
docker run --rm -v /tmp/somefile:/x --entrypoint sh haproxy:3.1-alpine \
  -c 'if [ -d /x ]; then echo "DIRECTORY (mount broken)"; else echo "file, $(wc -c </x) bytes"; fi'
```

`~/.pytest-tmp` sits under `/Users`, which Docker Desktop shares out of the box. Use the tasks, or add your
basetemp to Docker Desktop → Settings → Resources → File sharing.

The focused profiles reuse one Docker stack per command:

```bash
# Setup and repository prerequisites
uv run invoke test-integration-fast

# Full integration matrix
uv run invoke test-integration
```

The fast profile stops after setup, repository synchronization and the `full-topology-suite` kick. The full profile runs every suite kick.

### Not covered after the 30_all suite was removed

The `30_all` demo loads with `uv run invoke load-all-demo`, but nothing asserts on the result any more:

- Re-run idempotency (was `test_65`)
- Generator cleanup on removal (was `test_66`)
- Every dci/handoff circuit has its sessions
- Generator completeness: all generators dispatched, object counts, the device set per design, a deployment per scope DC, the terminated segment's border ports
- Application dependency rules (was `test_60`)
- Minor: `virtual_circuits` group membership, the `inbound_rules` mirror

### Run Setup Only

```bash
uv run invoke test-integration-fast
```

### Validate the Queries First

Queries are validated by the smoke tests: every query under `queries/` has a `syntax_check` entry in
`tests/smoke/test_graphql.yml`, run by `uv run invoke validate`. For any schema, query or generator change, run the
fast loop first:

```bash
uv run invoke test-integration-fast
```

It loads the full-topology sample as a proposed change on its own branch (never merged into `main`) and requires every check to pass; its repository sync (`test_02`) registers every query of `.infrahub.yml` against the live schema, so a field the schema
rejects (`Cannot query field`, `Unknown type`, a "Fields ... conflict") fails there in minutes; the task worker log
(`docker logs <project>-task-worker-1`) names the field. A query under `tests/` is not registered: the test that runs
it is its check.

### Run One Kick

Keep the setup pair in front (session-scoped dependencies mean a module run without its prerequisites skips rather
than fails) and pick the kick by its name:

```bash
uv run invoke test-integration --tests "tests/integration/test_01_setup.py \
  tests/integration/test_02_repository.py tests/integration/test_04_kicks.py -k dc6-suite"
```

`--server-port` moves the stack off the default 8100 if you need two runs side by side. With
`INFRAHUB_TESTING_KEEP_STACK=1` the stack stays up afterwards, every stage on its branch `<kick>-<stage>`.

## Troubleshooting

### Test Failures

- **Generated compose project**: `~/.pytest-tmp/infrahub-demo/` — the compose file, `haproxy.cfg` and the logs
- **Infrahub logs**: `docker compose ls -a` to find the live `infrahub-test-*` project, then
  `docker logs <project>-infrahub-server-1`
- **Branch state**: the suite's own GraphQL UI is on the test port (8100 by default), not 8000
- **Task details**: the Tasks page of the stage branch `<kick>-<stage>` named in the failure
- **A stack that never becomes healthy**: on Docker Desktop this is almost always the basetemp/file-sharing trap
  described under [Run All Integration Tests](#run-all-integration-tests), not anything wrong with the tests.
  Work from the outside in: `docker compose ls -a` to find the project, then read *one* service's log rather than
  the conftest's dump of all of them

### Timeout Adjustments

The settle and artifact polls are constants of `tasks/__init__.py` (`SETTLE_*`, `ARTIFACT_*`,
`GENERATOR_RUN_TIMEOUT`), the proposed change ones of `tasks/proposed_change.py`; a longer timeout fixes nothing, it
lets a stuck queue fail at a more leisurely pace.

## Quick Reference

### Common Commands

```bash
# Run everything
uv run invoke test-integration

# Run foundation only
uv run invoke test-integration-fast

# One scenario, with its prerequisites in front
uv run invoke test-integration --tests "tests/integration/test_01_setup.py \
  tests/integration/test_02_repository.py tests/integration/test_04_kicks.py -k full-topology-suite"

# Two runs side by side
uv run invoke test-integration-fast --server-port 8200

# Every kick, demo and suite
uv run invoke kicks
```
