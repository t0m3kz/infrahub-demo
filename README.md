# Infrahub demo

Infrahub demo suggestions

## Create new branch

```bash
infrahubctl branch create my-branch
```

## Load schema

```bash
infrahubctl schema load model/base --branch my-branch
```

## Load menu

```bash
infrahubctl menu load menu --branch my-branch
```

## Load initial data

All initial data are stored as static in bootstrap/data_bootstrap.py file.

### Bootstrap

Add data to Infrahub.

```bash
infrahubctl run bootstrap/bootstrap.py --branch my-branch
```

### False Bootstrap

In this script we're tyrying to save data on the end of run function using single batch.
This leads to the situation that relations are not saved properly.

```bash
infrahubctl run bootstrap/bootstrap_false.py --branch my-branch
```

## Demo 1 - Firewall

### Load firewall data

Firewall data are stored as statics in demo_firewall.py file.

```bash
infrahubctl run bootstrap/demo_firewall.py --branch my-branch
```

### Test firewall config

```bash
infrahubctl render firewall_config device=dc1-fra-fw1 --branch my-branch
```

You can try to manually modify data and test is configuration was updated.


or you can use the scripts:

This script will load all schemas and boostrtrap data into main branch and add repository.

```bash
./scripts/bootstrap.sh
```

Demos (each will be in separated branch) can be run using scripts:

```bash
./scripts/demo.sh firewall or router or design
```

