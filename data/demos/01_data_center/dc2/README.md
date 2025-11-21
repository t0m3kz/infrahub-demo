# DC2 - Medium Middle Rack Data Center

## Overview
**Location:** Frankfurt
**Size:** Medium (M)
**Platform:** Arista EOS
**Design Pattern:** M-Standard-MR (Medium Standard with Middle Rack)

**Use Case:** Medium-sized data center with **middle_rack deployment** demonstrating hierarchical rack aggregation. Ideal for cost-effective deployments where rack-level aggregation reduces spine port requirements.

---

## Architecture

### Fabric Scale
- **Super Spines:** 2 (Arista DCS-7050CX3-32C-R)
- **Total Pods:** 2
- **Total Spines:** 4 (2+2 across pods)
- **Total Racks:** 6
- **Deployment Type:** middle_rack (all pods)

### Pod Structure
| Pod | Spines | Racks | Deployment Type |
|-----|--------|-------|----------------|
| Pod 1 | 2 (DCS-7050PX4-32S-R) | 3 | middle_rack |
| Pod 2 | 2 (DCS-7050CX3-32C-R) | 3 | middle_rack |

### Design Template Constraints
- maximum_spines: 2 per pod
- maximum_pods: 2
- maximum_leafs: 16
- maximum_rack_leafs: 6
- maximum_middle_racks: 4
- maximum_tors: 24
- naming_convention: standard

---

## Hardware Stack

### Spine Layer
- **Pod 1:** Arista DCS-7050PX4-32S-R (32x100GbE)
- **Pod 2:** Arista DCS-7050CX3-32C-R (32x100GbE)
- **Role:** Pod-level aggregation

### Super Spine Layer
- **Model:** Arista DCS-7050CX3-32C-R
- **Ports:** 32x100GbE
- **Role:** Inter-pod connectivity

### Leaf Layer (in racks)
- **Models:** DCS-7050CX3-32C-R, DCS-7050CX4M-48D8-F, DCS-7050SX3-24YC4C-S-R
- **Role:** Rack-level aggregation

---

## Deployment Strategy

### Middle Rack (All Pods)
**ToR Connectivity:**
```
ToR → Local Leafs (within rack)
ToR → External Leafs (if no local Leafs, least utilized)
```

**Benefits:**
- Reduced spine port consumption
- Rack-level aggregation
- Lower cost per port
- Easier cable management

---

## Use Case Analysis

### ✅ **Strengths**
- **Cost Effective:** Middle rack aggregation reduces spine ports needed
- **Arista EOS:** Industry-leading open network OS
- **Medium Scale:** Perfect for SMB or departmental DC
- **Consistent Deployment:** Single deployment type simplifies operations

### 🎯 **Best For**
- Medium enterprises (500-2000 servers)
- Cost-conscious deployments
- Organizations standardizing on Arista
- Middle-of-row or end-of-row aggregation

### 📊 **Capacity Estimate**
- 6 network racks with Leafs
- ~12-24 server racks (with ToRs)
- Supports 300-600 servers

---

## Quick Start

```bash
# Load topology
uv run infrahubctl object load data/demos/01_data_center/dc2/

# Generate fabric
uv run infrahubctl generator create_dc name=DC2 --branch main
```

---

## Files
- `00_topology.yml` - DC and Pod definitions
- `01_suites.yml` - 2 suites (PAR-1 Suite-1, PAR-1 Suite-2)
- `02_racks.yml` - 6 network racks with varying Leaf configurations

---

## Related Scenarios
- **DC1:** Mixed deployment (middle_rack + tor)
- **DC3:** Pure ToR deployment
- **DC6:** Similar M-Standard-MR with different vendors
