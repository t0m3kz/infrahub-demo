#!/bin/bash
# Multi-scenario deployment script for InfraHub Demo
# Supports deploying multiple infrastructure scenarios with servers and additional services

set -e

# Color codes
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

# Default configuration
SCENARIO=""
INCLUDE_SERVERS="true"
INCLUDE_SECURITY="false"
INCLUDE_LB="false"
INCLUDE_CLOUD_SECURITY="false"
TIMESTAMP=$(date +%s)
BRANCH=""

# Available scenarios (format: scenario:DC-NAME:POD-NAME:RACK-NAME:SERVER-COUNT)
SCENARIOS_CONFIG="
dc1:DC-1:Pod-A1:Rack-A1-1:4:Large data center (2x Super-Spines, Multiple Pods)
dc6:DC-6:Pod-B1:Rack-B1-1:0:Small data center (2x Super-Spines, Single Pod)
dc7:DC-7:Pod-C1:Rack-C1-1:2:Medium data center (2x Super-Spines, Single Pod)
dc8:DC-8:Pod-D1:Rack-D1-1:3:Medium data center (2x Super-Spines, Single Pod)
dc9:DC-9:Pod-E1:Rack-E1-1:4:Large data center (3x Super-Spines, Single Pod)
"

print_usage() {
    cat <<EOF
${BLUE}Multi-Scenario Deployment Script${NC}

${YELLOW}Usage:${NC}
    $0 --scenario <scenario> [--no-servers] [--with-security] [--with-lb] [--with-cloud-security] [--branch <name>]

${YELLOW}Available Scenarios:${NC}
    ${BLUE}dc1${NC} - Large data center (2x Super-Spines, Multiple Pods)
    ${BLUE}dc6${NC} - Small data center (2x Super-Spines, Single Pod)
    ${BLUE}dc7${NC} - Medium data center (2x Super-Spines, Single Pod)
    ${BLUE}dc8${NC} - Medium data center (2x Super-Spines, Single Pod)
    ${BLUE}dc9${NC} - Large data center (3x Super-Spines, Single Pod)

${YELLOW}Options:${NC}
    --scenario <name>          Scenario to deploy (required)
    --no-servers              Skip server connectivity generation
    --with-security           Include security configurations
    --with-lb                 Include load balancer configurations
    --with-cloud-security     Include cloud security configurations
    --branch <name>           Custom branch name (auto-generated if not provided)
    --help, -h                Show this help message

${YELLOW}Examples:${NC}
    # Deploy DC-8 with servers (default)
    $0 --scenario dc8

    # Deploy DC-9 with security
    $0 --scenario dc9 --with-security

    # Deploy DC-1 without servers but with all security features
    $0 --scenario dc1 --no-servers --with-security --with-cloud-security

    # Deploy DC-6 with custom branch name
    $0 --scenario dc6 --branch my-dc6-deployment

EOF
}

parse_scenario() {
    local scenario=$1
    local scenario_line=$(echo "$SCENARIOS_CONFIG" | grep "^$scenario:")

    if [[ -z "$scenario_line" ]]; then
        return 1
    fi

    IFS=':' read -r _ DC_NAME POD_NAME RACK_NAME SERVER_COUNT SCENARIO_DESC <<< "$scenario_line"
}

print_header() {
    echo -e "\n${BLUE}════════════════════════════════════════════════════════════${NC}"
    echo -e "${BLUE}  $1${NC}"
    echo -e "${BLUE}════════════════════════════════════════════════════════════${NC}\n"
}

print_step() {
    echo -e "${YELLOW}[$1]${NC} $2"
}

print_success() {
    echo -e "${GREEN}✓${NC} $1\n"
}

print_error() {
    echo -e "${RED}✗${NC} $1\n"
}

validate_scenario() {
    if [[ ! -d "data/$DC_NAME" ]]; then
        print_error "Scenario data directory not found: data/$DC_NAME"
        exit 1
    fi
}

