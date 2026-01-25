# Validation & Health Checks Demo

## ✅ Morpheus's Training Program Verification

*"There is no bug in your network."* – Like Morpheus testing Neo in the training program, our validation checks ensure your network topology is production-ready.

## What Gets Created

**Infrahub Objects Created:**

- CheckDefinition
- ValidationResult

## Validation Coverage

5 comprehensive validation checks:

1. **Spine Validation** - Verify spine switch configuration
2. **Leaf Validation** - Verify leaf switch configuration
3. **Server Connectivity** - Verify all servers are connected
4. **Interface Health** - Verify all interfaces are operational
5. **Configuration Accuracy** - Verify generated configurations

## Quick Start

```bash
./deploy.sh --scenario dc3
```

## Validation Process

Each check validates:

- Device presence and status
- Interface configuration
- Connectivity paths
- Configuration compliance
- Performance metrics

## Output

Validation results include:

- ✅ Passed checks
- ⚠️ Warnings
- ❌ Failed checks
- 📊 Detailed reports

## Check Details

### Spine Validation

- Device count verification
- Interface configuration
- Routing protocol status

### Leaf Validation

- Device count verification
- VLAN configuration
- Spanning tree status

### Server Connectivity

- Server connections verified
- Redundancy confirmed
- Interface status

### Interface Health

- Physical link status
- MTU configuration
- Error counts

### Configuration Accuracy

- Device naming compliance
- IP addressing validation
- Policy enforcement

## Running Checks

All checks run automatically during deployment. To run specific checks:

```bash
uv run infrahubctl check run --check-name spine_validation
```

## Reports

Access validation reports in Infrahub UI:

- Services → Health Checks → [Device Name]
