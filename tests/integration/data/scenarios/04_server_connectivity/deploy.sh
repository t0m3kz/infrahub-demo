#!/bin/bash

# Server Connectivity Cabling Demo
# Generate server connections with dual-uplink redundancy

set -e

# Colors for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}🔌 Starting Server Connectivity Cabling Demo${NC}"

# Use deploy_scenario.sh with --with-servers flag
cd "$(dirname "$0")"/../../scripts

if [ -f "deploy_scenario.sh" ]; then
    ./deploy_scenario.sh "${@}" --with-servers
else
    echo "Error: deploy_scenario.sh not found in scripts folder"
    exit 1
fi

echo -e "${GREEN}✅ Server Connectivity Cabling Demo Complete!${NC}"
echo -e "${BLUE}📊 All servers are connected with dual-uplink redundancy${NC}"
