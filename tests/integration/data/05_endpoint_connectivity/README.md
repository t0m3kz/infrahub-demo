# Endpoint Connectivity Test Data

Comprehensive test scenarios for the `add_endpoint` generator covering all three deployment types.

## Purpose

Tests the `add_endpoint` generator's ability to:
- Connect servers to network devices based on deployment type (tor, middle_rack, mixed)
- Match interface speeds (25G/100G) correctly
- Handle dual-homing across consecutive device pairs
- Maintain idempotency when run multiple times
- Query interfaces efficiently with single GraphQL query
- Filter by correct interface roles (server=uplink, switch=customer)

## Prerequisites

- **DC1** exists with multiple PODs configured
- **POD-1**: tor deployment (ToR switches in each rack)
- **POD-2**: mixed deployment (ToR + middle rack Leafs)
- **POD-4**: middle_rack deployment (network racks with Leafs per row)

## Data Files

### Bootstrap Data
- **00_device_types.yml**: Server device type definitions (PowerEdge R640/R740)
- **00_racks.yml**: Compute rack definitions for server placement

### Test Scenarios
- **01_devices.yml**: Original test servers (legacy, for backward compatibility)
- **02_servers_tor_deployment.yml**: ToR deployment test servers
- **03_servers_middle_rack_deployment.yml**: Middle_rack deployment test servers
- **04_servers_mixed_deployment.yml**: Mixed deployment test servers

## Deployment Type Patterns

### 1. ToR Deployment (POD-1)

**Design Pattern**: Each rack has dedicated ToR switches

**Strategy**:
- Server connects to ToR switches **in same rack** first
- Falls back to ToR switches **in same row** if needed
- Dual-homing across two consecutive ToR switches

**Topology**:

**Topology**:

```
DC1-1-POD-1 (tor deployment)
├── Rack 5, Row 1
│   ├── dc1-fab1-pod1-suite1-row1-rack5-tor-01 (ToR switch)
│   ├── dc1-fab1-pod1-suite1-row1-rack5-tor-02 (ToR switch)
│   └── server-tor-01 → connects to tor-01 & tor-02
└── Rack 5, Row 2
    ├── dc1-fab1-pod1-suite1-row2-rack5-tor-01 (ToR switch)
    ├── dc1-fab1-pod1-suite1-row2-rack5-tor-02 (ToR switch)
    └── server-tor-02 → connects to tor-01 & tor-02
```

**Test Servers**:
- `server-tor-01`: Rack 5, Row 1, 4x 25G interfaces
- `server-tor-02`: Rack 5, Row 2, 4x 25G interfaces

**Expected Behavior**:
- Server finds ToR switches in same rack
- Dual-homing: alternates between tor-01 and tor-02
- Sequential interface consumption (Ethernet1/1/1, 1/1/2, 1/1/3, 1/1/4)
- No IP allocation (pool=none)

---

### 2. Middle_Rack Deployment (POD-4)

**Design Pattern**: One network rack per row with Leaf switches serving multiple compute racks

**Strategy**:
- Server in **compute rack** connects to Leaf switches in **network rack** (middle rack) in **same row**
- Network rack contains Leaf switches that aggregate traffic from compute racks
- Dual-homing across two consecutive Leaf switches

**Topology**:

```
DC1-1-POD-4 (middle_rack deployment)
├── Row 1
│   ├── Rack 1 (network rack - middle)
│   │   ├── dc1-fab1-pod4-suite1-row1-rack1-leaf-01 (Leaf switch)
│   │   └── dc1-fab1-pod4-suite1-row1-rack1-leaf-02 (Leaf switch)
│   └── Rack 10 (compute rack)
│       └── server-middle-01 → connects to leaf-01 & leaf-02 in network rack
└── Row 2
    ├── Rack 1 (network rack - middle)
    │   ├── dc1-fab1-pod4-suite1-row2-rack1-leaf-01 (Leaf switch)
    │   └── dc1-fab1-pod4-suite1-row2-rack1-leaf-02 (Leaf switch)
    └── Rack 10 (compute rack)
        └── server-middle-02 → connects to leaf-01 & leaf-02 in network rack
```

**Test Servers**:
- `server-middle-01`: Compute Rack 10, Row 1, 4x 25G interfaces
- `server-middle-02`: Compute Rack 10, Row 2, 4x 25G interfaces

**Expected Behavior**:
- Server searches for network rack (rack_type="network") in same row
- Connects to Leaf switches in middle rack, not ToRs
- Dual-homing across two Leaf switches
- Sequential interface consumption

---

### 3. Mixed Deployment (POD-2)

**Design Pattern**: Hybrid - some racks have ToRs, fallback to middle rack Leafs

**Strategy**:
- Server tries to connect to **ToR switches in same rack** first
- Falls back to **Leaf switches in network rack** in same row if no ToRs available
- Flexible deployment supporting both ToR and Leaf connectivity

**Topology**:

