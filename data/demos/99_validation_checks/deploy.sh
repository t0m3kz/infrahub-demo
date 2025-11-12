#!/bin/bash

# Validation & Health Checks Demo
# Run comprehensive validation checks on infrastructure

set -e

# Colors for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${BLUE}✅ Starting Validation & Health Checks Demo${NC}"

# Source environment
if [ -f "../../.env" ]; then
    source ../../.env
fi

# Check if Infrahub is running
if ! curl -s -f "${INFRAHUB_ADDRESS:-http://localhost:8000}" > /dev/null 2>&1; then
    echo "Error: Infrahub not running. Please start it first:"
    echo "uv run invoke start"
    exit 1
fi

echo -e "${BLUE}📋 Running validation checks...${NC}"

# Run validation script
cd "$(dirname "$0")"/../../scripts

if [ -f "validate.sh" ]; then
    ./validate.sh "${@}"
else
    echo "Error: validate.sh not found in scripts folder"
    exit 1
fi

echo -e "${GREEN}✅ Validation Checks Complete!${NC}"
echo -e "${BLUE}📊 Review results in Infrahub UI under Services → Health Checks${NC}"
