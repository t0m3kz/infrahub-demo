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

```bash
infrahubctl run  bootstrap/bootstrap --branch my-branch
```

All data are stored as static in bootstrap/data.py file.
