# Infrahub-Demo Implementation Guide

This guide explains the key concepts and deployment patterns for network automation in Infrahub-demo. It's designed for network architects and operators who need to understand how different topology types work.

## Table of Contents

1. [Pod Deployment Types](#pod-deployment-types)
2. [Device Naming Conventions](#device-naming-conventions)
3. [Cable Type Detection](#cable-type-detection)
4. [Layout-Design Compatibility Validation](#layout-design-compatibility-validation)

---

## Pod Deployment Types

Infrahub-demo supports three deployment patterns for data center pods, each optimized for different use cases. The deployment type determines how ToR (Top-of-Rack) switches connect to the spine/leaf fabric and how port offsets are calculated to avoid conflicts.

---

### 1. Middle Rack Deployment

**Use Case**: Dense deployments where each rack is self-contained with its own leaf switches.

**Topology**:

- Each network rack contains both leaf switches AND ToR switches
- ToRs connect only to leafs within the same rack
- Leafs connect upward to pod spines
- No cross-rack ToR connections

**Port Allocation**:

- **ToR Offset**: Always 0 (intra-rack connections)
- **Leaf Offset**: Based on row position to avoid spine port conflicts

**Connection Pattern**:

```text
Pod Structure
├── Spine Layer (connects to all leafs)
│   ├── spine-01
│   └── spine-02
│
├── Row 1
│   └── Rack 5 (network rack)
│       ├── leaf-01 ──────────── connects to spines
│       ├── leaf-02 ──────────── connects to spines
│       ├── tor-01 ───┐
│       └── tor-02 ───┴────────── both connect to local leafs only
│
└── Row 2
    └── Rack 5 (network rack)
        ├── leaf-01 ──────────── connects to spines
        ├── leaf-02 ──────────── connects to spines
        ├── tor-01 ───┐
        └── tor-02 ───┴────────── both connect to local leafs only
```

**End Device Connectivity**:

- Servers in compute racks connect to ToRs in the same row's network rack
- ToRs provide L2/L3 access
- Leafs aggregate traffic from ToRs to spines

**When to Use**:

- High rack density with many server racks per row
- Each row needs dedicated leaf switches
- Simplified cabling (no cross-rack connections)
- Easier troubleshooting (isolated per rack)

---

### 2. ToR Deployment

**Use Case**: Simplified deployments where ToRs connect directly to spines without intermediate leaf layer.

**Topology**:

- Racks contain ONLY ToR switches (no leafs)
- All ToRs connect directly to pod spines
- Two-tier architecture (spine-ToR)

**Port Allocation**:

- **ToR Offset**: Cumulative across rows and racks
- Formula: `(max_tors_per_row × (row - 1)) + (tors_in_rack × (rack - 1))`
- Prevents port conflicts on shared spines

**Connection Pattern**:

```text
Pod Structure
├── Spine Layer (connects to all ToRs)
│   ├── spine-01 (ports 0-31)
│   └── spine-02 (ports 0-31)
│       │
├── Row 1
│   ├── Rack 1 (tor-only)
│   │   ├── tor-01 ────────────── spine ports 0-1 (offset=0)
│   │   └── tor-02 ────────────── spine ports 2-3 (offset=0)
│   │
│   └── Rack 2 (tor-only)
│       ├── tor-01 ────────────── spine ports 4-5 (offset=4)
│       └── tor-02 ────────────── spine ports 6-7 (offset=4)
│
└── Row 2
    ├── Rack 1 (tor-only)
    │   ├── tor-01 ────────────── spine ports 8-9 (offset=8)
    │   └── tor-02 ────────────── spine ports 10-11 (offset=8)
    │
    └── Rack 2 (tor-only)
        ├── tor-01 ────────────── spine ports 12-13 (offset=12)
        └── tor-02 ────────────── spine ports 14-15 (offset=12)
```

**End Device Connectivity**:

- Servers connect directly to ToRs in their rack
- ToRs provide L2/L3 access and uplink to spines
- No intermediate aggregation layer

**When to Use**:

- Smaller deployments (few racks)
- Simplified architecture (fewer switch tiers)
- Lower cost (no leaf switches)
- Sufficient spine port density

---

### 3. Mixed Deployment

**Use Case**: Hybrid approach combining middle rack leafs for aggregation with ToR-only racks for compute.

**Topology**:

- Some racks have leafs only (middle racks) - these aggregate traffic
- Other racks have ToRs only (compute racks) - these connect to middle rack leafs
- ToRs in the same row connect to leafs in that row's middle rack

**Port Allocation**:

- **Leaf Offset**: Based on row position (for spine connections)
- **ToR Offset**: Based on rack position within the row (for leaf connections)
- Allows flexible mix of network and compute racks per row

**Connection Pattern**:

```text
Pod Structure
├── Spine Layer
│   ├── spine-01
│   └── spine-02
│       │
├── Row 1
│   ├── Rack 4 (middle rack - network)
│   │   ├── leaf-01 ──────────── connects to spines (offset=0)
│   │   └── leaf-02 ──────────── connects to spines (offset=0)
│   │       │
│   ├── Rack 1 (tor-only - compute) ┘
│   │   ├── tor-01 ────────────── connects to Row 1 leafs (offset=0)
│   │   └── tor-02 ────────────── connects to Row 1 leafs (offset=0)
│   │
│   └── Rack 7 (tor-only - compute)
│       ├── tor-01 ────────────── connects to Row 1 leafs (offset=12)
│       └── tor-02 ────────────── connects to Row 1 leafs (offset=12)
│
└── Row 2
    ├── Rack 4 (middle rack - network)
    │   ├── leaf-01 ──────────── connects to spines (offset=4)
    │   └── leaf-02 ──────────── connects to spines (offset=4)
    │       │
    ├── Rack 1 (tor-only - compute) ┘
    │   ├── tor-01 ────────────── connects to Row 2 leafs (offset=0)
    │   └── tor-02 ────────────── connects to Row 2 leafs (offset=0)
    │
    └── Rack 7 (tor-only - compute)
        ├── tor-01 ────────────── connects to Row 2 leafs (offset=12)
        └── tor-02 ────────────── connects to Row 2 leafs (offset=12)
```

**End Device Connectivity**:

- Servers in compute racks (R1, R7) connect to ToRs
- ToRs connect to middle rack leafs in the same row (R4)
- Leafs aggregate traffic and connect to spines
- Each row operates independently

**When to Use**:

- Large deployments with many compute racks
- Need for traffic aggregation before spine layer
- Flexible rack placement (mix network and compute)
- Maximizes port efficiency on spines

---

### Deployment Type Comparison

| Feature | Middle Rack | ToR | Mixed |
| ------- | ----------- | --- | ----- |
| **Architecture** | 3-tier (spine-leaf-tor) | 2-tier (spine-tor) | 3-tier (spine-leaf-tor) |
| **Rack Density** | Medium | Low | High |
| **Complexity** | Low | Very Low | Medium |
| **Port Efficiency** | Good | Fair | Excellent |
| **Scalability** | Good | Limited | Excellent |
| **Best For** | Standard pods | Small pods | Large pods |
| **Cabling** | Intra-rack only | All to spines | Row-based |

---

## Device Naming Conventions

Infrahub-demo supports three naming strategies for network devices. Choose the one that matches your organization's standards and automation requirements.

### 1. Standard Naming (Recommended)

**Format**: `{fabric}-fab{dc_index}-pod{pod_index}-row{row_index}-rack{rack_index}-{role}-{device_index}`

**Characteristics**:

- Most verbose and descriptive
- Every hierarchy level explicitly labeled
- Easy to parse and understand
- Best for large, complex environments

**Examples**:

```text
# Super-spines (data center level)
DC1-fab1-super-spine-01
DC1-fab1-super-spine-02

# Spines (pod level)
DC1-fab1-pod1-spine-01
DC1-fab1-pod2-spine-02

# Leafs (row level)
DC1-fab1-pod1-row1-rack4-leaf-01
DC1-fab1-pod1-row2-rack4-leaf-02

# ToRs (rack level)
DC1-fab1-pod1-row1-rack1-tor-01
DC1-fab1-pod1-row1-rack7-tor-02
```

**Benefits**:

- Self-documenting device location
- Easy to filter in automation (e.g., all row1 devices)
- Clear hierarchy for troubleshooting
- No ambiguity in device placement

**Use When**:

- You have multiple data centers or fabrics
- Large scale (many pods, rows, racks)
- Need clear hierarchy visibility
- Automation relies on structured names

---

### 2. Hierarchical Naming

**Format**: `{fabric}-fab{dc_index}-pod{pod_index}-{role}-{device_index}`

**Characteristics**:

- Shorter names than Standard
- Omits row and rack information
- Focuses on fabric hierarchy (DC → Pod → Device)
- Good for medium-sized environments

**Examples**:

```text
# Super-spines
DC1-fab1-super-spine-01
DC1-fab1-super-spine-02

# Spines
DC1-fab1-pod1-spine-01
DC1-fab1-pod2-spine-02

# Leafs (no row/rack in name)
DC1-fab1-pod1-leaf-01
DC1-fab1-pod1-leaf-02

# ToRs (no row/rack in name)
DC1-fab1-pod1-tor-01
DC1-fab1-pod1-tor-02
```

**Benefits**:

- Shorter device names
- Simpler naming scheme
- Still maintains fabric context
- Easier for humans to remember

**Use When**:

- Medium-sized deployments
- Row/rack info not critical in device name
- You prefer shorter hostnames
- FQDN length is a concern

---

### 3. Flat Naming

**Format**: `{fabric}-{role}-{device_index}`

**Characteristics**:

- Shortest possible names
- No hierarchy information in name
- Relies on inventory system for location
- Best for small or simple environments

**Examples**:

```text
# All devices use simple flat naming
DC1-super-spine-01
DC1-super-spine-02
DC1-spine-01
DC1-spine-02
DC1-leaf-01
DC1-leaf-02
DC1-tor-01
DC1-tor-02
```

**Benefits**:

- Very short device names
- Simplest naming scheme
- Easy to type and remember
- Good for lab/test environments

**Use When**:

- Small deployments (single pod)
- Lab or proof-of-concept environments
- Inventory system tracks device location
- You prioritize name simplicity

---

### Naming Strategy Comparison

| Strategy | Name Length | Hierarchy Info | Readability | Best For |
| -------- | ----------- | -------------- | ----------- | -------- |
| **Standard** | Longest | Complete | Excellent | Production, Large Scale |
| **Hierarchical** | Medium | Partial | Good | Medium Scale |
| **Flat** | Shortest | None | Fair | Small Scale, Labs |

**Recommendation**: Use **Standard** for production deployments. The longer names provide valuable context for troubleshooting and automation without relying on external systems.

---

## Cable Type Detection

Infrahub-demo automatically detects and sets appropriate cable types based on interface characteristics. This ensures accurate inventory and proper media selection for your physical infrastructure.

### Supported Cable Types

1. **Copper** - Cat6a/Cat7 patch cables for base-t interfaces
2. **MMF** (Multi-Mode Fiber) - OM3/OM4 fiber for most data center links (<300m)
3. **SMF** (Single-Mode Fiber) - OS2 fiber for long-distance links (>300m)

### Detection Logic

The system analyzes interface types on both ends of a connection:

**Both Copper** (e.g., `10gbase-t` ↔ `10gbase-t`)

- **Result**: Copper patch cable
- **Use Case**: Server NICs with copper, legacy ToR switches

**Both Fiber** (e.g., `100gbase-x-qsfp28` ↔ `100gbase-x-qsfp28`)

- **Result**: Multi-mode fiber (MMF) patch cable
- **Use Case**: Most spine-leaf, leaf-tor connections in modern DCs

**Mixed** (e.g., `10gbase-t` ↔ `10gbase-x-sfp+`)

- **Result**: DAC (Direct Attach Copper) or AOC (Active Optical Cable)
- **Use Case**: Copper server NICs connecting to fiber switch ports

### Common Scenarios

#### Data Center Infrastructure (Typical)

**Spine ↔ Leaf**: All fiber interfaces

```text
spine-01:100gbase-x-qsfp28 ↔ leaf-01:100gbase-x-qsfp28
Cable Type: MMF (multi-mode fiber patch)
```

**Leaf ↔ ToR**: All fiber interfaces

```text
leaf-01:25gbase-x-sfp28 ↔ tor-01:25gbase-x-sfp28
Cable Type: MMF (multi-mode fiber patch)
```

#### Server Connectivity

**Server ↔ ToR**: Mixed (copper NIC to fiber switch)

```text
server-01:10gbase-t ↔ tor-01:10gbase-x-sfp+
Cable Type: MMF (DAC or AOC cable)
```

#### Legacy Infrastructure

**Old Equipment**: All copper

```text
tor-01:10gbase-t ↔ aggregation-sw:10gbase-t
Cable Type: Copper (Cat6a patch)
```

---

## Layout-Design Compatibility Validation

The pod generator validates that the selected site layout can accommodate the pod design before resource allocation begins. This prevents wasted effort and ensures configurations are viable.

### Validation Mechanism

**TopologyPodDesign** has an optional `compatible_layouts` field that acts as a whitelist for governance and audit control.

**Behavior:**

- **When empty (default)**: Any layout allowed if capacity checks pass
- **When populated**: Layout must be in whitelist AND pass capacity checks

### Capacity Checks (Always Applied)

Regardless of whitelist, these validations always occur:

1. **ToR Capacity**: `max_tors_per_row ≤ layout.compute_racks_per_row`
2. **Leaf Capacity**: `max_leafs_per_row ≤ layout.network_racks_per_row × 4`

### Use Cases for Whitelist

**1. Governance & Compliance**

Enforce organizational policies about which designs work with which layouts:

```yaml
# Pod Design: enterprise-high-density
compatible_layouts:
  - large-dc-layout
  - xlarge-dc-layout
# Blocks usage with small-dc-layout even if capacity fits
```

**2. Prevent Configuration Errors**

Stop invalid combinations before they cause issues:

```yaml
# Pod Design: 8-spine-design
compatible_layouts:
  - large-dc-layout
# Prevents deploying 8 spines in small layout with insufficient power/cooling
```

**3. Document Tested Configurations**

Create an audit trail of validated combinations:

```yaml
# Pod Design: prod-standard-4spine
compatible_layouts:
  - medium-dc-layout
  - large-dc-layout
# Only layouts that passed certification
```

### Implementation Example

**Without Whitelist (Permissive)**:

```yaml
# data/bootstrap/11_pod_designs.yml
nodes:
  - name: spine-leaf-tor-4spine
    deployment_type: tor
    spine_count: 4
    max_tors_per_row: 10
    # compatible_layouts: <not set>
    # ✅ Works with any layout if capacity fits
```

**With Whitelist (Restricted)**:

```yaml
nodes:
  - name: enterprise-spine-leaf-8spine
    deployment_type: mixed
    spine_count: 8
    max_tors_per_row: 16
    max_leafs_per_row: 8
    compatible_layouts:
      - large-dc-layout
      - xlarge-dc-layout
    # ❌ Blocks small-dc-layout even if capacity sufficient
    # ✅ Audit trail: only certified for large deployments
```

### Validation Errors

**Whitelist Violation**:

```text
RuntimeError: Pod design 'enterprise-spine-leaf-8spine' not compatible with
site layout 'small-dc-layout'.
Compatible layouts: large-dc-layout, xlarge-dc-layout
```

**Capacity Violation**:

```text
RuntimeError: Design 'spine-leaf-tor-4spine' requires 12 ToRs/row
but layout 'small-dc-layout' only has 8 compute racks/row
```

### Recommendations

- **Development/Testing**: Leave `compatible_layouts` empty for flexibility
- **Production**: Define whitelist for critical designs
- **Documentation**: Use whitelist as living documentation of tested combos
- **Auditing**: Track which designs are restricted and why

---

*This guide is maintained as part of the infrahub-demo project. For implementation details, see the source code in `generators/` directory.*
