# Infrahub demo

Infrahub demo suggestions

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

## Load demo

Load schemas

```bash
poetry run infrahubctl schema load model --wait 10
```

Load menu

```bash
poetry run infrahubctl menu load menu
```

Load demo data

```bash
poetry run infrahubctl run bootstrap/bootstrap.py
```

Add demo repository

```bash
poetry run infrahubctl repository add DEMO https://github.com/t0m3kz/infrahub-demo.git --read-only
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

### Demo 3 - Design

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