```
DC1-1-POD-2 (mixed deployment)
├── Row 1
│   ├── Rack 1 (network rack - middle)
│   │   ├── dc1-fab1-pod2-suite1-row1-rack1-leaf-01 (Leaf switch)
│   │   └── dc1-fab1-pod2-suite1-row1-rack1-leaf-02 (Leaf switch)
│   ├── Rack 5 (with ToRs)
│   │   ├── dc1-fab1-pod2-suite1-row1-rack5-tor-01 (ToR switch)
│   │   ├── dc1-fab1-pod2-suite1-row1-rack5-tor-02 (ToR switch)
│   │   └── server-mixed-01 → connects to tor-01 & tor-02 (same rack)
│   └── Rack 10 (compute, no ToRs)
│       └── server-mixed-02 → falls back to leaf-01 & leaf-02 (network rack)
```

**Test Servers**:
- `server-mixed-01`: Rack 5 (with ToRs), 4x 25G interfaces
- `server-mixed-02`: Rack 10 (compute, no ToRs), 4x 25G interfaces

**Expected Behavior**:
- `server-mixed-01`: Connects to ToRs in same rack (primary path)
- `server-mixed-02`: No ToRs in rack, falls back to Leafs in network rack (same row)
- Demonstrates fallback mechanism
- Maintains dual-homing in both scenarios

---

## Features Tested

### 1. Deployment Type Handling
- **tor**: Same-rack ToR connectivity with row fallback
- **middle_rack**: Cross-rack connectivity to network rack Leafs
- **mixed**: ToR priority with Leaf fallback

### 2. Interface Role Filtering
- Server interfaces: role="uplink", status="active"
- ToR/Leaf interfaces: role="customer", status="free"
- Proper filtering prevents role mismatches

### 3. Idempotency
- Detects existing connections on server interfaces
- Only creates connections for free (uncabled) interfaces
- Skips fully connected servers with clear log message
- Supports partial connectivity (some interfaces connected)

### 4. GraphQL Optimization
- Single nested query: `racks → devices → interfaces`
- Inline fragments for physical interface fields
- Server-side filtering (role, status, device_type)
- Significant performance improvement over sequential queries

### 5. Interface Speed Matching
- Groups interfaces by speed (25G, 100G)
- Only connects matching speeds in speed-aware mode
- Validates speed compatibility
- Handles mixed-speed scenarios

### 6. Dual-Homing
- Alternates between two consecutive switches
- Round-robin distribution for load balancing
- Validates device pair selection

### 7. Sequential Interface Consumption
- Uses `netutils.interface.sort_interface_list` for proper ordering
- Interfaces consumed sequentially (1/1/1, 1/1/2, 1/1/3, 1/1/4)
- Avoids interface number gaps

### 8. IP Address Management
- No IP allocation for endpoint connections (`pool=None`)
- Validates pool handling respects explicit None value

---

## Usage Examples

### Test ToR Deployment
```bash
uv run infrahubctl object load tests/integration/data/05_endpoint_connectivity/02_servers_tor_deployment.yml --branch test
uv run infrahubctl generator add_endpoint device_name=server-tor-01 --branch test
uv run infrahubctl generator add_endpoint device_name=server-tor-02 --branch test
```

### Test Middle_Rack Deployment
```bash
uv run infrahubctl object load tests/integration/data/05_endpoint_connectivity/03_servers_middle_rack_deployment.yml --branch test
uv run infrahubctl generator add_endpoint device_name=server-middle-01 --branch test
uv run infrahubctl generator add_endpoint device_name=server-middle-02 --branch test
```

### Test Mixed Deployment
```bash
uv run infrahubctl object load tests/integration/data/05_endpoint_connectivity/04_servers_mixed_deployment.yml --branch test
uv run infrahubctl generator add_endpoint device_name=server-mixed-01 --branch test
uv run infrahubctl generator add_endpoint device_name=server-mixed-02 --branch test
```

### Test Idempotency
```bash
# Run twice - second run should skip with idempotency message
uv run infrahubctl generator add_endpoint device_name=server-tor-01 --branch test
uv run infrahubctl generator add_endpoint device_name=server-tor-01 --branch test
# Expected: "Endpoint server-tor-01 already has 4 connection(s) - all interfaces connected, skipping"
```

---

## Validation Checks

After running generators, verify:

1. **Connection Count**: Each server should have 4 cables created
2. **Dual-Homing**: Cables alternate between two switches
3. **Interface Sequence**: Interfaces used sequentially (no gaps)
4. **No IP Allocation**: No IP addresses assigned to connections
5. **Correct Switch Type**: ToR deployment uses ToRs, middle_rack uses Leafs
6. **Fallback Behavior**: Mixed deployment falls back correctly

### Query Examples

```bash
# Check server connections
uv run infrahubctl get DcimCable --branch test --filter endpoints__device__name=server-tor-01

# Verify no IP addresses
uv run infrahubctl get IpamIPAddress --branch test --filter interface__device__name=server-tor-01
# Expected: No results

# Check interface consumption pattern
uv run infrahubctl get DcimPhysicalInterface --branch test \
  --filter device__name=dc1-fab1-pod1-suite1-row1-rack5-tor-01 \
  --filter cable__isnull=false
```

---

