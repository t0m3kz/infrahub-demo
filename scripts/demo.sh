#!/bin/bash
set -euo pipefail

# Usage: ./demo.sh <DATA_FILE_PREFIX> [BRANCH]
# Example: ./demo.sh POP-1 change-1

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <DATA_FILE_PREFIX> [BRANCH]"
  exit 1
fi

BRANCH=${2:-main}
echo "---"
echo "Branch: $BRANCH"

if uv run infrahubctl branch list | grep -Eo '│[[:space:]]*[a-zA-Z0-9_-]+[[:space:]]*│' | awk -F'│' '{gsub(/^ +| +$/, "", $2); print $2}' | grep -Fxq "$BRANCH"; then
  echo "Branch $BRANCH exists. Rebasing..."
  uv run infrahubctl branch rebase $BRANCH
else
  echo "Branch $BRANCH does not exist. Creating..."
  uv run infrahubctl branch create "$BRANCH"
fi

echo "Load initial data"
uv run infrahubctl object load "data/$1.yml" --branch "$BRANCH"