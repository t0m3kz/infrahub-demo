# Infrahub demo

Infrahub demo suggestions

## Create new branch

```bash
infrahubctl new branch my-branch
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

### Bootstrap 1

In this script we're saving data per function called function.

```bash
infrahubctl run bootstrap/bootstrap1.py --branch my-branch
```

### Bootstrap 2

In this script we're saving data on the end of run function.

```bash
infrahubctl run bootstrap/bootstrap2.py --branch my-branch
```
