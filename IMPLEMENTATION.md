# Infrahub-Demo Implementation Guide

## Table of Contents

1. [Offset Calculation for Pod Deployment Types](#offset-calculation-for-pod-deployment-types)
2. [Naming Convention Types](#naming-convention-types)

---

## Offset Calculation for Pod Deployment Types

### Overview

The offset calculation determines which spine/leaf ports ToR devices connect to, ensuring proper load distribution and avoiding port conflicts. The calculation varies by deployment type.

### Deployment Types

#### 1. `middle_rack` Deployment

**Description**: Each rack contains both leafs and ToRs. ToRs connect to leafs within the same rack.

**Offset Calculation**:

```python
cabling_offset = 0  # No cumulative offset needed
```

**Visual Representation**:

```text
Pod 1 (middle_rack deployment)
├── DC1-fab1-pod1-spine-01 ────────┐
├── DC1-fab1-pod1-spine-02 ────────┤
│                                  │
├── Row 1                          │
│   └── Rack 5 (network rack)      │
│       ├── DC1-fab1-pod1-row1-rack5-leaf-01 ◄──┬───┐
│       ├── DC1-fab1-pod1-row1-rack5-leaf-02 ◄──┤   │ (leafs connect to spines)
│       │   (ports 0-15 available)              │   │
│       ├── DC1-fab1-pod1-row1-rack5-tor-01 ────┴───┘ (connects to local leafs, offset=0)
│       └── DC1-fab1-pod1-row1-rack5-tor-02 ────────┘ (connects to local leafs, offset=0)
│
└── Row 2
    └── Rack 5 (network rack)
        ├── DC1-fab1-pod1-row2-rack5-leaf-01 ◄──┬───┐
        ├── DC1-fab1-pod1-row2-rack5-leaf-02 ◄──┤   │ (leafs connect to spines)
        │   (ports 0-15 available)              │   │
        ├── DC1-fab1-pod1-row2-rack5-tor-01 ────┴───┘ (connects to local leafs, offset=0)
        └── DC1-fab1-pod1-row2-rack5-tor-02 ────────┘ (connects to local leafs, offset=0)
```

**Key Points**:

- Each rack is self-contained
- ToRs only connect to leafs in the same rack
- No need for cumulative offset tracking
- Simple intra-rack cabling pattern

**Example from tests/integration/data/dc**:

```yaml
- index: 1
  deployment_type: middle_rack
  amount_of_spines: 2
  number_of_rows: 2
  maximum_leafs_per_row: 2
  maximum_tors_per_row: 2
```

---

#### 2. `tor` Deployment

**Description**: Racks contain only ToRs (no leafs). All ToRs connect directly to pod spines.

**Offset Calculation**:

```python
# Cumulative offset based on rack position
offset = (maximum_tors_per_row × (row_index - 1)) + (tors_in_rack × (rack_index - 1))
```

**Visual Representation**:

```text
Pod 2 (tor deployment - all ToRs connect to spines)
├── DC1-fab1-pod2-spine-01 (32 ports) ────┬───┬─┬──┬─┬──┬─┬──┬─┐
├── DC1-fab1-pod2-spine-02 (32 ports) ────┘   │ |  │ |  │ |  | |
│                                             │ |  │ |  │ |  | |
├── Row 1                                     │ |  │ |  │ |  | |
│   ├── Rack 1 (tor-only)                     │ |  │ |  │ |  | |
│   │   ├── DC1-fab1-pod2-row1-rack1-tor-01 ──┘ |  │ |  │ |  | |
│   │   │   (offset=0, spine ports 0-1)         |  │ |  │ |  | |
│   │   └── DC1-fab1-pod2-row1-rack1-tor-02 ────┘  │ |  │ |  | |
│   │       (offset=0, spine ports 2-3)            │ |  │ |  | |
│   │                                              │ |  │ |  | |
│   └── Rack 5 (tor-only)                          │ |  │ |  | |
│       ├── DC1-fab1-pod2-row1-rack5-tor-01 ───────┘ |  │ |  | |
│       │   (offset=8, spine ports 16-17).           |  │ |  | |
│       └── DC1-fab1-pod2-row1-rack5-tor-02 ─────────┘  │ |  | |
│           (offset=8, spine ports 18-19)               │ |  | |
│                                                       │ |  | |
└── Row 2                                               │ |  | |
    ├── Rack 1 (tor-only)                               | |  | |
    │   ├── DC1-fab1-pod2-row2-rack1-tor-01 ────────────┘ |  | |
    │   │   (offset=10, spine ports 20-21)                │  | |
    │   └── DC1-fab1-pod2-row2-rack1-tor-02 ──────────────┘  | |
    │       (offset=10, spine ports 22-23)                   | |
    │                                                        | |
    └── Rack 5 (tor-only)                                    | |
        ├── DC1-fab1-pod2-row2-rack5-tor-01 ─────────────────┘ |
        │   (offset=18, spine ports 36-37)                     |
        └── DC1-fab1-pod2-row2-rack5-tor-02 ───────────────────┘
            (offset=18, spine ports 38-39)
```

**Offset Calculation Example**:

- Row 1, Rack 1: `offset = (10 × (1-1)) + (2 × (1-1)) = 0 + 0 = 0`
- Row 1, Rack 5: `offset = (10 × (1-1)) + (2 × (5-1)) = 0 + 8 = 8`
- Row 2, Rack 1: `offset = (10 × (2-1)) + (2 × (1-1)) = 10 + 0 = 10`
- Row 2, Rack 5: `offset = (10 × (2-1)) + (2 × (5-1)) = 10 + 8 = 18`

**Key Points**:

- All ToRs connect to pod spines
- Offset accumulates across rows and racks
- Prevents port conflicts on spines
- Maximizes spine port utilization

**Example from tests/integration/data/dc**:

```yaml
- index: 2
  deployment_type: tor
  amount_of_spines: 2
  number_of_rows: 2
  maximum_leafs_per_row: 0
  maximum_tors_per_row: 10
```

---

#### 3. `mixed` Deployment

**Description**: Hybrid approach with both middle racks (containing leafs) and ToR-only racks.

**Offset Calculation** (depends on rack type):

```python
# Middle racks (network type, contains leafs)
if rack_type == "network":
    cabling_offset = 0  # ToRs connect to local leafs

# ToR-only racks
else:
    cabling_offset = (rack_index - 1) × tors_per_rack  # ToRs connect to middle rack leafs
```

**Visual Representation**:

```text
Pod 3 (mixed deployment)
├── DC1-fab1-pod3-spine-01 ───────────────────────────┐
├── DC1-fab1-pod3-spine-02 ───────────────────────────┤
│                                                     │
├── Row 1                                             │
│   ├── Rack 1 (tor-only)                             │
│   │   ├── DC1-fab1-pod3-row1-rack1-tor-01 ───────┐  │
│   │   │   (offset=0, connects to Rack 4 leaf) │  │  │
│   │   └── DC1-fab1-pod3-row1-rack1-tor-02 ────┤  │  │
│   │       (offset=0, ports 0-3)               │  │  │
│   │                                           │  │  │
│   ├── Rack 4 (network - middle rack)          │  │  │
│   │   ├── DC1-fab1-pod3-row1-rack4-leaf-01 ───┼──┤──│
│   │   │   (ports 0-15, receives ToRs)         │  │  │ (leafs connect to spines)
│   │   └── DC1-fab1-pod3-row1-rack4-leaf-02 ───┼──┤──│
│   │                                           │  │  │
│   └── Rack 7 (tor-only)                       │  │  │
│       ├── DC1-fab1-pod3-row1-rack7-tor-01 ────┘  │  │
│       │   (offset=12, connects to Rack 4 leaf)   │  │
│       └── DC1-fab1-pod3-row1-rack7-tor-02 ───────┘  │
│           (offset=12, ports 24-27)                  │
│                                                     │
└── Row 2                                             │
│   ├── Rack 1 (tor-only)                             │
│   │   ├── DC1-fab1-pod3-row2-rack1-tor-01 ───────┐  │
│   │   │   (offset=0, connects to Rack 4 leaf) │  │  │
│   │   └── DC1-fab1-pod3-row2-rack1-tor-02 ────┤  │  │
│   │       (offset=0, ports 0-3)               │  │  │
│   │                                           │  │  │
│   ├── Rack 4 (network - middle rack)          │  │  │
│   │   ├── DC1-fab1-pod3-row2-rack4-leaf-01 ───┼──┤──│
│   │   │   (ports 0-15, receives ToRs)         │  │  │ (leafs connect to spines)
│   │   └── DC1-fab1-pod3-row2-rack4-leaf-02 ───┼──┤──┘
│   │                                           │  │
│   └── Rack 7 (tor-only)                       │  │
│       ├── DC1-fab1-pod3-row2-rack7-tor-01 ────┘  │
│       │   (offset=12, connects to Rack 4 leaf)   │
│       └── DC1-fab1-pod3-row2-rack7-tor-02 ───────┘
│           (offset=12, ports 24-27)
│
```

**Offset Logic**:

```python
# For middle rack leafs (offsets for connecting to pod spines)
leaf_offset = (row_index - 1) × maximum_leafs_per_row

# For ToRs in middle racks (not used in test data - middle racks have only leafs)
tor_offset = 0  # Would connect to local rack leafs

# For ToRs in ToR-only racks (connect to middle rack leafs in same row)
tor_offset = (rack_index - 1) × tors_per_rack
# Rack 1: (1-1) × 2 = 0
# Rack 7: (7-1) × 2 = 12
```

**Key Points**:

- Middle racks (R1-4, R2-4): Contain only leafs that connect to spines
- ToR-only racks (R1-1, R1-7, R2-1, R2-7): ToRs connect to middle rack leafs in the same row
- Offset is based on absolute rack index (not row position)
  - Rack 1: offset = 0
  - Rack 7: offset = 12 (reserves ports 0-23 for racks 1-6)
- Each row has independent middle rack, so offsets can repeat across rows
- More complex but maximizes density

**Example from tests/integration/data/dc**:

```yaml
- index: 3
  deployment_type: mixed
  amount_of_spines: 2
  number_of_rows: 2
  maximum_leafs_per_row: 2
  maximum_tors_per_row: 12
```

---

### Offset Calculation Code Reference

**File**: `generators/add/rack.py`

**Method**: `calculate_cabling_offsets()`

```python
def calculate_cabling_offsets(self, device_count: int, device_type: str = "leaf") -> int:
    """Calculate cabling offset using simple formula based on rack position."""

    current_index = self.data.index
    deployment_type = self.data.pod.deployment_type

    # For middle_rack deployment ToRs: always offset=0 (intra-rack)
    if deployment_type == "middle_rack" and device_type == "tor":
        offset = 0

    # For mixed deployment ToRs: static offset based on rack index
    elif deployment_type == "mixed" and device_type == "tor":
        offset = (current_index - 1) * device_count

    # For mixed/middle_rack deployment leafs: offset based on row
    elif deployment_type in ("mixed", "middle_rack") and device_type == "leaf":
        offset = (self.data.row_index - 1) * device_count

    # For tor deployment: cumulative offset across rows and racks
    elif deployment_type == "tor" and device_type == "tor":
        # Complex formula accounting for rows and rack position
        offset = calculate_tor_deployment_offset(...)

    return offset
```

---

## Naming Convention Types

Infrahub-demo supports three device naming strategies, each suitable for different organizational preferences and automation requirements.

### 1. Standard Naming Convention

**Format**: `{fabric}-fab{dc_index}-pod{pod_index}-row{row_index}-rack{rack_index}-{role}-{device_index}`

**Characteristics**:

- Most verbose and descriptive
- Includes explicit hierarchy labels
- Easy to understand at a glance
- Recommended for large, complex fabrics

**Examples**:

```text
# Data Center level (super-spines)
DC1-fab1-super-spine-01
DC1-fab1-super-spine-02

# Pod level (spines)
DC1-fab1-pod1-spine-01
DC1-fab1-pod1-spine-02
DC1-fab1-pod2-spine-01

# Rack level (leafs and ToRs)
DC1-fab1-pod1-row1-rack1-leaf-01
DC1-fab1-pod1-row1-rack1-leaf-02
DC1-fab1-pod1-row1-rack1-tor-01
DC1-fab1-pod1-row2-rack5-leaf-01
DC1-fab1-pod2-row1-rack1-tor-01

# Edge devices
DC1-fab1-pod1-edge-01
DC1-fab1-pod1-firewall-01
DC1-fab1-pod1-loadbalancer-01
```

**Configuration**:

```yaml
design_pattern:
  naming_convention: standard
```

**When to Use**:

- Multi-datacenter deployments
- Complex hierarchies with many pods and rows
- Teams new to the infrastructure
- Compliance/audit requirements needing explicit naming

---

### 2. Hierarchical Naming Convention

**Format**: `{fabric}-{dc_index}-{pod_index}-{row_index}-{rack_index}-{role}-{device_index}`

**Characteristics**:

- Numeric hierarchy without labels
- More concise than standard
- Still preserves full hierarchy
- Good balance of brevity and clarity

**Examples**:

```text
# Data Center level (super-spines)
DC1-1-super-spine-01
DC1-1-super-spine-02

# Pod level (spines)
DC1-1-1-spine-01
DC1-1-1-spine-02
DC1-1-2-spine-01

# Rack level (leafs and ToRs)
DC1-1-1-1-1-leaf-01
DC1-1-1-1-1-leaf-02
DC1-1-1-1-1-tor-01
DC1-1-1-2-5-leaf-01
DC1-1-2-1-1-tor-01

# Edge devices
DC1-1-1-edge-01
DC1-1-1-firewall-01
```

**Configuration**:

```yaml
design_pattern:
  naming_convention: hierarchical
```

**When to Use**:

- Automation-heavy environments
- Shorter hostnames preferred
- Clear numeric hierarchy is acceptable
- Programmatic device identification

---

### 3. Flat Naming Convention

**Format**: `{fabric}{role}{dc_index}{pod_index}{row_index}{rack_index}{device_index}`

**Characteristics**:

- No separators (except before role)
- Most compact format
- All numeric indices concatenated
- Requires familiarity with structure

**Examples**:

```text
# Data Center level (super-spines)
DC1superspine101
DC1superspine102

# Pod level (spines)
DC1spine11101
DC1spine11102
DC1spine12101

# Rack level (leafs and ToRs)
DC1leaf1111101
DC1leaf1111102
DC1tor1111101
DC1leaf1125101
DC1tor121101

# Edge devices
DC1edge1101
DC1firewall1101
DC1loadbalancer1101
```

**Configuration**:

```yaml
design_pattern:
  naming_convention: flat
```

**When to Use**:

- Space-constrained environments (hostname length limits)
- Highly automated environments
- Consistent device positioning/indexing
- Teams familiar with the numbering scheme
