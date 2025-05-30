# Infrahub demo

[![Ruff][ruff-badge]][ruff-link]
[![Python][python-badge]][python-link]
[![Actions status][github-badge]][github-link]
[![GitHub Discussion][github-discussions-badge]][github-discussions-link]

## Use code spaces

Deploy demo using codespaces 
[![Launch in GitHub Codespaces](https://img.shields.io/badge/Launch%20Infrahub%20Demo-0B6581?logo=github)](https://codespaces.new/t0m3kz/infrahub-demo?devcontainer_path=.devcontainer%2Fdevcontainer.json&ref=stable)

## Install the Infrahub SDK

```bash
uv sync 
```

## Start Infrahub

```bash
uv run invoke start
```

## Setup environment variables

```bash
export INFRAHUB_ADDRESS="http://localhost:8000"
export INFRAHUB_API_TOKEN="06438eb2-8019-4776-878c-0941b1f1d1ec"
```

## Load initial setup

Load schemas

```bash
uv run infrahubctl schema load schemas
```

Load menu

```bash
uv run infrahubctl menu load menu
```

Load demo data

```bash
uv run infrahubctl object load data/bootstrap
```

Add demo repository

```bash
uv run infrahubctl repository add DEMO https://github.com/t0m3kz/infrahub-demo.git --read-only
```

You can also use the script to execute all previous steps

```bash
./scripts/bootstrap.sh
```

## Demo use cases

Currently we have 3 demo use cases: firewall, router and design
You can use the script to generate all use cases (script will create separated branch for each demo)

```bash
./scripts/demo.sh firewall or router or design
```

### Demo 1 - Data Center

In this demo we're genetating confifiguration for composable data center.

```bash
./scripts/demo.sh design
```

If you would like to process all steps manually, you have to follow the steps:

1. Create branch

    ```bash
    infrahubctl branch create my-branch
    ```

2. Load example design data stored as statics in demo_design.py file.

    ```bash
    infrahubctl run bootstrap/demo_design.py --branch my-branch
    ```

  You can review designs in Design Patterns and in Design Elements.

  New deployment shoulkd be added into Services -> Topology Deployments -> Data center

3. Change branch to design 
4. Go to the Actions - > Generator Definitions -> create_dc
5. Select Run -> Selected Targets , select DC-3 and click Run Generator
6. Wait until task is completed
7. Go to the devices and see the generated hosts.
8. Go to the Propose Changes - > New Proposed change
9. Select design as source branch, add name and Create proposed change
10. Wait until all tasks are completed and check the artifacts / data.

[ruff-badge]:
<https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json>
[ruff-link]:
(https://github.com/astral-sh/ruff)
[github-discussions-link]:
<https://github.com/t0m3kz/infrahub-demo/discussions/>
[github-discussions-badge]:
<https://img.shields.io/static/v1?label=Discussions&message=Ask&color=blue&logo=github>
[github-badge]:
<https://github.com/t0m3kz/infrahub-demo/actions/workflows/main.yml/badge.svg?branch=main>
[github-link]:
<https://github.com/t0m3kz/infrahub-demo/actions/workflows/main.yml>
[python-badge]:
<https://img.shields.io/badge/python-3.10%7C3.11%7C3.12-000000?logo=python>
[python-link]:
<https://www.python.org>
