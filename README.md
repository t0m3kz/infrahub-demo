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
poetry install --no-interaction --no-ansi --no-root
```

## Start Infrahub

```bash
poetry run invoke start
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

### Demo 1 - Firewall

In this demo we're generating configuration for firewalls.

```bash
./scripts/demo.sh firewall
```

or

If you would like to process all steps manually, you have to follow the steps:

1. Create branch

    ```bash
    infrahubctl branch create my-branch
    ```

2. Load example firewall data stored as statics in demo_firewall.py file.

    ```bash
    infrahubctl run bootstrap/demo_firewall.py --branch my-branch
    ```

#### Test firewall config

```bash
infrahubctl render firewall_config device=dc1-fra-fw1 --branch my-branch
```

You can try to manually modify data and check if configuration was updated.

### Demo 2 - POP Router deployment

In this demo we're generating configuration pop routers.

```bash
./scripts/demo.sh router
```

If you would like to process all steps manually, you have to follow the steps:

1. Create branch

    ```bash
    infrahubctl branch create my-branch
    ```

2. Load example router data stored as statics in demo_router.py file.

    ```bash
    infrahubctl run bootstrap/demo_router.py --branch my-branch
    ```

### Demo 3 - Data Center

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
<https://img.shields.io/badge/python-3.9%7C3.10%7C3.11%7C3.12-000000?logo=python>
[python-link]:
<https://www.python.org>