validate_data_files() {
    local required_files=("00_topology.yml" "01_suites.yml" "02_racks.yml")
    for file in "${required_files[@]}"; do
        if [[ ! -f "data/$DC_NAME/$file" ]]; then
            print_error "Required file missing: data/$DC_NAME/$file"
            exit 1
        fi
    done

    if [[ "$INCLUDE_SERVERS" == "true" && ! -f "data/$DC_NAME/03_servers.yml" ]]; then
        print_error "Server file not found: data/$DC_NAME/03_servers.yml (use --no-servers to skip)"
        exit 1
    fi
}

deploy_core_infrastructure() {
    local step_num=1
    local total_steps=$(( 4 + (INCLUDE_SERVERS == "true" ? 1 : 0) + (INCLUDE_SECURITY == "true" ? 1 : 0) + (INCLUDE_LB == "true" ? 1 : 0) + (INCLUDE_CLOUD_SECURITY == "true" ? 1 : 0) ))

    # Load bootstrap data
    print_step "$step_num/$total_steps" "Loading bootstrap infrastructure"
    uv run infrahubctl object load data/bootstrap/00_groups_servers.yml --branch "$BRANCH" > /dev/null 2>&1
    print_success "Bootstrap data loaded"
    step_num=$((step_num + 1))

    # Load scenario-specific data
    print_step "$step_num/$total_steps" "Loading $DC_NAME infrastructure data"
    uv run infrahubctl object load "data/$DC_NAME/" --branch "$BRANCH" > /dev/null 2>&1
    print_success "$DC_NAME topology, suites, and racks loaded"
    step_num=$((step_num + 1))

    # Generate Data Center
    print_step "$step_num/$total_steps" "Generating Data Center ($DC_NAME) - Super-Spines"
    uv run infrahubctl generator create_dc name="$DC_NAME" --branch "$BRANCH" > /dev/null 2>&1
    print_success "Data Center generated"
    step_num=$((step_num + 1))

    # Generate Pod
    print_step "$step_num/$total_steps" "Generating Pod ($POD_NAME) - Spines + Fabric Cabling"
    uv run infrahubctl generator create_pod name="$POD_NAME" --branch "$BRANCH" > /dev/null 2>&1
    print_success "Pod generated with spine-to-super-spine cabling"
    step_num=$((step_num + 1))

    # Generate Rack
    print_step "$step_num/$total_steps" "Generating Rack ($RACK_NAME) - Leaves + Fabric Cabling"
    uv run infrahubctl generator create_rack name="$RACK_NAME" --branch "$BRANCH" > /dev/null 2>&1
    print_success "Rack generated with leaf-to-spine cabling"
    step_num=$((step_num + 1))

    # Generate Server Connectivity (if enabled)
    if [[ "$INCLUDE_SERVERS" == "true" ]]; then
        print_step "$step_num/$total_steps" "Generating Server Connectivity (Servers → Leaves)"
        uv run infrahubctl generator create_server_connectivity name="$POD_NAME" --branch "$BRANCH" > /dev/null 2>&1
        print_success "Server connectivity generated"
        step_num=$((step_num + 1))
    fi

    # Load security configurations (if enabled)
    if [[ "$INCLUDE_SECURITY" == "true" ]]; then
        print_step "$step_num/$total_steps" "Loading security configurations"
        uv run infrahubctl object load data/security/ --branch "$BRANCH" > /dev/null 2>&1
        print_success "Security configurations loaded"
        step_num=$((step_num + 1))
    fi

    # Load load balancer configurations (if enabled)
    if [[ "$INCLUDE_LB" == "true" ]]; then
        print_step "$step_num/$total_steps" "Loading load balancer configurations"
        uv run infrahubctl object load data/lb/ --branch "$BRANCH" > /dev/null 2>&1
        print_success "Load balancer configurations loaded"
        step_num=$((step_num + 1))
    fi

    # Load cloud security configurations (if enabled)
    if [[ "$INCLUDE_CLOUD_SECURITY" == "true" ]]; then
        print_step "$step_num/$total_steps" "Loading cloud security configurations"
        uv run infrahubctl object load data/cloud_security/ --branch "$BRANCH" > /dev/null 2>&1
        print_success "Cloud security configurations loaded"
        step_num=$((step_num + 1))
    fi
}

