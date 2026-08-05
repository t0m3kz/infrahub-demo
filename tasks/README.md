# Invoke Tasks

All tasks run via `uv run invoke <task>`. Every task below has a top-level
shortcut (e.g. `uv run invoke start`) that aliases the namespaced version
(e.g. `uv run invoke infra.start`) — use whichever you prefer, they're
identical. Run `uv run invoke --list` to see the live list.

## infra — Docker container lifecycle

| Task | Description |
| --- | --- |
| `infra.start` | Start all Infrahub containers. |
| `infra.stop` | Stop all Infrahub containers (keeps volumes). |
| `infra.restart [--component=<name>]` | Restart all containers, or just one (e.g. `infrahub-server`). |
| `infra.destroy` | Stop all containers **and delete volumes** — full reset. |
| `infra.setup` | Start containers (if not running), load schema, menu, and bootstrap data on `main`. One-shot environment bring-up. |
| `infra.register-repo [--ref=<branch>]` | Register the local repo (`/upstream`) with Infrahub as a read-only repository, wait for import, then load event actions. Default ref: `routing`. |

```bash
uv run invoke start
uv run invoke stop
uv run invoke restart --component infrahub-server
uv run invoke destroy
uv run invoke setup
uv run invoke register-repo --ref main
```

## data — schema, menu, and object loading

| Task | Description |
| --- | --- |
| `data.load-schema [--schema=<path>] [--branch=<branch>]` | Load `<path>/base` and `<path>/extensions` schemas. Default path: `./schemas/`. |
| `data.load-menu [--menu=<path>] [--branch=<branch>]` | Load the navigation menu. Default path: `menu`. |
| `data.load-objects [--path=<path>] [--branch=<branch>]` | Load object YAML files from a folder or file. Default path: `data/bootstrap/`. |
| `data.load-data [--name=<script>] [--branch=<branch>]` | Run a Python bootstrap script under `bootstrap/`. Default: `bootstrap.py`. |

All four default to `--branch=main` when omitted.

```bash
uv run invoke load-schema
uv run invoke load-schema --branch my-branch
uv run invoke load-objects --path data/demos/30_all --branch test
uv run invoke load-menu
uv run invoke load-data --name bootstrap.py
```

## dev — code quality, linting, tests

| Task | Description |
| --- | --- |
| `dev.validate` | Run all pre-commit hooks, then smoke + unit tests with coverage. |
| `dev.setup-precommit` | Install pre-commit (`prek`) git hooks. |
| `dev.test-unit [--basetemp=<path>]` | Run unit tests. Default basetemp: `.pytest-tmp`. |
| `dev.test-integration [--basetemp=<path>] [--server-port=<port>] [--tests=<paths>]` | Run integration tests against a live Infrahub (requires Docker). |
| `dev.upgrade` | `uv lock --upgrade` + `prek auto-update` — bump dependencies and hook revisions. |
| `dev.release [--increment=<patch\|minor\|major>]` | Bump version, update CHANGELOG, commit, and tag via commitizen. |
| `dev.clean-testcontainers` | Remove leftover Docker containers/networks/volumes from integration test runs. |

```bash
uv run invoke validate
uv run invoke test-unit
uv run invoke test-integration --server-port 8200
uv run invoke upgrade
uv run invoke release --increment patch
```

## demo — end-to-end demo flows

| Task | Description |
| --- | --- |
| `demo.deploy-dc [--scenario=<name>] [--branch=<branch>]` | Load a single DC scenario's object files (does not run generators). Default scenario: `dc1`. |
| `demo.deploy-universal-topology [--branch=<branch>] [--dcs=<"dc1 dc2">] [--skip-generators] [--dry-run]` | Load the universal-topology demo data, then run `add_mlag`/`add_ha` for the resulting `mlag_domains`/`ha_domains` groups. |
| `demo.run-demo [--phases=<"1 2">] [--dcs=<"dc5 dc6">] [--skip-generators] [--skip-merge] [--dry-run]` | Run the full 7-phase demo flow (DCs → switch → rack → pod → LLM/spines → servers → customers), each phase on its own branch with a proposed change merged to `main`. |

```bash
uv run invoke deploy-dc --scenario dc2 --branch change-1
uv run invoke deploy-universal-topology --dcs "dc1 dc2"
uv run invoke run-demo
uv run invoke run-demo --phases "1 2" --dcs "dc5 dc6"
uv run invoke run-demo --dry-run
```

## Typical first-time setup

```bash
uv run invoke setup           # containers + schema + menu + bootstrap data
uv run invoke register-repo   # register repo, load event actions/generators/checks
uv run invoke run-demo        # populate the full demo dataset
```
