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

Use the following branch naming convention:

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

## Versioning

This project follows [Semantic Versioning](https://semver.org/).

### Automatic version bumps

Every merge to `main` triggers CI to:

1. Run `cz bump --yes` — reads commits since last tag, determines bump level automatically
2. Update `pyproject.toml` and `CHANGELOG.md`
3. Commit and push the new tag to `main`

**No manual version bumping.** Never edit `pyproject.toml` version by hand.

### Publishing a GitHub Release

Version bumps happen silently on every merge. A GitHub Release is published
manually when you want to announce a version:

1. Ensure all desired PRs are merged to `main`
2. Go to **Actions → CI → Run workflow** (select `main` branch)
3. CI reads the current version from `pyproject.toml` and creates the GitHub
   Release with auto-generated release notes

This keeps release noise low — dependabot and minor patches bump the version
automatically; you publish a Release when it actually matters.

## Pull Requests

- Target `main`
- Keep PRs focused — one concern per PR
- Ensure all CI checks pass before requesting review
- Squash merge is preferred to keep `main` history clean