print_deployment_summary() {
    echo -e "${BLUE}Deployment Summary:${NC}"
    echo -e "  Branch: ${YELLOW}$BRANCH${NC}"
    echo -e "  Scenario: ${YELLOW}$SCENARIO ($SCENARIO_DESC)${NC}"
    echo -e "  Data Center: ${YELLOW}$DC_NAME${NC}"
    echo -e "  Pod: ${YELLOW}$POD_NAME${NC}"
    echo -e "  Rack: ${YELLOW}$RACK_NAME${NC}\n"

    echo -e "${BLUE}Deployment Configuration:${NC}"
    echo -e "  Servers: ${YELLOW}${INCLUDE_SERVERS}${NC}"
    echo -e "  Security: ${YELLOW}${INCLUDE_SECURITY}${NC}"
    echo -e "  Load Balancer: ${YELLOW}${INCLUDE_LB}${NC}"
    echo -e "  Cloud Security: ${YELLOW}${INCLUDE_CLOUD_SECURITY}${NC}\n"

    echo -e "${BLUE}Verification:${NC}"
    echo "  1. Navigate to InfraHub UI: http://localhost:8000"
    echo "  2. Go to Inventory → Physical Devices"
    if [[ "$INCLUDE_SERVERS" == "true" ]]; then
        echo "  3. Filter by Role: Server (should see $SERVER_COUNT servers)"
    fi
    echo "  4. Go to Connections → Cables"
    echo "  5. Go to IP Management → Prefixes"
    echo "  6. Review deployed infrastructure"

    if [[ "$INCLUDE_SECURITY" == "true" ]]; then
        echo "  7. Go to Security → Security Policies"
    fi
    if [[ "$INCLUDE_LB" == "true" ]]; then
        echo "  8. Go to Services → Load Balancers"
    fi
    if [[ "$INCLUDE_CLOUD_SECURITY" == "true" ]]; then
        echo "  9. Review cloud security integrations"
    fi
    echo ""
}

# Main script logic
if [[ $# -eq 0 ]]; then
    print_usage
    exit 0
fi

while [[ $# -gt 0 ]]; do
    case $1 in
        --scenario)
            SCENARIO="$2"
            shift 2
            ;;
        --no-servers)
            INCLUDE_SERVERS="false"
            shift
            ;;
        --with-security)
            INCLUDE_SECURITY="true"
            shift
            ;;
        --with-lb)
            INCLUDE_LB="true"
            shift
            ;;
        --with-cloud-security)
            INCLUDE_CLOUD_SECURITY="true"
            shift
            ;;
        --branch)
            BRANCH="$2"
            shift 2
            ;;
        --help|-h)
            print_usage
            exit 0
            ;;
        *)
            print_error "Unknown option: $1"
            print_usage
            exit 1
            ;;
    esac
done

# Validate inputs
if [[ -z "$SCENARIO" ]]; then
    print_error "Scenario is required (use --help for more information)"
    exit 1
fi

# Parse scenario configuration
if ! parse_scenario "$SCENARIO"; then
    print_error "Unknown scenario: $SCENARIO"
    print_usage
    exit 1
fi

# Set default branch name if not provided
if [[ -z "$BRANCH" ]]; then
    BRANCH="${SCENARIO}-deployment-${TIMESTAMP}"
    BRANCH="${BRANCH}-$(date +%Y%m%d-%H%M%S)"
fi

# Validate scenario data
validate_scenario
validate_data_files

# Print header
print_header "Multi-Scenario Deployment - $SCENARIO"

# Create branch
print_step "0/7" "Creating branch: $BRANCH"
uv run infrahubctl branch create "$BRANCH" > /dev/null 2>&1
print_success "Branch created"

# Deploy infrastructure
deploy_core_infrastructure

# Print summary
print_header "✓ Deployment Successful!"
print_deployment_summary

echo -e "${BLUE}Next Steps:${NC}"
echo "  1. Review topology in InfraHub UI"
echo "  2. Verify resource allocation"
echo "  3. Run validation checks"
echo "  4. Propose changes if satisfied"
echo "  5. Merge with main branch\n"

echo -e "${YELLOW}Branch for testing: $BRANCH${NC}"
echo -e "${BLUE}════════════════════════════════════════════════════════════${NC}\n"

