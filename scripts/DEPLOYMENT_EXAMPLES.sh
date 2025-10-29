#!/bin/bash
# Quick Usage Examples for Multi-Scenario Deployment Script

# =============================================================================
# Example 1: Basic DC-8 Deployment with Servers
# Branch: dc8-deployment-<timestamp>-<date-time>
# =============================================================================
echo "Example 1: Deploy DC-8 with servers"
echo "./scripts/deploy_scenario.sh --scenario dc8"
echo ""

# =============================================================================
# Example 2: DC-1 Large Environment with Security
# Branch: dc1-deployment-<timestamp>-<date-time>
# =============================================================================
echo "Example 2: Deploy DC-1 with security policies"
echo "./scripts/deploy_scenario.sh --scenario dc1 --with-security"
echo ""

# =============================================================================
# Example 3: DC-6 Minimal Deployment (No Servers)
# Branch: dc6-deployment-<timestamp>-<date-time>
# =============================================================================
echo "Example 3: Deploy DC-6 infrastructure only (no servers)"
echo "./scripts/deploy_scenario.sh --scenario dc6 --no-servers"
echo ""

# =============================================================================
# Example 4: DC-9 Full Stack with All Features
# Branch: dc9-deployment-<timestamp>-<date-time>
# =============================================================================
echo "Example 4: Deploy DC-9 with all features"
echo "./scripts/deploy_scenario.sh --scenario dc9 --with-security --with-lb --with-cloud-security"
echo ""

# =============================================================================
# Example 5: DC-7 with Custom Branch Name
# Branch: production-dc7-v1
# =============================================================================
echo "Example 5: Deploy DC-7 with custom branch name"
echo "./scripts/deploy_scenario.sh --scenario dc7 --branch production-dc7-v1"
echo ""

# =============================================================================
# Example 6: Help & Available Options
# =============================================================================
echo "Example 6: Show help and usage"
echo "./scripts/deploy_scenario.sh --help"
echo ""

# =============================================================================
# Scenario Sizes Reference
# =============================================================================
cat <<'EOF'

AVAILABLE SCENARIOS:
  dc1  - Large     (2x Super-Spines, 4x Servers, Pod-A1, Rack-A1-1)
  dc6  - Small     (2x Super-Spines, 0x Servers, Pod-B1, Rack-B1-1)
  dc7  - Medium    (2x Super-Spines, 2x Servers, Pod-C1, Rack-C1-1)
  dc8  - Medium    (2x Super-Spines, 3x Servers, Pod-D1, Rack-D1-1)
  dc9  - Large     (3x Super-Spines, 4x Servers, Pod-E1, Rack-E1-1)

OPTIONAL FEATURES:
  --no-servers              Skip server connectivity generation
  --with-security           Include security policies and access control
  --with-lb                 Include load balancer configurations
  --with-cloud-security     Include cloud security integrations

BRANCH NAMING:
  Auto-generated: <scenario>-deployment-<unix-timestamp>-<date-time>
  Custom:        <your-custom-name>

Example branch names:
  dc8-deployment-1761736048-20251029-120728  (auto-generated)
  production-dc7-v1                           (custom)

EOF
