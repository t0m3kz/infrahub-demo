# Deployment Scripts

This directory contains automation scripts for deploying InfraHub infrastructure scenarios.

## Main Scripts

### `deploy_scenario.sh` ⭐ **RECOMMENDED**
**Multi-scenario deployment script with flexible configuration**

Supports deploying multiple infrastructure scenarios with optional features like security, load balancing, and cloud security.

**Usage:**
```bash
./scripts/deploy_scenario.sh --scenario <dc1|dc6|dc7|dc8|dc9> [OPTIONS]
```

**Examples:**
```bash
# Basic deployment
./scripts/deploy_scenario.sh --scenario dc8

# With security
./scripts/deploy_scenario.sh --scenario dc1 --with-security

# Full stack
./scripts/deploy_scenario.sh --scenario dc9 --with-security --with-lb --with-cloud-security

# Without servers
./scripts/deploy_scenario.sh --scenario dc6 --no-servers

# Custom branch
./scripts/deploy_scenario.sh --scenario dc8 --branch my-test-branch
```

**Branch Names:**
- Auto-generated: `<scenario>-deployment-<unix-time>-<date-time>` (e.g., `dc8-deployment-1761736098-20251029-120818`)
- Custom: Use `--branch` flag for custom names

**Supported Options:**
- `--scenario <name>` - Scenario to deploy (required)
- `--no-servers` - Skip server connectivity generation
- `--with-security` - Include security configurations
- `--with-lb` - Include load balancer configurations
- `--with-cloud-security` - Include cloud security configurations
- `--branch <name>` - Custom branch name
- `--help` - Show usage information

**Available Scenarios:**
- `dc1` - Large (2x Super-Spines, 4 servers)
- `dc6` - Small (2x Super-Spines, 0 servers)
- `dc7` - Medium (2x Super-Spines, 2 servers)
- `dc8` - Medium (2x Super-Spines, 3 servers)
- `dc9` - Large (3x Super-Spines, 4 servers)

---

### `deploy_dc8_with_servers.sh`
**Legacy DC-8 specific deployment script**

Kept for backward compatibility. For new deployments, use `deploy_scenario.sh` instead.

---

### Other Scripts

- `bootstrap.sh` - Bootstrap initial environment setup
- `demo.sh` - Run demo scenarios
- `clab.sh` - Containerlab integration
- `generate.sh` - Generate configurations
- `transform.sh` - Transform data
- `validate.sh` - Validate configurations
- `render.sh` - Render templates
- `repo.sh` - Repository management
- `test_servers.sh` - Test server connectivity
- `deploy_dc8_with_servers.sh` - Deploy DC-8 (legacy)

---

## Documentation

- `DEPLOYMENT_SCENARIOS.md` - Comprehensive deployment guide
- `DEPLOYMENT_EXAMPLES.sh` - Copy-paste ready examples
- `MULTI_SCENARIO_DEPLOYMENT_SUMMARY.md` - Implementation summary

---

## Quick Start

### 1. Show Help
```bash
./scripts/deploy_scenario.sh --help
```

### 2. Deploy DC-8 with Servers
```bash
./scripts/deploy_scenario.sh --scenario dc8
```

### 3. Deploy DC-9 with All Features
```bash
./scripts/deploy_scenario.sh --scenario dc9 --with-security --with-lb --with-cloud-security
```

### 4. Check Examples
```bash
cat scripts/DEPLOYMENT_EXAMPLES.sh
```

---

## Features

✅ Multi-scenario support (DC-1 through DC-9)
✅ Optional modular features (servers, security, LB, cloud security)
✅ Timestamped branch names with customization
✅ Comprehensive validation and error handling
✅ Built-in help documentation
✅ Colored output for readability
✅ Progress tracking with step counters
✅ Deployment summary and verification checklist

---

## Data Sources

All scripts utilize data from the `data/` directory:

- `data/bootstrap/` - Common infrastructure templates
- `data/DC-{1,6,7,8,9}/` - Data center specific configurations
- `data/security/` - Security policies (optional)
- `data/lb/` - Load balancer configs (optional)
- `data/cloud_security/` - Cloud security (optional)
- `data/events/` - Event actions (optional)

---

## Branch Management

Branches are automatically created during deployment with unique names:

**Format:** `<scenario>-deployment-<unix-timestamp>-<YYYYMMDD-HHMMSS>`

**Example:** `dc8-deployment-1761736098-20251029-120818`

This ensures:
- Each deployment has a unique branch
- Branches are identifiable by deployment time
- Multiple deployments can coexist
- Easy cleanup and comparison

**Custom branch names:**
```bash
./scripts/deploy_scenario.sh --scenario dc8 --branch my-custom-name
```

---

## Troubleshooting

### Issue: Script not executable
```bash
chmod +x scripts/deploy_scenario.sh
```

### Issue: Unknown scenario error
Check available scenarios:
```bash
./scripts/deploy_scenario.sh --help
```

### Issue: Missing data files
Ensure all required data files exist in `data/<scenario>/`:
- `00_topology.yml`
- `01_suites.yml`
- `02_racks.yml`
- `03_servers.yml` (if using servers)

### Issue: Deployment fails
Review the error message and check:
1. InfraHub is running (`uv run invoke start`)
2. All data files are valid YAML
3. Sufficient resources available
4. No conflicting branches already exist

---

## Related Documentation

- [DEPLOYMENT_SCENARIOS.md](../DEPLOYMENT_SCENARIOS.md) - Detailed design and analysis
- [MULTI_SCENARIO_DEPLOYMENT_SUMMARY.md](../MULTI_SCENARIO_DEPLOYMENT_SUMMARY.md) - Implementation summary
- [DEPLOYMENT_EXAMPLES.sh](./DEPLOYMENT_EXAMPLES.sh) - Usage examples
- [README.md](../README.md) - Project overview

---

## Contributing

To add new scenarios:
1. Create `data/DC-<N>/` directory with required YAML files
2. Update `deploy_scenario.sh` with new scenario mapping
3. Test deployment: `./scripts/deploy_scenario.sh --scenario dc<N>`
4. Document in this README

