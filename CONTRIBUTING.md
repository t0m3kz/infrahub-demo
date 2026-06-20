# Contributing

## Prerequisites

- Python 3.10+
- [uv](https://docs.astral.sh/uv/) package manager
- Docker (for integration tests)

```bash
pip install uv
uv sync
```

## Development Workflow

### Branching

| Prefix | Purpose |
| --- | --- |
| `feat/` | New features |
| `fix/` | Bug fixes |
| `chore/` | Maintenance, deps, tooling |
| `docs/` | Documentation only |
| `refactor/` | Code restructuring |

### Commit Messages

This project uses [Conventional Commits](https://www.conventionalcommits.org/)
enforced by `commitizen`. Every commit must follow this format:

```text
<type>(<optional scope>): <short summary>

[optional body]

[optional footer: BREAKING CHANGE: ...]
```

| Type | When to use | Version bump |
| --- | --- | --- |
| `feat` | New feature or capability | minor |
| `fix` | Bug fix | patch |
| `chore` | Maintenance, deps, tooling | patch |
| `docs` | Documentation only | patch |
| `refactor` | Restructure without behaviour change | patch |
| `test` | Adding or fixing tests | patch |
| `perf` | Performance improvement | patch |
| `ci` | CI/CD pipeline changes | patch |
| `feat!` / `BREAKING CHANGE:` footer | Breaking change | major |

**Scope** (optional) narrows the area: `schema`, `generator`, `template`,
`check`, `transform`, `data`, `ci`, `deps`.

Examples:

```text
feat(schema): add VirtCluster node with node pool support
fix(query): use inline fragment for name on ManagedGenericDevice
chore(deps): upgrade infrahub-sdk to 1.9.6
feat(generator)!: rename add_pod generator parameters
```

### Pre-commit hooks

Hooks run automatically on commit. To run manually:

```bash
uv run prek run --all-files
```

### Testing

```bash
# Unit and smoke tests (no Docker required)
uv run pytest tests/unit tests/smoke -v

# Integration tests (requires running Infrahub instance)
uv run pytest tests/integration -v
```

## Pull Requests

- Target `main`
- Keep PRs focused — one concern per PR
- Ensure all CI checks pass before requesting review
- Squash merge is preferred to keep `main` history clean

## Versioning and Releases

This project follows [Semantic Versioning](https://semver.org/).

### Normal PRs — no version bump

Bug fixes, features, and dependabot updates are merged without touching the
version. Just write proper conventional commits and merge.

### Release PR — when you want to ship

When enough has accumulated and you want to publish a release, create a
dedicated release branch:

```bash
git checkout -b release/vX.Y.Z
uv run invoke release        # auto-detects bump level from commits since last tag
                             # or: uv run invoke release --increment minor
git push && git push --tags  # push branch AND tag together
```

Open a PR, merge it. CI detects the `v*` tag push and automatically:

1. Runs tests
2. Creates the GitHub Release with auto-generated release notes

The tag is read directly from the pushed tag (`github.ref_name`) — it is always
in sync with the `pyproject.toml` version because `invoke release` creates both
from the same `cz bump` run.

**Never edit `pyproject.toml` version by hand** — always use `invoke release`.
