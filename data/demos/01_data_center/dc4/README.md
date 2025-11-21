# DC4 - Large Mixed Deployment Data Center

## Overview
**Location:** Amsterdam
**Size:** Large (L)
**Platform:** Edgecore (SONiC)
**Design Pattern:** L-Standard-Mixed (Large Standard with Mixed Deployment)

**Use Case:** Large data center demonstrating **mixed deployment** patterns where different pods use different connectivity strategies. Showcases the full power of flexible deployment types in a single fabric.

---

## Architecture

### Fabric Scale
- **Super Spines:** 2 (Edgecore 7726-32X-O)
- **Total Pods:** 3
- **Total Spines:** 9 (4+2+3 across pods)
- **Total Racks:** 12
- **Deployment Types:** Mixed (Pods 1-2: mixed, Pod 3: middle_rack)

### Pod Structure
| Pod | Spines | Racks | Deployment Type | Strategy |
|-----|--------|-------|----------------|----------|
| Pod 1 | 4 | 4 | **mixed** | Local ToR→Local Leaf + External ToR→This Leaf |
| Pod 2 | 2 | 4 | **mixed** | Local ToR→Local Leaf + External ToR→This Leaf |
| Pod 3 | 3 | 4 | middle_rack | ToR→Local/External Leafs |

### Design Template Constraints
- maximum_super_spines: 4
- maximum_spines: 4 per pod
- maximum_pods: 4
- maximum_leafs: 32
- maximum_rack_leafs: 8
- maximum_middle_racks: 8
- maximum_tors: 32
- naming_convention: standard

---

## Hardware Stack

### Spine Layer
- **Model:** Edgecore 7726-32X-O
- **Ports:** 32x100GbE
- **Role:** Pod-level aggregation

### Super Spine Layer
- **Model:** Edgecore 7726-32X-O
- **Ports:** 32x100GbE
- **Role:** Inter-pod connectivity

### Leaf Layer (in racks)
- **Model:** Edgecore AS7326-56X-O
- **Ports:** 48x25GbE + 8x100GbE
- **Role:** Rack aggregation and server connectivity

---

## Deployment Strategy

### Mixed Deployment (Pods 1-2) ⭐
**Complex Connectivity Pattern:**
```
Local ToRs (same rack) → Local Leafs
External ToRs (other racks in pod) → This Rack's Leafs (least utilized)
```

**Use Cases:**
- Variable rack types (some with Leafs, some without)
- Flexible connectivity requirements
- Maximizing Leaf utilization across racks

### Middle Rack Deployment (Pod 3)
**Standard Hierarchical Pattern:**
```
ToR → Local Leafs (if available)
ToR → External Leafs (if no local, least utilized)
```

**Use Cases:**
- Traditional hierarchical aggregation
- Consistent rack designs

---

## Use Case Analysis

### ✅ **Strengths**
- **Ultimate Flexibility:** Demonstrates all deployment types
- **Open Source:** Edgecore with SONiC OS
- **Large Scale:** 12 racks, 3 pods
- **Testing Platform:** Best for testing mixed deployment logic
- **Real-World:** Mimics actual DC evolution with mixed strategies

### 🎯 **Best For**
- Large enterprises with evolving requirements
- Hyperscalers needing deployment flexibility
- Testing and validating mixed deployment code
- Organizations migrating between deployment strategies
- Multi-tenant environments with varying SLAs

### 💡 **Unique Features**
- **Mixed Deployment:** Only DC with native mixed deployment in topology
- **Three Strategies:** Shows mixed, mixed, and middle_rack in one fabric
- **Educational:** Perfect for understanding deployment type differences

### 📊 **Capacity Estimate**
- 12 network racks with Leafs
- ~24-48 server racks (potential)
- Supports 600-1200 servers

---

## Quick Start

```bash
# Load topology
uv run infrahubctl object load data/demos/01_data_center/dc4/

# Generate fabric
uv run infrahubctl generator create_dc name=DC4 --branch main
```

---

## Testing Scenarios

### Mixed Deployment Validation
DC4 is the **primary test platform** for mixed deployment:

```bash
# Pods 1-2 already use mixed deployment
# Generate specific rack to test mixed logic
uv run infrahubctl generator create_rack name=dc4-r-1-1 --branch main

# Verify ToR connections:
# - Local ToRs connect to local Leafs
# - External ToRs connect to this rack's Leafs (least utilized)
```

---

## Files
- `00_topology.yml` - DC and Pod definitions with mixed deployment
- `01_suites.yml` - 3 suites (AMS-1 Room-1/2/3)
- `02_racks.yml` - 12 network racks with Edgecore Leafs

---

## Related Scenarios
- **DC1:** Mixed deployment types across pods
- **DC2/DC6:** Pure middle_rack for comparison
- **DC3:** Pure ToR for comparison
- **DC5:** Large middle_rack deployment
