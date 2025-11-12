#!/bin/bash

# Equinix POP (Point of Presence) Demo
# Deploy distributed Points of Presence across locations

set -e

# Colors for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}📡 Starting Equinix POP Deployment Demo${NC}"

# Use deploy_scenario.sh with --scenario pop
cd "$(dirname "$0")"/../../scripts

if [ -f "deploy_scenario.sh" ]; then
    ./deploy_scenario.sh --scenario pop "${@}"
else
    echo "Error: deploy_scenario.sh not found in scripts folder"
    exit 1
fi

echo -e "${GREEN}✅ Equinix POP Deployment Demo Complete!${NC}"
echo -e "${BLUE}📡 POPs deployed across multiple locations${NC}"
echo ""
echo -e "${BLUE}Optional: Simulate POP network locally${NC}"
echo "uv run invoke clab up --pop"
