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

All data are stored as static in bootstrap/data.py file.

### Bootstrap

In this script we're saving data per function called function.

```bash
infrahubctl run bootstrap/bootstrap.py --branch my-branch
```

### False Bootstrap

In this script we're saving data on the end of run function.
This means that we're saving all data in one batch what is causing that relations are not saved properly.

```bash
infrahubctl run bootstrap/bootstrap_false.py --branch my-branch
```

## Demo 1 - Firewall

### Load firewall data

```bash
infrahubctl run bootstrap/demo_firewall.py --branch my-branch
```

### Test firewall config

```bash
infrahubctl render firewall_config device=dc1-fra-fw1 --branch my-branch
```
