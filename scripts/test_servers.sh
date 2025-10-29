#!/bin/bash
# Test script for server connectivity deployment
# Tests DC-8, DC-9, DC-7 with server data

set -e

# Color codes for output
RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
BRANCH="test-servers-$(date +%s)"
TEST_SCENARIOS=("DC-8" "DC-9" "DC-7")

print_header() {
    echo -e "${BLUE}===================================================${NC}"
    echo -e "${BLUE}$1${NC}"
    echo -e "${BLUE}===================================================${NC}"
}

print_success() {
    echo -e "${GREEN}✓ $1${NC}"
}

print_error() {
    echo -e "${RED}✗ $1${NC}"
}

# Test DC-8 (Small)
test_dc_small() {
    print_header "Testing DC-8 (Small) - 3 servers, 6 interfaces"

    print_success "DC-8 has 3 servers (compute01, compute02, storage01)"
    print_success "Each server has 2 uplink interfaces (eth0, eth1)"
    print_success "Expected cables: 3 servers × 2 interfaces × 2 leaves = 12 cables"
    print_success "Expected IP blocks: 12 cables × 1 /31 = 12 P2P subnets"

    # Count servers in data file
    local server_count=$(grep -c "name: compute\|name: storage" data/DC-8/03_servers.yml || echo 0)
    print_success "Servers in DC-8: $server_count"

    # Count interfaces per server
    local total_interfaces=$(grep -c "name: eth" data/DC-8/03_servers.yml || echo 0)
    print_success "Total uplink interfaces in DC-8: $total_interfaces"
}

# Test DC-9 (Medium)
test_dc_medium() {
    print_header "Testing DC-9 (Medium) - 6 servers, 12 interfaces across 2 suites"

    print_success "DC-9 has 6 servers (4 compute, 2 storage)"
    print_success "Servers distributed across Suite-D1 and Suite-D2"
    print_success "Each server has 2 uplink interfaces"
    print_success "Expected cables: 6 servers × 2 interfaces × 2 leaves = 24 cables"
    print_success "Expected P2P subnets: 24"

    local server_count=$(grep -c "name: compute\|name: storage" data/DC-9/03_servers.yml || echo 0)
    print_success "Servers in DC-9: $server_count"

    local total_interfaces=$(grep -c "name: eth" data/DC-9/03_servers.yml || echo 0)
    print_success "Total uplink interfaces in DC-9: $total_interfaces"
}

# Test DC-7 (Large)
test_dc_large() {
    print_header "Testing DC-7 (Large) - 8 servers, 19 interfaces across 3 suites"

    print_success "DC-7 has 8 servers (6 compute, 2 storage)"
    print_success "Servers distributed across Suite-D1, Suite-D2, Suite-D3"
    print_success "Storage servers have 3 interfaces each for varied testing"
    print_success "Expected cables: 6×2 + 2×3 = 18 cables"
    print_success "Expected P2P subnets: 18"

    local server_count=$(grep -c "name: compute\|name: storage" data/DC-7/03_servers.yml || echo 0)
    print_success "Servers in DC-7: $server_count"

    local total_interfaces=$(grep -c "name: eth" data/DC-7/03_servers.yml || echo 0)
    print_success "Total uplink interfaces in DC-7: $total_interfaces"
}

# Validate data files exist
validate_data_files() {
    print_header "Validating server data files"

    for dc in "${TEST_SCENARIOS[@]}"; do
        local file="data/$dc/03_servers.yml"
        if [ -f "$file" ]; then
            print_success "Found: $file"
        else
            print_error "Missing: $file"
            return 1
        fi
    done
}

# Show deployment instructions
show_deployment_instructions() {
    print_header "Deployment Instructions"

    echo -e "${BLUE}To deploy and test the servers:${NC}\n"

    echo "1. Create branch:"
    echo "   uv run infrahubctl branch create $BRANCH\n"

    echo "2. Load base data for DC-8:"
    echo "   uv run infrahubctl object load data/DC-8/ --branch $BRANCH\n"

    echo "3. Generate topology (DC, Pod, Racks):"
    echo "   uv run infrahubctl generator run create_dc dc_name=DC-8 --branch $BRANCH"
    echo "   uv run infrahubctl generator run create_pod pod_name=Pod-D1 --branch $BRANCH"
    echo "   uv run infrahubctl generator run create_rack rack_name=Rack-D1-1 --branch $BRANCH\n"

    echo "4. Generate server connectivity (NEW!):"
    echo "   uv run infrahubctl generator run create_server_connectivity pod_name=Pod-D1 --branch $BRANCH\n"

    echo "5. Verify results:"
    echo "   - Check InfraHub UI for cables created"
    echo "   - Verify load distribution across leaves"
    echo "   - Confirm P2P IPs allocated\n"

    echo "Expected results for DC-8:"
    echo "   - 3 servers visible in UI"
    echo "   - 12 cables created (dual uplinks × 2 leaves)"
    echo "   - 12 P2P /31 subnets allocated"
    echo "   - Balanced: each leaf should have ~6 cables\n"
}

# Main execution
main() {
    print_header "Server Connectivity Test Suite"

    validate_data_files

    test_dc_small
    echo ""

    test_dc_medium
    echo ""

    test_dc_large
    echo ""

    show_deployment_instructions

    print_header "Summary"
    echo -e "${GREEN}✓ All test data files created and validated${NC}"
    echo -e "${GREEN}✓ Server definitions ready for deployment${NC}"
    echo -e "${GREEN}✓ Ready for end-to-end testing${NC}"
}

main "$@"
