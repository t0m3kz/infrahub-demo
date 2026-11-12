# Contributing

This guide covers the development workflow: environment, making a change, testing, commits, pull requests and
releases. The data model rules and conventions are in [AGENTS.md](AGENTS.md); the fabric concepts are in
[IMPLEMENTATION.md](IMPLEMENTATION.md).

## Prerequisites

- Python 3.11 to 3.14
- [uv](https://docs.astral.sh/uv/) package manager
- Docker (local Infrahub stack and integration tests)

```bash
uv sync --group dev
uv run invoke setup-precommit   # install the pre-commit hooks
```

A local stack is started and loaded with `uv run invoke setup`, and the repository is registered with
`uv run invoke register-repo --ref <branch>` (see the [README](README.md#quick-start)).

## Invoke Tasks

| Task | Purpose |
| ---- | ------- |
| `setup` | Start the containers, load schemas, menu and bootstrap data |
| `start`, `stop`, `restart`, `destroy` | Manage the local Infrahub containers (`destroy` removes the volumes) |
| `register-repo --ref <branch>` | Register this repository and load the event rules |
| `load-schema`, `load-menu`, `load-objects --path <dir>` | Load schemas, the menu or object files (`--branch` to target a branch) |
| `load-all-demo` | Load `data/demos/30_all` stage by stage, waiting for the generators (`--from-stage` to resume) |
| `flatten-queries` | Regenerate `queries/flat/` from the query sources |
| `validate` | Pre-commit hooks, smoke and unit tests with coverage |
| `test-unit` | Unit tests |
| `test-integration-fast`, `test-integration-all-demo`, `test-integration-routing`, `test-integration-all-demo-chain`, `test-integration` | Integration profiles (see [Testing](#testing)) |
| `clean-testcontainers` | Remove leftover integration test containers, networks and volumes |
| `reset-generator-groups` | Empty generator tracking groups (see the task docstring before use) |
| `upgrade` | Upgrade the lockfile and pre-commit hook revisions |
| `release` | Bump the version, update the changelog and tag |

`uv run invoke --list` shows every task; `uv run invoke --help <task>` shows its options.

## Making a Change

1. **Branch** from `main` (see [Branching](#branching)).
2. **Schema first.** If the data model changes, update `schemas/` before the code. The schema is the single source
   of truth; generators, transforms and checks derive from it. `infrahubctl schema load` is additive: a change of
   `inherit_from`, `hierarchy` or `parent` on a kind with instances needs a fresh stack.
3. **Reuse before writing.** Search for the flow you are about to implement (an allocation, a template stanza, a
   lookup). If it exists, call it or move it into a shared helper; do not copy it. Values used in more than one place
   belong in `utils/limits.py`.
4. **Implement** the generator, transform, check or template. Every generator must be idempotent: running it twice
   produces no duplicates and no errors.
5. **Queries.** Edit the sources under `queries/` (and their fragments), never `queries/flat/`, then run
   `uv run invoke flatten-queries`. Add a `syntax_check` entry to `tests/smoke/test_graphql.yml` for every new
   registered query.
6. **Templates.** A template refactor must keep the rendered output byte-identical: render the golden fixtures
   before and after and compare. A new platform is a `DcimPlatform` record plus
   `templates/configs/<role>/<platform name>.j2`.
7. **Tests.** Add or update tests for both the success and the failure path (see [Testing](#testing)).
8. **Validate** with `uv run invoke validate`, and run the integration profile the change requires.

### Code standards

- Format and lint: `uv run ruff check . --fix` (also run by the pre-commit hooks).
- Type check: `uv run ty check .`
- No Python module above 1000 lines (`tests/unit/test_test_file_sizes.py`); split by topic into mixins or sibling
  modules.
- Type hints and docstrings on all functions, including tests.
- Data under `data/` is YAML only; anything that computes belongs in `generators/`, `utils/` or `tests/`.
- Markdown: blank lines around fenced code blocks and lists, spaces around the pipes of table separator rows.

## Testing

### Test suites

| Suite | Location | Needs Docker | Run with |
| ----- | -------- | ------------ | -------- |
| Unit | `tests/unit/` | No | `uv run invoke test-unit` |
| Smoke (query syntax, golden structure) | `tests/smoke/` | No | `uv run invoke validate` |
| Integration | `tests/integration/` | Yes | `uv run invoke test-integration-*` |

Unit tests mock the Infrahub SDK and GraphQL responses; canned data lives in `tests/unit/simulators/`. Use the
`root_dir` fixture instead of hard-coded paths.

### Integration profiles

Integration tests share one Infrahub stack per pytest run on port 8100 (`--server-port` moves it). Run one stack at
a time and clean the previous one first with `uv run invoke clean-testcontainers`.

| Profile | Tests | Purpose |
| ------- | ----- | ------- |
| `test-integration-fast` | `test_01`, `test_02`, `test_03` | Setup, repository sync (registers every query against the live schema) and the `00_full_topology` sample on its own branch; minutes |
| `test-integration-all-demo` | `test_01`, `test_02`, `test_59` to `test_66` | The `30_all` demo: staged load, generator dispatch, compute, interconnects, firewall and load-balancer configs, idempotency (`test_65`) and circuit lifecycle (`test_66`) |
| `test-integration-routing` | `test_01`, `test_02`, `test_10` | DC deployment and routing regression |
| `test-integration-all-demo-chain` | `test_01`, `test_02`, `test_58` | `30_all` as a chain of reviewed changes (prototype): each step of `ALL_DEMO_CHAIN` in `tasks.py` on its own branch, validated and merged into `main`; logs the seconds per phase. Merges into `main`, so it runs on its own stack |
| `test-integration` | All | Every integration test, including the full DC matrix |

Set `INFRAHUB_TESTING_KEEP_STACK=1` to keep the stack running after the fast profile for manual inspection.

### Which run a change needs

| Change touches | Run |
| -------------- | --- |
| Schema, queries, model checks, `data/demos/00_full_topology` | `test-integration-fast` |
| Generators and their helpers, `data/bootstrap`, `data/demos/30_all`, queries used by generators | `test-integration-fast`, then `test-integration-all-demo` including `test_65` and `test_66` |
| Templates, transforms, checks and their queries | `test-integration-fast`, then `test_01`, `test_02`, `test_59` to `test_64`, plus the smoke golden diff |
| Documentation or tests only | `uv run invoke validate` |

Run `test-integration-fast` before any longer profile after a schema, query or generator change: a field the schema
rejects fails in `test_02` within minutes. After adding a check or transform query, also run it against a kept stack
and on real data; unit tests with canned responses do not catch schema-invalid queries or false positives.

Run `test_65` (idempotency) on `main` before every release.

### macOS with Colima

Colima does not share `/var/folders` with its VM, which breaks the bind mounts of integration stacks started from a
pytest temporary directory. The invoke test tasks use a repository-local `--basetemp`; when running pytest directly,
pass one yourself:

```bash
INFRAHUB_TESTING_ENABLE_INTEGRATION=1 uv run pytest -vv tests/integration --basetemp .pytest-tmp
```

## Branching

| Prefix | Purpose |
| ------ | ------- |
| `feat/` | New features |
| `fix/` | Bug fixes |
| `chore/` | Maintenance, dependencies, tooling |
| `docs/` | Documentation only |
| `refactor/` | Code restructuring |

Do not commit directly to `main`.

## Commit Messages

This project uses [Conventional Commits](https://www.conventionalcommits.org/), enforced by `commitizen`:

```text
<type>(<optional scope>): <short summary>

[optional body: explain why, not what]

[optional footer: BREAKING CHANGE: ...]
```

| Type | When to use | Version bump |
| ---- | ----------- | ------------ |
| `feat` | New feature or capability | minor |
| `fix` | Bug fix | patch |
| `chore` | Maintenance, dependencies, tooling | patch |
| `docs` | Documentation only | patch |
| `refactor` | Restructure without behaviour change | patch |
| `test` | Adding or fixing tests | patch |
| `perf` | Performance improvement | patch |
| `ci` | CI/CD pipeline changes | patch |
| `feat!` / `BREAKING CHANGE:` footer | Breaking change | major |

Scopes (optional): `schema`, `generator`, `template`, `check`, `transform`, `data`, `ci`, `deps`.

```text
feat(schema): add VirtCluster node with node pool support
fix(query): use inline fragment for name on ManagedGenericDevice
chore(deps): upgrade infrahub-sdk to 1.9.6
feat(generator)!: rename add_pod generator parameters
```

## Pull Requests

- Target `main` and keep each pull request to one concern.
- Describe the change and list the test profiles you ran.
- All CI checks must pass before review. CI runs lint, type checks, smoke and unit tests on every push and pull
  request; integration tests run in a separate workflow.
- Squash merge is preferred to keep the `main` history clean.

## Versioning and Releases

The project follows [Semantic Versioning](https://semver.org/).

Normal pull requests (features, fixes, dependency updates) are merged without a version bump. To publish a release,
create a dedicated release branch:

```bash
git checkout -b release/vX.Y.Z
uv run invoke release                    # bump level detected from the commits since the last tag
uv run invoke release --increment minor  # or set it explicitly
git push && git push --tags              # push the branch and the tag together
```

Open a pull request and merge it. When CI sees the `v*` tag it runs the tests and creates the release with generated
release notes. The tag and the `pyproject.toml` version come from the same `cz bump` run, so they always match.

Never edit the version in `pyproject.toml` by hand.
