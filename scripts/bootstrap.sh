#!/bin/bash
BRANCH=${1:-main}

echo "Load schema"
uv run infrahubctl schema load schemas --branch $BRANCH

echo "Load menu"
uv run infrahubctl menu load menu --branch $BRANCH

echo "Load initial data"
uv run infrahubctl object load data/bootstrap/ --branch $BRANCH

echo "Load security data"
uv run infrahubctl object load data/security/ --branch $BRANCH

echo "Add demo repository"
uv run infrahubctl repository add DEMO https://github.com/t0m3kz/infrahub-demo.git --ref main --read-only

echo "Add event actions"
sleep 15
uv run infrahubctl object load data/events/ --branch $BRANCH

