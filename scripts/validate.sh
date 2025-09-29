#!/bin/bash
set -euo pipefail

# Usage: ./validate.sh <DATACENTER> <DEVICE> [BRANCH]
# Example: ./validate.sh DC-1 leaf-1 main

if [[ $# -lt 2 ]]; then
	echo "Usage: $0 <DATACENTER> <DEVICE> [BRANCH]"
	exit 1
fi

BRANCH=${3:-main}
echo "Validating $1 Data Center, device $2 on branch $BRANCH"
uv run infrahubctl check validate_$1 device=$2 --branch $BRANCH
