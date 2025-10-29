#!/bin/bash
# Complete DC-8 deployment with server connectivity
# This script deploys the entire 4-layer topology and connects servers

set -e

# Configuration
BRANCH="dc8-complete-$(date +%s)"
DC_NAME="DC-8"
POD_NAME="Pod-D1"
RACK_NAME="Rack-D1-1"

# Color codes
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${BLUE}════════════════════════════════════════════════════════════${NC}"
echo -e "${BLUE}  DC-8 Complete Deployment with Server Connectivity${NC}"
echo -e "${BLUE}════════════════════════════════════════════════════════════${NC}\n"

# Step 1: Create branch
echo -e "${YELLOW}[Step 1/7]${NC} Creating branch: $BRANCH"
uv run infrahubctl branch create $BRANCH
echo -e "${GREEN}✓${NC} Branch created\n"

# Step 2: Load bootstrap data
echo -e "${YELLOW}[Step 2/7]${NC} Loading bootstrap data"
uv run infrahubctl object load data/bootstrap/00_groups_servers.yml --branch $BRANCH > /dev/null 2>&1
echo -e "${GREEN}✓${NC} Bootstrap data loaded\n"

# Step 3: Load DC-8 base data
echo -e "${YELLOW}[Step 3/7]${NC} Loading DC-8 infrastructure data"
uv run infrahubctl object load data/DC-8/ --branch $BRANCH > /dev/null 2>&1
echo -e "${GREEN}✓${NC} DC-8 data loaded (topology, suites, racks, servers)\n"

# Step 4: Generate Data Center (Super-Spines)
echo -e "${YELLOW}[Step 4/7]${NC} Generating Data Center ($DC_NAME) - Super-Spines"
uv run infrahubctl generator create_dc name=$DC_NAME --branch $BRANCH > /dev/null 2>&1
echo -e "${GREEN}✓${NC} Data Center generated\n"

# Step 5: Generate Pod (Spines + Spine↔Super-Spine cabling)
echo -e "${YELLOW}[Step 5/7]${NC} Generating Pod ($POD_NAME) - Spines + Fabric Cabling"
uv run infrahubctl generator create_pod name=$POD_NAME --branch $BRANCH > /dev/null 2>&1
echo -e "${GREEN}✓${NC} Pod generated with spine-to-super-spine cabling\n"

# Step 6: Generate Racks (Leaves + Leaf↔Spine cabling)
echo -e "${YELLOW}[Step 6/7]${NC} Generating Rack ($RACK_NAME) - Leaves + Fabric Cabling"
uv run infrahubctl generator create_rack name=$RACK_NAME --branch $BRANCH > /dev/null 2>&1
echo -e "${GREEN}✓${NC} Rack generated with leaf-to-spine cabling\n"

# Step 7: Generate Server Connectivity
echo -e "${YELLOW}[Step 7/7]${NC} Generating Server Connectivity (Servers → Leaves)"
uv run infrahubctl generator create_server_connectivity name=$POD_NAME --branch $BRANCH > /dev/null 2>&1
echo -e "${GREEN}✓${NC} Server connectivity generated\n"

# Summary
echo -e "${BLUE}════════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}✓ Complete Deployment Successful!${NC}\n"

echo -e "${BLUE}Deployment Summary:${NC}"
echo -e "  Branch: ${YELLOW}$BRANCH${NC}"
echo -e "  Data Center: ${YELLOW}$DC_NAME${NC}"
echo -e "  Pod: ${YELLOW}$POD_NAME${NC}"
echo -e "  Rack: ${YELLOW}$RACK_NAME${NC}\n"

echo -e "${BLUE}Infrastructure Created:${NC}"
echo "  • 2 Super-spines (DC-level)"
echo "  • 2 Spines (Pod-level)"
echo "  • 4 Leaves (Rack-level)"
echo "  • 3 Servers with uplinks"
echo "  • 4 Spine↔Super-spine cables (P2P /31)"
echo "  • 8 Leaf↔Spine cables (P2P /31)"
echo "  • 12 Server↔Leaf cables (P2P /31)"
echo "  • 24 P2P /31 subnets allocated\n"

echo -e "${BLUE}Verification:${NC}"
echo "  1. Navigate to InfraHub UI: http://localhost:8000"
echo "  2. Go to Inventory → Physical Devices"
echo "  3. Filter by Role: Server (should see 3 servers)"
echo "  4. Go to Connections → Cables"
echo "  5. Should see 24 cables total (4 + 8 + 12)"
echo "  6. Go to IP Management → Prefixes"
echo "  7. Should see 24 P2P /31 allocations\n"

echo -e "${BLUE}Next Steps:${NC}"
echo "  1. Review topology in InfraHub UI"
echo "  2. Verify load distribution across leaves"
echo "  3. Check P2P subnet allocation"
echo "  4. Propose changes if satisfied\n"

echo -e "${YELLOW}Branch for testing: $BRANCH${NC}"
echo -e "${BLUE}════════════════════════════════════════════════════════════${NC}\n"
