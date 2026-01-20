# Endpoint Connectivity Test Data

This scenario demonstrates adding endpoint devices (servers) and testing connectivity to existing network infrastructure in DC1-1-POD-2.

## Purpose

Tests the `add_endpoint` generator's ability to:
- Connect servers to ToR switches based on deployment type
- Match interface speeds (25G/100G) correctly
- Handle dual-homing across consecutive ToR pairs
- Maintain idempotency when run multiple times

## Prerequisites

- **DC1** exists (from scenario 1) with POD-2 (tor deployment)
- **POD-2** has existing ToR racks with fabric devices created by DC generator
- This scenario adds **compute racks** to POD-2 for server placement

## Data Files

- **00_racks.yml**: Adds 2 compute racks to DC1-1-POD-2
- **01_devices.yml**: Server definitions and all interface definitions (ToRs + Servers)

## Test Scenario

**Topology**: DC1-1-POD-2 with compute racks for servers

```
DC1-1-POD-2 (tor deployment)
├── Existing ToR racks (from scenario 1)
│   ├── ktw-1-s-2-r-1-1 (2x ToR switches)
│   ├── ktw-1-s-2-r-1-5 (2x ToR switches)
│   ├── ktw-1-s-2-r-2-1 (2x ToR switches)
│   └── ktw-1-s-2-r-2-5 (2x ToR switches)
├── NEW: ktw-1-s-2-r-1-10 (compute rack)
│   ├── server-01 (4x 100G uplinks)
│   ├── server-02 (4x 100G uplinks)
│   └── server-05 (2x 100G + 2x 25G uplinks)
└── NEW: ktw-1-s-2-r-2-10 (compute rack)
    ├── server-03 (4x 25G uplinks)
    └── server-04 (4x 25G uplinks)
```

## Features Tested

### 1. Suite-Level Distribution
- Servers distributed across multiple compute racks (Rack-2, Rack-3)
- All connect to ToRs in central network rack (Rack-1)
- Validates `_get_devices_in_suite()` functionality

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