## Design Patterns Summary

| Deployment Type | Primary Target | Fallback Target | Use Case |
|----------------|---------------|-----------------|----------|
| **tor** | ToR in same rack | ToR in same row | Rack-level isolation, simple scaling |
| **middle_rack** | Leaf in network rack | None | Row-level aggregation, high density |
| **mixed** | ToR in same rack | Leaf in network rack | Flexible, gradual migration |

---

## Notes

- All test servers use **25G interfaces** to match Leaf/ToR capabilities
- Interface names: `eno1`, `eno2`, `eno3`, `eno4` (standard server naming)
- Device types: `PowerEdge-R640` (2U), `PowerEdge-R740` (2U)
- Status: All interfaces set to `active` (not `free`) to test proper status filtering
- Role: Server interfaces use `uplink` role (not `customer`)


### 2. Mixed Interface Speeds
- ToR switches have both 25G and 100G interfaces
- Servers have different interface speeds:
  - server-01, server-02: 100G only
  - server-03, server-04: 25G only
  - server-05: Mixed 25G + 100G
- Validates `InterfaceSpeedMatcher.group_by_speed()` logic

### 3. Speed-Aware Matching
- 100G server interfaces connect to 100G ToR interfaces
- 25G server interfaces connect to 25G ToR interfaces
- Mixed-speed servers connect appropriately per interface
- Validates enhanced `_query_compatible_interfaces()` with speed filter

### 4. Connection Fingerprinting
- Each connection uniquely identified by server+interface+switch+interface
- Prevents duplicate connections within generator run
- Validates `ConnectionFingerprint` idempotency

### 5. Connection Validation
- Validates dual-homing (minimum 2 connections)
- Validates no duplicate server interfaces
- Validates no duplicate switch interfaces
- Validates `ConnectionValidator.validate_plan()` logic

### 6. Middle Rack Deployment
- Network rack (middle rack) contains ToRs
- Compute racks contain servers
- Validates middle_rack deployment strategy

## Expected Connectivity

### Server-01 (100G)
- ens1f0 → tor-1:Ethernet1/1
- ens1f1 → tor-2:Ethernet1/1
- ens2f0 → tor-1:Ethernet1/2
- ens2f1 → tor-2:Ethernet1/2

### Server-02 (100G)
- ens1f0 → tor-1:Ethernet1/3
- ens1f1 → tor-2:Ethernet1/3
- ens2f0 → tor-1:Ethernet1/4
- ens2f1 → tor-2:Ethernet1/4

### Server-03 (25G)
- eno1 → tor-1:Ethernet1/9
- eno2 → tor-2:Ethernet1/9
- eno3 → tor-1:Ethernet1/10
- eno4 → tor-2:Ethernet1/10

### Server-04 (25G)
- eno1 → tor-1:Ethernet1/11
- eno2 → tor-2:Ethernet1/11
- eno3 → tor-1:Ethernet1/12
- eno4 → tor-2:Ethernet1/12

### Server-05 (Mixed)
**100G Group**:
- eth0 → tor-1:Ethernet1/5 (if available after server-01/02)
- eth1 → tor-2:Ethernet1/5

**25G Group**:
- eth2 → tor-1:Ethernet1/13 (if available after server-03/04)
- eth3 → tor-2:Ethernet1/13

## Loading Test Data

```bash
# Load into test branch
uv run infrahubctl object load tests/integration/data/endpoint_connectivity/ --branch test-endpoint

# Add servers to endpoints group to trigger generator
# (Manual step or separate data file)
```

## Validation Queries

### Check 100G Connections
```graphql
query {
  DcimCable(interface_a__interface_type__value: "100gbase-x-qsfp28") {
    edges {
      node {
        interface_a {
          node {
            device { node { name { value } } }
            name { value }
          }
        }
        interface_b {
          node {
            device { node { name { value } } }
            name { value }
          }
        }
      }
    }
  }
}
```

### Check 25G Connections
```graphql
query {
  DcimCable(interface_a__interface_type__value: "25gbase-x-sfp28") {
    edges {
      node {
        interface_a {
          node {
            device { node { name { value } } }
            name { value }
          }
        }
        interface_b {
          node {
            device { node { name { value } } }
            name { value }
          }
        }
      }
    }
  }
}
```

### Verify Dual-Homing
```graphql
query {
  DcimPhysicalDevice(role__value: "server") {
    edges {
      node {
        name { value }
        interfaces {
          count
          edges {
            node {
              name { value }
              interface_type { value }
              cable {
                id
                interface_b {
                  node {
                    device { node { name { value } } }
                    name { value }
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}
```

## Test Assertions

1. **Each server has 4 cables** (dual-homed to tor-1 and tor-2)
2. **Speeds match**: 100G servers → 100G ToR ports, 25G servers → 25G ToR ports
3. **No duplicate interfaces**: Each interface used at most once
4. **Alternating pattern**: Interfaces alternate between tor-1 and tor-2
5. **Suite-level**: All servers in Suite-A connect to ToRs in Rack-1-Network
6. **Idempotency**: Running generator multiple times doesn't create duplicates
